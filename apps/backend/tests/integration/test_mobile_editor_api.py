"""Bearer authorization and revision semantics through the actual mobile router."""

import copy
import zlib
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.database import Database
from app.main import app
from app.mobile_access import supabase_backend
from app.resume_ownership import resume_owner
from app.routers import mobile_editor as routes
from app.routers import mobile_tailoring
from app.services.supabase_backend import InvalidSession
from tests.pdf_samples import build_pdf


@pytest.fixture
async def editor_api(tmp_path, monkeypatch):
    database = Database(tmp_path / "api.db")
    owner, other, tailoring_id = str(uuid4()), str(uuid4()), str(uuid4())
    with resume_owner(owner):
        original = await database.create_resume(
            content="source", is_master=True, processed_data={"summary": "source"}
        )
        saved = await database.create_resume(
            content="json",
            content_type="json",
            processed_data={
                "personalInfo": {"name": "Taylor"},
                "summary": "Engineer using Python",
            },
            parent_id=original["resume_id"],
        )
        job = await database.create_job(
            "Engineer using Python", resume_id=original["resume_id"]
        )
    record = {
        "id": tailoring_id,
        "engine_resume_id": saved["resume_id"],
        "source_resume_id": original["resume_id"],
        "job_id": job["job_id"],
        "status": "ready",
        "job_title": "Engineer",
        "company": "Example",
    }

    class Backend:
        async def verify_user(self, token):
            if token == "owner":
                return owner
            if token == "other":
                return other
            raise InvalidSession("Invalid token")

        async def get_tailored_record(self, user_id, requested_id):
            return (
                copy.deepcopy(record)
                if user_id == owner and requested_id == tailoring_id
                else None
            )

        async def list_tailored_records(self, user_id, **kwargs):
            return [copy.deepcopy(record)] if user_id == owner else []

    app.dependency_overrides[supabase_backend] = lambda: Backend()
    monkeypatch.setattr(settings, "mobile_api_enabled", True)
    monkeypatch.setattr(routes, "db", database)
    monkeypatch.setattr(mobile_tailoring, "db", database)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, tailoring_id, database, owner, record
    finally:
        app.dependency_overrides.pop(supabase_backend, None)
        await database.close()


async def test_editor_auth_validation_latest_and_replay(editor_api):
    client, tailoring_id, database, owner, record = editor_api
    url = f"/api/v1/mobile/tailored-cvs/{tailoring_id}/editor"
    assert (await client.get(url)).status_code == 401
    assert (
        await client.get(url, headers={"Authorization": "Bearer invalid"})
    ).status_code == 401
    assert (
        await client.get(url, headers={"Authorization": "Bearer other"})
    ).status_code == 404
    headers = {"Authorization": "Bearer owner"}
    loaded = await client.get(url, headers=headers)
    assert loaded.status_code == 200
    assert loaded.json()["revision"] == 1
    unavailable = await client.get(
        f"/api/v1/mobile/tailored-cvs/{tailoring_id}/ats-score", headers=headers
    )
    assert unavailable.status_code == 200
    assert unavailable.json()["total_keywords"] == 0
    assert unavailable.json()["matched_count"] == 0
    calculated = await client.post(
        f"/api/v1/mobile/tailored-cvs/{tailoring_id}/ats-score",
        headers=headers,
        json={"expected_revision": 1},
    )
    assert calculated.status_code == 200
    assert calculated.json()["status"] == "unavailable"
    assert calculated.json()["total_keywords"] == 0
    # Persisted unavailable analysis must remain a valid editor payload.
    assert (await client.get(url, headers=headers)).json()["analysis"][
        "matched_count"
    ] == 0
    payload = {
        "expected_revision": 1,
        "mutation_id": str(uuid4()),
        "resume": {**loaded.json()["resume"], "summary": "New user authored summary"},
        "confirmed_skills": ["Rust"],
    }
    response = await client.patch(url, headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert (
        await client.patch(url, headers=headers, json=payload)
    ).json() == response.json()
    assert (await client.get(url, headers=headers)).json()["resume"][
        "summary"
    ] == payload["resume"]["summary"]
    payload["mutation_id"] = str(uuid4())
    assert (await client.patch(url, headers=headers, json=payload)).status_code == 409
    payload["expected_revision"] = 0
    assert (await client.patch(url, headers=headers, json=payload)).status_code == 422
    with resume_owner(owner):
        assert (await database.get_resume(record["source_resume_id"]))[
            "content"
        ] == "source"


async def test_pdf_requires_owner_and_revision_returns_real_bytes(
    editor_api, monkeypatch
):
    client, tailoring_id, _, _, _ = editor_api
    url = f"/api/v1/mobile/tailored-cvs/{tailoring_id}/pdf"
    headers = {"Authorization": "Bearer owner"}
    assert (await client.get(url, headers=headers)).status_code == 422
    assert (
        await client.get(url + "?revision=1", headers={"Authorization": "Bearer other"})
    ).status_code == 404

    async def render(*args, **kwargs):
        return build_pdf(zlib.compress(b"BT /F1 12 Tf 72 720 Td (Taylor) Tj ET"))

    monkeypatch.setattr(routes, "render_resume_pdf", render)
    response = await client.get(url + "?revision=1&page_size=LETTER", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["x-resume-revision"] == "1"
    assert response.headers["x-pdf-page-count"] == "1"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content.startswith(b"%PDF-")
    assert (await client.get(url + "?revision=999", headers=headers)).status_code == 404


async def test_mobile_layout_is_saved_per_revision_and_shared_by_pdf_and_check(
    editor_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, tailoring_id, _, _, _ = editor_api
    base = f"/api/v1/mobile/tailored-cvs/{tailoring_id}"
    headers = {"Authorization": "Bearer owner"}
    loaded = (await client.get(base + "/editor", headers=headers)).json()
    assert loaded["layout"]["template"] == "swiss-single"
    assert loaded["layout"]["pageSize"] == "A4"
    assert "modern" in loaded["layout_options"]["templates"]
    payload = {
        "expected_revision": 1,
        "mutation_id": str(uuid4()),
        "resume": loaded["resume"],
        "confirmed_skills": loaded["confirmed_skills"],
        "layout": {
            "template": "modern",
            "pageSize": "LETTER",
            "margins": {"left": 14},
            "spacing": {"lineHeight": 4},
            "fontSize": {"base": 4, "headerFont": "sans-serif"},
        },
    }
    invalid = await client.patch(
        base + "/editor",
        headers=headers,
        json={**payload, "layout": {"template": "unknown"}},
    )
    assert invalid.status_code == 422
    saved = await client.patch(base + "/editor", headers=headers, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 2
    assert saved.json()["resume"] == loaded["resume"]
    assert saved.json()["layout"]["template"] == "modern"
    assert saved.json()["layout"]["margins"]["right"] == 10

    legacy_save = {key: value for key, value in payload.items() if key != "layout"}
    legacy_save.update(expected_revision=2, mutation_id=str(uuid4()))
    latest = await client.patch(base + "/editor", headers=headers, json=legacy_save)
    assert latest.status_code == 200
    assert latest.json()["layout"] == saved.json()["layout"]
    assert (await client.get(base + "/editor", headers=headers)).json()[
        "layout"
    ] == saved.json()["layout"]
    replay = await client.patch(base + "/editor", headers=headers, json=payload)
    assert replay.json()["revision"] == 2
    assert replay.json()["layout"] == saved.json()["layout"]
    changed_retry = await client.patch(
        base + "/editor",
        headers=headers,
        json={**payload, "layout": {"template": "clean"}},
    )
    assert changed_retry.status_code == 409

    renders = []

    async def render(url: str, page_size: str, **kwargs: object) -> bytes:
        query = parse_qs(urlparse(url).query)
        query.pop("draft")
        renders.append((page_size, query, kwargs["margins"]))
        return build_pdf(zlib.compress(b"BT /F1 12 Tf 72 720 Td (Taylor) Tj ET"))

    monkeypatch.setattr(routes, "render_resume_pdf", render)
    for revision in (1, 2):
        response = await client.get(
            base + "/pdf", headers=headers, params={"revision": revision}
        )
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF-")
    check = await client.post(
        base + "/parse-check", headers=headers, json={"expected_revision": 2}
    )
    assert check.status_code == 200
    assert check.json()["revision"] == 2
    assert renders[0][0] == "A4"
    assert renders[0][1]["template"] == ["swiss-single"]
    assert renders[1] == renders[2]
    page_size, query, margins = renders[1]
    assert page_size == "LETTER"
    assert query["template"] == ["modern"]
    assert query["fontSize"] == ["4"]
    assert query["headerFont"] == ["sans-serif"]
    assert query["lineHeight"] == ["4"]
    assert margins == {"top": 10, "right": 10, "bottom": 10, "left": 14}
    override = await client.get(base + "/pdf?revision=2&page_size=A4", headers=headers)
    assert override.status_code == 200
    assert renders[-1][0] == "A4"
    assert renders[-1][1]["template"] == ["modern"]


async def test_confirm_rejects_changed_preview_before_claim(editor_api, monkeypatch):
    client, tailoring_id, _, _, record = editor_api
    record.update(status="draft", preview_id="new-preview")
    # Fixture Backend reads this same record and auth still runs normally.
    response = await client.post(
        f"/api/v1/mobile/tailor/{tailoring_id}/confirm",
        headers={"Authorization": "Bearer owner"},
        json={"expected_preview_id": "old-preview"},
    )
    assert response.status_code == 409
    assert "Preview changed" in response.json()["detail"]


async def test_saved_cv_survives_original_deletion_and_library_uses_save_date(
    editor_api,
):
    client, tailoring_id, database, owner, record = editor_api
    with resume_owner(owner):
        await database.delete_resume(record["source_resume_id"])
    record["source_resume_id"] = None
    record["updated_at"] = "2000-01-01T00:00:00+00:00"
    headers = {"Authorization": "Bearer owner"}
    loaded = await client.get(
        f"/api/v1/mobile/tailored-cvs/{tailoring_id}/editor", headers=headers
    )
    assert loaded.status_code == 200
    saved = await client.patch(
        f"/api/v1/mobile/tailored-cvs/{tailoring_id}/editor",
        headers=headers,
        json={
            "expected_revision": 1,
            "mutation_id": str(uuid4()),
            "resume": loaded.json()["resume"],
            "confirmed_skills": [],
        },
    )
    assert saved.status_code == 200
    listing = await client.get(
        "/api/v1/mobile/tailored-cvs?status=ready&limit=30&offset=0", headers=headers
    )
    assert listing.status_code == 200
    assert listing.json()[0]["updated_at"] == saved.json()["updated_at"]
    recalculated = await client.post(
        f"/api/v1/mobile/tailored-cvs/{tailoring_id}/ats-score",
        headers=headers,
        json={"expected_revision": 2},
    )
    assert recalculated.status_code == 200
