import json
from pathlib import Path

import pytest

from nexus_lens.catalog import ProcessingCatalog
from nexus_lens.normalization import normalize_match
from nexus_lens.platform_repair import (
    PlatformIsolationRepairError,
    repair_platform_isolation,
)
from nexus_lens.schemas import RiotMatch
from nexus_lens.storage import normalized_match_paths, write_normalized_batch
from tests.factories import make_match_payload


def test_platform_isolation_repair_reconciles_all_stores(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)

    report = repair_platform_isolation(
        **inputs,
        expected_platform="EUN1",
        expected_mismatches=2,
        validate_only=False,
    )

    assert report.accepted_before == 3
    assert report.accepted_after == 1
    assert report.catalog_rows_reclassified == 2
    assert report.normalized_files_removed == 6
    assert report.raw_evidence_retained == 2
    checkpoint = json.loads(inputs["checkpoint_path"].read_text(encoding="utf-8"))
    manifest = json.loads(inputs["manifest_path"].read_text(encoding="utf-8"))
    assert _status_counts(checkpoint) == {
        "accepted": 1,
        "rejected_platform_mismatch": 2,
    }
    assert checkpoint["accepted_match_counts_by_public_patch"] == {"26.15": 1}
    assert manifest["summary"]["accepted_matches"] == 1
    assert len(manifest["match_files"]) == 1
    assert all(path.is_file() for path in inputs["raw_run_dir"].glob("*.json"))
    assert sum(path.is_file() for path in inputs["backup_directory"].rglob("*")) == 9

    validation = repair_platform_isolation(
        **{**inputs, "backup_directory": None},
        expected_platform="eun1",
        expected_mismatches=2,
        validate_only=True,
    )
    assert validation.accepted_platform_conflicts_before == 0
    assert validation.rejected_platform_mismatches == 2
    assert validation.stores_reconciled is True


def test_validation_only_writes_nothing(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    before = {
        path: path.read_bytes()
        for path in (
            inputs["checkpoint_path"],
            inputs["manifest_path"],
            inputs["catalog_path"],
        )
    }

    report = repair_platform_isolation(
        **{**inputs, "backup_directory": None},
        expected_platform="EUN1",
        expected_mismatches=2,
        validate_only=True,
    )

    assert report.mode == "validation-only"
    assert report.accepted_platform_conflicts_before == 2
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not inputs["backup_directory"].exists()


def test_repair_refuses_unexpected_mismatch_count(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)

    with pytest.raises(
        PlatformIsolationRepairError,
        match="differs from the explicit expectation",
    ):
        repair_platform_isolation(
            **inputs,
            expected_platform="EUN1",
            expected_mismatches=1,
            validate_only=False,
        )

    assert not inputs["backup_directory"].exists()


def _make_inputs(tmp_path: Path) -> dict[str, Path | None]:
    raw_run = tmp_path / "raw" / "run"
    normalized = tmp_path / "processed"
    checkpoint_path = tmp_path / "state" / "run" / "checkpoint.json"
    manifest_path = raw_run / "manifest.json"
    catalog_path = normalized / "catalog.sqlite3"
    raw_run.mkdir(parents=True)
    checkpoint_path.parent.mkdir(parents=True)
    records: dict[str, dict[str, object]] = {}
    raw_references: list[str] = []
    for index, platform in enumerate(("EUN1", "EUW1", "EUW1"), start=1):
        match_id = f"SYNTHETIC_{index}"
        payload = make_match_payload(
            match_id=match_id,
            platform_id=platform,
            game_version="15.15.1",
        )
        raw_reference = f"payload-{index}.json"
        (raw_run / raw_reference).write_text(json.dumps(payload), encoding="utf-8")
        raw_references.append(raw_reference)
        records[match_id] = {
            "status": "accepted",
            "raw_path": raw_reference,
            "public_patch": "26.15",
            "api_patch": "15.15",
            "patch_resolution_status": "resolved",
            "sources": [{"tier": "GOLD", "division": "I"}],
        }
        batch = normalize_match(RiotMatch.model_validate(payload))
        batch.match.public_patch = "26.15"
        write_normalized_batch(normalized, "eune", batch)
    checkpoint = {
        "version": 4,
        "run_id": "SYNTHETIC_RUN",
        "config": {
            "platform": "eun1",
            "analysis_region": "eune",
            "queue_id": 420,
            "target_public_patch": "26.15",
            "accepted_public_patches": ["26.15"],
            "target_matches": 3,
        },
        "players": {"opaque": {}},
        "matches": records,
        "accepted_match_counts_by_public_patch": {"26.15": 3},
    }
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    manifest = {
        "run_id": "SYNTHETIC_RUN",
        "platform": "eun1",
        "match_files": raw_references,
        "accepted_matches_by_public_patch": {"26.15": 3},
        "summary": {"accepted_matches": 3, "rejected_matches": 0},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with ProcessingCatalog(catalog_path) as catalog:
        for index in range(1, 4):
            catalog.record_processed(
                match_id=f"SYNTHETIC_{index}",
                routing_region="eune",
                api_game_version="15.15.1",
                api_patch="15.15",
                public_patch="26.15",
                patch_resolution_method="synthetic",
                patch_resolution_status="resolved",
                queue_id=420,
                source_snapshot="SYNTHETIC_RUN",
            )
    return {
        "checkpoint_path": checkpoint_path,
        "manifest_path": manifest_path,
        "catalog_path": catalog_path,
        "raw_run_dir": raw_run,
        "normalized_root": normalized,
        "backup_directory": tmp_path / "backup",
    }


def _status_counts(checkpoint: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    matches = checkpoint["matches"]
    assert isinstance(matches, dict)
    for record in matches.values():
        assert isinstance(record, dict)
        status = str(record["status"])
        counts[status] = counts.get(status, 0) + 1
    return counts


def test_normalized_match_paths_matches_writer(tmp_path: Path) -> None:
    payload = make_match_payload(match_id="SYNTHETIC:PATH", platform_id="EUN1")
    batch = normalize_match(RiotMatch.model_validate(payload))
    write_normalized_batch(tmp_path, "eune", batch)

    paths = normalized_match_paths(
        tmp_path,
        "eune",
        str(batch.match.public_patch),
        420,
        "SYNTHETIC:PATH",
    )

    assert all(path.is_file() for path in paths)
