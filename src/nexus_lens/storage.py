"""Deterministic, atomic local storage for normalized Stage 1 records."""

import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from nexus_lens.schemas import NormalizedBatch

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_.-]")


def write_normalized_batch(
    processed_root: Path,
    routing_region: str,
    batch: NormalizedBatch,
) -> Path:
    """Atomically replace the three files belonging to one normalized match."""

    partition = (
        processed_root
        / f"region={_safe(routing_region)}"
        / f"patch={_safe(batch.match.patch)}"
        / f"queue={batch.match.queue_id}"
    )
    filename = f"{_safe(batch.match.match_id)}"
    _atomic_write_lines(
        partition / "matches" / f"{filename}.json",
        [_serialized(batch.match.model_dump(mode="json"))],
    )
    _atomic_write_lines(
        partition / "participants" / f"{filename}.jsonl",
        [
            _serialized(participant.model_dump(mode="json"))
            for participant in batch.participants
        ],
    )
    _atomic_write_lines(
        partition / "teams" / f"{filename}.jsonl",
        [_serialized(team.model_dump(mode="json")) for team in batch.teams],
    )
    return partition


def iter_json_records(root: Path, record_type: str) -> Iterator[dict[str, Any]]:
    """Yield normalized records in stable path and line order."""

    pattern = f"region=*/patch=*/queue=*/{record_type}/*"
    for path in sorted(root.glob(pattern)):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def _atomic_write_lines(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _serialized(record: dict[str, Any]) -> str:
    return json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _safe(value: str) -> str:
    return _SAFE_SEGMENT.sub("_", value)
