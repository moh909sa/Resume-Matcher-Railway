"""Durable, authenticated mobile tailoring worker.

Fast POST queues a private Supabase row. An independently started Railway
lifespan worker executes AI without an open mobile HTTP request; stage updates
and the completed draft can be fetched after iOS suspension/relaunch.

Exactly-once *submission* is enforced by a client job UUID and a one-active-job
unique index. Worker crashes do not silently re-run paid AI calls: stale
in-flight jobs are marked interrupted unless a saved result is recoverable.
"""
import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from app.ai_budget import operation_budget
from app.config import settings
from app.database import db
from app.resume_ownership import resume_owner
from app.routers.resumes import improve_resume_preview_endpoint
from app.schemas.models import ImproveResumeRequest, PageFitSettings
from app.services.mobile_progress import finished_progress_update, mirror_engine_stages
from app.services.supabase_backend import (
    MobileJobLeaseLost, SupabaseBackend, SupabaseUnavailable,
)

logger = logging.getLogger(__name__)


async def _heartbeat(backend: SupabaseBackend, job: dict[str, Any]) -> None:
    while True:
        await asyncio.sleep(20)
        await backend.heartbeat_mobile_job(job)


async def process_queued_mobile_job(backend: SupabaseBackend, job: dict[str, Any]) -> None:
    """All AI work runs here, not inside an incoming HTTP request."""
    user_id = job["user_id"]
    progress_id = job["id"]
    source_id = job["source_resume_id"]
    watcher = asyncio.create_task(mirror_engine_stages(backend, user_id, progress_id))
    heartbeat = asyncio.create_task(_heartbeat(backend, job))
    job_id: str | None = None
    saved: dict[str, Any] | None = None
    result_status = "failed"
    failure_code: str | None = "processing_failed"
    try:
        # The durable job owns its budget independently of HTTP/proxy timeouts.
        async with operation_budget(settings.mobile_tailoring_timeout_seconds):
            with resume_owner(user_id):
                if not source_id or await backend.get_record(user_id, source_id) is None:
                    raise RuntimeError("Original CV no longer exists")
                source = await db.get_resume(source_id)
                if not source or source.get("processing_status") != "ready":
                    raise RuntimeError("Original CV is not Ready")
                jobs = await db.create_jobs([job["job_description"]], resume_id=source_id)
                job_id = jobs[0]["job_id"]
                options: dict[str, Any] = {}
                if job.get("max_pages") is not None:
                    options["max_pages"] = job["max_pages"]
                    options["page_fit"] = PageFitSettings(
                        template="swiss-single", pageSize="A4")
                if job.get("max_bullets_per_entry") is not None:
                    options["max_bullets_per_entry"] = job["max_bullets_per_entry"]
                preview = await improve_resume_preview_endpoint(
                    ImproveResumeRequest(
                        resume_id=source_id, job_id=job_id,
                        prompt_id="full", progress_id=progress_id, **options))
                data = preview.data.model_dump(mode="json")
                if not data.get("preview_id"):
                    raise RuntimeError("AI preview not registered")
                score = (data.get("ats_score") or {}).get("overall_score")
                if isinstance(score, (int, float)):
                    data["_mobile"] = {"ats_history": [score]}
                info = await db.get_job(job_id) or {}
                saved = await backend.create_tailored_record({
                    "user_id": user_id, "source_resume_id": source_id,
                    "progress_id": progress_id, "job_id": job_id,
                    "job_description": job["job_description"],
                    "job_title": info.get("role"), "company": info.get("company"),
                    "max_pages": job.get("max_pages"),
                    "max_bullets_per_entry": job.get("max_bullets_per_entry"),
                    "prompt_id": "full", "status": "draft",
                    "preview_id": data["preview_id"],
                    "preview_expires_at": data.get("preview_expires_at"),
                    "preview_json": data,
                })
                result_status = "succeeded"
                failure_code = None
    except asyncio.CancelledError:
        # On deployment shutdown the DB lease expires; a restarted worker
        # reconciles the saved result or reports "interrupted" instead of
        # triggering a second potentially billable OpenRouter operation.
        raise
    except TimeoutError:
        failure_code = "processing_timeout"
        logger.warning("Background tailoring timed out (no CV data logged)")
    except (MobileJobLeaseLost, SupabaseUnavailable) as error:
        failure_code = "service_unavailable"
        logger.warning("Background tailoring storage/lease issue: %s", type(error).__name__)
    except Exception as error:
        failure_code = "processing_failed"
        logger.warning("Background tailoring failed: %s", type(error).__name__)
    finally:
        watcher.cancel()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        with suppress(asyncio.CancelledError):
            await heartbeat

    # If draft persistence completed, success MUST win even if telemetry failed.
    if saved is None and job_id is not None:
        with resume_owner(user_id):
            with suppress(Exception):
                await db.delete_job(job_id)
    if not saved:
        existing = await backend.find_tailored_for_progress(user_id, progress_id)
        if existing:
            saved = existing
            result_status = "succeeded"
            failure_code = None

    # CAS update fences out another worker if lease ownership was lost.
    timeline = finished_progress_update(
        progress_id, succeeded=result_status == "succeeded",
        tailoring_id=str(saved["id"]) if saved else None)
    await backend.finish_mobile_job(
        job, result_status,
        tailoring_id=str(saved["id"]) if saved else None,
        error_code=failure_code,
        timeline={key: val for key, val in timeline.items()
                  if key not in {"status", "tailoring_id", "stage"}})


class MobileTailoringWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if settings.mobile_api_enabled and self._task is None:
            self._task = asyncio.create_task(
                self._loop(), name="durable-mobile-tailoring-worker")
            logger.info(
                "Durable mobile tailoring worker started (budget=%ss)",
                settings.mobile_tailoring_timeout_seconds,
            )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        key = settings.supabase_secret_key.get_secret_value()
        if not settings.supabase_url or not key:
            logger.warning("Mobile worker disabled: missing Supabase server credentials")
            return
        async with httpx.AsyncClient(
            base_url=settings.supabase_url,
            headers={"apikey": key},
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
        ) as client:
            backend = SupabaseBackend(client)
            loop_count = 0
            while True:
                try:
                    # At most once per 10 polling iterations. Never auto retry AI.
                    if loop_count % 10 == 0:
                        await backend.mark_stale_mobile_jobs()
                    loop_count += 1
                    pending = await backend.pending_mobile_jobs()
                    if pending:
                        item = pending[0]
                        job = await backend.claim_mobile_job(
                            item["user_id"], item["id"])
                        if job:
                            await process_queued_mobile_job(backend, job)
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning(
                        "Background worker check failed: %s", type(error).__name__)
                    await asyncio.sleep(3)
                await asyncio.sleep(1.5)


mobile_tailoring_worker = MobileTailoringWorker()
