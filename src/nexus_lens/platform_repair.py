"""Audited repair of accepted payloads from the wrong Riot platform."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus_lens.population_state import atomic_write_json, exclusive_population_run
from nexus_lens.schemas import RANKED_SOLO_QUEUE_ID, RiotMatch
from nexus_lens.storage import normalized_match_paths


class PlatformIsolationRepairError(ValueError):
    """A non-sensitive platform repair precondition or integrity failure."""


@dataclass(frozen=True)
class PlatformIsolationRepairReport:
    mode: str
    accepted_before: int
    accepted_after: int
    accepted_platform_conflicts_before: int
    accepted_platform_conflicts_after: int
    rejected_platform_mismatches: int
    raw_evidence_retained: int
    catalog_rows_reclassified: int
    normalized_files_removed: int
    checkpoint_version: int
    stores_reconciled: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _Candidate:
    match_id: str
    raw_path: Path
    normalized_paths: tuple[Path, Path, Path]


def repair_platform_isolation(
    *,
    checkpoint_path: Path,
    manifest_path: Path,
    catalog_path: Path,
    raw_run_dir: Path,
    normalized_root: Path,
    backup_directory: Path | None,
    expected_platform: str,
    expected_mismatches: int,
    validate_only: bool,
) -> PlatformIsolationRepairReport:
    """Validate or repair wrong-platform accepted records without exposing IDs."""

    if expected_mismatches < 1:
        raise PlatformIsolationRepairError("expected mismatch count must be positive")
    lock_path = checkpoint_path.parent / "collector.lock.sqlite3"
    with exclusive_population_run(lock_path):
        return _repair_locked(
            checkpoint_path=checkpoint_path,
            manifest_path=manifest_path,
            catalog_path=catalog_path,
            raw_run_dir=raw_run_dir,
            normalized_root=normalized_root,
            backup_directory=backup_directory,
            expected_platform=expected_platform,
            expected_mismatches=expected_mismatches,
            validate_only=validate_only,
        )


def _repair_locked(
    *,
    checkpoint_path: Path,
    manifest_path: Path,
    catalog_path: Path,
    raw_run_dir: Path,
    normalized_root: Path,
    backup_directory: Path | None,
    expected_platform: str,
    expected_mismatches: int,
    validate_only: bool,
) -> PlatformIsolationRepairReport:
    expected_platform = expected_platform.strip().upper()
    if not expected_platform:
        raise PlatformIsolationRepairError("expected platform is required")
    checkpoint = _load_object(checkpoint_path, "checkpoint")
    manifest = _load_object(manifest_path, "manifest")
    if checkpoint.get("version") != 4:
        raise PlatformIsolationRepairError("checkpoint schema must be version 4")
    if checkpoint.get("active_request_invocation") is not None:
        raise PlatformIsolationRepairError("checkpoint has an active invocation")
    config = checkpoint.get("config")
    if not isinstance(config, dict):
        raise PlatformIsolationRepairError("checkpoint configuration is missing")
    if str(config.get("platform", "")).upper() != expected_platform:
        raise PlatformIsolationRepairError("checkpoint platform does not match")
    if int(config.get("queue_id", -1)) != RANKED_SOLO_QUEUE_ID:
        raise PlatformIsolationRepairError("checkpoint queue is not 420")
    if manifest.get("run_id") != checkpoint.get("run_id"):
        raise PlatformIsolationRepairError("checkpoint and manifest run IDs differ")
    if str(manifest.get("platform", "")).upper() != expected_platform:
        raise PlatformIsolationRepairError("manifest platform does not match")

    matches = checkpoint.get("matches")
    if not isinstance(matches, dict):
        raise PlatformIsolationRepairError("checkpoint match state is malformed")
    accepted_before = sum(_is_accepted(record) for record in matches.values())
    accepted_conflicts: list[_Candidate] = []
    repaired_conflicts: list[_Candidate] = []
    accepted_raw_paths: set[str] = set()
    analysis_region = str(config.get("analysis_region", ""))

    for match_id, record in matches.items():
        if not isinstance(match_id, str) or not isinstance(record, dict):
            raise PlatformIsolationRepairError("checkpoint match state is malformed")
        status = record.get("status")
        if not (_is_accepted(record) or status == "rejected_platform_mismatch"):
            continue
        raw_reference = record.get("raw_path")
        if not isinstance(raw_reference, str):
            raise PlatformIsolationRepairError("audited match lacks raw evidence")
        raw_path = _confined(raw_run_dir, raw_reference)
        try:
            raw_match = RiotMatch.model_validate_json(
                raw_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise PlatformIsolationRepairError(
                "audited raw payload is missing or invalid"
            ) from error
        if raw_match.metadata.matchId != match_id:
            raise PlatformIsolationRepairError(
                "raw payload identity conflicts with state"
            )
        if raw_match.info.queueId != RANKED_SOLO_QUEUE_ID:
            raise PlatformIsolationRepairError("accepted payload is not queue 420")
        if len(raw_match.info.participants) != 10:
            raise PlatformIsolationRepairError(
                "accepted payload does not have ten participants"
            )
        if record.get("public_patch") not in config.get(
            "accepted_public_patches", []
        ):
            raise PlatformIsolationRepairError(
                "accepted payload is outside the configured patch window"
            )
        platform = str(raw_match.info.platformId or "").upper()
        if _is_accepted(record):
            accepted_raw_paths.add(raw_reference)
            if platform != expected_platform:
                accepted_conflicts.append(
                    _candidate(
                        match_id,
                        raw_path,
                        normalized_root,
                        analysis_region,
                        str(record["public_patch"]),
                        raw_match.info.queueId,
                    )
                )
        elif platform != expected_platform:
            repaired_conflicts.append(
                _candidate(
                    match_id,
                    raw_path,
                    normalized_root,
                    analysis_region,
                    str(record["public_patch"]),
                    raw_match.info.queueId,
                )
            )
        else:
            raise PlatformIsolationRepairError(
                "platform-mismatch rejection has expected-platform payload"
            )

    all_conflicts = accepted_conflicts + repaired_conflicts
    if len(all_conflicts) != expected_mismatches:
        raise PlatformIsolationRepairError(
            "platform mismatch count differs from the explicit expectation"
        )
    _validate_manifest(manifest, accepted_raw_paths, accepted_before, checkpoint)
    _validate_catalog(
        catalog_path,
        accepted_conflicts=accepted_conflicts,
        repaired_conflicts=repaired_conflicts,
    )
    for candidate in accepted_conflicts:
        if not all(path.is_file() for path in candidate.normalized_paths):
            raise PlatformIsolationRepairError(
                "accepted mismatch lacks its complete normalized contribution"
            )
    for candidate in repaired_conflicts:
        if any(path.exists() for path in candidate.normalized_paths):
            raise PlatformIsolationRepairError(
                "rejected mismatch still has a normalized contribution"
            )

    if validate_only or not accepted_conflicts:
        return PlatformIsolationRepairReport(
            mode="validation-only" if validate_only else "already-repaired",
            accepted_before=accepted_before,
            accepted_after=accepted_before,
            accepted_platform_conflicts_before=len(accepted_conflicts),
            accepted_platform_conflicts_after=len(accepted_conflicts),
            rejected_platform_mismatches=len(repaired_conflicts),
            raw_evidence_retained=len(all_conflicts),
            catalog_rows_reclassified=0,
            normalized_files_removed=0,
            checkpoint_version=4,
            stores_reconciled=True,
        )
    if backup_directory is None:
        raise PlatformIsolationRepairError("a recovery backup directory is required")
    if backup_directory.exists():
        raise PlatformIsolationRepairError("recovery backup directory already exists")

    repaired_checkpoint = deepcopy(checkpoint)
    repaired_manifest = deepcopy(manifest)
    repaired_at = datetime.now(UTC).isoformat()
    for candidate in accepted_conflicts:
        repaired_checkpoint["matches"][candidate.match_id]["status"] = (
            "rejected_platform_mismatch"
        )
    accepted_after = accepted_before - len(accepted_conflicts)
    _reconcile_checkpoint(repaired_checkpoint, repaired_at, len(accepted_conflicts))
    _reconcile_manifest(repaired_manifest, repaired_checkpoint, repaired_at)

    _make_backup(
        backup_directory,
        checkpoint_path,
        manifest_path,
        catalog_path,
        normalized_root,
        accepted_conflicts,
    )
    removed: list[tuple[Path, Path]] = []
    try:
        for candidate in accepted_conflicts:
            for path in candidate.normalized_paths:
                relative = path.resolve().relative_to(normalized_root.resolve())
                backup_path = backup_directory / "normalized" / relative
                path.unlink()
                removed.append((path, backup_path))
        catalog_rows = _reclassify_catalog(catalog_path, accepted_conflicts)
        atomic_write_json(checkpoint_path, repaired_checkpoint)
        atomic_write_json(manifest_path, repaired_manifest)
    except Exception:
        _restore_backup(
            backup_directory,
            checkpoint_path,
            manifest_path,
            catalog_path,
            removed,
        )
        raise

    return PlatformIsolationRepairReport(
        mode="repaired",
        accepted_before=accepted_before,
        accepted_after=accepted_after,
        accepted_platform_conflicts_before=len(accepted_conflicts),
        accepted_platform_conflicts_after=0,
        rejected_platform_mismatches=len(all_conflicts),
        raw_evidence_retained=len(all_conflicts),
        catalog_rows_reclassified=catalog_rows,
        normalized_files_removed=len(removed),
        checkpoint_version=4,
        stores_reconciled=True,
    )


def _candidate(
    match_id: str,
    raw_path: Path,
    normalized_root: Path,
    analysis_region: str,
    public_patch: str,
    queue_id: int,
) -> _Candidate:
    return _Candidate(
        match_id=match_id,
        raw_path=raw_path,
        normalized_paths=normalized_match_paths(
            normalized_root,
            analysis_region,
            public_patch,
            queue_id,
            match_id,
        ),
    )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PlatformIsolationRepairError(f"{label} is missing or invalid") from error
    if not isinstance(value, dict):
        raise PlatformIsolationRepairError(f"{label} is not an object")
    return value


def _confined(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise PlatformIsolationRepairError("raw evidence path must be relative")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise PlatformIsolationRepairError(
            "raw evidence path escapes run root"
        ) from error
    return candidate


def _is_accepted(record: object) -> bool:
    return isinstance(record, dict) and record.get("status") in {
        "accepted",
        "already_cataloged_target",
        "already_cataloged_accepted",
    }


def _accepted_patch_counts(checkpoint: dict[str, Any]) -> dict[str, int]:
    counts = Counter(
        str(record["public_patch"])
        for record in checkpoint["matches"].values()
        if _is_accepted(record) and record.get("public_patch")
    )
    return dict(sorted(counts.items()))


def _validate_manifest(
    manifest: dict[str, Any],
    accepted_raw_paths: set[str],
    accepted_count: int,
    checkpoint: dict[str, Any],
) -> None:
    files = manifest.get("match_files")
    if not isinstance(files, list) or len(files) != len(set(files)):
        raise PlatformIsolationRepairError("manifest match file list is malformed")
    if set(files) != accepted_raw_paths:
        raise PlatformIsolationRepairError("manifest accepted files do not reconcile")
    counts = _accepted_patch_counts(checkpoint)
    if manifest.get("accepted_matches_by_public_patch") != counts:
        raise PlatformIsolationRepairError("manifest patch counts do not reconcile")
    summary = manifest.get("summary")
    if (
        not isinstance(summary, dict)
        or summary.get("accepted_matches") != accepted_count
    ):
        raise PlatformIsolationRepairError("manifest accepted total does not reconcile")


def _validate_catalog(
    catalog_path: Path,
    *,
    accepted_conflicts: list[_Candidate],
    repaired_conflicts: list[_Candidate],
) -> None:
    connection = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)
    try:
        for candidate in accepted_conflicts:
            row = connection.execute(
                "SELECT status FROM processed_matches WHERE match_id = ?",
                (candidate.match_id,),
            ).fetchone()
            if row is None or row[0] != "processed":
                raise PlatformIsolationRepairError(
                    "accepted mismatch does not reconcile with catalog"
                )
        for candidate in repaired_conflicts:
            row = connection.execute(
                "SELECT status, failure_code FROM processed_matches "
                "WHERE match_id = ?",
                (candidate.match_id,),
            ).fetchone()
            if row is None or tuple(row) != ("rejected", "platform_mismatch"):
                raise PlatformIsolationRepairError(
                    "repaired mismatch does not reconcile with catalog"
                )
    finally:
        connection.close()


def _reconcile_checkpoint(
    checkpoint: dict[str, Any], repaired_at: str, repair_count: int
) -> None:
    counts = _accepted_patch_counts(checkpoint)
    checkpoint["accepted_match_counts_by_public_patch"] = counts
    history = checkpoint.setdefault("platform_isolation_repairs", [])
    if not isinstance(history, list):
        raise PlatformIsolationRepairError("repair history is malformed")
    history.append(
        {
            "schema_version": "platform-isolation-repair-v1",
            "repaired_at": repaired_at,
            "reason": "platform_mismatch",
            "reclassified_matches": repair_count,
            "raw_evidence_retained": True,
        }
    )


def _reconcile_manifest(
    manifest: dict[str, Any], checkpoint: dict[str, Any], repaired_at: str
) -> None:
    accepted_records = [
        record for record in checkpoint["matches"].values() if _is_accepted(record)
    ]
    accepted_files = [
        record["raw_path"] for record in accepted_records if "raw_path" in record
    ]
    counts = _accepted_patch_counts(checkpoint)
    target_patch = str(checkpoint["config"]["target_public_patch"])
    target_count = counts.get(target_patch, 0)
    previous_count = sum(
        count for patch, count in counts.items() if patch != target_patch
    )
    contributing: Counter[str] = Counter()
    unattributed = 0
    for record in accepted_records:
        sources = record.get("sources", [])
        if sources:
            source = sources[0]
            contributing[f"{source['tier']} {source['division']}"] += 1
        else:
            unattributed += 1
    statuses = Counter(
        record.get("status") for record in checkpoint["matches"].values()
    )
    rejected = sum(
        count
        for status, count in statuses.items()
        if str(status).startswith("rejected_")
        or status
        in {
            "unresolved_patch",
            "request_failed",
            "cached_rejected",
            "newer_patch_transition",
            "newer_patch_transition_cached",
        }
    )
    summary = manifest["summary"]
    total = len(accepted_records)
    players = len(checkpoint["players"])
    summary.update(
        {
            "accepted_matches": total,
            "total_accepted_matches_credited": total,
            "accepted_target_patch_matches": target_count,
            "total_target_patch_matches_credited": target_count,
            "accepted_previous_patch_matches": previous_count,
            "accepted_matches_by_public_patch": counts,
            "target_matches_by_contributing_stratum": dict(
                sorted(contributing.items())
            ),
            "accepted_matches_by_contributing_stratum": dict(
                sorted(contributing.items())
            ),
            "unattributed_target_matches": unattributed,
            "unattributed_accepted_matches": unattributed,
            "accepted_matches_per_player_examined": (
                total / players if players else None
            ),
            "rejected_matches": rejected,
            "target_reached": False,
            "completion_status": "platform_isolation_repair_pending",
            "accepted_matches_credited_this_run": 0,
            "target_patch_matches_credited_this_run": 0,
            "newly_downloaded_accepted_matches": 0,
            "accepted_matches_reused_this_run": 0,
            "accepted_matches_reused_from_catalog_this_run": 0,
            "accepted_matches_reused_from_raw_cache_this_run": 0,
            "accepted_matches_reused_from_checkpoint_state_this_run": 0,
            "payloads_downloaded": 0,
            "newly_downloaded_wrong_patch_matches": 0,
            "known_terminal_matches_reused_without_download": 0,
            "known_wrong_patch_matches_reused_without_download": 0,
            "examined_terminal_matches_this_run": 0,
            "downloaded_payloads_with_other_outcome": 0,
            "new_download_acceptance_rate": None,
            "new_download_wrong_patch_rate": None,
            "overall_examined_match_acceptance_rate": None,
            "new_payloads_per_newly_downloaded_accepted_match": None,
            "new_payloads_per_accepted_match_credited_this_run": None,
            "elapsed_seconds": 0.0,
        }
    )
    manifest["match_files"] = accepted_files
    manifest["accepted_matches_by_public_patch"] = counts
    manifest["created_at"] = repaired_at
    manifest["repair"] = {
        "schema_version": "platform-isolation-repair-v1",
        "reason": "platform_mismatch",
        "reclassified_matches": statuses["rejected_platform_mismatch"],
        "raw_evidence_retained": True,
    }


def _make_backup(
    backup_directory: Path,
    checkpoint_path: Path,
    manifest_path: Path,
    catalog_path: Path,
    normalized_root: Path,
    candidates: list[_Candidate],
) -> None:
    backup_directory.mkdir(parents=True, mode=0o700)
    shutil.copy2(checkpoint_path, backup_directory / "checkpoint.json")
    shutil.copy2(manifest_path, backup_directory / "manifest.json")
    source = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)
    destination = sqlite3.connect(backup_directory / "catalog.sqlite3")
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    for candidate in candidates:
        for path in candidate.normalized_paths:
            relative = path.resolve().relative_to(normalized_root.resolve())
            destination_path = backup_directory / "normalized" / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination_path)
    for path in backup_directory.rglob("*"):
        if path.is_file():
            path.chmod(0o600)


def _reclassify_catalog(
    catalog_path: Path, candidates: list[_Candidate]
) -> int:
    connection = sqlite3.connect(catalog_path)
    try:
        with connection:
            affected = 0
            for candidate in candidates:
                cursor = connection.execute(
                    """
                    UPDATE processed_matches
                    SET status = 'rejected', failure_code = 'platform_mismatch',
                        failure_reason =
                            'payload platform differs from collection platform',
                        processing_timestamp = ?
                    WHERE match_id = ? AND status = 'processed'
                    """,
                    (datetime.now(UTC).isoformat(), candidate.match_id),
                )
                affected += cursor.rowcount
            if affected != len(candidates):
                raise PlatformIsolationRepairError(
                    "catalog reclassification count does not reconcile"
                )
        return affected
    finally:
        connection.close()


def _restore_backup(
    backup_directory: Path,
    checkpoint_path: Path,
    manifest_path: Path,
    catalog_path: Path,
    removed: list[tuple[Path, Path]],
) -> None:
    shutil.copy2(backup_directory / "checkpoint.json", checkpoint_path)
    shutil.copy2(backup_directory / "manifest.json", manifest_path)
    source = sqlite3.connect(backup_directory / "catalog.sqlite3")
    destination = sqlite3.connect(catalog_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    for original, backup in removed:
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, original)
