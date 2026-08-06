"""Private cross-location match deduplication for external collection roots."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

DEDUP_SCHEMA_VERSION = "private-match-dedup-v1"


def verified_catalog_match_ids(
    catalog_path: Path,
    *,
    analysis_region: str,
    public_patch: str,
    queue_id: int = 420,
) -> set[str]:
    """Read compatible terminal IDs from an existing catalog without modifying it."""

    connection = _connect_read_only(catalog_path)
    try:
        rows = connection.execute(
            """
            SELECT match_id
            FROM processed_matches
            WHERE status = 'processed'
              AND lower(routing_region) = lower(?)
              AND public_patch = ?
              AND queue_id = ?
              AND patch_resolution_status = 'resolved'
            """,
            (analysis_region, public_patch, queue_id),
        ).fetchall()
    except sqlite3.Error as error:
        raise ValueError("deduplication source catalog is incompatible") from error
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


def create_private_deduplication_index(
    index_path: Path,
    *,
    match_ids: set[str],
    platform: str,
    analysis_region: str,
    public_patch: str,
    queue_id: int,
    source_catalog_sha256: str,
    source_stage3a_metadata_sha256: str,
) -> None:
    """Create an immutable private SQLite index without copying raw payloads."""

    if not match_ids:
        raise ValueError("deduplication index cannot be empty")
    expected = {
        "schema_version": DEDUP_SCHEMA_VERSION,
        "platform": platform.lower(),
        "analysis_region": analysis_region.lower(),
        "public_patch": public_patch,
        "queue_id": str(queue_id),
        "match_count": str(len(match_ids)),
        "match_set_sha256": match_set_sha256(match_ids),
        "source_catalog_sha256": source_catalog_sha256,
        "source_stage3a_metadata_sha256": source_stage3a_metadata_sha256,
    }
    if index_path.exists():
        actual_ids, actual_metadata = _load_index(index_path)
        if actual_ids != match_ids or actual_metadata != expected:
            raise ValueError("existing private deduplication index differs")
        return
    index_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{index_path.name}.", suffix=".tmp", dir=index_path.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary)
        with connection:
            connection.execute(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE match_ids (match_id TEXT PRIMARY KEY NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                sorted(expected.items()),
            )
            connection.executemany(
                "INSERT INTO match_ids (match_id) VALUES (?)",
                ((match_id,) for match_id in sorted(match_ids)),
            )
        connection.close()
        os.replace(temporary, index_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_private_deduplication_index(
    index_path: Path,
    *,
    platform: str,
    analysis_region: str,
    public_patch: str,
    queue_id: int = 420,
) -> set[str]:
    """Load and integrity-check private IDs without returning them in reports."""

    match_ids, metadata = _load_index(index_path)
    expected_scope = {
        "schema_version": DEDUP_SCHEMA_VERSION,
        "platform": platform.lower(),
        "analysis_region": analysis_region.lower(),
        "public_patch": public_patch,
        "queue_id": str(queue_id),
        "match_count": str(len(match_ids)),
        "match_set_sha256": match_set_sha256(match_ids),
    }
    if any(metadata.get(key) != value for key, value in expected_scope.items()):
        raise ValueError("private deduplication index scope or integrity differs")
    return match_ids


def match_set_sha256(match_ids: set[str]) -> str:
    digest = hashlib.sha256()
    for match_id in sorted(match_ids):
        digest.update(match_id.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_index(index_path: Path) -> tuple[set[str], dict[str, str]]:
    connection = _connect_read_only(index_path)
    try:
        metadata = {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT key, value FROM metadata")
        }
        match_ids = {
            str(row[0]) for row in connection.execute("SELECT match_id FROM match_ids")
        }
    except sqlite3.Error as error:
        raise ValueError("private deduplication index is incompatible") from error
    finally:
        connection.close()
    return match_ids, metadata


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("private catalog or deduplication index is missing")
    try:
        return sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
        )
    except sqlite3.Error as error:
        raise ValueError(
            "private catalog or deduplication index is unreadable"
        ) from error
