"""Finish confirmed deletions even when a device disconnects or is uninstalled."""

import asyncio
import logging
from contextlib import suppress

from app.mobile_access import supabase_backend
from app.mobile_privacy import db, finish_confirmed_deletion
from app.services.supabase_backend import SupabaseBackend

logger = logging.getLogger(__name__)


async def drain_confirmed_deletions(backend: SupabaseBackend) -> None:
    # The local fence proves authenticated confirmation even if its upstream
    # acknowledgement was interrupted. Supabase retains it across engine restores.
    local = await db.pending_account_deletions()
    for row in local:
        try:
            await backend.begin_account_deletion(row["user_id"], row["receipt_hash"])
            record = await backend.deletion_receipt(row["receipt_hash"])
            if record and record["status"] == "complete":
                await db.finish_account_deletion(row["user_id"])
        except Exception as error:
            logger.warning("Deletion fence will retry: %s", type(error).__name__)
    pending = await backend.pending_account_deletions()
    for row in pending:
        try:
            await finish_confirmed_deletion(
                row["user_id"], row["receipt_hash"], backend
            )
        except Exception as error:
            logger.warning("Deletion cleanup will retry: %s", type(error).__name__)


class MobileDeletionWorker:
    def __init__(self):
        self.task = None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="mobile-account-deletion")

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def run(self):
        while True:
            try:
                async for backend in supabase_backend():
                    await drain_confirmed_deletions(backend)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Account deletion will retry: %s", type(error).__name__)
            await asyncio.sleep(10)


mobile_deletion_worker = MobileDeletionWorker()
