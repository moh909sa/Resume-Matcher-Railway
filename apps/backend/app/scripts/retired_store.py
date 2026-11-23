"""Privacy cleanup of retired source files, only after confirmed account deletion."""

import sqlite3
from pathlib import Path


def erase_retired_account(directory: Path, owner: str) -> None:
    """Never read these files as live data or reimport after the cutover marker."""
    for filename in ("resume_matcher.db", "resume_matcher.mobile.db"):
        path = directory / filename
        if not path.is_file():
            continue
        with sqlite3.connect(path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
            for table in (
                "mobile_editor_revisions",
                "tailoring_previews",
                "applications",
                "improvements",
                "queue_items",
                "jobs",
                "resumes",
            ):
                if table not in tables:
                    continue
                columns = {
                    row[1]
                    for row in connection.execute(f'pragma table_info("{table}")')
                }
                if "owner_id" in columns:
                    connection.execute(
                        f'delete from "{table}" where owner_id=?', (owner,)
                    )
            connection.commit()
            # Remove deleted payloads from free pages and the WAL too.
            connection.execute("pragma wal_checkpoint(truncate)")
            connection.execute("vacuum")
