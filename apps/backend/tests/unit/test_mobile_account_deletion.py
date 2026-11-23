"""Deletion isolates owners, fences late work, and retries after Auth disappears."""

import asyncio
import hashlib
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app import mobile_privacy
from app.account_activity import account_activity, stop_account_activity
from app.mobile_access import supabase_backend
from app.models import Job, MobileEditorRevision
from app.resume_ownership import resume_owner
from app.services.supabase_backend import SupabaseUnavailable

A = "760c63e9-c782-4631-a1fb-471e988740a1"
B = "760c63e9-c782-4631-a1fb-471e988740b2"


async def test_deletion_removes_only_owner_data_and_blocks_stale_writers(isolated_db):
    with resume_owner(A):
        first = await isolated_db.create_resume_atomic_master("Private CV A")
        await isolated_db.create_jobs(["Private job A"], resume_id=first["resume_id"])
        async with isolated_db._write_session() as session:
            session.add(
                MobileEditorRevision(
                    owner_id=A,
                    resume_id=first["resume_id"],
                    revision=1,
                    document={"secret": "A"},
                )
            )
            await session.commit()
    with resume_owner(B):
        other = await isolated_db.create_resume_atomic_master("Private CV B")
        await isolated_db.create_jobs(["Private job B"], resume_id=other["resume_id"])
    with resume_owner(A):
        await isolated_db.mark_account_deleting(A, "a" * 64)
        with pytest.raises(ValueError, match="deletion"):
            await isolated_db.update_resume(first["resume_id"], {"title": "late write"})
        await isolated_db.erase_account_documents(A)
        await isolated_db.erase_account_documents(A)
        assert await isolated_db.list_resumes() == []
        async with isolated_db._session() as session:
            assert (await session.scalars(select(Job.content))).all() == [
                "Private job B"
            ]
            assert not (await session.scalars(select(MobileEditorRevision))).all()
        with pytest.raises(ValueError, match="deletion"):
            await isolated_db.create_resume_atomic_master("Late resurrection")
    with resume_owner(B):
        assert (await isolated_db.get_resume(other["resume_id"]))[
            "content"
        ] == "Private CV B"


async def test_receipt_resumes_after_partial_failure_and_auth_deletion(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(mobile_privacy, "db", isolated_db)
    receipt = "a" * 64
    digest = hashlib.sha256(receipt.encode()).hexdigest()
    backend = AsyncMock()
    backend.verify_user.return_value = A
    backend.deletion_receipt.side_effect = lambda value: (
        {"user_id": A, "status": "deleting"} if value == digest else None
    )
    backend.erase_account_storage.side_effect = [SupabaseUnavailable("temporary"), None]
    app = FastAPI()
    app.include_router(mobile_privacy.router)
    app.dependency_overrides[supabase_backend] = lambda: backend
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {"receipt": receipt}
        assert (
            await client.post("/mobile/account/deletion", json=body)
        ).status_code == 401
        assert (
            await client.post(
                "/mobile/account/deletion",
                json=body,
                headers={"Authorization": "Bearer valid"},
            )
        ).status_code == 200
        with pytest.raises(SupabaseUnavailable):
            await client.post("/mobile/account/deletion/complete", json=body)
        backend.erase_auth_user.assert_not_awaited()
        assert (
            await client.post(
                "/mobile/account/deletion/complete", json={"receipt": "b" * 64}
            )
        ).status_code == 404
        assert (
            await client.post("/mobile/account/deletion/complete", json=body)
        ).json() == {"status": "complete"}
        backend.erase_auth_user.assert_awaited_once_with(A)
        backend.deletion_receipt.side_effect = None
        backend.deletion_receipt.return_value = {"user_id": A, "status": "complete"}
        assert (
            await client.post("/mobile/account/deletion/complete", json=body)
        ).status_code == 200
        backend.erase_auth_user.assert_awaited_once()


async def test_deletion_cancels_only_the_owners_active_job():
    started = asyncio.Event()

    async def work(owner):
        with account_activity(owner):
            started.set()
            await asyncio.Event().wait()

    first = asyncio.create_task(work(A))
    await started.wait()
    second = asyncio.create_task(work(B))
    await asyncio.sleep(0)
    await stop_account_activity(A)
    assert first.cancelled()
    assert not second.done()
    second.cancel()
    await asyncio.gather(second, return_exceptions=True)


async def test_server_finishes_confirmed_deletion_without_device_or_auth(
    isolated_db, monkeypatch
):
    from app.services import mobile_deletions

    monkeypatch.setattr(mobile_privacy, "db", isolated_db)
    monkeypatch.setattr(mobile_deletions, "db", isolated_db)
    with resume_owner(A):
        await isolated_db.create_resume_atomic_master("Confirmed then device lost")
        await isolated_db.mark_account_deleting(A, "a" * 64)
    backend = AsyncMock()
    backend.deletion_receipt.return_value = {"status": "deleting"}
    backend.pending_account_deletions.return_value = [
        {"user_id": A, "receipt_hash": "a" * 64}
    ]
    backend.erase_account_storage.side_effect = [SupabaseUnavailable("outage"), None]
    await mobile_deletions.drain_confirmed_deletions(backend)
    backend.erase_auth_user.assert_not_awaited()
    await mobile_deletions.drain_confirmed_deletions(backend)
    backend.erase_auth_user.assert_awaited_once_with(A)
    assert await isolated_db.pending_account_deletions() == []
    with resume_owner(A):
        assert await isolated_db.list_resumes() == []
        assert await isolated_db.account_deleting(A)
