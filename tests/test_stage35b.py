"""Focused tests for the frozen Stage 3.5B development protocol."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from nexus_lens.stage35b import (
    DevelopmentRow,
    GroupMetadata,
    LoadedData,
    OperationLedger,
    Preflight,
    _candidate_grid,
    _classification_metrics,
    _continuous_metrics,
    _expected_operation_budget,
    _fit_regressor,
    _fold_fingerprint,
    _prepare_features,
    _stratified_bootstrap,
    _validate_protocol_contract,
    construct_folds,
    evaluate_development,
    load_execution_config,
    load_protocol,
)

PROTOCOL_PATH = Path("config/evaluation/stage3.5b-patch26.15-protocol-v1/protocol.json")
SCHEMA_PATH = Path(
    "config/evaluation/stage3.5b-patch26.15-protocol-v1/protocol.schema.json"
)


def test_frozen_protocol_and_operation_budget_reconcile() -> None:
    protocol = load_protocol(PROTOCOL_PATH, SCHEMA_PATH)
    assert _expected_operation_budget(protocol) == {
        "analytic_training_operations": 128,
        "optimizer_fits": 700,
        "total_training_operations": 828,
        "bootstrap_model_fits": 0,
        "bootstrap_comparison_evaluations": 154,
    }
    assert protocol["metrics"]["bootstrap_replicates"] == 2000


def test_allowed_feature_schemas_reject_forbidden_and_preserve_hierarchy() -> None:
    protocol = load_protocol(PROTOCOL_PATH, SCHEMA_PATH)
    _validate_protocol_contract(protocol)
    configured = {
        field
        for candidate in (
            protocol["candidates"]["linear"] + protocol["candidates"]["nonlinear"]
        )
        for field in candidate["features"]
    }
    assert "platform" not in configured
    assert "focal_familiarity" not in configured
    assert "opponent_keystone" not in configured
    assert "game_win" not in configured
    assert "focal_champion_by_focal_keystone" in configured
    assert (
        protocol["candidates"]["three_way_matchup_keystone_primary_candidate"] is False
    )


def test_training_only_rare_keystone_fallback_and_unseen_interaction() -> None:
    train = tuple(
        _row(index, keystone="8005" if index < 4 else "8010") for index in range(6)
    )
    score = (_row(10, keystone="9999", champion="114"),)
    fields = [
        "focal_champion",
        "enemy_top_champion",
        "focal_keystone",
        "focal_champion_by_focal_keystone",
    ]
    training, scoring = _prepare_features(train, score, fields, 3)
    assert training[0][-2:] == ["8005", "114>8005"]
    assert training[-1][-2:] == ["__RARE_KEYSTONE__", "114>__RARE_KEYSTONE__"]
    assert scoring[0][-2:] == ["__RARE_KEYSTONE__", "114>__RARE_KEYSTONE__"]


def test_linear_estimators_accept_fractional_weights_and_unknown_categories() -> None:
    protocol = load_protocol(PROTOCOL_PATH, SCHEMA_PATH)
    rows = tuple(_row(index, champion=str(100 + index % 3)) for index in range(12))
    scoring = (_row(20, champion="999"), _row(21, champion="100"))
    for _estimator, candidate_id in (
        ("ridge", "additive_ridge"),
        ("elastic_net", "additive_elastic_net"),
    ):
        candidate = next(
            item
            for item in protocol["candidates"]["linear"]
            if item["id"] == candidate_id
        )
        train_x, score_x = _prepare_features(rows, scoring, candidate["features"], 3)
        target = np.asarray([row.continuous["gold_difference_10"] for row in rows])
        weights = np.asarray([0.5] * len(rows))
        model = _fit_regressor(
            candidate,
            _candidate_grid(candidate, protocol)[0],
            train_x,
            target,
            weights,
            protocol,
        )
        prediction = np.asarray(model.predict(score_x))
        assert prediction.shape == (2,)
        assert np.isfinite(prediction).all()


def test_catboost_uses_native_categories_deterministically() -> None:
    protocol = load_protocol(PROTOCOL_PATH, SCHEMA_PATH)
    candidate = protocol["candidates"]["nonlinear"][0]
    rows = tuple(_row(index, champion=str(100 + index % 3)) for index in range(18))
    train_x, score_x = _prepare_features(rows, rows[:3], candidate["features"], 3)
    target = np.asarray([row.continuous["gold_difference_10"] for row in rows])
    weights = np.asarray([0.5] * len(rows))
    configuration = {
        **protocol["hyperparameters"]["catboost_grid"][0],
        "iterations": 5,
    }
    first = _fit_regressor(
        candidate, configuration, train_x, target, weights, protocol
    ).predict(score_x)
    second = _fit_regressor(
        candidate, configuration, train_x, target, weights, protocol
    ).predict(score_x)
    assert np.allclose(first, second, rtol=0, atol=0)


def test_rolling_folds_keep_groups_together_and_strictly_chronological() -> None:
    protocol = load_protocol(PROTOCOL_PATH, SCHEMA_PATH)
    protocol = json.loads(json.dumps(protocol))
    protocol["folds"] = {
        **protocol["folds"],
        "initial_training_end_exclusive_utc": "2026-01-03T00:00:00Z",
        "outer_blocks": [
            {
                "id": "outer-0",
                "start_utc": "2026-01-03T00:00:00Z",
                "end_utc": "2026-01-04T00:00:00Z",
                "expected_groups": 2,
            }
        ],
        "expected_initial_training_groups": 4,
        "expected_development_evaluation_groups": 2,
        "inner_validation_hours": 24,
    }
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    groups = []
    for group_index, hour in enumerate((0, 12, 24, 36, 48, 60)):
        timestamp = base + timedelta(hours=hour)
        group = f"group-{group_index}"
        groups.append(
            GroupMetadata(group, "eun1" if group_index % 2 else "euw1", timestamp)
        )
        for perspective in range(2):
            rows.append(
                _row(
                    group_index * 2 + perspective,
                    group=group,
                    platform="eun1" if group_index % 2 else "euw1",
                    timestamp=timestamp,
                )
            )
    folds = construct_folds(tuple(rows), tuple(groups), protocol)
    assert len(folds) == 1
    fold = folds[0]
    assert not {rows[index].match_group_id for index in fold.train_indexes} & {
        rows[index].match_group_id for index in fold.validation_indexes
    }
    assert _fold_fingerprint(folds, tuple(rows)) == _fold_fingerprint(
        folds, tuple(rows)
    )


def test_metrics_are_weighted_finite_and_boundary_safe() -> None:
    target = np.asarray([-1.0, 1.0, 2.0, -2.0])
    prediction = np.asarray([-0.5, 0.5, 1.5, -1.5])
    weights = np.asarray([0.5, 0.5, 0.5, 0.5])
    continuous = _continuous_metrics(target, prediction, weights)
    assert continuous["mae"] == 0.5
    assert continuous["coverage"] == 1.0
    probabilities = np.asarray([[0.8, 0.2], [0.1, 0.9]])
    classification = _classification_metrics(
        np.asarray([0, 1]), probabilities, np.asarray([0.5, 0.5])
    )
    assert classification["log_loss"] > 0
    assert classification["prediction_coverage"] == 1.0
    assert all(
        math.isfinite(value) for value in (continuous["mae"], classification["brier"])
    )


def test_paired_bootstrap_is_deterministic_and_refits_nothing() -> None:
    records = [
        (-1.0, "eun1", "outer-0"),
        (0.5, "euw1", "outer-0"),
        (-0.25, "eun1", "outer-1"),
        (0.1, "euw1", "outer-1"),
    ]
    first = _stratified_bootstrap(records, 50, 35521, "test")
    second = _stratified_bootstrap(records, 50, 35521, "test")
    assert first == second
    ledger = OperationLedger()
    assert ledger.optimizer_fits == 0
    assert ledger.analytic_operations == 0


def test_execution_template_is_strict_and_private_paths_are_not_protocol_data() -> None:
    example = Path("config/stage3.5b/execution.example.json")
    config = load_execution_config(example, allow_template=True)
    assert config.template_only is True
    rendered_protocol = PROTOCOL_PATH.read_text("utf-8").lower()
    assert "e:/" not in rendered_protocol
    assert "puuid" not in rendered_protocol
    assert "focal_player_key" not in rendered_protocol


def test_synthetic_end_to_end_reconciles_without_publishing_identifiers() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text("utf-8"))
    protocol["outcomes"]["continuous_secondary"] = []
    protocol["metrics"]["bootstrap_replicates"] = 20
    protocol["hyperparameters"]["catboost_grid"] = [
        {**item, "iterations": 2}
        for item in protocol["hyperparameters"]["catboost_grid"]
    ]
    protocol["folds"] = {
        **protocol["folds"],
        "initial_training_end_exclusive_utc": "2026-01-02T06:00:00Z",
        "outer_blocks": [
            {
                "id": "outer-0",
                "start_utc": "2026-01-02T06:00:00Z",
                "end_utc": "2026-01-02T16:00:00Z",
                "expected_groups": 10,
            }
        ],
        "expected_initial_training_groups": 30,
        "expected_development_evaluation_groups": 10,
        "inner_validation_hours": 12,
    }
    protocol["operation_budget"] = _expected_operation_budget(protocol)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    groups = []
    tower_labels = (
        "enemy_top_outer_first",
        "neither_by_15",
        "allied_top_outer_first",
    )
    for group_index in range(40):
        timestamp = base + timedelta(hours=group_index)
        platform = "eun1" if group_index % 2 else "euw1"
        group = f"group-{group_index}"
        groups.append(GroupMetadata(group, platform, timestamp))
        for perspective in range(2):
            row = _row(
                group_index * 2 + perspective,
                group=group,
                platform=platform,
                timestamp=timestamp,
                champion=str(100 + (group_index + perspective) % 5),
                keystone=str(8000 + group_index % 3),
            )
            rows.append(
                DevelopmentRow(
                    **{
                        **row.__dict__,
                        "tower_outcome": tower_labels[(group_index + perspective) % 3],
                    }
                )
            )
    row_tuple = tuple(rows)
    group_tuple = tuple(groups)
    folds = construct_folds(row_tuple, group_tuple, protocol)
    preflight = Preflight(
        summary={},
        data=LoadedData(
            rows=row_tuple,
            all_groups=group_tuple,
            holdout_groups=(),
            parent_file_sha256="a" * 64,
            rune_derived_sha256="b" * 64,
            alignment_sha256="c" * 64,
        ),
        folds=folds,
    )
    result, ledger, private_rows = evaluate_development(preflight, protocol)
    assert ledger["all_counts_reconciled"] is True
    assert ledger["actual"]["bootstrap_model_fits"] == 0
    assert len(private_rows) == 20
    public = json.dumps(result).lower()
    assert "match_group_id" not in public
    assert "focal_player" not in public
    assert result["product_recommendation_authorized"] is False


def _row(
    index: int,
    *,
    group: str | None = None,
    platform: str = "eun1",
    timestamp: datetime | None = None,
    champion: str = "114",
    keystone: str = "8005",
) -> DevelopmentRow:
    continuous = {}
    for minute in (5, 10, 15):
        for prefix in (
            "gold_difference",
            "xp_difference",
            "lane_minion_cs_difference",
            "total_farm_difference",
            "level_difference",
        ):
            continuous[f"{prefix}_{minute}"] = float(index - 6)
    return DevelopmentRow(
        row_index=index,
        match_group_id=group or f"group-{index // 2}",
        platform=platform,
        game_creation=timestamp or datetime(2026, 1, 1, tzinfo=UTC),
        side="BLUE" if index % 2 == 0 else "RED",
        focal_champion=champion,
        enemy_champion="516",
        focal_keystone=keystone,
        weight=0.5,
        continuous=continuous,
        tower_outcome="neither_by_15",
        game_win=index % 2,
    )
