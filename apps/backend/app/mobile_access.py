"""Authentication dependencies and fail-closed mobile deployment routing."""

from collections.abc import AsyncIterator

import httpx
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.account_activity import account_activity
from app.config import settings
from app.services.supabase_backend import (
    InvalidSession,
    SupabaseBackend,
)

bearer = HTTPBearer(auto_error=False)


async def supabase_backend() -> AsyncIterator[SupabaseBackend]:
    """Construct a server-only client; a missing setup never enables old APIs."""
    if not settings.mobile_api_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    key = settings.supabase_secret_key.get_secret_value()
    if not settings.supabase_url or not key:
        raise HTTPException(status_code=503, detail="Mobile service is not configured")
    async with httpx.AsyncClient(
        base_url=settings.supabase_url,
        headers={"apikey": key},
        timeout=httpx.Timeout(30, connect=10),
        follow_redirects=False,
    ) as client:
        yield SupabaseBackend(client)


async def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    backend: SupabaseBackend = Depends(supabase_backend),
) -> AsyncIterator[str]:
    """Return only the identity confirmed by this project's Supabase Auth."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Sign in to continue",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        owner = await backend.verify_user(credentials.credentials)
    except InvalidSession as error:
        raise HTTPException(
            status_code=401,
            detail="Sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    from app.database import db
    from app.resume_ownership import resume_owner

    with resume_owner(owner):
        if await db.account_deleting(owner):
            raise HTTPException(
                403, "Account deletion is in progress. Resume it in My Account."
            )
    with account_activity(owner):
        yield owner


class MobileApiGuardMiddleware:
    """Close unauthenticated standalone APIs when serving customer accounts.

    This includes shared provider settings/reset and callbacks. Mobile routes
    verify sessions themselves. Local standalone mode remains the default.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and settings.mobile_api_enabled:
            path = scope.get("path", "")
            public = path in {
                "/",
                "/api/v1/health",
                "/docs",
                "/redoc",
                "/openapi.json",
                "/docs/oauth2-redirect",
            } and scope["method"] in {"GET", "HEAD"}
            # The Next.js print page fetches one-time drafts from the backend
            # over container loopback. Permit only that narrow internal route,
            # never a general bypass of mobile authorization or customer APIs.
            draft_prefix = "/api/v1/resumes/render-drafts/"
            peer = (scope.get("client") or ("", 0))[0]
            draft_token = (
                path[len(draft_prefix) :] if path.startswith(draft_prefix) else ""
            )
            internal_page_fit = (
                scope["method"] == "GET"
                and peer in {"127.0.0.1", "::1"}
                and len(draft_token) == 32
                and all(c in "0123456789abcdef" for c in draft_token)
            )
            if (
                not public
                and not internal_page_fit
                and not path.startswith("/api/v1/mobile/")
            ):
                await JSONResponse({"detail": "Not found"}, status_code=404)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)
