"""Atomic local checkpoint state for controlled population collection."""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

_ATOMIC_REPLACE_ATTEMPTS = 5
_ATOMIC_REPLACE_INITIAL_DELAY_SECONDS = 0.05


class PopulationState:
    """Sensitive local state; callers must keep its directory Git-ignored."""

    def __init__(self, path: Path, payload: dict[str, Any]) -> None:
        self.path = path
        self.payload = payload

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        run_id: str,
        config: dict[str, Any],
    ) -> "PopulationState":
        state = cls(
            path,
            {
                "version": 4,
                "run_id": run_id,
                "config": config,
                "players": {},
                "matches": {},
                "overlap_events": 0,
                "request_metrics": {},
            },
        )
        state.save()
        return state

    @classmethod
    def load(cls, path: Path) -> "PopulationState":
        return cls(path, json.loads(path.read_text(encoding="utf-8")))

    @property
    def players(self) -> dict[str, dict[str, Any]]:
        return self.payload["players"]

    @property
    def matches(self) -> dict[str, dict[str, Any]]:
        return self.payload["matches"]

    def save(self) -> None:
        _atomic_write(
            self.path,
            json.dumps(self.payload, indent=2, sort_keys=True) + "\n",
        )


def atomic_write_json(path: Path, payload: object) -> None:
    _atomic_write(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )


def _atomic_write(path: Path, content: str) -> None:
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
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_transient_retry(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _replace_with_transient_retry(source: Path, destination: Path) -> None:
    """Retry only transient Windows-style access failures during atomic replace."""

    delay = _ATOMIC_REPLACE_INITIAL_DELAY_SECONDS
    for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == _ATOMIC_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay *= 2
