"""Backfill deterministic PDFs for saved CVs, without tailoring or AI."""

import asyncio
import logging
import os
from contextlib import suppress

from app.account_activity import account_activity
from app.database import db
from app.mobile_access import supabase_backend
from app.resume_ownership import resume_owner

logger = logging.getLogger(__name__)


async def backfill_saved_pdfs() -> None:
    """Only missing current revisions. Failures retry; existing artifacts stay immutable."""
    from app.routers.mobile_editor import _render_snapshot
    from app.services.mobile_editor import EditorStore
    from app.services.page_fit import count_pdf_pages

    while True:
        try:
            missing = False
            async for backend in supabase_backend():
                offset = 0
                while True:
                    rows = backend._json(
                        await backend._request(
                            "GET",
                            "/rest/v1/user_tailored_cvs",
                            params={
                                "status": "eq.ready",
                                "select": "*",
                                "order": "id",
                                "limit": "50",
                                "offset": str(offset),
                            },
                        )
                    )
                    for record in rows:
                        owner = record["user_id"]
                        with resume_owner(owner), account_activity(owner):
                            if await db.account_deleting(owner):
                                continue
                            snapshot = await EditorStore(db).read(
                                record["engine_resume_id"]
                            )
                            revision = snapshot["revision"]
                            if (
                                await backend.stored_pdf(owner, record["id"], revision)
                                is None
                            ):
                                try:
                                    content, _, _ = await _render_snapshot(
                                        record, revision, None
                                    )
                                    await backend.persist_pdf(
                                        owner,
                                        record["id"],
                                        revision,
                                        content,
                                        count_pdf_pages(content),
                                    )
                                except Exception as error:
                                    missing = True
                                    logger.warning(
                                        "Saved PDF backfill will retry: %s",
                                        type(error).__name__,
                                    )
                    if len(rows) < 50:
                        break
                    offset += len(rows)
            if not missing:
                logger.info("Saved CV PDF backfill is complete")
                return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Saved PDF backfill unavailable: %s", type(error).__name__)
        await asyncio.sleep(30)


class DurablePdfBackfill:
    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if db.url and os.environ.get("BACKFILL_SAVED_PDFS") == "1":
            self.task = asyncio.create_task(backfill_saved_pdfs(), name="saved-cv-pdfs")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None


durable_pdf_backfill = DurablePdfBackfill()
