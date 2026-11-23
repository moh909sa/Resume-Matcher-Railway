"""Authenticated mobile CV tailoring: user-owned preview, explicit approval, and library."""

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.ai_budget import AIOperationRoute
from app.database import db
from app.mobile_access import current_user, supabase_backend
from app.mobile_consent import ai_processing_user
from app.resume_ownership import resume_owner
from app.routers.resumes import (
    apply_keywords_endpoint,
    improve_resume_confirm_endpoint,
    improve_resume_preview_endpoint,
)
from app.schemas.models import (
    ImproveResumeConfirmRequest,
    ImproveResumeRequest,
    PageFitSettings,
    ResumeData,
)
from app.schemas.writing import ApplyKeywordsRequest, ForcedKeyword
from app.services.mobile_editor import EditorStore
from app.services.mobile_progress import (
    finished_progress_update,
    mirror_engine_stages,
)
from app.services.supabase_backend import (
    MobileJobConflict,
    SupabaseBackend,
    SupabaseUnavailable,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mobile", tags=["mobile-tailoring"], route_class=AIOperationRoute)


class MobileTailorRequest(BaseModel):
    """Full tailoring is server-enforced; no cap is the default for both limits."""

    resume_id: UUID
    job_description: str = Field(min_length=50, max_length=12000)
    progress_id: UUID | None = None  # Client supplies a fresh UUID for live polling.
    max_pages: Literal[2, 3] | None = None
    max_bullets_per_entry: Literal[3, 5] | None = None

    @field_validator("job_description")
    @classmethod
    def normalize_description(cls, text: str) -> str:
        text = text.strip()
        if len(text) < 50:
            raise ValueError("Paste at least 50 characters from the job posting")
        return text


class MobileApplyKeywordsRequest(BaseModel):
    expected_preview_id: str = Field(min_length=1, max_length=100)
    keywords: list[ForcedKeyword] = Field(min_length=1, max_length=30)


class MobileConfirmRequest(BaseModel):
    expected_preview_id: str = Field(min_length=1, max_length=100)


def _keyword_options(preview: dict[str, Any]) -> dict[str, Any]:
    ats = preview.get("ats_score") or {}
    supported = ats.get("injectable_keywords") or []
    missing = ats.get("missing_keywords") or []
    applied = preview.get("forced_keywords") or []
    used = {str(x).casefold() for x in applied}
    possible = {str(x).casefold() for x in supported}
    def not_used(items: list[str]) -> list[str]:
        return [x for x in items if x.casefold() not in used]
    return {
        "supported": not_used(supported),
        "needs_attestation": not_used([x for x in missing if x.casefold() not in possible]),
        "applied": applied,
        "remaining_slots": max(0, 30 - len(used)),
    }


def _review_ui(preview: dict[str, Any]) -> dict[str, Any]:
    summary = preview.get("diff_summary") or {}
    sections: dict[str, list[dict[str, Any]]] = {}
    for change in preview.get("detailed_changes") or []:
        if isinstance(change, dict) and isinstance(change.get("field_type"), str):
            sections.setdefault(change["field_type"], []).append(change)
    return {
        "step_order": ["keywords", "changes", "confirm"],
        "preview_id": preview.get("preview_id"),
        "preview_expires_at": preview.get("preview_expires_at"),
        "keyword_options": _keyword_options(preview),
        "ats_score": preview.get("ats_score"),
        "score_history": (preview.get("_mobile") or {}).get("ats_history") or [],
        "keyword_placements": preview.get("keyword_placements") or [],
        "change_summary": {key: summary.get(key, 0) for key in (
            "skills_added", "skills_removed", "certifications_added",
            "descriptions_modified", "high_risk_changes", "total_changes"
        )},
        "change_sections": [
            {"type": kind, "items": sections[kind]}
            for kind in ("summary", "skill", "experience", "description",
                         "education", "project", "certification", "language", "award")
            if kind in sections
        ],
        "warnings": preview.get("warnings") or [],
    }


def _preview_length_options(body: MobileTailorRequest) -> dict[str, Any]:
    """No limit (None) means no forced page fitting or bullet truncation."""
    options: dict[str, Any] = {}
    if body.max_pages is not None:
        options["max_pages"] = body.max_pages
        options["page_fit"] = PageFitSettings(template="swiss-single", pageSize="A4")
    if body.max_bullets_per_entry is not None:
        options["max_bullets_per_entry"] = body.max_bullets_per_entry
    return options


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    """Do not send full job text and CV contents in a library-list response."""
    return {
        key: record.get(key)
        for key in (
            "id", "source_resume_id", "engine_resume_id", "job_title", "company",
            "max_pages", "max_bullets_per_entry", "prompt_id", "status",
            "created_at", "updated_at",
        )
    }


async def _owned_tailoring(
    backend: SupabaseBackend, user_id: str, tailoring_id: UUID
) -> dict[str, Any]:
    record = await backend.get_tailored_record(user_id, str(tailoring_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Tailored CV not found")
    return record


def _public_job(row: dict[str, Any]) -> dict[str, Any]:
    """Exclude job description, private CV data and worker credentials."""
    return {
        "progress_id": row["id"],
        "status": row["status"],
        "tailoring_id": row.get("tailoring_id"),
        "current_step": row.get("current_step"),
        "completed_steps": row.get("completed_steps") or [],
        "visited_steps": row.get("visited_steps") or [],
        "error_code": row.get("error_code"),
        "started_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@router.post("/tailor/jobs", status_code=202)
async def enqueue_mobile_tailor(
    body: MobileTailorRequest,
    user_id: str = Depends(ai_processing_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    """Idempotently enqueue a durable AI job and return immediately to iOS."""
    source_id = str(body.resume_id)
    if await backend.get_record(user_id, source_id) is None:
        raise HTTPException(status_code=404, detail="Original CV not found")
    with resume_owner(user_id):
        source = await db.get_resume(source_id)
        if not source or source.get("processing_status") != "ready":
            raise HTTPException(status_code=409, detail="CV must be Ready before tailoring")
    if body.progress_id is None:
        raise HTTPException(status_code=422, detail="A unique progress_id UUID is required")
    try:
        job = await backend.enqueue_mobile_tailoring(
            user_id, str(body.progress_id), source_id, body.job_description,
            body.max_pages, body.max_bullets_per_entry)
    except MobileJobConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {**_public_job(job), "queued": job["status"] == "queued"}


@router.get("/tailor/jobs/recent")
async def recent_mobile_tailor_jobs(
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    """Recover interrupted navigation or app relaunch from durable server state."""
    jobs = await backend.list_mobile_jobs(user_id, limit=10)
    return {"jobs": [_public_job(job) for job in jobs]}


@router.get("/tailor/jobs/{progress_id}/result")
async def completed_mobile_tailor_result(
    progress_id: UUID,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    """Recover the already-generated preview; never start another paid AI run."""
    progress = await backend.get_mobile_progress(user_id, str(progress_id))
    if progress is None:
        raise HTTPException(status_code=404, detail="Tailoring job not found")
    if progress["status"] != "succeeded" or not progress.get("tailoring_id"):
        raise HTTPException(status_code=409, detail="Tailoring job has not completed successfully")
    record = await backend.get_tailored_record(user_id, progress["tailoring_id"])
    if record is None or record["status"] != "draft":
        raise HTTPException(status_code=409, detail="Tailored preview already saved or unavailable")
    data = record.get("preview_json") or {}
    if record.get("preview_id") != data.get("preview_id"):
        raise HTTPException(status_code=409, detail="Tailored preview unavailable")
    return {"tailoring_id": record["id"], "progress_id": str(progress_id),
            "status": "draft", "preview": data,
            "review": _review_ui(data),
            "preferences": {"max_pages": record.get("max_pages"),
                            "max_bullets_per_entry": record.get("max_bullets_per_entry"),
                            "tailoring_intensity": "full"}}


@router.get("/tailor/progress/{progress_id}")
async def get_mobile_tailor_progress(
    progress_id: UUID,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    """Owner-only, lightweight polling endpoint; never returns private CV content."""
    row = await backend.get_mobile_progress(user_id, str(progress_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Progress not found")
    return {
        "progress_id": row["id"],
        "status": row["status"],
        "current_step": row.get("current_step"),
        "completed_steps": row.get("completed_steps") or [],
        "visited_steps": row.get("visited_steps") or [],
        "tailoring_id": row.get("tailoring_id"),
        "error_code": row.get("error_code"),
        "started_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@router.post("/tailor/preview", status_code=201)
async def preview_mobile_tailor(
    body: MobileTailorRequest,
    user_id: str = Depends(ai_processing_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    """Run full tailoring and report ACTUAL stages while POST is in flight."""
    source_id = str(body.resume_id)
    if await backend.get_record(user_id, source_id) is None:
        raise HTTPException(status_code=404, detail="Original CV not found")

    with resume_owner(user_id):
        source = await db.get_resume(source_id)
        if source is None or source.get("processing_status") != "ready":
            raise HTTPException(status_code=409, detail="CV must be Ready before tailoring")

        progress_id = str(body.progress_id or uuid4())
        # A progress UUID is unique to this run, and cannot be queried without
        # the authenticated user's identity. Registration occurs BEFORE AI work.
        await backend.create_mobile_progress(user_id, progress_id, source_id)
        watcher = asyncio.create_task(
            mirror_engine_stages(backend, user_id, progress_id),
            name="mobile-tailoring-stage-mirror",
        )
        job_id: str | None = None
        stored: dict[str, Any] | None = None
        succeeded = False
        try:
            # The mobile client never supplies arbitrary job IDs.
            jobs = await db.create_jobs([body.job_description], resume_id=source_id)
            job_id = jobs[0]["job_id"]
            preview = await improve_resume_preview_endpoint(
                ImproveResumeRequest(
                    resume_id=source_id,
                    job_id=job_id,
                    prompt_id="full",
                    progress_id=progress_id,
                    **_preview_length_options(body),
                )
            )
            data = preview.data.model_dump(mode="json")
            if not data.get("preview_id"):
                raise HTTPException(status_code=503, detail="Tailoring preview was not registered")
            score = (data.get("ats_score") or {}).get("overall_score")
            if isinstance(score, (int, float)):
                data["_mobile"] = {"ats_history": [score]}
            stored = await backend.create_tailored_record({
                "user_id": user_id,
                "source_resume_id": source_id,
                "job_id": job_id,
                "job_description": body.job_description,
                "job_title": (await db.get_job(job_id) or {}).get("role"),
                "company": (await db.get_job(job_id) or {}).get("company"),
                "max_pages": body.max_pages,
                "max_bullets_per_entry": body.max_bullets_per_entry,
                "prompt_id": "full",
                "status": "draft",
                "preview_id": data["preview_id"],
                "preview_expires_at": data.get("preview_expires_at"),
                "preview_json": data,
            })
            succeeded = True
        except Exception:
            if job_id is not None:
                await db.delete_job(job_id)
            raise
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            try:
                await backend.update_mobile_progress(
                    user_id,
                    progress_id,
                    finished_progress_update(
                        progress_id,
                        succeeded=succeeded,
                        tailoring_id=str(stored["id"]) if stored else None,
                    ),
                )
            except SupabaseUnavailable:
                # Do not discard an already-generated preview because of
                # telemetry. The synchronous POST still determines navigation.
                logger.warning("Unable to finalize mobile progress telemetry")

    assert stored is not None
    return {
        "tailoring_id": stored["id"],
        "progress_id": progress_id,
        "status": "draft",
        "preview": data,
        "review": _review_ui(data),
        "preferences": {
            "max_pages": body.max_pages,
            "max_bullets_per_entry": body.max_bullets_per_entry,
            "tailoring_intensity": "full",
        },
    }


@router.get("/tailor/{tailoring_id}/review")
async def get_mobile_tailor_review(
    tailoring_id: UUID,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    """Reload the owner's latest saved draft after navigation or app relaunch."""
    record = await _owned_tailoring(backend, user_id, tailoring_id)
    if record["status"] != "draft":
        raise HTTPException(status_code=409, detail="Tailored CV has already been saved")
    preview = record.get("preview_json") or {}
    if preview.get("preview_id") != record.get("preview_id"):
        raise HTTPException(status_code=409, detail="Draft preview is unavailable")
    return {"tailoring_id": str(tailoring_id), "status": "draft",
            "preview": preview, "review": _review_ui(preview)}


@router.post("/tailor/{tailoring_id}/keywords")
async def apply_mobile_tailor_keywords(
    tailoring_id: UUID,
    body: MobileApplyKeywordsRequest,
    user_id: str = Depends(ai_processing_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    """Apply real job keywords repeatedly without changing the original CV."""
    record = await _owned_tailoring(backend, user_id, tailoring_id)
    if record["status"] != "draft" or record.get("preview_id") != body.expected_preview_id:
        raise HTTPException(status_code=409, detail="Draft changed. Refresh review")
    current = record.get("preview_json") or {}
    if current.get("preview_id") != body.expected_preview_id or not current.get("resume_preview"):
        raise HTTPException(status_code=409, detail="Draft preview is unavailable")
    expiry = record.get("preview_expires_at")
    if not expiry or datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="Preview expired. Generate a new draft")
    source_id = record.get("source_resume_id")
    if not source_id or await backend.get_record(user_id, source_id) is None:
        raise HTTPException(status_code=409, detail="Original CV is unavailable")
    options = _keyword_options(current)
    applied = {str(x).casefold() for x in options["applied"]}
    selected = [x.keyword.strip().casefold() for x in body.keywords]
    if len(set(selected)) != len(selected) or any(x in applied or not x for x in selected):
        raise HTTPException(status_code=422, detail="Choose only new, distinct keywords")
    if len(applied) + len(selected) > 30:
        raise HTTPException(status_code=422, detail="Maximum 30 keywords per draft")
    supported = {x.casefold() for x in options["supported"]}
    needs_attestation = {x.casefold() for x in options["needs_attestation"]}
    for item in body.keywords:
        key = item.keyword.strip().casefold()
        if key not in supported and key not in needs_attestation:
            raise HTTPException(status_code=422, detail="Choose suggested job keywords only")
        if key in needs_attestation and not item.attested:
            raise HTTPException(status_code=422, detail="Confirm 'I have this' for new skills")

    token = await backend.claim_tailored_operation(user_id, str(tailoring_id), body.expected_preview_id)
    if not token:
        raise HTTPException(status_code=409, detail="Another tailoring operation is running")
    try:
        with resume_owner(user_id):
            source = await db.get_resume(source_id)
            job = await db.get_job(record["job_id"])
            if not source or not job or job.get("resume_id") != source_id:
                raise HTTPException(status_code=409, detail="Tailoring source was removed")
            result = await apply_keywords_endpoint(ApplyKeywordsRequest(
                resume_id=source_id, job_id=record["job_id"],
                preview_id=body.expected_preview_id,
                improved_data=ResumeData.model_validate(current["resume_preview"]),
                keywords=body.keywords, prompt_id="full",
            ))
        data = result.data.model_dump(mode="json")
        if not data.get("preview_id") or data["preview_id"] == body.expected_preview_id:
            raise HTTPException(status_code=503, detail="Engine did not register a new preview")
        # Preserve placements across multiple keyword iterations.
        previous = current.get("keyword_placements") or []
        recent = data.get("keyword_placements") or []
        placements = {x["keyword"].casefold(): x for x in previous + recent
                      if isinstance(x, dict) and isinstance(x.get("keyword"), str)}
        data["keyword_placements"] = list(placements.values())
        merged: dict[str, str] = {}
        for key in (current.get("forced_keywords") or []) + (data.get("forced_keywords") or []):
            if isinstance(key, str):
                merged[key.casefold()] = key
        data["forced_keywords"] = list(merged.values())
        scores = list((current.get("_mobile") or {}).get("ats_history") or [])
        score = (data.get("ats_score") or {}).get("overall_score")
        if isinstance(score, (int, float)):
            scores.append(score)
        data["_mobile"] = {"ats_history": scores[-31:]}
        await backend.finish_tailored_operation(user_id, str(tailoring_id), token, {
            "preview_id": data["preview_id"],
            "preview_expires_at": data.get("preview_expires_at"),
            "preview_json": data,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    finally:
        await backend.release_tailored_operation(user_id, str(tailoring_id), token)
    return {"tailoring_id": str(tailoring_id), "status": "draft",
            "preview": data, "review": _review_ui(data)}


@router.post("/tailor/{tailoring_id}/confirm")
async def confirm_mobile_tailor(
    tailoring_id: UUID,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
    body: MobileConfirmRequest | None = None,
) -> dict[str, Any]:
    """Approve exactly the saved server-generated preview, not client-authored facts."""
    record = await _owned_tailoring(backend, user_id, tailoring_id)
    if body is not None and record.get("preview_id") != body.expected_preview_id:
        raise HTTPException(status_code=409, detail="Preview changed. Review the latest CV before saving")
    if record["status"] == "ready":
        return {"tailoring_id": record["id"], "status": "ready",
                "tailored_cv": record.get("result_json")}
    if record["status"] != "draft":
        raise HTTPException(status_code=409, detail="Tailoring is not ready for confirmation")
    source_id = record.get("source_resume_id")
    if not source_id or await backend.get_record(user_id, source_id) is None:
        raise HTTPException(status_code=409, detail="The original CV is no longer available")
    expires = record.get("preview_expires_at")
    if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) < datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="Preview expired. Create a new preview")

    saved = record.get("preview_json") or {}
    if not saved.get("preview_id") or saved.get("preview_id") != record.get("preview_id"):
        raise HTTPException(status_code=409, detail="Stored preview is incomplete")

    token = await backend.claim_tailored_operation(user_id, str(tailoring_id), record["preview_id"])
    if not token:
        raise HTTPException(status_code=409, detail="Another tailoring operation is running")
    try:
        with resume_owner(user_id):
            source = await db.get_resume(source_id)
            job = await db.get_job(record["job_id"])
            if not source or not job or job.get("resume_id") != source_id:
                raise HTTPException(status_code=409, detail="Preview source is no longer available")
            result = await improve_resume_confirm_endpoint(
                ImproveResumeConfirmRequest(
                    resume_id=source_id,
                    job_id=record["job_id"],
                    preview_id=record["preview_id"],
                    improved_data=saved["resume_preview"],
                    improvements=saved.get("improvements", []),
                    forced_keywords=saved.get("forced_keywords", []),
                    master_skills_to_add=[],  # Never mutate the original CV.
                )
            )
        output = result.data.model_dump(mode="json")
        engine_id = output.get("resume_id")
        if not engine_id:
            raise HTTPException(status_code=503, detail="Tailored CV was not saved")
        await backend.finish_tailored_operation(user_id, str(tailoring_id), token, {
            "engine_resume_id": str(UUID(engine_id)), "status": "ready",
            "result_json": output,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, final_status="ready")
    finally:
        await backend.release_tailored_operation(user_id, str(tailoring_id), token)
    return {"tailoring_id": str(tailoring_id), "status": "ready", "tailored_cv": output}


@router.get("/tailored-cvs")
async def list_mobile_tailored_cvs(
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: Literal["ready", "draft"] | None = None,
) -> list[dict[str, Any]]:
    rows = await backend.list_tailored_records(user_id, limit=limit, offset=offset, status=status)
    with resume_owner(user_id):
        dates = await EditorStore(db).timestamps([row["engine_resume_id"] for row in rows if row.get("engine_resume_id") and row.get("status") == "ready"])
    return [{**_summary(row), **({"updated_at": dates[row["engine_resume_id"]]} if row.get("engine_resume_id") in dates else {})} for row in rows]


@router.get("/tailored-cvs/{tailoring_id}")
async def get_mobile_tailored_cv(
    tailoring_id: UUID,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    return await _owned_tailoring(backend, user_id, tailoring_id)
