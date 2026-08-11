from __future__ import annotations

import copy
import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import nexus_lens.stage34b as stage34b
from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.composition_modeling import (
    POSITIONS,
    ChampionRoleAssignment,
    MatchDraftObservation,
    build_feature_vocabulary,
    swap_teams,
)
from nexus_lens.stage34b import (
    ALL_POLICIES,
    ChronologicalFold,
    SharedInteractionModel,
    SharedModelConfig,
    TimedDraft,
    construct_inner_folds,
    construct_outer_folds,
    evaluate_material_usefulness_gate,
    expected_fit_accounting,
    fit_shared_interaction_model,
    load_stage34b_protocol,
    paired_bootstrap_intervals,
    predict_shared_probability,
    select_shared_config,
    validate_stage34b_artifact,
)

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_DIRECTORY = (
    ROOT / "config/evaluation/stage3.4b-1-patch26.15-protocol-v1"
)
PROTOCOL_PATH = PROTOCOL_DIRECTORY / "protocol.json"
SCHEMA_PATH = PROTOCOL_DIRECTORY / "protocol.schema.json"


def test_frozen_protocol_schema_language_and_fit_accounting() -> None:
    protocol = load_stage34b_protocol(PROTOCOL_PATH, schema_path=SCHEMA_PATH)

    assert "26.16" not in json.dumps(protocol, sort_keys=True)
    assert "future sealed temporal holdout" in protocol["future_holdout_policy"]
    assert expected_fit_accounting(protocol) == protocol["execution_budget"]
    assert protocol["execution_budget"] == {
        "analytic_baseline_training_operations": 8,
        "bootstrap_model_fits": 0,
        "calibration_metric_evaluations": 63,
        "calibration_metric_optimizer_fits_upper_bound": 63,
        "candidate_final_all_development_fits": 4,
        "candidate_inner_selection_fits": 135,
        "candidate_outer_fits": 16,
        "expected_predictive_training_operations": 171,
        "predictive_optimizer_fits": 163,
        "stage3_4a_baseline_optimizer_fits": 8,
        "total_optimizer_invocations_upper_bound_including_metric_regressions": 226,
    }

    changed = copy.deepcopy(protocol)
    changed["hyperparameter_policy"]["maximum_iterations"] += 1
    with pytest.raises(Stage3ValidationError, match="protocol hash differs"):
        stage34b.validate_stage34b_protocol(changed)


def test_chronological_folds_are_deterministic_strict_and_nonoverlapping() -> None:
    protocol = _test_protocol()
    rows = _timed_rows()

    first = construct_outer_folds(rows, protocol, enforce_frozen_counts=False)
    second = construct_outer_folds(
        tuple(reversed(rows)), protocol, enforce_frozen_counts=False
    )

    assert _fold_keys(first) == _fold_keys(second)
    assert len(first) == 4
    for fold in first:
        assert max(row.game_creation for row in fold.training) < min(
            row.game_creation for row in fold.validation
        )
        assert {row.game_creation for row in fold.training}.isdisjoint(
            row.game_creation for row in fold.validation
        )
    scored = [
        (row.draft.platform, row.draft.match_id)
        for fold in first
        for row in fold.validation
    ]
    assert len(scored) == len(set(scored))


def test_inner_selection_never_contains_outer_test_observation() -> None:
    protocol = _test_protocol()
    outer = construct_outer_folds(
        _timed_rows(), protocol, enforce_frozen_counts=False
    )[0]
    inner = construct_inner_folds(
        outer.training,
        outer.cutoff,
        fold_count=3,
        minimum_training_drafts=1,
        minimum_per_platform=1,
        minimum_hours=0,
    )
    outer_test = {
        (row.draft.platform, row.draft.match_id) for row in outer.validation
    }
    inner_rows = {
        (row.draft.platform, row.draft.match_id)
        for fold in inner
        for row in (*fold.training, *fold.validation)
    }

    assert not outer_test & inner_rows
    assert all(
        max(row.game_creation for row in fold.training)
        < min(row.game_creation for row in fold.validation)
        for fold in inner
    )


def test_fold_local_vocabulary_rates_and_unseen_features() -> None:
    training = tuple(_draft(f"train-{index}", outcome=index % 2) for index in range(6))
    unseen = _draft("unseen", outcome=1, champion_offset=10_000)
    vocabulary = build_feature_vocabulary(training, include_lane_matchups=False)
    rates = stage34b._fit_role_rate_baseline(training, minimum_support=1)

    assert not set(
        build_feature_vocabulary((unseen,), include_lane_matchups=False).keys
    ) & set(vocabulary.keys)
    assert all(key[0] in POSITIONS for key in rates)
    assert stage34b._predict_role_rate(unseen, rates, 0.5) == 0.5

    config = SharedModelConfig("side", 0.1, 0.0, 0, 0)
    model = fit_shared_interaction_model(
        training,
        variant="composition_with_side_intercept",
        config=config,
        embedding_support_minimum=1,
        seed=7,
        initialization_scope="unseen-test",
        maximum_iterations=300,
        tolerance=1e-9,
    )
    assert predict_shared_probability(model, unseen) == pytest.approx(
        stage34b._sigmoid(model.intercept)
    )


def test_invalid_or_incomplete_draft_structure_fails_closed() -> None:
    draft = _draft("invalid", outcome=1)
    invalid = replace(draft, opposing=draft.opposing[:-1])
    config = SharedModelConfig("side", 0.1, 0.0, 0, 0)

    with pytest.raises(Stage3ValidationError, match="draft structure differs"):
        fit_shared_interaction_model(
            (invalid,),
            variant="composition_with_side_intercept",
            config=config,
            embedding_support_minimum=1,
            seed=7,
            initialization_scope="invalid",
            maximum_iterations=20,
            tolerance=1e-8,
        )


@pytest.mark.parametrize(
    ("variant", "synergy_dimension", "counter_dimension"),
    [
        ("composition_with_side_intercept", 0, 0),
        ("shared_allied_synergy", 2, 0),
        ("shared_lane_counter", 0, 2),
        ("combined_shared_interactions", 2, 2),
    ],
)
def test_team_swap_negates_draft_terms_around_side_intercept(
    variant: str, synergy_dimension: int, counter_dimension: int
) -> None:
    draft = _draft("swap", outcome=1)
    model = _manual_model(
        draft,
        variant=variant,
        synergy_dimension=synergy_dimension,
        counter_dimension=counter_dimension,
        intercept=0.23,
    )

    score = stage34b._logit(predict_shared_probability(model, draft))
    swapped = stage34b._logit(
        predict_shared_probability(model, swap_teams(draft))
    )
    assert score + swapped == pytest.approx(2 * model.intercept, abs=1e-12)


def test_counter_orientation_reverses_when_lane_champions_reverse() -> None:
    draft = _draft("counter", outcome=1)
    model = _manual_model(
        draft,
        variant="shared_lane_counter",
        synergy_dimension=0,
        counter_dimension=2,
        intercept=0.0,
        composition_scale=0.0,
    )

    original = stage34b._logit(predict_shared_probability(model, draft))
    reversed_score = stage34b._logit(
        predict_shared_probability(model, swap_teams(draft))
    )
    assert original == pytest.approx(-reversed_score, abs=1e-12)


def test_tiny_shared_fit_is_finite_and_deterministic() -> None:
    drafts = tuple(
        _draft(
            f"fit-{index}",
            outcome=index % 2,
            champion_offset=(index % 3) * 20,
        )
        for index in range(18)
    )
    config = SharedModelConfig("combined", 0.1, 1.0, 2, 2)
    kwargs = {
        "variant": "combined_shared_interactions",
        "config": config,
        "embedding_support_minimum": 1,
        "seed": 44,
        "initialization_scope": "determinism",
        "maximum_iterations": 300,
        "tolerance": 1e-9,
    }

    first = fit_shared_interaction_model(drafts, **kwargs)
    second = fit_shared_interaction_model(drafts, **kwargs)
    events = []
    counter = stage34b._FitCounter(events.append)
    operation = counter.start(
        phase="instrumentation-regression",
        model="combined_shared_interactions",
        optimizer_fit=True,
        config_id=config.config_id,
    )
    instrumented = fit_shared_interaction_model(drafts, **kwargs)
    counter.complete(
        operation,
        optimizer_iterations=instrumented.optimizer_iterations,
        optimizer_status=instrumented.optimizer_status,
        parameter_count=stage34b._shared_parameter_count(instrumented),
    )
    probabilities = [predict_shared_probability(first, row) for row in drafts]
    instrumented_probabilities = [
        predict_shared_probability(instrumented, row) for row in drafts
    ]

    assert first == second == instrumented
    assert probabilities == instrumented_probabilities
    assert all(math.isfinite(value) and 0 < value < 1 for value in probabilities)
    assert counter.count == counter.optimizer_fits == 1
    assert counter.analytic_operations == 0


def test_selection_and_tie_break_are_deterministic() -> None:
    protocol = _test_protocol()
    rows = _timed_rows()
    folds = construct_inner_folds(
        tuple(row for row in rows if row.game_creation < _utc(2026, 8, 3)),
        _utc(2026, 8, 3),
        fold_count=3,
        minimum_training_drafts=1,
        minimum_per_platform=1,
        minimum_hours=0,
    )
    configs = (
        SharedModelConfig("z-config", 0.1, 0.0, 0, 0),
        SharedModelConfig("a-config", 0.1, 0.0, 0, 0),
    )

    first = select_shared_config(
        variant="composition_with_side_intercept",
        configs=configs,
        inner_folds=folds,
        protocol=protocol,
    )
    second = select_shared_config(
        variant="composition_with_side_intercept",
        configs=configs,
        inner_folds=folds,
        protocol=protocol,
    )
    events = []
    counter = stage34b._FitCounter(events.append)
    instrumented = select_shared_config(
        variant="composition_with_side_intercept",
        configs=configs,
        inner_folds=folds,
        protocol=protocol,
        fit_counter=counter,
        phase="instrumentation-regression",
    )

    assert first == second
    assert first == instrumented
    assert first[0].config_id == "a-config"
    assert counter.count == len(configs) * len(folds)
    assert counter.optimizer_fits == counter.count
    assert counter.analytic_operations == 0
    assert len(
        [
            event
            for event in events
            if event["event"] == "predictive_training_operation_completed"
        ]
    ) == counter.count
    assert all(
        event.get("optimizer_status") == "converged"
        for event in events
        if event["event"] == "predictive_training_operation_completed"
    )


def test_metric_instrumentation_does_not_change_scientific_content() -> None:
    records = tuple(
        stage34b._PredictionRow(
            private_key=(platform, f"private-{index}"),
            platform=platform,
            outer_block=f"outer-{index % 2}",
            outcome=(index // 2) % 2,
            probabilities={
                policy: 0.4 + 0.02 * ((policy_index + index) % 9)
                for policy_index, policy in enumerate(ALL_POLICIES)
            },
        )
        for platform in ("eun1", "euw1")
        for index in range(8)
    )

    without_instrumentation = stage34b._aggregate_metrics(records, 10)
    events = []
    calibration = stage34b._CalibrationCounter(events.append)
    with_instrumentation = stage34b._aggregate_metrics(
        records, 10, calibration_counter=calibration
    )

    assert without_instrumentation == with_instrumentation
    assert stage34b._sha256_json(without_instrumentation) == stage34b._sha256_json(
        with_instrumentation
    )
    assert calibration.evaluations == len(ALL_POLICIES) * 5
    assert calibration.optimizer_fits + calibration.analytic_evaluations == (
        calibration.evaluations
    )


def test_paired_bootstrap_preserves_all_comparisons_and_private_rows() -> None:
    records = tuple(
        stage34b._PredictionRow(
            private_key=(platform, f"private-{index}"),
            platform=platform,
            outer_block=f"outer-{index % 2}",
            outcome=index % 2,
            probabilities={
                policy: 0.45 + 0.01 * ((policy_index + index) % 5)
                for policy_index, policy in enumerate(ALL_POLICIES)
            },
        )
        for platform in ("eun1", "euw1")
        for index in range(6)
    )

    first = paired_bootstrap_intervals(
        records,
        candidates=stage34b.MODEL_VARIANTS_B,
        baselines=stage34b.BASELINES,
        replicates=25,
        seed=123,
    )
    second = paired_bootstrap_intervals(
        records,
        candidates=stage34b.MODEL_VARIANTS_B,
        baselines=stage34b.BASELINES,
        replicates=25,
        seed=123,
    )

    assert first == second
    assert all(
        set(first[candidate]) == set(stage34b.BASELINES)
        for candidate in stage34b.MODEL_VARIANTS_B
    )
    rendered = json.dumps(first, allow_nan=False)
    assert "private-" not in rendered
    assert all(math.isfinite(value) for value in _floats(first))

    incomplete = copy.deepcopy(records[0].probabilities)
    incomplete.pop(ALL_POLICIES[-1])
    with pytest.raises(Stage3ValidationError, match="identical test rows"):
        paired_bootstrap_intervals(
            (stage34b._PredictionRow(("eun1", "x"), "eun1", "outer-0", 0, incomplete),),
            candidates=stage34b.MODEL_VARIANTS_B,
            baselines=stage34b.BASELINES,
            replicates=2,
            seed=1,
        )


def test_public_artifact_validation_rejects_row_identifier_fields() -> None:
    artifact = {
        "schema_version": stage34b.RESULT_SCHEMA_VERSION,
        "protocol_id": stage34b.PROTOCOL_ID,
        "privacy": {
            "aggregate_only": True,
            "oof_rows_published": False,
            "match_or_player_identifiers_published": False,
            "external_paths_published": False,
        },
        "metrics": {"count": 4, "log_loss": 0.5},
    }
    validate_stage34b_artifact(artifact)

    artifact["metrics"]["match_id"] = "private"
    with pytest.raises(Stage3ValidationError, match="private identifier"):
        validate_stage34b_artifact(artifact)


def test_material_gate_is_evaluated_from_same_fold_aggregates() -> None:
    protocol = load_stage34b_protocol(PROTOCOL_PATH, schema_path=SCHEMA_PATH)
    scopes = ("overall", "eun1", "euw1", "outer-0", "outer-1", "outer-2", "outer-3")

    def summaries(log_loss: float, brier: float) -> dict[str, dict[str, float]]:
        return {
            scope: {
                "log_loss": log_loss,
                "brier_score": brier,
                "expected_calibration_error": 0.01,
                "calibration_intercept": 0.0,
                "calibration_slope": 1.0,
            }
            for scope in scopes
        }

    metrics = {policy: summaries(0.65, 0.24) for policy in ALL_POLICIES}
    metrics["training_fold_blue_win_rate_intercept"] = summaries(0.66, 0.242)
    composition = "stage3_4a_composition_only_l2_0_1_no_intercept"
    metrics[composition] = summaries(0.655, 0.2415)
    intervals = {
        candidate: {
            baseline: {
                metric: {"upper_0_975": -0.001}
                for metric in ("log_loss", "brier_score")
            }
            for baseline in stage34b.BASELINES
        }
        for candidate in stage34b.MODEL_VARIANTS_B
    }
    coverage = {
        candidate: {
            "overall": 1.0,
            "maximum_role_coverage_drop_vs_composition": 0.0,
        }
        for candidate in stage34b.MODEL_VARIANTS_B
    }

    result = evaluate_material_usefulness_gate(
        metrics=metrics,
        paired_intervals=intervals,
        coverage=coverage,
        protocol=protocol,
    )
    assert all(row["passes_all"] for row in result.values())

    metrics[stage34b.MODEL_VARIANTS_B[0]]["overall"][
        "expected_calibration_error"
    ] = 0.03
    result = evaluate_material_usefulness_gate(
        metrics=metrics,
        paired_intervals=intervals,
        coverage=coverage,
        protocol=protocol,
    )
    assert not result[stage34b.MODEL_VARIANTS_B[0]]["passes_all"]
    assert not result[stage34b.MODEL_VARIANTS_B[0]]["checks"]["ece"]


def _test_protocol() -> dict:
    protocol = load_stage34b_protocol(PROTOCOL_PATH, schema_path=SCHEMA_PATH)
    protocol = copy.deepcopy(protocol)
    protocol["validation"]["minimum_preceding_training"] = {
        "drafts": 1,
        "hours": 0,
        "per_platform_drafts": 1,
    }
    protocol["hyperparameter_policy"].update(
        {
            "embedding_support_minimum_training_matches": 1,
            "maximum_iterations": 300,
            "optimizer_tolerance": 1e-9,
            "rate_baseline_minimum_training_matches": 1,
        }
    )
    return protocol


def _timed_rows() -> tuple[TimedDraft, ...]:
    rows = []
    start = _utc(2026, 7, 29)
    for day in range(14):
        for platform_index, platform in enumerate(("eun1", "euw1")):
            for sample in range(2):
                index = day * 4 + platform_index * 2 + sample
                rows.append(
                    TimedDraft(
                        _draft(
                            f"row-{index}",
                            outcome=index % 2,
                            platform=platform,
                            champion_offset=(index % 4) * 20,
                        ),
                        start
                        + timedelta(
                            days=day,
                            hours=10 + sample,
                            minutes=platform_index,
                        ),
                    )
                )
    return tuple(rows)


def _draft(
    match_id: str,
    *,
    outcome: int,
    platform: str = "eun1",
    champion_offset: int = 0,
) -> MatchDraftObservation:
    allied = tuple(
        ChampionRoleAssignment(role, champion_offset + index + 1)
        for index, role in enumerate(POSITIONS)
    )
    opposing = tuple(
        ChampionRoleAssignment(role, champion_offset + index + 11)
        for index, role in enumerate(POSITIONS)
    )
    return MatchDraftObservation(
        match_id=match_id,
        public_patch="26.15",
        platform=platform,
        queue_id=420,
        allied_team_id=100,
        opposing_team_id=200,
        allied=allied,
        opposing=opposing,
        outcome=outcome,
    )


def _manual_model(
    draft: MatchDraftObservation,
    *,
    variant: str,
    synergy_dimension: int,
    counter_dimension: int,
    intercept: float,
    composition_scale: float = 0.01,
) -> SharedInteractionModel:
    vocabulary = build_feature_vocabulary((draft,), include_lane_matchups=False)
    count = len(vocabulary.keys)

    def matrix(dimension: int, scale: float) -> tuple[tuple[float, ...], ...]:
        return tuple(
            tuple(scale * (row + 1) * (column + 1) for column in range(dimension))
            for row in range(count)
        )

    return SharedInteractionModel(
        variant=variant,
        config=SharedModelConfig(
            "manual", 0.1, 1.0, synergy_dimension, counter_dimension
        ),
        vocabulary=vocabulary,
        embedding_feature_indexes=tuple(range(count)),
        intercept=intercept,
        composition_coefficients=tuple(
            composition_scale * (index + 1) for index in range(count)
        ),
        synergy_embeddings=matrix(synergy_dimension, 0.003),
        counter_attack_embeddings=matrix(counter_dimension, 0.004),
        counter_defense_embeddings=matrix(counter_dimension, -0.002),
        optimizer_iterations=0,
        optimizer_status="synthetic_manual",
    )


def _fold_keys(
    folds: tuple[ChronologicalFold, ...],
) -> tuple[tuple[str, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]], ...]:
    return tuple(
        (
            fold.fold_id,
            tuple((row.draft.platform, row.draft.match_id) for row in fold.training),
            tuple((row.draft.platform, row.draft.match_id) for row in fold.validation),
        )
        for fold in folds
    )


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _floats(value: object) -> list[float]:
    found = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, float):
            found.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found
