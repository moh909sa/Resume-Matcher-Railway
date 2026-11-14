"""Real database regressions for saved editing; no network or AI calls."""

import copy
import hashlib
import sqlite3
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.database import Database
from app.resume_ownership import resume_owner
from app.routers.mobile_editor import SaveEditorRequest, _render_snapshot
from app.services.mobile_editor import EditorStore, calculate_analysis, print_document


@pytest.fixture
async def editing(tmp_path):
    database = Database(tmp_path / "editor.db")
    owner = str(uuid4())
    document = {
        "personalInfo": {"name": "Taylor", "title": "Engineer"},
        "summary": "Python engineer",
        "additional": {"technicalSkills": ["Python"]},
        "vendorData": {"keep": "unknown metadata"},
    }
    with resume_owner(owner):
        master = await database.create_resume(
            content="original",
            is_master=True,
            processed_data=document,
            processing_status="ready",
        )
        tailored = await database.create_resume(
            content="tailored",
            parent_id=master["resume_id"],
            processed_data=document,
            processing_status="ready",
        )
    yield database, owner, master["resume_id"], tailored["resume_id"], document
    await database.close()


async def test_atomic_save_replay_conflict_preserves_master_and_unknown(editing):
    database, owner, master_id, resume_id, document = editing
    store = EditorStore(database)
    changed = copy.deepcopy(document)
    changed["summary"] = "My own achievement"
    mutation = str(uuid4())
    with resume_owner(owner):
        assert (await store.read(resume_id))["revision"] == 1
        saved = await store.save(resume_id, 1, mutation, changed, ["Rust"])
        assert saved["revision"] == 2
        assert saved["resume"]["vendorData"] == document["vendorData"]
        assert saved["confirmed_skills"] == ["Rust"]
        assert (await database.get_resume(master_id))["content"] == "original"
        assert (await database.get_resume(resume_id))["processed_data"][
            "summary"
        ] == "My own achievement"
        assert await store.save(resume_id, 1, mutation, changed, ["Rust"]) == saved
        with pytest.raises(HTTPException) as conflict:
            await store.save(resume_id, 1, str(uuid4()), document, [])
        assert conflict.value.status_code == 409
        with pytest.raises(HTTPException):
            await store.save(resume_id, 1, mutation, document, [])
        assert (await store.read(resume_id, 1))["resume"]["summary"] == document[
            "summary"
        ]
        assert (await store.read(resume_id))["resume"] == changed
    # Same SQLite file, another authenticated owner cannot read or write it.
    with resume_owner(str(uuid4())):
        with pytest.raises(HTTPException) as missing:
            await store.read(resume_id)
        assert missing.value.status_code == 404
        with pytest.raises(HTTPException):
            await store.save(resume_id, 2, str(uuid4()), document, [])
    with resume_owner(owner):
        with pytest.raises(HTTPException):
            await store.save(master_id, 1, str(uuid4()), document, [])


async def test_committed_receipts_and_content_survive_reopen(editing):
    database, owner, _, resume_id, document = editing
    mutation = str(uuid4())
    with resume_owner(owner):
        saved = await EditorStore(database).save(resume_id, 1, mutation, document, [])
    path = database.db_path
    await database.close()
    reopened = Database(path)
    try:
        with resume_owner(owner):
            assert await EditorStore(reopened).read(resume_id) == saved
            assert (
                await EditorStore(reopened).save(resume_id, 1, mutation, document, [])
                == saved
            )
    finally:
        await reopened.close()


async def test_layout_migrates_existing_editor_revisions_and_survives_reopen(
    editing,
) -> None:
    database, owner, _, resume_id, document = editing
    with resume_owner(owner):
        original = await EditorStore(database).read(resume_id)
    path = database.db_path
    await database.close()
    with sqlite3.connect(path) as old_db:
        old_db.execute("ALTER TABLE mobile_editor_revisions DROP COLUMN layout")

    reopened = Database(path)
    try:
        with resume_owner(owner):
            store = EditorStore(reopened)
            assert await store.read(resume_id, 1) == original
            layout = {**original["layout"], "template": "clean", "pageSize": "LETTER"}
            saved = await store.save(resume_id, 1, str(uuid4()), document, [], layout)
            assert saved["layout"] == layout
    finally:
        await reopened.close()

    reopened = Database(path)
    try:
        with resume_owner(owner):
            store = EditorStore(reopened)
            assert await store.read(resume_id) == saved
            assert await store.read(resume_id, 1) == original
    finally:
        await reopened.close()


def job_context():
    content = "Engineer using Python, Rust and SQL"
    return {
        "content": content,
        "job_keywords_hash": hashlib.sha256(content.encode()).hexdigest(),
        "job_keywords": {
            "role": "Engineer",
            "required_skills": ["Python", "Rust"],
            "preferred_skills": ["SQL"],
            "keywords": ["Engineer"],
        },
    }


async def test_analysis_is_revision_bound_and_stale_after_edit(editing):
    database, owner, _, resume_id, document = editing
    with resume_owner(owner):
        store = EditorStore(database)
        analysis = await store.score(resume_id, 1, job_context())
        assert analysis["status"] == "current"
        assert analysis["revision"] == 1
        assert analysis["matched_count"] == 2
        assert analysis["missing_keywords"] == ["Rust", "SQL"]
        await store.save(resume_id, 1, str(uuid4()), document, [])
        result = await store.read(resume_id)
        assert result["analysis"]["status"] == "stale"
        assert result["analysis"]["revision"] == 1
        with pytest.raises(HTTPException):
            await store.score(resume_id, 1, job_context())
        assert (await store.score(resume_id, 2, job_context()))["revision"] == 2


def test_hidden_sections_html_attributes_and_condensed_bullets_do_not_score():
    document = {
        "personalInfo": {"title": "Engineer"},
        "summary": '<a href="Python">Rust</a>',
        "workExperience": [
            {"title": "Past role", "description": ["SQL"], "condensed": True}
        ],
        "additional": {"technicalSkills": ["Python"]},
        "sectionMeta": [
            {
                "id": "additional",
                "key": "additional",
                "displayName": "Skills",
                "sectionType": "stringList",
                "isVisible": False,
                "order": 1,
            }
        ],
    }
    result = calculate_analysis(document, job_context(), 3)
    assert result["matched_keywords"] == ["Rust", "Engineer"]
    assert result["missing_keywords"] == ["Python", "SQL"]
    assert calculate_analysis(document, {"content": "no keywords"}, 3)["score"] is None


def test_resume_validation_keeps_unknown_fields():
    raw = {"summary": "User-authored text", "unknown": {"keep": True}}
    body = SaveEditorRequest(expected_revision=1, mutation_id=uuid4(), resume=raw)
    assert body.resume == raw
    with pytest.raises(ValidationError):
        SaveEditorRequest(expected_revision=0, mutation_id=uuid4(), resume=raw)


async def test_pdf_uses_frozen_revision_and_discards_private_token(
    editing, monkeypatch
):
    from app.routers import mobile_editor as routes
    from app.services.page_fit import render_drafts

    database, owner, _, resume_id, document = editing
    monkeypatch.setattr(routes, "db", database)
    record = {"engine_resume_id": resume_id}
    captured = {}

    async def render(url, page_size, **kwargs):
        from urllib.parse import parse_qs, urlparse

        token = parse_qs(urlparse(url).query)["draft"][0]
        captured["token"] = token
        captured["data"] = copy.deepcopy(render_drafts.get(token))
        return b"%PDF-test"

    monkeypatch.setattr(routes, "render_resume_pdf", render)
    with resume_owner(owner):
        await EditorStore(database).read(resume_id)
        changed = {**document, "summary": "New version"}
        await EditorStore(database).save(resume_id, 1, str(uuid4()), changed, [])
        content, snapshot, fit = await _render_snapshot(record, 1, "LETTER")
    assert content == b"%PDF-test"
    assert snapshot["summary"] == document["summary"]
    assert captured["data"] == print_document(document)
    assert render_drafts.get(captured["token"]) is None
    assert fit.pageSize == "LETTER"


def test_partial_metadata_preserves_populated_sections_in_print_and_score():
    document = {
        "summary": "Engineer",
        "additional": {"technicalSkills": ["Python"]},
        "customSections": {"research": {"sectionType": "text", "text": "Rust"}},
        "sectionMeta": [
            {
                "id": "summary",
                "key": "summary",
                "displayName": "Profile",
                "sectionType": "text",
                "isVisible": True,
                "order": 0,
            }
        ],
    }
    printed = print_document(document)
    assert printed["sectionMeta"][0]["displayName"] == "Profile"
    assert {m["key"] for m in printed["sectionMeta"]} >= {
        "summary",
        "additional",
        "research",
    }
    result = calculate_analysis(document, job_context(), 1)
    assert "Python" in result["matched_keywords"]
    assert "Rust" in result["matched_keywords"]
    assert "sectionMeta" not in document["customSections"]


@pytest.mark.parametrize("visible", [True, False])
@pytest.mark.parametrize("include_unprefixed_metadata", [True, False])
def test_prefixed_custom_section_metadata_matches_editor_in_print_and_score(
    visible: bool, include_unprefixed_metadata: bool
) -> None:
    legacy_metadata = {
        "id": "legacy-research",
        "key": "customSections.research",
        "displayName": "Publications",
        "sectionType": "text",
        "isDefault": False,
        "isVisible": visible,
        "order": 2,
    }
    metadata = [legacy_metadata]
    if include_unprefixed_metadata:
        metadata.insert(
            0,
            {
                **legacy_metadata,
                "id": "research",
                "key": "research",
                "displayName": "Research",
                "isVisible": not visible,
                "order": 7,
            },
        )
    document = {
        "customSections": {"research": {"sectionType": "text", "text": "Rust"}},
        "sectionMeta": metadata,
    }
    original = copy.deepcopy(document)

    printed = print_document(document)
    custom_meta = [
        meta
        for meta in printed["sectionMeta"]
        if meta["key"] in {"research", "customSections.research"}
    ]
    assert custom_meta == [{**legacy_metadata, "key": "research"}]
    assert print_document(printed) == printed
    assert document == original
    result = calculate_analysis(document, job_context(), 1)
    assert ("Rust" in result["matched_keywords"]) is visible
