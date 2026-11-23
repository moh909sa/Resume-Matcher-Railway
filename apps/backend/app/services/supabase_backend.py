"""Server-only Supabase Auth, private Storage, and CV registry integration."""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx

logger = logging.getLogger(__name__)
BUCKET = "resume-files"
MAX_ORIGINAL_BYTES = 4 * 1024 * 1024


class MobileJobConflict(RuntimeError):
    """An existing active job belongs to this user."""


class MobileJobLeaseLost(RuntimeError):
    """A different worker or recovered run owns the lease."""


class SupabaseUnavailable(RuntimeError):
    """The trusted upstream could not complete an operation."""


class InvalidSession(RuntimeError):
    """Supabase Auth rejected the access token."""


class ResumeQuotaExceeded(RuntimeError):
    """An account already holds the allowed number of original CVs."""


class OriginalFileMissing(RuntimeError):
    """A registry entry has no recoverable original object."""


class SupabaseBackend:
    """A request-local HTTP client; the secret stays in its apikey header.

    The user JWT is sent only to Auth /user. It is never substituted for the
    server credential on Storage or database writes (which are server-only).
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            logger.warning(
                "Supabase transport failed: %s %s (%s)",
                method,
                path.split("?")[0],
                type(error).__name__,
            )
            raise SupabaseUnavailable("Storage service unavailable") from error
        if response.status_code == 409 and path.endswith("/reserve_user_resume"):
            raise ResumeQuotaExceeded("Original resume limit reached")
        if response.is_error:
            # Never log response bodies, credentials, file bytes, or user JWTs.
            logger.warning(
                "Supabase request failed: %s %s status=%s",
                method,
                path.split("?")[0],
                response.status_code,
            )
            raise SupabaseUnavailable("Storage service unavailable")
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as error:
            raise SupabaseUnavailable("Invalid storage service response") from error

    async def ai_consent(self, owner: str, version: str) -> bool | None:
        response = await self._request("GET", "/rest/v1/mobile_ai_consent", params={
            "user_id": f"eq.{owner}", "version": f"eq.{version}", "select": "accepted", "limit": "1",
        })
        rows = self._json(response)
        if not isinstance(rows, list) or (rows and (
            not isinstance(rows[0], dict) or type(rows[0].get("accepted")) is not bool
        )):
            raise SupabaseUnavailable("Invalid AI consent response")
        return rows[0]["accepted"] if rows else None

    async def begin_account_deletion(self, owner: str, receipt_hash: str) -> None:
        await self._request("POST", "/rest/v1/mobile_account_deletions", headers={
            "Prefer": "resolution=ignore-duplicates,return=minimal",
        }, params={"on_conflict": "user_id"}, json={
            "user_id": owner, "receipt_hash": receipt_hash, "status": "deleting",
        })
        record = await self.deletion_receipt(receipt_hash)
        if record is None or record.get("user_id") != owner:
            raise SupabaseUnavailable("Use the existing deletion receipt")

    async def deletion_receipt(self, receipt_hash: str) -> dict[str, Any] | None:
        response = await self._request("GET", "/rest/v1/mobile_account_deletions", params={
            "receipt_hash": f"eq.{receipt_hash}", "select": "user_id,status", "limit": "1",
        })
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) > 1:
            raise SupabaseUnavailable("Invalid deletion receipt")
        return rows[0] if rows else None

    async def pending_account_deletions(self) -> list[dict[str, str]]:
        response = await self._request("GET", "/rest/v1/mobile_account_deletions", params={
            "status": "eq.deleting", "select": "user_id,receipt_hash", "limit": "20",
        })
        rows = self._json(response)
        if not isinstance(rows, list):
            raise SupabaseUnavailable("Invalid deletion queue")
        return rows

    async def erase_account_storage(self, owner: str) -> None:
        # List the namespace, not only registry rows: includes interrupted uploads.
        prefix = str(UUID(owner))
        async def erase_folder(folder: str, bucket: str = BUCKET) -> None:
            while True:
                response = await self._request("POST", f"/storage/v1/object/list/{bucket}", json={
                    "prefix": folder, "limit": 100, "offset": 0,
                    "sortBy": {"column": "name", "order": "asc"},
                })
                rows = self._json(response)
                if not isinstance(rows, list):
                    raise SupabaseUnavailable("Invalid storage listing")
                if not rows:
                    break
                files: list[str] = []
                folders: list[str] = []
                for row in rows:
                    name = row.get("name") if isinstance(row, dict) else None
                    if not isinstance(name, str) or "/" in name or name in ("", ".", ".."):
                        raise SupabaseUnavailable("Invalid storage path")
                    path = f"{folder}/{name}"
                    if row.get("id") is None:
                        folders.append(path)
                    else:
                        files.append(path)
                if files:
                    await self._request("DELETE", f"/storage/v1/object/{bucket}", json={"prefixes": files})
                for child in folders:
                    await erase_folder(child, bucket)
        await erase_folder(prefix)
        await erase_folder(prefix, "cv-pdf-artifacts")
        # Migration archives can include multiple owners. Retire the whole
        # snapshot on deletion of any included account; never retain their CVs
        # inside a backup after claiming that their data has been erased.
        backups = self._json(
            await self._request(
                "GET",
                "/rest/v1/cv_engine_backup_objects",
                params={"user_id": f"eq.{UUID(owner)}", "select": "storage_path"},
            )
        )
        for row in backups:
            path = row["storage_path"]
            if not path.startswith("sqlite-to-supabase/"):
                raise SupabaseUnavailable("Invalid backup reference")
            await self._request(
                "DELETE",
                "/storage/v1/object/cv-engine-backups",
                json={"prefixes": [path]},
            )
            await self._request(
                "DELETE",
                "/rest/v1/cv_engine_backup_objects",
                params={"storage_path": f"eq.{path}"},
            )

    async def stored_pdf(
        self, owner: str, tailoring_id: str, revision: int
    ) -> bytes | None:
        """An exact immutable revision; absence never falls back to an older PDF."""
        rows = self._json(
            await self._request(
                "GET",
                "/rest/v1/cv_pdf_artifacts",
                params={
                    "user_id": f"eq.{UUID(owner)}",
                    "tailoring_id": f"eq.{UUID(tailoring_id)}",
                    "revision": f"eq.{revision}",
                    "select": "*",
                },
            )
        )
        if not rows:
            return None
        row = rows[0]
        path = f"{UUID(owner)}/{UUID(tailoring_id)}/{revision}.pdf"
        if (
            row.get("storage_path") != path
            or row.get("storage_bucket") != "cv-pdf-artifacts"
        ):
            raise SupabaseUnavailable("Invalid PDF reference")
        content = (
            await self._request(
                "GET", f"/storage/v1/object/authenticated/cv-pdf-artifacts/{path}"
            )
        ).content
        if (
            len(content) != row["file_size_bytes"]
            or hashlib.sha256(content).hexdigest() != row["sha256"]
        ):
            raise SupabaseUnavailable("Stored PDF verification failed")
        return content

    async def persist_pdf(
        self,
        owner: str,
        tailoring_id: str,
        revision: int,
        content: bytes,
        page_count: int,
    ) -> bytes:
        """Upload before publishing its manifest; acknowledge only durable bytes."""
        path = f"{UUID(owner)}/{UUID(tailoring_id)}/{revision}.pdf"
        # Serialize identical revision renders in PostgreSQL at the caller.
        existing = await self.stored_pdf(owner, tailoring_id, revision)
        if existing is not None:
            return existing
        response = await self.client.post(
            f"/storage/v1/object/cv-pdf-artifacts/{path}",
            content=content,
            headers={"Content-Type": "application/pdf", "x-upsert": "false"},
        )
        if response.status_code in (400, 409):
            # A simultaneous renderer (or an interrupted manifest write) may
            # have already uploaded this exact revision. Retain its bytes.
            stored = await self._request(
                "GET", f"/storage/v1/object/authenticated/cv-pdf-artifacts/{path}"
            )
            content = stored.content
        elif response.is_error:
            raise SupabaseUnavailable("PDF storage is unavailable")
        if not content.startswith(b"%PDF-"):
            raise SupabaseUnavailable("Invalid PDF artifact")
        from app.services.page_fit import count_pdf_pages

        await self._request(
            "POST",
            "/rest/v1/cv_pdf_artifacts",
            headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
            params={"on_conflict": "tailoring_id,revision"},
            json={
                "user_id": owner,
                "tailoring_id": tailoring_id,
                "revision": revision,
                "storage_path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "file_size_bytes": len(content),
                "page_count": count_pdf_pages(content) or page_count,
            },
        )
        verified = await self.stored_pdf(owner, tailoring_id, revision)
        if verified is None:
            raise SupabaseUnavailable("PDF persistence was not acknowledged")
        return verified


    async def erase_account_records(self, owner: str) -> None:
        for table, key in (("user_tailoring_progress", "user_id"), ("user_tailored_cvs", "user_id"),
                           ("user_resumes", "user_id"), ("mobile_ai_consent", "user_id"), ("profiles", "id")):
            await self._request("DELETE", f"/rest/v1/{table}", params={key: f"eq.{UUID(owner)}"})

    async def erase_auth_user(self, owner: str) -> None:
        response = await self.client.request(
            "DELETE", f"/auth/v1/admin/users/{UUID(owner)}", json={"should_soft_delete": False},
        )
        if response.status_code not in (200, 204, 404):
            raise SupabaseUnavailable("Authentication deletion is unavailable")

    async def finish_account_deletion(self, receipt_hash: str) -> None:
        await self._request("PATCH", "/rest/v1/mobile_account_deletions", params={
            "receipt_hash": f"eq.{receipt_hash}",
        }, json={"status": "complete"})

    async def save_ai_consent(self, owner: str, version: str, accepted: bool) -> None:
        await self._request(
            "POST", "/rest/v1/mobile_ai_consent",
            params={"on_conflict": "user_id,version"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json={"user_id": owner, "version": version, "accepted": accepted,
                  "decided_at": datetime.now(timezone.utc).isoformat()},
        )

    async def verify_user(self, access_token: str) -> str:
        """Use the configured project's Auth server, not unverified JWT claims."""
        try:
            response = await self.client.get(
                "/auth/v1/user", headers={"Authorization": f"Bearer {access_token}"}
            )
        except httpx.HTTPError as error:
            raise SupabaseUnavailable("Authentication service unavailable") from error
        if response.status_code in (400, 401, 403, 404):
            raise InvalidSession("Sign in again")
        if response.is_error:
            raise SupabaseUnavailable("Authentication service unavailable")
        data = self._json(response)
        if not isinstance(data, dict):
            raise SupabaseUnavailable("Invalid authentication response")
        if data.get("is_anonymous"):
            raise InvalidSession("A permanent account is required")
        try:
            return str(UUID(data["id"]))
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise SupabaseUnavailable("Invalid authentication response") from error

    @staticmethod
    def object_path(user_id: str, resume_id: str, suffix: str) -> str:
        """Keys are generated IDs; a supplied filename never controls a path."""
        if suffix not in (".pdf", ".docx"):
            raise ValueError("Unsupported original file type")
        return f"{UUID(user_id)}/{UUID(resume_id)}/original{suffix}"

    @staticmethod
    def checked_path(record: dict[str, Any], user_id: str) -> str:
        """Refuse unexpected object paths even with a privileged credential."""
        path = record.get("storage_path")
        allowed = {
            SupabaseBackend.object_path(user_id, record["id"], suffix)
            for suffix in (".pdf", ".docx")
        }
        if (
            record.get("user_id") != user_id
            or record.get("storage_bucket") != BUCKET
            or path not in allowed
        ):
            raise SupabaseUnavailable("Invalid original file reference")
        return str(path)

    async def create_record(self, values: dict[str, Any]) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/rest/v1/rpc/reserve_user_resume",
            json={
                "p_user_id": values["user_id"],
                "p_resume_id": values["id"],
                "p_filename": values["original_filename"],
                "p_storage_path": values["storage_path"],
                "p_mime_type": values["mime_type"],
                "p_file_size": values["file_size_bytes"],
            },
        )
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) != 1:
            raise SupabaseUnavailable("Invalid registry response")
        return rows[0]

    async def list_records(self, user_id: str) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            "/rest/v1/user_resumes",
            params={
                "user_id": f"eq.{UUID(user_id)}",
                "select": "*",
                "order": "created_at.desc",
                "limit": "100",
            },
        )
        rows = self._json(response)
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or row.get("user_id") != user_id for row in rows
        ):
            raise SupabaseUnavailable("Invalid registry response")
        return rows

    async def get_record(self, user_id: str, resume_id: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            "/rest/v1/user_resumes",
            params={
                "user_id": f"eq.{UUID(user_id)}",
                "id": f"eq.{UUID(resume_id)}",
                "select": "*",
            },
        )
        rows = self._json(response)
        if not isinstance(rows, list):
            raise SupabaseUnavailable("Invalid registry response")
        if not rows:
            return None
        row = rows[0]
        if (
            not isinstance(row, dict)
            or row.get("user_id") != user_id
            or row.get("id") != resume_id
        ):
            raise SupabaseUnavailable("Invalid registry response")
        return row

    async def update_record(
        self, user_id: str, resume_id: str, values: dict[str, Any]
    ) -> None:
        await self._request(
            "PATCH",
            "/rest/v1/user_resumes",
            params={"user_id": f"eq.{UUID(user_id)}", "id": f"eq.{UUID(resume_id)}"},
            json=values,
        )

    async def delete_record(self, user_id: str, resume_id: str) -> None:
        await self._request(
            "DELETE",
            "/rest/v1/user_resumes",
            params={"user_id": f"eq.{UUID(user_id)}", "id": f"eq.{UUID(resume_id)}"},
        )

    async def sync_records(
        self, user_id: str, snapshot: list[dict[str, Any]], observed_at: str
    ) -> None:
        await self._request(
            "POST",
            "/rest/v1/rpc/sync_user_resume_registry",
            json={
                "p_user_id": str(UUID(user_id)),
                "p_snapshot": snapshot,
                "p_observed_at": observed_at,
            },
        )

    async def enqueue_mobile_tailoring(
        self, user_id: str, progress_id: str, resume_id: str,
        job_description: str, max_pages: int | None,
        max_bullets_per_entry: int | None,
    ) -> dict[str, Any]:
        """Idempotent queue insert; only one active mobile job per user."""
        # Returning a newly generated client UUID after losing the response must
        # not trigger another billed AI request for an identical recent preview.
        recent = await self.list_mobile_jobs(user_id, limit=10)
        for previous in recent:
            if previous.get("source_resume_id") != resume_id or previous.get("job_description") != job_description:
                continue
            if previous.get("max_pages") != max_pages or previous.get("max_bullets_per_entry") != max_bullets_per_entry:
                continue
            if previous["id"] == progress_id or previous.get("status") in ("queued", "running"):
                return previous
            if previous.get("status") == "succeeded" and previous.get("tailoring_id"):
                created = datetime.fromisoformat(previous["created_at"].replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - created < timedelta(minutes=15):
                    return previous
        values = {
            "id": str(UUID(progress_id)), "user_id": str(UUID(user_id)),
            "source_resume_id": str(UUID(resume_id)),
            "job_description": job_description,
            "max_pages": max_pages, "max_bullets_per_entry": max_bullets_per_entry,
            "status": "queued", "stage": "queued",
        }
        try:
            response = await self._request(
                "POST", "/rest/v1/user_tailoring_progress",
                headers={"Prefer": "return=representation"}, json=values)
            rows = self._json(response)
            if not isinstance(rows, list) or len(rows) != 1:
                raise SupabaseUnavailable("Invalid queue insert")
            return rows[0]
        except SupabaseUnavailable:
            # A replay with the same id or a double tap with a new id must
            # NEVER create a second AI operation. Check ownership and payload.
            matching = await self.get_mobile_job(user_id, progress_id)
            if matching is None:
                active = await self.list_mobile_jobs(user_id, limit=10)
                matching = next(
                    (job for job in active if job.get("status") in ("queued", "running")), None
                )
            if (
                matching
                and matching.get("source_resume_id") == resume_id
                and matching.get("job_description") == job_description
                and matching.get("max_pages") == max_pages
                and matching.get("max_bullets_per_entry") == max_bullets_per_entry
            ):
                return matching
            if matching:
                raise MobileJobConflict("Another tailoring request is already in progress")
            raise

    async def get_mobile_job(self, user_id: str, progress_id: str) -> dict[str, Any] | None:
        """Only trusted server code accesses full private queue inputs."""
        response = await self._request(
            "GET", "/rest/v1/user_tailoring_progress",
            params={"user_id": f"eq.{UUID(user_id)}", "id": f"eq.{UUID(progress_id)}",
                    "select": "*"})
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) > 1:
            raise SupabaseUnavailable("Invalid queue lookup")
        return rows[0] if rows and rows[0].get("user_id") == user_id else None

    async def list_mobile_jobs(self, user_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", "/rest/v1/user_tailoring_progress",
            params={"user_id": f"eq.{UUID(user_id)}",
                    "job_description": "not.is.null",
                    "select": "*", "order": "created_at.desc",
                    "limit": str(min(limit, 20))})
        rows = self._json(response)
        if not isinstance(rows, list) or any(row.get("user_id") != user_id for row in rows):
            raise SupabaseUnavailable("Invalid queue listing")
        return rows

    async def pending_mobile_jobs(self) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", "/rest/v1/user_tailoring_progress",
            params={"status": "eq.queued", "job_description": "not.is.null",
                    "select": "id,user_id", "order": "created_at.asc", "limit": "5"})
        rows = self._json(response)
        if not isinstance(rows, list):
            raise SupabaseUnavailable("Invalid queue listing")
        return rows

    async def claim_mobile_job(self, user_id: str, progress_id: str) -> dict[str, Any] | None:
        token = str(uuid4())
        now = datetime.now(timezone.utc)
        response = await self._request(
            "PATCH", "/rest/v1/user_tailoring_progress",
            params={"id": f"eq.{UUID(progress_id)}", "user_id": f"eq.{UUID(user_id)}",
                    "status": "eq.queued", "job_description": "not.is.null"},
            headers={"Prefer": "return=representation"},
            json={"status": "running", "stage": "starting", "claim_token": token,
                  "lease_until": (now + timedelta(minutes=3)).isoformat(),
                  "attempts": 1, "updated_at": now.isoformat()})
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) > 1:
            raise SupabaseUnavailable("Invalid job claim")
        return rows[0] if rows else None

    async def heartbeat_mobile_job(self, job: dict[str, Any]) -> None:
        response = await self._request(
            "PATCH", "/rest/v1/user_tailoring_progress",
            params={"id": f"eq.{UUID(job['id'])}",
                    "user_id": f"eq.{UUID(job['user_id'])}",
                    "status": "eq.running",
                    "claim_token": f"eq.{UUID(job['claim_token'])}"},
            headers={"Prefer": "return=representation"},
            json={"lease_until": (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),
                  "updated_at": datetime.now(timezone.utc).isoformat()})
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) != 1:
            raise MobileJobLeaseLost("Background job lease is no longer active")

    async def finish_mobile_job(
        self, job: dict[str, Any], status: str,
        *, tailoring_id: str | None = None, error_code: str | None = None,
        timeline: dict[str, Any] | None = None,
    ) -> None:
        if status not in ("succeeded", "failed", "interrupted"):
            raise ValueError("Invalid final mobile job state")
        now = datetime.now(timezone.utc).isoformat()
        response = await self._request(
            "PATCH", "/rest/v1/user_tailoring_progress",
            params={"id": f"eq.{UUID(job['id'])}",
                    "user_id": f"eq.{UUID(job['user_id'])}",
                    "status": "eq.running",
                    "claim_token": f"eq.{UUID(job['claim_token'])}"},
            headers={"Prefer": "return=representation"},
            json={"status": status, "stage": status,
                  "claim_token": None, "lease_until": None, "error_code": error_code,
                  "tailoring_id": tailoring_id, "finished_at": now,
                  "updated_at": now, **(timeline or {})})
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) != 1:
            raise MobileJobLeaseLost("Background job state changed")

    async def find_tailored_for_progress(self, user_id: str, progress_id: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET", "/rest/v1/user_tailored_cvs",
            params={"user_id": f"eq.{UUID(user_id)}",
                    "progress_id": f"eq.{UUID(progress_id)}",
                    "select": "id", "limit": "1"})
        rows = self._json(response)
        if not isinstance(rows, list):
            raise SupabaseUnavailable("Invalid result lookup")
        return rows[0] if rows else None

    async def mark_stale_mobile_jobs(self) -> None:
        """Never silently regenerate an AI preview that may already have been billed."""
        now = datetime.now(timezone.utc).isoformat()
        response = await self._request(
            "GET", "/rest/v1/user_tailoring_progress",
            params={"status": "eq.running", "job_description": "not.is.null",
                    "lease_until": f"lt.{now}", "select": "id,user_id,claim_token",
                    "limit": "10"})
        for job in self._json(response):
            existing = await self.find_tailored_for_progress(job["user_id"], job["id"])
            # Fence against a worker that recovered its heartbeat in the meantime.
            await self._request(
                "PATCH", "/rest/v1/user_tailoring_progress",
                params={"id": f"eq.{UUID(job['id'])}", "status": "eq.running",
                        "claim_token": f"eq.{UUID(job['claim_token'])}",
                        "lease_until": f"lt.{now}"},
                json={"status": "succeeded" if existing else "interrupted",
                      "tailoring_id": existing["id"] if existing else None,
                      "error_code": None if existing else "worker_interrupted",
                      "stage": "complete" if existing else "interrupted",
                      "finished_at": now, "updated_at": now,
                      "claim_token": None, "lease_until": None,
                      "current_step": None})

    async def create_mobile_progress(
        self, user_id: str, progress_id: str, resume_id: str
    ) -> None:
        """Create an owner-scoped progress row before the long AI operation."""
        response = await self._request(
            "POST", "/rest/v1/user_tailoring_progress",
            headers={"Prefer": "return=representation"},
            json={
                "id": str(UUID(progress_id)),
                "user_id": str(UUID(user_id)),
                "source_resume_id": str(UUID(resume_id)),
                "status": "running",
                "stage": "starting",
            },
        )
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) != 1:
            raise SupabaseUnavailable("Unable to initialize mobile progress")

    async def get_mobile_progress(
        self, user_id: str, progress_id: str
    ) -> dict[str, Any] | None:
        """No user can read progress for another user, even knowing its UUID."""
        response = await self._request(
            "GET", "/rest/v1/user_tailoring_progress",
            params={
                "user_id": f"eq.{UUID(user_id)}",
                "id": f"eq.{UUID(progress_id)}",
                "select": (
                    "id,user_id,status,stage,current_step,completed_steps,"
                    "visited_steps,tailoring_id,created_at,updated_at,finished_at,error_code"
                ),
            },
        )
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) > 1:
            raise SupabaseUnavailable("Invalid mobile progress response")
        if not rows:
            return None
        row = rows[0]
        if not isinstance(row, dict) or row.get("user_id") != user_id or row.get("id") != progress_id:
            raise SupabaseUnavailable("Invalid mobile progress owner")
        return row

    async def update_mobile_progress(
        self, user_id: str, progress_id: str, update: dict[str, Any]
    ) -> None:
        """Monotonic final status: background stage writes cannot undo completion."""
        await self._request(
            "PATCH", "/rest/v1/user_tailoring_progress",
            params={
                "user_id": f"eq.{UUID(user_id)}",
                "id": f"eq.{UUID(progress_id)}",
                "status": "eq.running",
            },
            json={**update, "updated_at": datetime.now(timezone.utc).isoformat()},
        )

    async def create_tailored_record(self, values: dict[str, Any]) -> dict[str, Any]:
        response = await self._request(
            "POST", "/rest/v1/user_tailored_cvs",
            headers={"Prefer": "return=representation"}, json=values,
        )
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) != 1:
            raise SupabaseUnavailable("Invalid tailored CV registry response")
        return rows[0]

    async def list_tailored_records(self, user_id: str, *, limit: int = 100, offset: int = 0, status: str | None = None) -> list[dict[str, Any]]:
        params = {"user_id": f"eq.{UUID(user_id)}", "select": "*",
                  "order": "created_at.desc,id.desc", "limit": str(limit), "offset": str(offset)}
        if status is not None:
            params["status"] = f"eq.{status}"
        response = await self._request(
            "GET", "/rest/v1/user_tailored_cvs",
            params=params,
        )
        rows = self._json(response)
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or row.get("user_id") != user_id for row in rows
        ):
            raise SupabaseUnavailable("Invalid tailored CV registry response")
        return rows

    async def get_tailored_record(
        self, user_id: str, tailoring_id: str
    ) -> dict[str, Any] | None:
        response = await self._request(
            "GET", "/rest/v1/user_tailored_cvs",
            params={"user_id": f"eq.{UUID(user_id)}",
                    "id": f"eq.{UUID(tailoring_id)}", "select": "*"},
        )
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) > 1:
            raise SupabaseUnavailable("Invalid tailored CV registry response")
        if not rows:
            return None
        row = rows[0]
        if not isinstance(row, dict) or row.get("user_id") != user_id or row.get("id") != tailoring_id:
            raise SupabaseUnavailable("Invalid tailored CV registry response")
        return row

    async def update_tailored_record(
        self, user_id: str, tailoring_id: str, changes: dict[str, Any]
    ) -> None:
        await self._request(
            "PATCH", "/rest/v1/user_tailored_cvs",
            params={"user_id": f"eq.{UUID(user_id)}",
                    "id": f"eq.{UUID(tailoring_id)}"},
            json=changes,
        )

    async def claim_tailored_operation(
        self, user_id: str, tailoring_id: str, expected_preview_id: str
    ) -> str | None:
        """Atomic lease: serialize keyword applies and confirmation across workers."""
        token = str(uuid4())
        now = datetime.now(timezone.utc)
        response = await self._request(
            "PATCH", "/rest/v1/user_tailored_cvs",
            params={
                "user_id": f"eq.{UUID(user_id)}",
                "id": f"eq.{UUID(tailoring_id)}",
                "status": "eq.draft",
                "preview_id": f"eq.{expected_preview_id}",
                "or": f"(operation_until.is.null,operation_until.lt.{now.isoformat()})",
            },
            headers={"Prefer": "return=representation"},
            json={"operation_token": token,
                  "operation_until": (now + timedelta(minutes=15)).isoformat()},
        )
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) > 1:
            raise SupabaseUnavailable("Invalid tailoring lease response")
        return token if rows else None

    async def finish_tailored_operation(
        self, user_id: str, tailoring_id: str, token: str,
        changes: dict[str, Any], *, final_status: str = "draft",
    ) -> None:
        response = await self._request(
            "PATCH", "/rest/v1/user_tailored_cvs",
            params={
                "user_id": f"eq.{UUID(user_id)}",
                "id": f"eq.{UUID(tailoring_id)}",
                "status": "eq.draft",
                "operation_token": f"eq.{UUID(token)}",
            },
            headers={"Prefer": "return=representation"},
            json={**changes, "status": final_status,
                  "operation_token": None, "operation_until": None},
        )
        rows = self._json(response)
        if not isinstance(rows, list) or len(rows) != 1:
            raise SupabaseUnavailable("Tailoring changed during this operation")

    async def release_tailored_operation(
        self, user_id: str, tailoring_id: str, token: str
    ) -> None:
        """Release only our lock; cannot clear a new worker's claim."""
        await self._request(
            "PATCH", "/rest/v1/user_tailored_cvs",
            params={
                "user_id": f"eq.{UUID(user_id)}",
                "id": f"eq.{UUID(tailoring_id)}",
                "operation_token": f"eq.{UUID(token)}",
            },
            json={"operation_token": None, "operation_until": None},
        )

    async def upload_original(self, path: str, content: bytes, mime_type: str) -> None:
        await self._request(
            "POST",
            f"/storage/v1/object/{BUCKET}/{quote(path, safe='/')}",
            content=content,
            headers={"Content-Type": mime_type, "x-upsert": "false"},
        )

    async def delete_original(self, path: str) -> None:
        # The Storage API removes bytes and metadata together; never SQL-delete objects.
        await self._request(
            "DELETE", f"/storage/v1/object/{BUCKET}", json={"prefixes": [path]}
        )

    async def download_original(self, path: str) -> bytes:
        try:
            async with self.client.stream(
                "GET",
                f"/storage/v1/object/authenticated/{BUCKET}/{quote(path, safe='/')}",
            ) as response:
                if response.status_code in (400, 404):
                    raise OriginalFileMissing("Original file is unavailable")
                if response.is_error:
                    raise SupabaseUnavailable("Storage service unavailable")
                content = bytearray()
                async for chunk in response.aiter_bytes(64 * 1024):
                    content.extend(chunk)
                    if len(content) > MAX_ORIGINAL_BYTES:
                        raise SupabaseUnavailable("Original exceeds the upload limit")
                return bytes(content)
        except httpx.HTTPError as error:
            raise SupabaseUnavailable("Storage service unavailable") from error
