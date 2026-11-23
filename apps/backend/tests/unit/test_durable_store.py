"""Migration checks never call AI or a live service."""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.models import Base
from app.scripts.durable_store import (
    canonical_hash,
    inventory_snapshot,
    merge_inventories,
)


def empty() -> dict[str, list[dict]]:
    return {table.name: [] for table in Base.metadata.sorted_tables}


def test_canonical_verification_ignores_order_but_detects_content_change() -> None:
    rows = [
        {"id": "a", "data": {"summary": "Exact wording", "layout": [1, 2]}},
        {"id": "b"},
    ]
    assert canonical_hash(rows) == canonical_hash(list(reversed(rows)))
    changed = json.loads(json.dumps(rows))
    changed[0]["data"]["summary"] = "Different wording"
    assert canonical_hash(rows) != canonical_hash(changed)


def test_merge_retains_ids_and_rejects_competing_documents() -> None:
    first, second = empty(), empty()
    first["resumes"] = [
        {
            "resume_id": "same",
            "owner_id": "owner",
            "processed_data": {"summary": "Original"},
        }
    ]
    second["resumes"] = list(first["resumes"])
    assert merge_inventories([first, second])["resumes"] == first["resumes"]
    second["resumes"] = [
        {**first["resumes"][0], "processed_data": {"summary": "Newer"}}
    ]
    with pytest.raises(RuntimeError, match="Conflicting source identity"):
        merge_inventories([first, second])


def test_inventory_preserves_typed_json_and_fails_on_unknown_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "snapshot.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            Base.metadata.tables["mobile_editor_revisions"].insert(),
            {
                "resume_id": "resume",
                "owner_id": "owner",
                "revision": 42,
                "document": {"unknown_section": ["Keep", "Everything"]},
                "confirmed_skills": ["Evidence"],
                "layout": {"custom": "retained"},
                "mutation_id": "receipt",
            },
        )
    snapshot = inventory_snapshot(path)
    row = snapshot["mobile_editor_revisions"][0]
    assert row["revision"] == 42
    assert row["document"] == {"unknown_section": ["Keep", "Everything"]}
    assert row["layout"] == {"custom": "retained"}
    with engine.begin() as conn:
        conn.exec_driver_sql("create table unknown_customer_data(id text)")
    with pytest.raises(RuntimeError, match="unrecognized tables"):
        inventory_snapshot(path)
    engine.dispose()
