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
        patch: str,
        queue_id: int,
        source_snapshot: str,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO processed_matches (
                    match_id, routing_region, patch, queue_id, source_snapshot,
                    processing_timestamp, status, failure_code, failure_reason
                ) VALUES (?, ?, ?, ?, ?, ?, 'processed', NULL, NULL)
                ON CONFLICT(match_id) DO UPDATE SET
                    routing_region = excluded.routing_region,
                    patch = excluded.patch,
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
                    patch,
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
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO processed_matches (
                    match_id, routing_region, patch, queue_id, source_snapshot,
                    processing_timestamp, status, failure_code, failure_reason
                ) VALUES (?, ?, NULL, ?, ?, ?, 'rejected', ?, ?)
                ON CONFLICT(match_id) DO UPDATE SET
                    routing_region = excluded.routing_region,
                    patch = NULL,
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
                    queue_id,
                    source_snapshot,
                    _utc_now(),
                    failure_code,
                    failure_reason,
                ),
            )

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

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_matches (
                    match_id TEXT PRIMARY KEY,
                    routing_region TEXT NOT NULL,
                    patch TEXT,
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
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_processed_matches_status
                ON processed_matches(status)
                """
            )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
