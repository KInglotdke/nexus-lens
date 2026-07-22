"""SQLite-backed processing catalog for deduplication and resumability."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ProcessingCatalog:
    """Track the terminal or resumable state of every discovered match."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()
        self._migrate_schema()

    def __enter__(self) -> "ProcessingCatalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def is_processed(self, match_id: str) -> bool:
        row = self._connection.execute(
            "SELECT status FROM processed_matches WHERE match_id = ?",
            (match_id,),
        ).fetchone()
        return row is not None and row["status"] == "processed"

    def begin_processing(
        self,
        *,
        match_id: str,
        routing_region: str,
        source_snapshot: str,
        queue_id: int | None,
    ) -> None:
        now = _utc_now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO processed_matches (
                    match_id, routing_region, patch, queue_id, source_snapshot,
                    processing_timestamp, status, failure_code, failure_reason
                ) VALUES (?, ?, NULL, ?, ?, ?, 'processing', NULL, NULL)
                ON CONFLICT(match_id) DO UPDATE SET
                    routing_region = excluded.routing_region,
                    queue_id = excluded.queue_id,
                    source_snapshot = excluded.source_snapshot,
                    processing_timestamp = excluded.processing_timestamp,
                    status = 'processing',
                    failure_code = NULL,
                    failure_reason = NULL
                WHERE processed_matches.status != 'processed'
                """,
                (match_id, routing_region, queue_id, source_snapshot, now),
            )

    def record_processed(
        self,
        *,
        match_id: str,
        routing_region: str,
        api_game_version: str | None,
        api_patch: str | None,
        public_patch: str | None,
        patch_resolution_method: str,
        patch_resolution_status: str,
        queue_id: int,
        source_snapshot: str,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO processed_matches (
                    match_id, routing_region, patch, api_game_version, api_patch,
                    public_patch, patch_resolution_method, patch_resolution_status,
                    queue_id, source_snapshot, processing_timestamp, status,
                    failure_code, failure_reason
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'processed', NULL, NULL)
                ON CONFLICT(match_id) DO UPDATE SET
                    routing_region = excluded.routing_region,
                    patch = NULL,
                    api_game_version = excluded.api_game_version,
                    api_patch = excluded.api_patch,
                    public_patch = excluded.public_patch,
                    patch_resolution_method = excluded.patch_resolution_method,
                    patch_resolution_status = excluded.patch_resolution_status,
                    queue_id = excluded.queue_id,
                    source_snapshot = excluded.source_snapshot,
                    processing_timestamp = excluded.processing_timestamp,
                    status = 'processed',
                    failure_code = NULL,
                    failure_reason = NULL
                """,
                (
                    match_id,
                    routing_region,
                    api_game_version,
                    api_patch,
                    public_patch,
                    patch_resolution_method,
                    patch_resolution_status,
                    queue_id,
                    source_snapshot,
                    _utc_now(),
                ),
            )

    def record_rejected(
        self,
        *,
        match_id: str,
        routing_region: str,
        source_snapshot: str,
        queue_id: int | None,
        failure_code: str,
        failure_reason: str,
        api_game_version: str | None = None,
        api_patch: str | None = None,
        public_patch: str | None = None,
        patch_resolution_method: str | None = None,
        patch_resolution_status: str | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO processed_matches (
                    match_id, routing_region, patch, api_game_version, api_patch,
                    public_patch, patch_resolution_method, patch_resolution_status,
                    queue_id, source_snapshot, processing_timestamp, status,
                    failure_code, failure_reason
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?)
                ON CONFLICT(match_id) DO UPDATE SET
                    routing_region = excluded.routing_region,
                    patch = NULL,
                    api_game_version = excluded.api_game_version,
                    api_patch = excluded.api_patch,
                    public_patch = excluded.public_patch,
                    patch_resolution_method = excluded.patch_resolution_method,
                    patch_resolution_status = excluded.patch_resolution_status,
                    queue_id = excluded.queue_id,
                    source_snapshot = excluded.source_snapshot,
                    processing_timestamp = excluded.processing_timestamp,
                    status = 'rejected',
                    failure_code = excluded.failure_code,
                    failure_reason = excluded.failure_reason
                WHERE processed_matches.status != 'processed'
                """,
                (
                    match_id,
                    routing_region,
                    api_game_version,
                    api_patch,
                    public_patch,
                    patch_resolution_method,
                    patch_resolution_status,
                    queue_id,
                    source_snapshot,
                    _utc_now(),
                    failure_code,
                    failure_reason,
                ),
            )

    def match_observation(self, match_id: str) -> dict[str, Any] | None:
        """Return cached terminal metadata without exposing it in reports."""

        row = self._connection.execute(
            """
            SELECT status, failure_code, api_patch, public_patch,
                   patch_resolution_status, queue_id, source_snapshot
            FROM processed_matches WHERE match_id = ?
            """,
            (match_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def stats(self) -> dict[str, Any]:
        status_rows = self._connection.execute(
            "SELECT status, COUNT(*) AS count FROM processed_matches GROUP BY status"
        ).fetchall()
        failure_rows = self._connection.execute(
            """
            SELECT failure_code, COUNT(*) AS count
            FROM processed_matches
            WHERE status = 'rejected'
            GROUP BY failure_code
            """
        ).fetchall()
        total = self._connection.execute(
            "SELECT COUNT(*) AS count FROM processed_matches"
        ).fetchone()["count"]
        return {
            "total_entries": total,
            "by_status": {row["status"]: row["count"] for row in status_rows},
            "failures_by_category": {
                row["failure_code"]: row["count"] for row in failure_rows
            },
        }

    def processed_match_ids(self) -> set[str]:
        """Return internal IDs used to hide orphaned partial files from reports."""

        rows = self._connection.execute(
            "SELECT match_id FROM processed_matches WHERE status = 'processed'"
        ).fetchall()
        return {str(row["match_id"]) for row in rows}

    def processed_public_patch(self, match_id: str) -> str | None:
        row = self._connection.execute(
            """
            SELECT public_patch FROM processed_matches
            WHERE match_id = ? AND status = 'processed'
            """,
            (match_id,),
        ).fetchone()
        return str(row["public_patch"]) if row and row["public_patch"] else None

    def needs_patch_migration(self, match_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT status, patch_resolution_status
            FROM processed_matches WHERE match_id = ?
            """,
            (match_id,),
        ).fetchone()
        return bool(
            row
            and row["status"] == "processed"
            and row["patch_resolution_status"] in (None, "legacy_unresolved")
        )

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_matches (
                    match_id TEXT PRIMARY KEY,
                    routing_region TEXT NOT NULL,
                    patch TEXT,
                    api_game_version TEXT,
                    api_patch TEXT,
                    public_patch TEXT,
                    patch_resolution_method TEXT,
                    patch_resolution_status TEXT,
                    queue_id INTEGER,
                    source_snapshot TEXT NOT NULL,
                    processing_timestamp TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('processing', 'processed', 'rejected')
                    ),
                    failure_code TEXT,
                    failure_reason TEXT
                )
                """
            )

    def _migrate_schema(self) -> None:
        existing = {
            row["name"]
            for row in self._connection.execute(
                "PRAGMA table_info(processed_matches)"
            ).fetchall()
        }
        additions = {
            "api_game_version": "TEXT",
            "api_patch": "TEXT",
            "public_patch": "TEXT",
            "patch_resolution_method": "TEXT",
            "patch_resolution_status": "TEXT",
        }
        with self._connection:
            for name, sql_type in additions.items():
                if name not in existing:
                    self._connection.execute(
                        f"ALTER TABLE processed_matches ADD COLUMN {name} {sql_type}"
                    )
            self._connection.execute(
                """
                UPDATE processed_matches
                SET api_patch = COALESCE(api_patch, patch),
                    patch_resolution_method = COALESCE(
                        patch_resolution_method, 'stage1_legacy'
                    ),
                    patch_resolution_status = COALESCE(
                        patch_resolution_status, 'legacy_unresolved'
                    )
                WHERE patch IS NOT NULL
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_processed_matches_status
                ON processed_matches(status)
                """
            )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
