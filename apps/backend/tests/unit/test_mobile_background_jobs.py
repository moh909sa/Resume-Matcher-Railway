"""Authenticated resumable background tailoring and anti-double-charge contract."""
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import mobile_tailoring as api
from app.services import mobile_jobs
from app.services.supabase_backend import SupabaseBackend, SupabaseUnavailable, MobileJobConflict


@pytest.mark.asyncio
async def test_enqueue_returns_quick_ack_without_calling_ai(monkeypatch):
    owner, resume, progress = (str(uuid4()) for _ in range(3))
    monkeypatch.setattr(api, "resume_owner", lambda _: nullcontext())
    monkeypatch.setattr(api.db, "get_resume", AsyncMock(return_value={
        "id": resume, "processing_status": "ready"}))
    backend = AsyncMock()
    backend.get_record.return_value = {"id": resume}
    backend.enqueue_mobile_tailoring.return_value = {
        "id": progress, "status": "queued", "tailoring_id": None,
        "created_at": datetime.now(timezone.utc).isoformat()}
    body = api.MobileTailorRequest(resume_id=resume, progress_id=progress,
                                   job_description="Network systems engineer role. " * 4)
    result = await api.enqueue_mobile_tailor(body, user_id=owner, backend=backend)
    assert result["status"] == "queued"
    assert result["progress_id"] == progress
    backend.enqueue_mobile_tailoring.assert_awaited_once()
    backend.create_tailored_record.assert_not_called()


@pytest.mark.asyncio
async def test_enqueue_refuses_missing_client_job_id(monkeypatch):
    owner, resume = (str(uuid4()) for _ in range(2))
    monkeypatch.setattr(api, "resume_owner", lambda _: nullcontext())
    monkeypatch.setattr(api.db, "get_resume", AsyncMock(return_value={
        "id": resume, "processing_status": "ready"}))
    backend = AsyncMock()
    backend.get_record.return_value = {"id": resume}
    body = api.MobileTailorRequest(
        resume_id=resume, job_description="Mission critical networks. " * 4)
    with pytest.raises(HTTPException) as failure:
        await api.enqueue_mobile_tailor(body, user_id=owner, backend=backend)
    assert failure.value.status_code == 422
    backend.enqueue_mobile_tailoring.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovered_finished_job_reuses_saved_preview_without_ai():
    owner, progress, tailoring = (str(uuid4()) for _ in range(3))
    backend = AsyncMock()
    backend.get_mobile_progress.return_value = {
        "id": progress, "status": "succeeded", "tailoring_id": tailoring}
    backend.get_tailored_record.return_value = {
        "id": tailoring, "status": "draft", "preview_id": "preview-x",
        "preview_json": {"preview_id": "preview-x",
                         "ats_score": {"overall_score": 61.5}}}
    result = await api.completed_mobile_tailor_result(
        uuid4(), user_id=owner, backend=backend)
    assert result["status"] == "draft"
    assert result["preview"]["preview_id"] == "preview-x"
    assert result["review"]["ats_score"]["overall_score"] == 61.5
    backend.get_tailored_record.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_success_persists_one_owned_draft(monkeypatch):
    owner, resume, progress, job_id, tailoring, claim = (str(uuid4()) for _ in range(6))
    job = {"user_id": owner, "id": progress, "source_resume_id": resume,
           "job_description": "Network maintenance, reliability, safety. " * 3,
           "claim_token": claim, "max_pages": None, "max_bullets_per_entry": None}
    backend = AsyncMock()
    backend.get_record.return_value = {"id": resume}
    backend.create_tailored_record.return_value = {"id": tailoring}
    monkeypatch.setattr(mobile_jobs, "resume_owner", lambda _: nullcontext())
    monkeypatch.setattr(mobile_jobs.db, "get_resume", AsyncMock(
        return_value={"id": resume, "processing_status": "ready"}))
    monkeypatch.setattr(mobile_jobs.db, "get_job", AsyncMock(
        return_value={"role": "Engineer", "company": "Demo"}))
    monkeypatch.setattr(mobile_jobs.db, "create_jobs", AsyncMock(
        return_value=[{"job_id": job_id}]))
    async def fake_engine(request):
        from app.services.progress import tracked_progress
        with tracked_progress(progress, "load_job_keywords") as state:
            state["stage"] = "plan_skill_targets"
            state["stage"] = "generate_resume"
        return SimpleNamespace(data=SimpleNamespace(model_dump=lambda mode: {
            "preview_id": "preview-new", "ats_score": {"overall_score": 53},
            "resume_preview": {"summary": "demo"}}))
    monkeypatch.setattr(mobile_jobs, "improve_resume_preview_endpoint", fake_engine)
    await mobile_jobs.process_queued_mobile_job(backend, job)
    assert backend.create_tailored_record.await_count == 1
    saved = backend.create_tailored_record.await_args.args[0]
    assert saved["progress_id"] == progress
    assert saved["user_id"] == owner
    assert saved["status"] == "draft"
    finish = backend.finish_mobile_job.await_args
    assert finish.args[1] == "succeeded"
    assert finish.kwargs["tailoring_id"] == tailoring


@pytest.mark.asyncio
async def test_worker_failure_does_not_generate_an_automatic_retry(monkeypatch):
    owner, resume, progress, claim, job_id = (str(uuid4()) for _ in range(5))
    job = {"user_id": owner, "id": progress, "source_resume_id": resume,
           "job_description": "Reliability engineering job. " * 4,
           "claim_token": claim, "max_pages": None, "max_bullets_per_entry": None}
    backend = AsyncMock()
    backend.get_record.return_value = {"id": resume}
    backend.find_tailored_for_progress.return_value = None
    monkeypatch.setattr(mobile_jobs, "resume_owner", lambda _: nullcontext())
    monkeypatch.setattr(mobile_jobs.db, "get_resume", AsyncMock(
        return_value={"id": resume, "processing_status": "ready"}))
    monkeypatch.setattr(mobile_jobs.db, "create_jobs", AsyncMock(
        return_value=[{"job_id": job_id}]))
    delete = AsyncMock()
    monkeypatch.setattr(mobile_jobs.db, "delete_job", delete)
    monkeypatch.setattr(mobile_jobs, "improve_resume_preview_endpoint", AsyncMock(
        side_effect=RuntimeError("Provider error")))
    await mobile_jobs.process_queued_mobile_job(backend, job)
    assert backend.finish_mobile_job.await_args.args[1] == "failed"
    backend.create_tailored_record.assert_not_awaited()
    delete.assert_awaited_once_with(job_id)


@pytest.mark.asyncio
async def test_idempotent_enqueue_returns_preexisting_inflight_job():
    owner, resume, progress = (str(uuid4()) for _ in range(3))
    description = "Network engineering and commissioning. " * 3
    backend = SupabaseBackend(AsyncMock())
    existing = {"id": progress, "user_id": owner, "status": "running",
                "source_resume_id": resume, "job_description": description,
                "max_pages": None, "max_bullets_per_entry": None}
    backend.list_mobile_jobs = AsyncMock(return_value=[existing])
    backend._request = AsyncMock()
    result = await backend.enqueue_mobile_tailoring(
        owner, progress, resume, description, None, None)
    assert result["id"] == progress
    backend._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_active_user_with_different_input_conflicts():
    owner, resume, old_progress, new_progress = (str(uuid4()) for _ in range(4))
    backend = SupabaseBackend(AsyncMock())
    existing = {"id": old_progress, "user_id": owner, "status": "running",
                "source_resume_id": resume, "job_description": "Other job. " * 10,
                "max_pages": None, "max_bullets_per_entry": None}
    backend.list_mobile_jobs = AsyncMock(return_value=[existing])
    backend.get_mobile_job = AsyncMock(return_value=None)
    backend._request = AsyncMock(side_effect=SupabaseUnavailable("db duplicate"))
    with pytest.raises(MobileJobConflict):
        await backend.enqueue_mobile_tailoring(
            owner, new_progress, resume, "My new job. " * 10, None, None)


@pytest.mark.asyncio
async def test_optional_writing_polish_skips_when_no_safe_time_remains(monkeypatch):
    from app.routers import resumes as preview_api

    ai = AsyncMock(side_effect=AssertionError("Do not bill optional AI"))
    monkeypatch.setattr(preview_api, "remaining_timeout", lambda: 40.0)
    monkeypatch.setattr(preview_api, "run_writing_loop", ai)
    tailored = {"summary": "Relevant network engineering experience"}
    result, report = await preview_api._run_tailor_writing_loop(
        tailored, "en", "full", {}
    )
    assert result is tailored
    assert report is None
    ai.assert_not_awaited()


@pytest.mark.asyncio
async def test_optional_writing_timeout_keeps_completed_tailoring(monkeypatch):
    from app.routers import resumes as preview_api

    async def stalled_polish(*_args, **_kwargs):
        raise TimeoutError("Optional provider call stalled")

    monkeypatch.setattr(preview_api, "remaining_timeout", lambda: 180.0)
    monkeypatch.setattr(preview_api, "run_writing_loop", stalled_polish)
    tailored = {"summary": "Already tailored and ready for final checks"}
    result, report = await preview_api._run_tailor_writing_loop(
        tailored, "en", "full", {}
    )
    assert result is tailored
    assert report is None


@pytest.mark.asyncio
async def test_optional_writing_preserves_global_deadline_failure(monkeypatch):
    from app.ai_budget import AIOperationDeadlineExceeded
    from app.routers import resumes as preview_api

    monkeypatch.setattr(preview_api, "remaining_timeout", lambda: 180.0)
    monkeypatch.setattr(
        preview_api, "run_writing_loop",
        AsyncMock(side_effect=AIOperationDeadlineExceeded("Global timeout")),
    )
    with pytest.raises(AIOperationDeadlineExceeded):
        await preview_api._run_tailor_writing_loop(
            {"summary": "content"}, "en", "full", {}
        )


def test_optional_writing_reserves_finalization_time():
    from app.routers.resumes import _optional_writing_budget

    assert _optional_writing_budget(240) == 45
    assert _optional_writing_budget(100) == 45
    assert _optional_writing_budget(60) == 30
    assert _optional_writing_budget(40) == 10
    assert _optional_writing_budget(25) == 0
