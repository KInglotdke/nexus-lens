from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

import nexus_lens.evaluation_freeze as freeze
from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.evaluation_freeze import (
    FreezeBundle,
    PlatformFreezeSpec,
    analyze_platform,
    build_freeze_bundle,
    validate_freeze_bundle,
    write_freeze_bundle,
)
from tests.test_composition_modeling import _stage33a


def test_paired_power_is_deterministic_and_inflated_separately() -> None:
    differences = np.asarray([-0.03, -0.01, 0.0, 0.01, 0.02])
    first = freeze._paired_analysis(
        differences, eligibility_rate=0.8, replicates=200, seed=123
    )
    second = freeze._paired_analysis(
        differences, eligibility_rate=0.8, replicates=200, seed=123
    )

    assert first == second
    assert first["sample_variance"] == pytest.approx(0.00037)
    for row in first["power_requirements"]:
        assert row["eligibility_inflated_accepted_matches"] >= row[
            "eligible_evaluation_matches"
        ]
        assert row["accepted_matches_with_10_percent_operational_reserve"] >= row[
            "eligibility_inflated_accepted_matches"
        ]


def test_platform_analysis_is_match_level_separate_and_side_metadata_only(
    tmp_path: Path,
) -> None:
    source = _stage33a(tmp_path)
    result = analyze_platform(
        stage33a=source,
        spec=PlatformFreezeSpec(
            analysis_region="EUNE",
            platform="eun1",
            input_directory=tmp_path / "input",
            development_output_directory=tmp_path / "development",
            composition_only_l2=0.1,
            composition_plus_matchups_l2=0.1,
        ),
        power_replicates=100,
    )

    assert result["queue_id"] == 420
    assert result["development_fold"]["one_observation_per_eligible_match"]
    assert result["development_fold"]["match_sets_disjoint"]
    assert result["team_side"]["representation"] == (
        "metadata_only_no_explicit_model_feature"
    )
    assert not result["team_side"]["can_represent_training_only_side_advantage"]
    for comparison in result["paired_log_loss"].values():
        assert comparison["paired_match_count"] == 3


def test_freeze_bundle_schema_hashes_and_immutable_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eune = _stage33a(tmp_path / "eune")
    euw_source = _stage33a(tmp_path / "euw")
    euw = replace(
        euw_source,
        matches=[
            row.model_copy(update={"platform": "euw1"})
            for row in euw_source.matches
        ],
        participants=[
            row.model_copy(update={"platform": "euw1"})
            for row in euw_source.participants
        ],
    )
    monkeypatch.setattr(
        freeze,
        "load_stage3_3a_input",
        lambda path, **_kwargs: euw if "euw" in path.as_posix() else eune,
    )
    monkeypatch.setattr(
        freeze,
        "_verify_development_publication",
        lambda _path, expected_l2: {
            "stage3_4a_metadata_sha256": "a" * 64,
            "stage3_4a_output_sha256": {"metrics.json": "b" * 64},
            "stage3_4a_publication_tree_sha256": "c" * 64,
            "stage3_4a_code_sha256": "d" * 64,
        },
    )
    lock = tmp_path / "dependency-lock.txt"
    lock.write_text("python==3.12.10\n", encoding="utf-8")
    output = tmp_path / "freeze"
    specs = (
        PlatformFreezeSpec(
            "EUNE", "eun1", tmp_path / "eune", tmp_path / "eune-dev", 0.1, 0.1
        ),
        PlatformFreezeSpec(
            "EUW", "euw1", tmp_path / "euw", tmp_path / "euw-dev", 1.0, 1.0
        ),
    )
    bundle = build_freeze_bundle(
        specs=specs,
        output_directory=output,
        dependency_lock_path=lock,
        expected_match_count=11,
        power_replicates=100,
    )

    validate_freeze_bundle(bundle, lock)
    assert not output.exists()
    write_freeze_bundle(bundle)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    write_freeze_bundle(bundle)
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}
    assert json.loads((output / "eune.freeze.json").read_text())["status"] == (
        "frozen_before_26.16_evaluation"
    )

    changed = FreezeBundle(
        analysis={**bundle.analysis, "status": "changed"},
        manifests=bundle.manifests,
        output_directory=output,
    )
    with pytest.raises(Stage3ValidationError) as caught:
        write_freeze_bundle(changed)
    assert caught.value.category == "immutable_freeze_conflict"
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}


def test_manifest_validation_rejects_changed_analysis_hash(tmp_path: Path) -> None:
    lock = tmp_path / "dependency-lock.txt"
    lock.write_text("python==3.12.10\n", encoding="utf-8")
    bundle = FreezeBundle(
        analysis={"schema_version": "synthetic"},
        manifests={
            "EUNE": {
                "schema_version": freeze.FREEZE_SCHEMA_VERSION,
                "status": "frozen_before_26.16_evaluation",
                "future_evaluation_outcomes_observed": False,
                "queue_id": 420,
                "sample_size_analysis": {"sha256": "0" * 64},
                "software": {
                    "dependency_lock_sha256": freeze._sha256_file(lock)
                },
            }
        },
        output_directory=tmp_path / "out",
    )

    with pytest.raises(Stage3ValidationError) as caught:
        validate_freeze_bundle(bundle, lock)
    assert caught.value.category == "sample_size_hash_conflict"
