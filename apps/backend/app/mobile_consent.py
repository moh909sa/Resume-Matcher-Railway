"""Authenticated, versioned consent for mobile CV processing."""

import hashlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterator

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from app.mobile_access import current_user, supabase_backend
from app.services.supabase_backend import SupabaseBackend

if TYPE_CHECKING:
    from app.llm import LLMConfig


class Disclosure(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_base: str | None = None
    recipients: list[str] = Field(min_length=1, max_length=5)
    retention: str = Field(min_length=1, max_length=480)
    openrouter_only: list[str] = Field(default_factory=list, max_length=5)


class ConsentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    accepted: StrictBool


class ConsentView(BaseModel):
    version: str
    recipients: list[str]
    retention: str
    accepted: bool | None


# Bump this revision if the purposes or data categories in the client notice change.
_NOTICE_REVISION = "cv-consent-v1"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def current_disclosure(config: "LLMConfig | None" = None) -> tuple[Disclosure, str]:
    if config is None:
        from app.llm import get_llm_config

        config = get_llm_config()
    try:
        notice = Disclosure.model_validate_json(os.getenv("MOBILE_AI_DISCLOSURE", ""))
    except ValidationError as error:
        raise HTTPException(
            503, "AI processing is unavailable while the disclosure is verified."
        ) from error
    if (
        not config.endpoint_confirmed
        or config.provider != notice.provider
        or config.model != notice.model
        or (config.api_base or "").rstrip("/") != (notice.api_base or "").rstrip("/")
        or any(
            not recipient.strip() or len(recipient) > 100
            for recipient in notice.recipients
        )
        or (
            config.provider == "openrouter"
            and (
                not notice.openrouter_only
                or any(not slug.strip() for slug in notice.openrouter_only)
                or (config.api_base or "").rstrip("/") not in ("", _OPENROUTER_BASE)
                or config.model.rstrip("/") in ("auto", "openrouter/auto")
            )
        )
        or (config.provider != "openrouter" and notice.openrouter_only)
    ):
        raise HTTPException(
            503, "AI processing is unavailable while the disclosure is verified."
        )
    version = hashlib.sha256(
        json.dumps([_NOTICE_REVISION, notice.model_dump()], sort_keys=True).encode()
    ).hexdigest()
    return notice, version


async def read_consent(backend: SupabaseBackend, owner: str) -> ConsentView:
    notice, version = current_disclosure()
    return ConsentView(
        version=version,
        recipients=notice.recipients,
        retention=notice.retention,
        accepted=await backend.ai_consent(owner, version),
    )


async def save_consent(
    backend: SupabaseBackend,
    owner: str,
    decision: ConsentDecision,
) -> ConsentView:
    notice, version = current_disclosure()
    if decision.version != version:
        raise HTTPException(409, "The AI disclosure changed. Review it again.")
    await backend.save_ai_consent(owner, version, decision.accepted)
    return ConsentView(
        version=version,
        recipients=notice.recipients,
        retention=notice.retention,
        accepted=decision.accepted,
    )


async def require_consent(backend: SupabaseBackend, owner: str) -> str:
    _, version = current_disclosure()
    if await backend.ai_consent(owner, version) is not True:
        raise HTTPException(428, "Allow AI processing before sending CV data.")
    return version


_scope: ContextVar[tuple[SupabaseBackend, str, str] | None] = ContextVar(
    "mobile_ai_consent",
    default=None,
)


@contextmanager
def consent_scope(backend: SupabaseBackend, owner: str, version: str) -> Iterator[None]:
    token = _scope.set((backend, owner, version))
    try:
        yield
    finally:
        _scope.reset(token)


async def ai_processing_user(
    owner: str = Depends(current_user),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> AsyncIterator[str]:
    version = await require_consent(backend, owner)
    with consent_scope(backend, owner, version):
        yield owner


async def consent_routing(config: "LLMConfig") -> dict[str, object] | None:
    scope = _scope.get()
    if scope is None:
        return None
    backend, owner, accepted_version = scope
    notice, current_version = current_disclosure(config)
    if (
        current_version != accepted_version
        or await backend.ai_consent(owner, current_version) is not True
    ):
        raise HTTPException(428, "Allow AI processing before sending CV data.")
    if notice.provider == "openrouter":
        return {"only": notice.openrouter_only, "allow_fallbacks": False}
    return None
