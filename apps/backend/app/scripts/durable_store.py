"""One-way, verified SQLite -> Supabase PostgreSQL cutover. No AI requests.

Runs before serving requests/workers. Source files and volumes are never removed.
The uploaded archive has no customer read policy. Counts and canonical row hashes
are verified inside the destination transaction before its completion marker.
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, func, inspect, select, text

from app.config import settings
from app.database import db
from app.models import Base

logger = logging.getLogger(__name__)
MIGRATION_KEY = "sqlite-to-supabase-v1"
BACKUP_BUCKET = "cv-engine-backups"


def canonical_hash(rows: list[dict[str, Any]]) -> str:
    """Stable across SQLite/PostgreSQL JSON whitespace and row order."""
    items = sorted(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)
    return hashlib.sha256("\n".join(items).encode()).hexdigest()


def inventory_snapshot(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read model-typed values from a consistent SQLite backup, never raw JSON."""
    engine = create_engine(f"sqlite:///{path}")
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            unknown = tables - set(Base.metadata.tables) - {"sqlite_sequence"}
            if unknown:
                raise RuntimeError(
                    "Source contains unrecognized tables; manual inventory required"
                )
            for table in Base.metadata.sorted_tables:
                if table.name not in tables:
                    result[table.name] = []
                    continue
                columns = {
                    column["name"] for column in inspect(conn).get_columns(table.name)
                }
                if columns != set(table.columns.keys()):
                    raise RuntimeError(f"Source schema needs review: {table.name}")
                result[table.name] = [
                    dict(row) for row in conn.execute(select(table)).mappings()
                ]
        return result
    finally:
        engine.dispose()


def merge_inventories(
    sources: list[dict[str, list[dict[str, Any]]]],
) -> dict[str, list[dict[str, Any]]]:
    """Retain IDs; conflicting IDs fail closed instead of selecting a winner."""
    result: dict[str, list[dict[str, Any]]] = {}
    for table in Base.metadata.sorted_tables:
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        for source in sources:
            for row in source[table.name]:
                key = tuple(row[column.name] for column in table.primary_key.columns)
                if key in rows and canonical_hash([rows[key]]) != canonical_hash([row]):
                    raise RuntimeError(f"Conflicting source identity in {table.name}")
                rows[key] = row
        result[table.name] = list(rows.values())
    return result


def make_backup(
    directory: Path,
) -> tuple[bytes, dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """sqlite backup() includes WAL transactions; tar also preserves key/config/uploads."""
    inventories = []
    filenames = []
    for filename in ("resume_matcher.db", "resume_matcher.mobile.db"):
        source = settings.data_dir / filename
        if not source.is_file():
            continue
        target = directory / filename
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as read:
            with sqlite3.connect(target) as write:
                read.backup(write)
                if write.execute("pragma integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("SQLite backup integrity check failed")
        inventories.append(inventory_snapshot(target))
        filenames.append(filename)
    if not filenames:
        raise RuntimeError("No source SQLite files found; refusing an empty cutover")
    rows = merge_inventories(inventories)
    manifest = {
        name: {"count": len(items), "sha256": canonical_hash(items)}
        for name, items in rows.items()
    }
    (directory / "inventory.json").write_text(json.dumps(manifest, sort_keys=True))
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for filename in [*filenames, "inventory.json"]:
            archive.add(directory / filename, arcname=filename)
        for filename in ("config.json", ".secret_key", "uploads"):
            source = settings.data_dir / filename
            if source.exists():
                archive.add(source, arcname=filename)
    return output.getvalue(), rows, manifest


def import_verified(
    rows: dict[str, list[dict[str, Any]]], manifest: dict[str, Any]
) -> None:
    """One PostgreSQL transaction: all rows, exact hashes, then completion marker."""
    db._ensure_initialized()
    with db._sync_engine.begin() as conn:
        conn.execute(text("select pg_advisory_xact_lock(21070, 1)"))
        complete = conn.execute(
            text("select status from migration_manifest where migration_key=:key"),
            {"key": MIGRATION_KEY},
        ).scalar()
        if complete == "complete":
            return
        for table in Base.metadata.sorted_tables:
            if conn.scalar(select(func.count()).select_from(table)):
                raise RuntimeError(
                    "Destination is not empty; refusing to overwrite existing documents"
                )
            items = rows[table.name]
            if items:
                conn.execute(table.insert(), items)
            saved = [dict(row) for row in conn.execute(select(table)).mappings()]
            if canonical_hash(saved) != canonical_hash(items):
                raise RuntimeError(f"Destination verification failed: {table.name}")
        conn.execute(
            text(
                "insert into migration_manifest(migration_key,status,manifest) values(:key,'complete',cast(:manifest as jsonb)) on conflict(migration_key) do update set status='complete',manifest=excluded.manifest,completed_at=now()"
            ),
            {"key": MIGRATION_KEY, "manifest": json.dumps(manifest)},
        )


async def prepare_durable_store() -> None:
    """Explicit deployment switch; never fall back to a competing writable file."""
    if os.environ.get("MIGRATE_ENGINE_SQLITE") != "1":
        return
    if not settings.database_url or settings.database_schema != "resume_engine":
        raise RuntimeError(
            "Durable migration requires the configured private PostgreSQL schema"
        )
    db._ensure_initialized()
    with db._sync_engine.connect() as conn:
        if (
            conn.execute(
                text("select status from migration_manifest where migration_key=:key"),
                {"key": MIGRATION_KEY},
            ).scalar()
            == "complete"
        ):
            return
    with tempfile.TemporaryDirectory(prefix="cv-engine-migration-") as scratch:
        archive, rows, inventory = await asyncio.to_thread(make_backup, Path(scratch))
        checksum = hashlib.sha256(archive).hexdigest()
        path = f"sqlite-to-supabase/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid4()}.tar.gz"
        key = settings.supabase_secret_key.get_secret_value()
        async with httpx.AsyncClient(
            base_url=settings.supabase_url, headers={"apikey": key}, timeout=120
        ) as client:
            response = await client.post(
                f"/storage/v1/object/{BACKUP_BUCKET}/{path}",
                content=archive,
                headers={"Content-Type": "application/gzip", "x-upsert": "false"},
            )
            response.raise_for_status()
            verified = await client.get(
                f"/storage/v1/object/authenticated/{BACKUP_BUCKET}/{path}"
            )
            verified.raise_for_status()
            if hashlib.sha256(verified.content).hexdigest() != checksum:
                raise RuntimeError("Remote backup verification failed")
        manifest = {
            "backup_bucket": BACKUP_BUCKET,
            "backup_path": path,
            "backup_sha256": checksum,
            "tables": inventory,
        }
        with db._sync_engine.begin() as conn:
            owners = {
                row.get("owner_id")
                for items in rows.values()
                for row in items
                if row.get("owner_id")
            }
            for owner in owners:
                conn.execute(
                    text(
                        "insert into public.cv_engine_backup_objects(storage_path,user_id) values(:path,cast(:owner as uuid)) on conflict do nothing"
                    ),
                    {"path": path, "owner": owner},
                )
            conn.execute(
                text(
                    "insert into migration_manifest(migration_key,status,manifest) values(:key,'backed_up',cast(:manifest as jsonb)) on conflict(migration_key) do update set manifest=excluded.manifest"
                ),
                {"key": MIGRATION_KEY, "manifest": json.dumps(manifest)},
            )
        await asyncio.to_thread(import_verified, rows, manifest)
        logger.info(
            "Durable engine migration verified: %s",
            {name: data["count"] for name, data in inventory.items()},
        )
