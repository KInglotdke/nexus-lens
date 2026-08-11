from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = (
    ROOT
    / "config/evaluation/stage3.4a-patch26.15-pooled-dev-protocol-v1/protocol.json"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _protocol() -> dict:
    return json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_protocol_is_prospective_and_preserves_future_temporal_test() -> None:
    protocol = _protocol()

    assert protocol["status"] == "prospectively_frozen_before_pooled_model_fitting"
    assert protocol["base_repository_commit"] == (
        "86956725fe55f45534bfec0543ff33bc4b858793"
    )
    assert protocol["future_final_evaluation"] == {
        "development_model_retraining_or_tuning_allowed": False,
        "evaluation_patch": "26.16",
        "outcomes_may_be_inspected_before_model_lock": False,
        "platform_feature_allowed": False,
        "platforms_collected_and_sealed_separately": True,
        "report_overall_and_by_platform": True,
        "status": "untouched_not_collected",
        "training_patch": "26.15",
    }
    assert protocol["privacy"]["credentials_loaded"] is False


def test_protocol_freezes_counts_models_splits_and_leakage_controls() -> None:
    protocol = _protocol()
    sources = protocol["sealed_sources"]
    counts = protocol["combined_input"]["expected_counts"]

    assert sum(row["accepted_matches"] for row in sources) == 10_000
    assert sum(row["expected_composition_eligible_matches"] for row in sources) == (
        9_414
    )
    assert counts["composition_eligible_matches"] == 9_414
    assert protocol["models"] == {
        "primary_development_model": "composition_plus_lane_matchups",
        "reference_model": "composition_only",
        "specifications": [
            "composition_only",
            "composition_plus_lane_matchups",
        ],
    }
    cross_validation = protocol["cross_validation"]
    assert cross_validation["outer"]["folds"] == 5
    assert cross_validation["inner"]["folds"] == 4
    assert cross_validation["inner"]["l2_candidates"] == [0.1, 1.0]
    assert cross_validation["inner"]["tie_break"] == "largest_l2"
    assert cross_validation["fold_assignment"]["strata"] == [
        "platform",
        "binary_target",
    ]
    assert protocol["features"]["platform_predictive_feature"] is False
    assert protocol["features"]["global_stage3_3b_outcome_aggregates_used"] is False
    assert protocol["features"]["fold_local_vocabulary"] is True


def test_protocol_references_unchanged_scientific_artifacts_and_safe_paths() -> None:
    protocol = _protocol()
    source_tags = protocol["source_tags"]

    assert source_tags["scientific_method_freeze"]["peeled_commit"] == (
        "59d13d5023af2e64c48e74c261d3ee64736ffd52"
    )
    assert source_tags["final_data_seal"]["peeled_commit"] == (
        "86956725fe55f45534bfec0543ff33bc4b858793"
    )
    assert _sha256(
        ROOT / "config/evaluation/stage3.4a-pre-26.16-v1/eune.freeze.json"
    ) == "86d4e01210f601d9fa05774c6709bee55b8a0a9b4bd1950d7a464bc13056bc39"
    assert _sha256(
        ROOT / "config/evaluation/stage3.4a-pre-26.16-v1/euw.freeze.json"
    ) == "f3732b9f1fb43fb0ef126dddc1e30514382fb302e0643a76857a33d1030fb783"
    rendered = PROTOCOL_PATH.read_text(encoding="utf-8")
    assert not re.search(r"(?i)(?:^|[\"\s])[a-z]:[\\/]", rendered)
    assert not re.search(r"(?i)RGAPI-[A-Za-z0-9_-]+", rendered)
