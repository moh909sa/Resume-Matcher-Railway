"""Public deployment facts and resumable, explicitly confirmed account deletion."""

import hashlib
import os
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from app.account_activity import stop_account_activity
from app.database import db
from app.mobile_access import bearer, supabase_backend
from app.mobile_consent import current_disclosure
from app.resume_ownership import resume_owner
from app.services.supabase_backend import InvalidSession, SupabaseBackend

router = APIRouter(prefix="/mobile", tags=["privacy"])


class DeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.get("/privacy")
async def privacy_facts() -> dict:
    """Allow inspection of nonsecret runtime facts; never expose URLs or keys."""
    from app.llm import get_llm_config

    config = get_llm_config()
    result = {
        "provider": config.provider,
        "model": config.model,
        "disclosure_ready": False,
        "recipients": [],
        "retention": None,
        "data_use": None,
        "support_email": os.getenv("PUBLIC_SUPPORT_EMAIL") or None,
        "operator": os.getenv("PUBLIC_OPERATOR_NAME") or None,
        "country": os.getenv("PUBLIC_OPERATOR_COUNTRY") or None,
        "app_retention": os.getenv("PUBLIC_APP_RETENTION") or None,
        "legal_approved": os.getenv("PUBLIC_LEGAL_APPROVED") == "true",
    }
    try:
        notice, version = current_disclosure(config)
        result.update(
            disclosure_ready=True,
            recipients=notice.recipients,
            retention=notice.retention,
            data_use=notice.data_use,
            version=version,
            downstream_routes=notice.openrouter_only,
        )
    except HTTPException:
        pass
    return result


@router.post("/account/deletion")
async def begin_deletion(
    body: DeletionRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, str]:
    if credentials is None:
        raise HTTPException(401, "Sign in to delete your account")
    try:
        owner = await backend.verify_user(credentials.credentials)
    except InvalidSession as error:
        raise HTTPException(401, "Sign in again") from error
    digest = hashlib.sha256(body.receipt.encode()).hexdigest()
    with resume_owner(owner):
        try:
            await db.mark_account_deleting(owner, digest)
        except ValueError as error:
            raise HTTPException(
                409,
                "Resume the existing deletion request on the device where you started it.",
            ) from error
    await backend.begin_account_deletion(owner, digest)
    await stop_account_activity(owner)
    return {"status": "deleting"}


@router.post("/account/deletion/complete")
async def complete_deletion(
    body: DeletionRequest,
    backend: SupabaseBackend = Depends(supabase_backend),
) -> dict[str, str]:
    # A high-entropy receipt authorizes only finishing the already-confirmed deletion.
    # It is sent in the body, never a URL/log; survives removal of Auth identity.
    digest = hashlib.sha256(body.receipt.encode()).hexdigest()
    record = await backend.deletion_receipt(digest)
    if record is None:
        raise HTTPException(404, "Deletion request not found")
    if record["status"] == "complete":
        return {"status": "complete"}
    owner = str(UUID(record["user_id"]))
    with resume_owner(owner):
        await db.mark_account_deleting(owner, digest)
        await stop_account_activity(owner)
        await db.erase_account_documents(owner)
    await backend.erase_account_storage(owner)
    await backend.erase_account_records(owner)
    await backend.erase_auth_user(owner)
    await backend.finish_account_deletion(digest)
    return {"status": "complete"}
