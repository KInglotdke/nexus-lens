"""Resumable orchestration from immutable raw snapshots to normalized records."""

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nexus_lens.catalog import ProcessingCatalog
from nexus_lens.normalization import NormalizationError, normalize_match
from nexus_lens.schemas import RiotMatch
from nexus_lens.storage import write_normalized_batch


@dataclass
class ProcessingSummary:
    snapshots_examined: int = 0
    matches_discovered: int = 0
    newly_processed_matches: int = 0
    already_processed_matches: int = 0
    rejected_matches: int = 0
    participant_rows_written: int = 0
    team_rows_written: int = 0
    patches_encountered: set[str] = field(default_factory=set)
    failure_reasons: Counter[str] = field(default_factory=Counter)
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshots_examined": self.snapshots_examined,
            "matches_discovered": self.matches_discovered,
            "newly_processed_matches": self.newly_processed_matches,
            "already_processed_matches": self.already_processed_matches,
            "rejected_matches": self.rejected_matches,
            "participant_rows_written": self.participant_rows_written,
            "team_rows_written": self.team_rows_written,
            "patches_encountered": sorted(self.patches_encountered),
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "failure_reasons": dict(sorted(self.failure_reasons.items())),
        }


class SnapshotProcessor:
    """Process raw snapshots without deleting or changing their contents."""

    def __init__(
        self,
        *,
        processed_root: Path,
        catalog: ProcessingCatalog,
    ) -> None:
        self._processed_root = processed_root
        self._catalog = catalog

    def process(self, snapshot_dirs: list[Path]) -> ProcessingSummary:
        summary = ProcessingSummary()
        started = time.perf_counter()
        for snapshot_dir in snapshot_dirs:
            summary.snapshots_examined += 1
            self._process_snapshot(snapshot_dir, summary)
        summary.elapsed_seconds = time.perf_counter() - started
        return summary

    def _process_snapshot(
        self,
        snapshot_dir: Path,
        summary: ProcessingSummary,
    ) -> None:
        try:
            manifest = json.loads(
                (snapshot_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            summary.failure_reasons["snapshot_manifest"] += 1
            return

        routing_region = str(manifest.get("routing_region") or "unknown")
        match_files = manifest.get("match_files") or []
        for index, relative_name in enumerate(match_files):
            summary.matches_discovered += 1
            match_path = snapshot_dir / str(relative_name)
            self._process_match(
                match_path=match_path,
                fallback_match_id=f"invalid:{snapshot_dir.name}:{index}",
                routing_region=routing_region,
                source_snapshot=snapshot_dir.name,
                summary=summary,
            )

    def _process_match(
        self,
        *,
        match_path: Path,
        fallback_match_id: str,
        routing_region: str,
        source_snapshot: str,
        summary: ProcessingSummary,
    ) -> None:
        raw_payload: dict[str, Any]
        try:
            raw_payload = json.loads(match_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._reject(
                match_id=fallback_match_id,
                routing_region=routing_region,
                source_snapshot=source_snapshot,
                queue_id=None,
                code="invalid_json",
                reason="match file is missing or is not valid JSON",
                summary=summary,
            )
            return

        metadata = raw_payload.get("metadata") or {}
        info = raw_payload.get("info") or {}
        match_id = str(metadata.get("matchId") or fallback_match_id)
        queue_value = info.get("queueId")
        queue_id = queue_value if isinstance(queue_value, int) else None
        if self._catalog.is_processed(match_id):
            summary.already_processed_matches += 1
            return

        self._catalog.begin_processing(
            match_id=match_id,
            routing_region=routing_region,
            source_snapshot=source_snapshot,
            queue_id=queue_id,
        )
        try:
            raw_match = RiotMatch.model_validate(raw_payload)
            batch = normalize_match(raw_match)
            write_normalized_batch(self._processed_root, routing_region, batch)
        except ValidationError:
            self._reject(
                match_id=match_id,
                routing_region=routing_region,
                source_snapshot=source_snapshot,
                queue_id=queue_id,
                code="schema_validation",
                reason="payload failed the Match-V5 schema",
                summary=summary,
            )
            return
        except NormalizationError as error:
            self._reject(
                match_id=match_id,
                routing_region=routing_region,
                source_snapshot=source_snapshot,
                queue_id=queue_id,
                code=error.code,
                reason=str(error),
                summary=summary,
            )
            return
        except OSError:
            self._reject(
                match_id=match_id,
                routing_region=routing_region,
                source_snapshot=source_snapshot,
                queue_id=queue_id,
                code="storage_error",
                reason="normalized output could not be written",
                summary=summary,
            )
            return

        self._catalog.record_processed(
            match_id=match_id,
            routing_region=routing_region,
            patch=batch.match.patch,
            queue_id=batch.match.queue_id,
            source_snapshot=source_snapshot,
        )
        summary.newly_processed_matches += 1
        summary.participant_rows_written += len(batch.participants)
        summary.team_rows_written += len(batch.teams)
        summary.patches_encountered.add(batch.match.patch)

    def _reject(
        self,
        *,
        match_id: str,
        routing_region: str,
        source_snapshot: str,
        queue_id: int | None,
        code: str,
        reason: str,
        summary: ProcessingSummary,
    ) -> None:
        self._catalog.record_rejected(
            match_id=match_id,
            routing_region=routing_region,
            source_snapshot=source_snapshot,
            queue_id=queue_id,
            failure_code=code,
            failure_reason=reason,
        )
        summary.rejected_matches += 1
        summary.failure_reasons[code] += 1


def select_snapshot_dirs(
    raw_root: Path,
    *,
    latest: bool = False,
    snapshot: str | None = None,
    process_all: bool = False,
) -> list[Path]:
    """Resolve a CLI snapshot selection without reading snapshot contents."""

    available = sorted(path for path in raw_root.iterdir() if path.is_dir())
    if snapshot is not None:
        if Path(snapshot).name != snapshot:
            raise ValueError("snapshot must be a directory name, not a path")
        selected = raw_root / snapshot
        if not selected.is_dir():
            raise FileNotFoundError(f"snapshot does not exist: {snapshot}")
        return [selected]
    if not available:
        raise FileNotFoundError(f"no snapshots found under {raw_root}")
    if process_all:
        return available
    if latest:
        return [available[-1]]
    raise ValueError("choose latest, a named snapshot, or all snapshots")
