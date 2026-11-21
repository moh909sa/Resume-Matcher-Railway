"""Authenticated original-CV lifecycle for the iOS application."""

import asyncio
import logging
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.datastructures import Headers

from app.ai_budget import AIOperationRoute
from app.config import settings
from app.database import db
from app.mobile_access import current_user, supabase_backend
from app.mobile_consent import ConsentDecision, ConsentView, ai_processing_user, read_consent, save_consent
from app.resume_ownership import resume_owner
from app.routers.resumes import (
    DOCUMENT_TYPES_BY_EXTENSION,
    read_resume_upload,
    retry_processing,
    store_resume_upload,
)
from app.services.supabase_backend import (
    BUCKET,
    OriginalFileMissing,
    SupabaseBackend,
    SupabaseUnavailable,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mobile", tags=["mobile"], route_class=AIOperationRoute)


class MobileResume(BaseModel):
    """App-facing data, without server keys, tokens, or other users' records."""

    id: UUID
    engine_resume_id: UUID | None = None
    original_filename: str
    display_name: str | None = None
    mime_type: str
    file_size_bytes: int
    processing_status: Literal["pending", "processing", "ready", "failed"]
    is_default: bool
    created_at: datetime
    updated_at: datetime
    processed_resume: dict[str, Any] | None = None
    recovery_required: bool = False


class RenameResume(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)


async def _record(
    backend: SupabaseBackend, user_id: str, resume_id: str
) -> dict[str, Any]:
    record = await backend.get_record(user_id, resume_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    return record


async def _sync(backend: SupabaseBackend, user_id: str) -> None:
    # Capture before the engine read; the database fences older default snapshots.
    observed_at = datetime.now(timezone.utc).isoformat()
    with resume_owner(user_id):
        rows = await db.list_resumes()
    snapshot = [
        {
            key: row[key]
            for key in (
                "resume_id",
                "processing_status",
                "updated_at",
                "is_default_master",
            )
        }
        for row in rows
    ]
    await backend.sync_records(user_id, snapshot, observed_at)


async def _best_effort_sync(backend: SupabaseBackend, user_id: str) -> None:
    try:
        async with asyncio.timeout(5):
            await _sync(backend, user_id)
    except (SupabaseUnavailable, TimeoutError):
        logger.warning("Registry reconciliation deferred; GET/list will retry")


async def _view(record: dict[str, Any], user_id: str) -> MobileResume:
    engine_id = record.get("engine_resume_id")
    with resume_owner(user_id):
        engine = await db.get_resume(str(engine_id)) if engine_id else None
    values = dict(record)
    if engine is not None:
        values.update(
            processing_status=engine["processing_status"],
            processed_resume=engine.get("processed_data"),
            is_default=engine.get("is_default_master", False),
        )
    elif engine_id:
        created = MobileResume.model_validate(record).created_at
        fresh = (
            record["processing_status"] == "pending"
            and (datetime.now(timezone.utc) - created).total_seconds()
            <= settings.request_timeout_seconds + 30
        )
        if not fresh:
            values.update(recovery_required=True, processing_status="failed")
    return MobileResume.model_validate(values)


@router.get("/me")
async def me(user_id: str = Depends(current_user)) -> dict[str, str]:
    return {"id": user_id}


@router.get("/ai-consent", response_model=ConsentView)
async def get_mobile_ai_consent(
    response: Response,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> ConsentView:
    response.headers["Cache-Control"] = "private, no-store"
    return await read_consent(backend, user_id)


@router.put("/ai-consent", response_model=ConsentView)
async def put_mobile_ai_consent(
    decision: ConsentDecision,
    response: Response,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> ConsentView:
    response.headers["Cache-Control"] = "private, no-store"
    return await save_consent(backend, user_id, decision)


@router.post("/resumes/upload", response_model=MobileResume, status_code=201)
async def upload_mobile_resume(
    request: Request,
    file: UploadFile = File(...),
    user_id: str = Depends(ai_processing_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> MobileResume:
    """Retain the validated original, then reuse the existing parse/CAS pipeline."""
    content, markdown = await read_resume_upload(file)
    filename = (file.filename or "resume.pdf").replace("\\", "/").rsplit("/", 1)[-1]
    if not 1 <= len(filename) <= 255:
        raise HTTPException(
            status_code=422, detail="Filename must be at most 255 characters"
        )
    resume_id = str(uuid4())
    suffix = Path(filename).suffix.lower()
    mime_type = DOCUMENT_TYPES_BY_EXTENSION[suffix]
    path = backend.object_path(user_id, resume_id, suffix)
    # A server-generated ID shared by the stores enables recovery after a crash.
    await backend.create_record(
        {
            "id": resume_id,
            "user_id": user_id,
            "engine_resume_id": resume_id,
            "original_filename": filename,
            "storage_bucket": BUCKET,
            "storage_path": path,
            "mime_type": mime_type,
            "file_size_bytes": len(content),
            "processing_status": "pending",
            "is_default": False,
        }
    )
    request.state.uploaded_resume = (resume_id, True)
    try:
        await backend.upload_original(path, content, mime_type)
        with resume_owner(user_id):
            await store_resume_upload(request, markdown, filename, resume_id=resume_id)
    except (Exception, asyncio.CancelledError):
        with resume_owner(user_id):
            engine = await db.get_resume(resume_id)
        if engine is None:
            try:
                await backend.update_record(
                    user_id, resume_id, {"processing_status": "failed"}
                )
            except SupabaseUnavailable:
                logger.warning("Pending upload remains recoverable by its registry ID")
        await _best_effort_sync(backend, user_id)
        raise
    await _sync(backend, user_id)
    return await _view(await _record(backend, user_id, resume_id), user_id)


@router.get("/resumes", response_model=list[MobileResume])
async def list_mobile_resumes(
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> list[MobileResume]:
    await _sync(backend, user_id)
    return [await _view(row, user_id) for row in await backend.list_records(user_id)]


@router.get("/resumes/{resume_id}", response_model=MobileResume)
async def get_mobile_resume(
    resume_id: UUID,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> MobileResume:
    await _record(backend, user_id, str(resume_id))
    await _sync(backend, user_id)
    return await _view(await _record(backend, user_id, str(resume_id)), user_id)


@router.post("/resumes/{resume_id}/retry-processing", response_model=MobileResume)
async def retry_mobile_resume(
    request: Request,
    resume_id: UUID,
    user_id: str = Depends(ai_processing_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> MobileResume:
    record = await _record(backend, user_id, str(resume_id))
    request.state.uploaded_resume = (str(resume_id), True)
    with resume_owner(user_id):
        engine = await db.get_resume(str(record.get("engine_resume_id")))
    try:
        if engine is None:
            try:
                content = await backend.download_original(
                    backend.checked_path(record, user_id)
                )
            except OriginalFileMissing as error:
                raise HTTPException(
                    status_code=409,
                    detail="Original file is unavailable. Upload it again.",
                ) from error
            upload = UploadFile(
                BytesIO(content),
                filename=record["original_filename"],
                headers=Headers({"content-type": record["mime_type"]}),
            )
            try:
                _, markdown = await read_resume_upload(upload)
            finally:
                await upload.close()
            with resume_owner(user_id):
                await store_resume_upload(
                    request,
                    markdown,
                    record["original_filename"],
                    resume_id=str(resume_id),
                )
        elif engine["processing_status"] != "ready" or not engine.get("processed_data"):
            with resume_owner(user_id):
                await retry_processing(str(resume_id))
    except (Exception, asyncio.CancelledError):
        await _best_effort_sync(backend, user_id)
        raise
    await _sync(backend, user_id)
    return await _view(await _record(backend, user_id, str(resume_id)), user_id)


@router.get("/resumes/{resume_id}/original")
async def download_original(
    resume_id: UUID,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> Response:
    record = await _record(backend, user_id, str(resume_id))
    try:
        content = await backend.download_original(backend.checked_path(record, user_id))
    except OriginalFileMissing as error:
        raise HTTPException(
            status_code=404, detail="Original file not found"
        ) from error
    return Response(
        content,
        media_type=record["mime_type"],
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(record['original_filename'], safe='')}"
        },
    )


@router.patch("/resumes/{resume_id}", response_model=MobileResume)
async def rename_mobile_resume(
    resume_id: UUID,
    body: RenameResume,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> MobileResume:
    await _record(backend, user_id, str(resume_id))
    with resume_owner(user_id):
        if await db.get_resume(str(resume_id)) is not None:
            await db.update_resume(str(resume_id), {"title": body.display_name})
    await backend.update_record(
        user_id, str(resume_id), {"display_name": body.display_name}
    )
    return await _view(await _record(backend, user_id, str(resume_id)), user_id)


@router.post("/resumes/{resume_id}/default", response_model=MobileResume)
async def default_mobile_resume(
    resume_id: UUID,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> MobileResume:
    await _record(backend, user_id, str(resume_id))
    with resume_owner(user_id):
        if not await db.set_default_master_resume(str(resume_id)):
            raise HTTPException(
                status_code=409, detail="Resume needs processing recovery first"
            )
    await _sync(backend, user_id)
    return await _view(await _record(backend, user_id, str(resume_id)), user_id)


@router.delete("/resumes/{resume_id}", status_code=204)
async def delete_mobile_resume(
    resume_id: UUID,
    user_id: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> Response:
    record = await _record(backend, user_id, str(resume_id))
    with resume_owner(user_id):
        await db.delete_resume(str(record.get("engine_resume_id")))
    # Keep the registry until bytes are removed, so transient failures can retry.
    await backend.delete_original(backend.checked_path(record, user_id))
    await backend.delete_record(user_id, str(resume_id))
    await _sync(backend, user_id)
    return Response(status_code=204)
