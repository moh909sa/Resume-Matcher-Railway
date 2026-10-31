"""Truthful progress tracking for the authenticated mobile tailoring API."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import mobile_tailoring as mobile
from app.services import mobile_progress
from app.services.progress import StageTracker, get_progress_history, tracked_progress


def test_saved_draft_does_not_mark_failed_writing_as_completed() -> None:
    progress_id = str(uuid4())
    with tracked_progress(progress_id, "critique_writing") as tracker:
        tracker["stage"] = "critique_writing_incomplete"
        tracker["stage"] = "rewrite_writing"
        tracker["stage"] = "rewrite_writing_incomplete"
        tracker["stage"] = "register_preview"
        timeline = mobile_progress.finished_progress_update(progress_id, succeeded=True)
        assert timeline["status"] == "succeeded"
        assert timeline["visited_steps"] == ["critique_writing", "rewrite_lines", "verify_review"]
        assert timeline["completed_steps"] == ["verify_review"]


def test_engine_stage_history_keeps_fast_transitions_and_ignores_timer():
    tracker = StageTracker("load_job_keywords")
    tracker["stage"] = "load_original_resume"
    tracker["stage"] = "plan_skill_targets"
    tracker["stage"] = "generate_resume_diffs"
    tracker["stage"] = "generate_resume_diffs"
    tracker["stage"] = "refine_resume"
    tracker["stage"] = "register_preview"
    assert tracker.history == [
        "load_job_keywords", "load_original_resume", "plan_skill_targets",
        "generate_resume_diffs", "refine_resume", "register_preview",
    ]
    timeline = mobile_progress.timeline_from_engine_history(tracker.history)
    assert timeline == {
        "current_step": "verify_review",
        "completed_steps": ["read_job", "plan_skills", "tailor_resume", "keywords_facts"],
        "visited_steps": ["read_job", "plan_skills", "tailor_resume",
                          "keywords_facts", "verify_review"],
    }
    assert "rewrite_lines" not in timeline["visited_steps"]


def test_finished_is_based_on_actual_stages_not_fake_100_percent():
    progress_id = str(uuid4())
    with tracked_progress(progress_id, "load_job_keywords") as tracker:
        tracker["stage"] = "generate_resume"
        timeline = mobile_progress.finished_progress_update(
            progress_id, succeeded=True, tailoring_id=str(uuid4()))
        assert timeline["status"] == "succeeded"
        assert timeline["stage"] == "complete"
        assert timeline["current_step"] is None
        assert timeline["completed_steps"] == ["read_job", "tailor_resume"]
        assert "rewrite_lines" not in timeline["completed_steps"]
    # Synchronous test contexts have no running event loop, so registration
    # is released immediately; async API calls retain the entry briefly.
    assert get_progress_history(progress_id) is None


def test_mobile_progress_request_is_optional_and_uuid_typed():
    request = mobile.MobileTailorRequest(
        resume_id=uuid4(), job_description="Experienced IT engineer required. " * 3)
    assert request.progress_id is None
    requested_id = uuid4()
    request = request.model_copy(update={"progress_id": requested_id})
    assert str(request.progress_id) == str(requested_id)


@pytest.mark.asyncio
async def test_another_users_progress_is_inaccessible():
    backend = AsyncMock()
    backend.get_mobile_progress.return_value = None
    user_id = str(uuid4())
    progress_id = uuid4()
    with pytest.raises(HTTPException) as error:
        await mobile.get_mobile_tailor_progress(
            progress_id, user_id=user_id, backend=backend)
    assert error.value.status_code == 404
    backend.get_mobile_progress.assert_awaited_once_with(user_id, str(progress_id))


@pytest.mark.asyncio
async def test_progress_mirror_only_writes_actual_stage_transitions():
    backend = AsyncMock()
    user_id = str(uuid4())
    progress_id = str(uuid4())
    with tracked_progress(progress_id, "load_job_keywords") as tracker:
        mirror = asyncio.create_task(mobile_progress.mirror_engine_stages(
            backend, user_id, progress_id))
        try:
            await asyncio.sleep(0.05)
            tracker["stage"] = "plan_skill_targets"
            tracker["stage"] = "generate_resume"
            await asyncio.sleep(0.74)
        finally:
            mirror.cancel()
            with pytest.raises(asyncio.CancelledError):
                await mirror
    assert backend.update_mobile_progress.await_count >= 1
    latest = backend.update_mobile_progress.await_args.args[2]
    assert latest["current_step"] == "tailor_resume"
    assert latest["completed_steps"] == ["read_job", "plan_skills"]


@pytest.mark.asyncio
async def test_successful_preview_links_a_real_progress_run(monkeypatch):
    user_id = str(uuid4())
    resume_id, job_id, tailoring_id, progress_id = (str(uuid4()) for _ in range(4))
    backend = AsyncMock()
    backend.get_record.return_value = {"id": resume_id}
    backend.create_tailored_record.return_value = {"id": tailoring_id}
    monkeypatch.setattr(mobile, "resume_owner", lambda _: nullcontext())
    monkeypatch.setattr(mobile.db, "get_resume", AsyncMock(
        return_value={"id": resume_id, "processing_status": "ready"}))
    monkeypatch.setattr(mobile.db, "get_job", AsyncMock(
        return_value={"resume_id": resume_id, "role": "Engineer", "company": "Test"}))
    monkeypatch.setattr(mobile.db, "create_jobs", AsyncMock(
        return_value=[{"job_id": job_id}]))
    monkeypatch.setattr(mobile.db, "delete_job", AsyncMock())

    async def simulate_engine(request):
        assert request.progress_id == progress_id
        with tracked_progress(request.progress_id, "load_job_keywords") as tracker:
            tracker["stage"] = "plan_skill_targets"
            tracker["stage"] = "generate_resume"
            tracker["stage"] = "register_preview"
        return SimpleNamespace(data=SimpleNamespace(model_dump=lambda mode: {
            "preview_id": "preview-abc",
            "ats_score": {"overall_score": 52.5},
            "resume_preview": {"summary": "Test"},
        }))
    monkeypatch.setattr(mobile, "improve_resume_preview_endpoint", simulate_engine)
    response = await mobile.preview_mobile_tailor(
        mobile.MobileTailorRequest(
            resume_id=resume_id,
            progress_id=progress_id,
            job_description="Network engineering experience required. " * 4,
        ), user_id=user_id, backend=backend)
    assert response["progress_id"] == progress_id
    assert response["tailoring_id"] == tailoring_id
    backend.create_mobile_progress.assert_awaited_once_with(
        user_id, progress_id, resume_id)
    assert backend.update_mobile_progress.await_count == 1
    update = backend.update_mobile_progress.await_args.args[2]
    assert update["status"] == "succeeded"
    assert update["tailoring_id"] == tailoring_id
    assert update["completed_steps"] == [
        "read_job", "plan_skills", "tailor_resume", "verify_review",
    ]
    mobile.db.delete_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_error_keeps_a_truthful_failed_progress(monkeypatch):
    user_id = str(uuid4())
    resume_id, job_id, progress_id = (str(uuid4()) for _ in range(3))
    backend = AsyncMock()
    backend.get_record.return_value = {"id": resume_id}
    monkeypatch.setattr(mobile, "resume_owner", lambda _: nullcontext())
    monkeypatch.setattr(mobile.db, "get_resume", AsyncMock(
        return_value={"id": resume_id, "processing_status": "ready"}))
    monkeypatch.setattr(mobile.db, "create_jobs", AsyncMock(
        return_value=[{"job_id": job_id}]))
    delete_mock = AsyncMock()
    monkeypatch.setattr(mobile.db, "delete_job", delete_mock)

    async def fail_engine(request):
        with tracked_progress(request.progress_id, "load_job_keywords") as tracker:
            tracker["stage"] = "generate_resume"
            raise RuntimeError("AI provider failed")
    monkeypatch.setattr(mobile, "improve_resume_preview_endpoint", fail_engine)
    with pytest.raises(RuntimeError, match="AI provider failed"):
        await mobile.preview_mobile_tailor(
            mobile.MobileTailorRequest(
                resume_id=resume_id, progress_id=progress_id,
                job_description="Experience in industrial networks. " * 3,
            ), user_id=user_id, backend=backend)
    delete_mock.assert_awaited_once_with(job_id)
    update = backend.update_mobile_progress.await_args.args[2]
    assert update["status"] == "failed"
    assert update["tailoring_id"] is None
    assert "tailor_resume" in update["visited_steps"]
    backend.create_tailored_record.assert_not_called()
