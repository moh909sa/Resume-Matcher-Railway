"""Authenticated saved CV editing, deterministic job match and snapshot PDF export."""

import hashlib
import json
from typing import Any, Literal, get_args
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import settings
from app.database import db
from app.mobile_access import current_user, supabase_backend
from app.pdf import PRINT_ERROR_SELECTOR, PDFRenderError, render_resume_pdf
from app.resume_ownership import resume_owner
from app.routers.mobile_tailoring import _owned_tailoring
from app.schemas.models import (
    PageFitSettings,
    ResumeData,
    ResumeLayoutSettings,
    ResumeTemplate,
)
from app.services.ats_parse import parse_check_pdf
from app.services.mobile_editor import (
    MAX_DOCUMENT_BYTES,
    SCORING_VERSION,
    EditorStore,
    print_document,
)
from app.services.page_fit import count_pdf_pages, print_margins, render_drafts
from app.services.supabase_backend import SupabaseBackend

router = APIRouter(prefix="/mobile/tailored-cvs", tags=["mobile-editor"])


class SaveEditorRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    mutation_id: UUID
    resume: dict[str, Any]
    confirmed_skills: list[str] = Field(default_factory=list, max_length=500)
    layout: ResumeLayoutSettings | None = None

    @field_validator("resume")
    @classmethod
    def validate_resume(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError("CV content is too large")
        # Validate the existing print schema, but persist the raw document so unknown
        # fields and metadata survive an edit/save round trip without being dropped.
        ResumeData.model_validate(value)
        return value

    @field_validator("confirmed_skills")
    @classmethod
    def validate_skills(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 200 for item in value):
            raise ValueError("Skills must contain between 1 and 200 characters")
        return list(dict.fromkeys(item.strip() for item in value))


class RevisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class ParseRequest(RevisionRequest):
    page_size: Literal["A4", "LETTER"] | None = None


async def _record(
    backend: SupabaseBackend, user_id: str, tailoring_id: UUID
) -> dict[str, Any]:
    record = await _owned_tailoring(backend, user_id, tailoring_id)
    if record.get("status") != "ready" or not record.get("engine_resume_id"):
        raise HTTPException(409, "Confirm and save this CV before editing")
    if record.get("engine_resume_id") == record.get("source_resume_id"):
        raise HTTPException(
            409, "The uploaded CV cannot be edited as a tailored document"
        )
    return record


async def _document(record: dict[str, Any]) -> dict[str, Any]:
    result = await EditorStore(db).read(record["engine_resume_id"])
    job = await db.get_job(record["job_id"])
    if (
        job
        and record.get("source_resume_id")
        and job.get("resume_id") != record.get("source_resume_id")
    ):
        raise HTTPException(409, "CV job context does not match")
    analysis = result.get("analysis")
    if analysis and (
        analysis.get("scoring_version") != SCORING_VERSION
        or analysis.get("job_hash")
        != hashlib.sha256(str((job or {}).get("content", "")).encode()).hexdigest()
    ):
        result["analysis"] = {**analysis, "status": "stale"}
    return {
        "tailoring_id": record["id"],
        **result,
        "layout_options": {
            "templates": list(get_args(ResumeTemplate)),
            "page_sizes": ["A4", "LETTER"],
        },
        "job": {
            "title": record.get("job_title"),
            "company": record.get("company"),
            "description": (job or {}).get("content")
            or record.get("job_description")
            or "",
        },
    }


@router.get("/{tailoring_id}/editor")
async def get_editor(
    tailoring_id: UUID,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    record = await _record(backend, user_id, tailoring_id)
    with resume_owner(user_id):
        return await _document(record)


@router.patch("/{tailoring_id}/editor")
async def save_editor(
    tailoring_id: UUID,
    body: SaveEditorRequest,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    record = await _record(backend, user_id, tailoring_id)
    with resume_owner(user_id):
        saved = await EditorStore(db).save(
            record["engine_resume_id"],
            body.expected_revision,
            str(body.mutation_id),
            body.resume,
            body.confirmed_skills,
            body.layout.model_dump(mode="json") if body.layout is not None else None,
        )
        document = await _document(record)
        # An idempotent replay must acknowledge exactly its own mutation even if
        # another device has subsequently saved a newer revision.
        return {**document, **saved}


@router.get("/{tailoring_id}/ats-score")
async def get_job_match(
    tailoring_id: UUID,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    record = await _record(backend, user_id, tailoring_id)
    with resume_owner(user_id):
        result = await _document(record)
        return result["analysis"] or {
            "status": "unavailable",
            "revision": result["revision"],
            "score": None,
            "reason": "Update Job Match to analyze this saved CV.",
            "matched_keywords": [],
            "missing_keywords": [],
            "required_skills": [],
            "preferred_skills": [],
            "total_keywords": 0,
            "matched_count": 0,
            "scoring_version": SCORING_VERSION,
            "job_hash": "",
            "calculated_at": None,
        }


@router.post("/{tailoring_id}/ats-score")
async def update_job_match(
    tailoring_id: UUID,
    body: RevisionRequest,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    record = await _record(backend, user_id, tailoring_id)
    with resume_owner(user_id):
        job = await db.get_job(record["job_id"])
        if not job or (
            record.get("source_resume_id")
            and job.get("resume_id") != record.get("source_resume_id")
        ):
            raise HTTPException(409, "Job context is unavailable")
        return await EditorStore(db).score(
            record["engine_resume_id"], body.expected_revision, job
        )


async def _render_snapshot(
    record: dict[str, Any], revision: int, page_size: Literal["A4", "LETTER"] | None
) -> tuple[bytes, dict[str, Any], PageFitSettings]:
    snapshot = await EditorStore(db).read(record["engine_resume_id"], revision)
    snapshot["resume"] = print_document(snapshot["resume"])
    try:
        ResumeData.model_validate(snapshot["resume"])
        layout = ResumeLayoutSettings.model_validate(snapshot["layout"])
    except ValidationError as error:
        raise HTTPException(
            409, "CV content needs correction before exporting"
        ) from error
    fit = PageFitSettings.for_mobile_layout(layout, page_size)
    token = render_drafts.put(snapshot["resume"])
    try:
        url = f"{settings.frontend_base_url}/print/resumes/draft?draft={token}&{urlencode(fit.to_query())}"
        content = await render_resume_pdf(
            url,
            fit.pageSize,
            selector=f".resume-print, {PRINT_ERROR_SELECTOR}",
            margins=print_margins(fit),
        )
        return content, snapshot["resume"], fit
    except PDFRenderError as error:
        raise HTTPException(
            503, "PDF generation is temporarily unavailable. Please try again."
        ) from error
    finally:
        render_drafts.discard(token)


@router.get("/{tailoring_id}/pdf")
async def export_pdf(
    tailoring_id: UUID,
    revision: int = Query(..., ge=1),
    page_size: Literal["A4", "LETTER"] | None = None,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> Response:
    record = await _record(backend, user_id, tailoring_id)
    with resume_owner(user_id):
        content, _, _ = await _render_snapshot(record, revision, page_size)
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="tailored_cv_{tailoring_id}_v{revision}.pdf"',
            "Cache-Control": "private, no-store",
            "X-Resume-Revision": str(revision),
            "X-PDF-Page-Count": str(count_pdf_pages(content)),
        },
    )


@router.post("/{tailoring_id}/parse-check")
async def check_pdf(
    tailoring_id: UUID,
    body: ParseRequest,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, Any]:
    record = await _record(backend, user_id, tailoring_id)
    with resume_owner(user_id):
        content, document, fit = await _render_snapshot(
            record, body.expected_revision, body.page_size
        )
        report = await parse_check_pdf(content, document, fit)
    return {
        "revision": body.expected_revision,
        "report": report.model_dump(mode="json"),
    }
