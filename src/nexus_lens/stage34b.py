"""Prospectively frozen Stage 3.4B-1 rolling-origin evaluation machinery."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

import numpy as np
from scipy.optimize import minimize

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.composition_modeling import (
    MODEL_VARIANTS,
    POSITIONS,
    FeatureVocabulary,
    MatchDraftObservation,
    build_feature_vocabulary,
    fit_composition_model,
    predict_probability,
    vectorize_draft,
)

PROTOCOL_SCHEMA_VERSION = "stage3.4b-1-protocol-v1"
PROTOCOL_ID = "stage3.4b-1-patch26.15-protocol-v1"
PROTOCOL_SHA256 = "e23a0c74517ea9a6700875c0a2ab8beb9e977cbdce9a1279ba4153beadae0c7f"
PROTOCOL_SCHEMA_SHA256 = (
    "5bcf3d8cc929edc8f0142758fbd3372c832cb62a3511fd30856c2674954127f7"
)
RESULT_SCHEMA_VERSION = "stage3.4b-1-development-results-v1"
MODEL_VARIANTS_B = (
    "composition_with_side_intercept",
    "shared_allied_synergy",
    "shared_lane_counter",
    "combined_shared_interactions",
)
BASELINES = (
    "constant_0_5",
    "training_fold_blue_win_rate_intercept",
    "stage3_4a_composition_only_l2_0_1_no_intercept",
    "stage3_4a_composition_plus_direct_matchup_l2_0_1_no_intercept",
    "fold_local_champion_role_rate_minimum_support_10",
)
ALL_POLICIES = (*BASELINES, *MODEL_VARIANTS_B)
FORBIDDEN_PUBLIC_KEYS = {
    "accountId",
    "match_id",
    "player_key",
    "puuid",
    "riotIdGameName",
    "riotIdTagline",
    "summonerId",
    "summonerName",
}
EXPECTED_OUTER_BLOCKS = 4
EXPECTED_PREDICTIVE_FITS = 171
ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class TimedDraft:
    draft: MatchDraftObservation
    game_creation: datetime


@dataclass(frozen=True)
class ChronologicalFold:
    fold_id: str
    cutoff: datetime
    validation_end: datetime
    training: tuple[TimedDraft, ...]
    validation: tuple[TimedDraft, ...]


@dataclass(frozen=True)
class SharedModelConfig:
    config_id: str
    main_l2: float
    embedding_l2: float
    synergy_dimension: int
    counter_dimension: int


@dataclass(frozen=True)
class SharedInteractionModel:
    variant: str
    config: SharedModelConfig
    vocabulary: FeatureVocabulary
    embedding_feature_indexes: tuple[int, ...]
    intercept: float
    composition_coefficients: tuple[float, ...]
    synergy_embeddings: tuple[tuple[float, ...], ...]
    counter_attack_embeddings: tuple[tuple[float, ...], ...]
    counter_defense_embeddings: tuple[tuple[float, ...], ...]
    optimizer_iterations: int
    optimizer_status: str


@dataclass(frozen=True)
class _PredictionRow:
    private_key: tuple[str, str]
    platform: str
    outer_block: str
    outcome: int
    probabilities: dict[str, float]


@dataclass(frozen=True)
class Stage34BEvaluation:
    artifact: dict[str, Any]
    final_candidate_models: dict[str, SharedInteractionModel]


class _FitCounter:
    def __init__(self, callback: ProgressCallback | None) -> None:
        self.count = 0
        self.optimizer_fits = 0
        self.analytic_operations = 0
        self.callback = callback

    def record(
        self,
        *,
        phase: str,
        model: str,
        optimizer_fit: bool,
        config_id: str | None = None,
    ) -> None:
        self.count += 1
        if optimizer_fit:
            self.optimizer_fits += 1
        else:
            self.analytic_operations += 1
        if self.callback is not None:
            self.callback(
                {
                    "event": "predictive_training_operation_completed",
                    "training_operation_number": self.count,
                    "phase": phase,
                    "model": model,
                    "config_id": config_id,
                    "optimizer_fit": optimizer_fit,
                }
            )


def load_stage34b_protocol(
    path: Path, *, schema_path: Path | None = None
) -> dict[str, Any]:
    try:
        protocol = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise Stage3ValidationError(
            "stage34b_protocol_invalid_json", "Stage 3.4B-1 protocol is invalid"
        ) from error
    validate_stage34b_protocol(protocol)
    if schema_path is not None:
        validate_stage34b_protocol_schema(protocol, schema_path)
    return protocol


def validate_stage34b_protocol_schema(
    protocol: dict[str, Any], schema_path: Path
) -> None:
    try:
        schema = json.loads(
            schema_path.read_text(encoding="utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise Stage3ValidationError(
            "stage34b_schema_invalid_json", "Stage 3.4B-1 schema is invalid"
        ) from error
    if _sha256_json(schema) != PROTOCOL_SCHEMA_SHA256:
        raise Stage3ValidationError(
            "stage34b_schema_hash", "Stage 3.4B-1 schema hash differs"
        )
    properties = schema.get("properties")
    required = schema.get("required")
    if (
        schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or not isinstance(properties, dict)
        or not isinstance(required, list)
        or set(properties) != set(required)
        or set(protocol) != set(required)
    ):
        raise Stage3ValidationError(
            "stage34b_schema_shape", "Stage 3.4B-1 schema differs"
        )
    for key, definition in properties.items():
        if "const" in definition and protocol[key] != definition["const"]:
            raise Stage3ValidationError(
                "stage34b_schema_const", "Stage 3.4B-1 schema constant differs"
            )
        expected_type = definition.get("type")
        if expected_type == "object" and not isinstance(protocol[key], dict):
            raise Stage3ValidationError(
                "stage34b_schema_type", "Stage 3.4B-1 schema type differs"
            )
        if expected_type == "string" and not isinstance(protocol[key], str):
            raise Stage3ValidationError(
                "stage34b_schema_type", "Stage 3.4B-1 schema type differs"
            )


def validate_stage34b_protocol(protocol: dict[str, Any]) -> None:
    required = {
        "candidate_models",
        "capacity_estimate",
        "data_scope",
        "execution_budget",
        "future_holdout_policy",
        "hyperparameter_policy",
        "material_usefulness_gate",
        "metrics",
        "models",
        "privacy",
        "protocol_id",
        "schema_version",
        "status",
        "target",
        "validation",
    }
    if set(protocol) != required:
        raise Stage3ValidationError(
            "stage34b_protocol_shape", "Stage 3.4B-1 protocol keys differ"
        )
    if _sha256_json(protocol) != PROTOCOL_SHA256:
        raise Stage3ValidationError(
            "stage34b_protocol_hash", "Stage 3.4B-1 protocol hash differs"
        )
    if (
        protocol["schema_version"] != PROTOCOL_SCHEMA_VERSION
        or protocol["protocol_id"] != PROTOCOL_ID
        or protocol["data_scope"]["public_patch"] != "26.15"
        or protocol["data_scope"]["eligible_drafts"] != 9414
        or protocol["data_scope"]["queue_id"] != 420
    ):
        raise Stage3ValidationError(
            "stage34b_protocol_identity", "Stage 3.4B-1 protocol identity differs"
        )
    rendered = json.dumps(protocol, sort_keys=True)
    if "26.16" in rendered:
        raise Stage3ValidationError(
            "stage34b_numbered_future_patch",
            "mutable Stage 3.4B protocol assumes a numbered future patch",
        )
    if "future sealed temporal holdout" not in protocol["future_holdout_policy"]:
        raise Stage3ValidationError(
            "stage34b_holdout_language", "future holdout language is not generic"
        )
    if tuple(protocol["models"]["baselines"]) != BASELINES or tuple(
        protocol["models"]["candidates"]
    ) != MODEL_VARIANTS_B:
        raise Stage3ValidationError(
            "stage34b_model_set", "Stage 3.4B-1 model set differs"
        )
    grids = _candidate_grids(protocol)
    if [len(grids[name]) for name in MODEL_VARIANTS_B] != [3, 2, 2, 2]:
        raise Stage3ValidationError(
            "stage34b_grid_shape", "Stage 3.4B-1 candidate grids differ"
        )
    hyper = protocol["hyperparameter_policy"]
    finite_positive = (
        hyper["optimizer_tolerance"],
        hyper["embedding_support_minimum_training_matches"],
        hyper["rate_baseline_minimum_training_matches"],
    )
    if any(not math.isfinite(value) or value <= 0 for value in finite_positive):
        raise Stage3ValidationError(
            "stage34b_hyperparameter_invalid", "Stage 3.4B hyperparameter is invalid"
        )
    if hyper["maximum_iterations"] < 1:
        raise Stage3ValidationError(
            "stage34b_optimizer_invalid", "Stage 3.4B optimizer limit is invalid"
        )
    blocks = protocol["validation"]["outer_blocks"]
    if len(blocks) != EXPECTED_OUTER_BLOCKS:
        raise Stage3ValidationError(
            "stage34b_outer_block_count", "Stage 3.4B outer block count differs"
        )
    previous_end = None
    for index, block in enumerate(blocks):
        start = _timestamp(block["start_inclusive_utc"])
        end = _timestamp(block["end_exclusive_utc"])
        if block["id"] != f"outer-{index}" or start >= end:
            raise Stage3ValidationError(
                "stage34b_outer_block_invalid", "Stage 3.4B outer block is invalid"
            )
        if previous_end is not None and start != previous_end:
            raise Stage3ValidationError(
                "stage34b_outer_block_gap", "Stage 3.4B outer blocks are not adjacent"
            )
        previous_end = end
    accounting = expected_fit_accounting(protocol)
    if (
        accounting["expected_predictive_training_operations"]
        != EXPECTED_PREDICTIVE_FITS
        or protocol["execution_budget"] != accounting
    ):
        raise Stage3ValidationError(
            "stage34b_fit_budget_conflict", "Stage 3.4B fit budget differs"
        )
    privacy = protocol["privacy"]
    if (
        not privacy["aggregate_only_publication"]
        or not privacy["oof_rows_ephemeral_only"]
        or privacy["player_history_features_used"]
    ):
        raise Stage3ValidationError(
            "stage34b_privacy_conflict", "Stage 3.4B privacy contract differs"
        )
    _reject_nonfinite(protocol)


def expected_fit_count(protocol: dict[str, Any]) -> int:
    return expected_fit_accounting(protocol)[
        "expected_predictive_training_operations"
    ]


def expected_fit_accounting(protocol: dict[str, Any]) -> dict[str, int]:
    grid_count = sum(len(rows) for rows in _candidate_grids(protocol).values())
    inner_count = int(protocol["validation"]["inner"]["folds"])
    outer_count = len(protocol["validation"]["outer_blocks"])
    analytic_baselines_per_outer = 2
    stage_a_baselines_per_outer = 2
    candidates_per_context = len(MODEL_VARIANTS_B)
    candidate_inner = grid_count * inner_count * (outer_count + 1)
    candidate_outer = outer_count * candidates_per_context
    candidate_final = candidates_per_context
    stage_a_baseline = outer_count * stage_a_baselines_per_outer
    analytic_baseline = outer_count * analytic_baselines_per_outer
    predictive_optimizer = (
        candidate_inner + candidate_outer + candidate_final + stage_a_baseline
    )
    predictive_operations = predictive_optimizer + analytic_baseline
    scopes = 3 + outer_count  # overall, two platforms, and each outer block
    calibration_evaluations = len(ALL_POLICIES) * scopes
    return {
        "analytic_baseline_training_operations": analytic_baseline,
        "bootstrap_model_fits": 0,
        "calibration_metric_evaluations": calibration_evaluations,
        "calibration_metric_optimizer_fits_upper_bound": calibration_evaluations,
        "candidate_final_all_development_fits": candidate_final,
        "candidate_inner_selection_fits": candidate_inner,
        "candidate_outer_fits": candidate_outer,
        "expected_predictive_training_operations": predictive_operations,
        "predictive_optimizer_fits": predictive_optimizer,
        "stage3_4a_baseline_optimizer_fits": stage_a_baseline,
        "total_optimizer_invocations_upper_bound_including_metric_regressions": (
            predictive_optimizer + calibration_evaluations
        ),
    }


def construct_outer_folds(
    rows: tuple[TimedDraft, ...],
    protocol: dict[str, Any],
    *,
    enforce_frozen_counts: bool,
) -> tuple[ChronologicalFold, ...]:
    ordered = _validate_timed_rows(rows)
    minimum = protocol["validation"]["minimum_preceding_training"]
    folds = []
    seen_validation: set[tuple[str, str]] = set()
    for block in protocol["validation"]["outer_blocks"]:
        start = _timestamp(block["start_inclusive_utc"])
        end = _timestamp(block["end_exclusive_utc"])
        training = tuple(row for row in ordered if row.game_creation < start)
        validation = tuple(
            row for row in ordered if start <= row.game_creation < end
        )
        _validate_chronological_fold(
            training,
            validation,
            start,
            minimum_drafts=int(minimum["drafts"]),
            minimum_per_platform=int(minimum["per_platform_drafts"]),
            minimum_hours=int(minimum["hours"]),
        )
        keys = {_private_key(row) for row in validation}
        if seen_validation & keys:
            raise Stage3ValidationError(
                "stage34b_outer_overlap", "an outer test match occurs twice"
            )
        seen_validation.update(keys)
        if enforce_frozen_counts:
            counts = Counter(row.draft.platform for row in validation)
            if (
                len(validation) != block["expected_test_drafts"]
                or counts["eun1"] != block["expected_eun1_test_drafts"]
                or counts["euw1"] != block["expected_euw1_test_drafts"]
            ):
                raise Stage3ValidationError(
                    "stage34b_outer_count_conflict",
                    "observed outer block count differs from the frozen protocol",
                )
        folds.append(
            ChronologicalFold(
                fold_id=str(block["id"]),
                cutoff=start,
                validation_end=end,
                training=training,
                validation=validation,
            )
        )
    if enforce_frozen_counts and len(seen_validation) != protocol["validation"][
        "scored_outer_test_drafts"
    ]:
        raise Stage3ValidationError(
            "stage34b_scored_count_conflict", "scored outer test count differs"
        )
    return tuple(folds)


def construct_inner_folds(
    outer_training: tuple[TimedDraft, ...],
    cutoff: datetime,
    *,
    fold_count: int,
    minimum_training_drafts: int,
    minimum_per_platform: int,
    minimum_hours: int,
) -> tuple[ChronologicalFold, ...]:
    ordered = _validate_timed_rows(outer_training)
    folds = []
    for index in range(fold_count):
        validation_start = cutoff - timedelta(days=fold_count - index)
        validation_end = validation_start + timedelta(days=1)
        training = tuple(row for row in ordered if row.game_creation < validation_start)
        validation = tuple(
            row
            for row in ordered
            if validation_start <= row.game_creation < validation_end
        )
        _validate_chronological_fold(
            training,
            validation,
            validation_start,
            minimum_drafts=minimum_training_drafts,
            minimum_per_platform=minimum_per_platform,
            minimum_hours=minimum_hours,
        )
        folds.append(
            ChronologicalFold(
                fold_id=f"inner-{index}",
                cutoff=validation_start,
                validation_end=validation_end,
                training=training,
                validation=validation,
            )
        )
    outer_keys = {_private_key(row) for row in outer_training}
    if any(
        _private_key(row) not in outer_keys
        for fold in folds
        for row in (*fold.training, *fold.validation)
    ):
        raise Stage3ValidationError(
            "stage34b_inner_outer_leakage", "inner fold contains an outer test row"
        )
    return tuple(folds)


def fit_shared_interaction_model(
    drafts: tuple[MatchDraftObservation, ...],
    *,
    variant: str,
    config: SharedModelConfig,
    embedding_support_minimum: int,
    seed: int,
    initialization_scope: str,
    maximum_iterations: int,
    tolerance: float,
) -> SharedInteractionModel:
    if variant not in MODEL_VARIANTS_B or not drafts:
        raise Stage3ValidationError(
            "stage34b_model_input_invalid", "shared model input is invalid"
        )
    for draft in drafts:
        _validate_draft_structure(draft)
    _validate_variant_config(variant, config)
    vocabulary = build_feature_vocabulary(drafts, include_lane_matchups=False)
    supports = Counter()
    for draft in drafts:
        for index in vectorize_draft(draft, vocabulary):
            supports[index] += 1
    embedding_feature_indexes = tuple(
        index
        for index in range(len(vocabulary.keys))
        if supports[index] >= embedding_support_minimum
    )
    embedding_lookup = {
        feature_index: index
        for index, feature_index in enumerate(embedding_feature_indexes)
    }
    encoded = _encode_drafts(drafts, vocabulary, embedding_lookup)
    layout = _ParameterLayout(
        composition_count=len(vocabulary.keys),
        embedding_count=len(embedding_feature_indexes),
        synergy_dimension=config.synergy_dimension,
        counter_dimension=config.counter_dimension,
    )
    initial = np.zeros(layout.total, dtype=np.float64)
    outcome_rate = mean(draft.outcome for draft in drafts)
    initial[0] = _logit(outcome_rate)
    rng = np.random.default_rng(
        _initialization_seed(seed, config, initialization_scope)
    )
    for section in layout.embedding_sections:
        initial[section] = rng.normal(0.0, 0.01, section.stop - section.start)

    def objective(parameters: np.ndarray) -> tuple[float, np.ndarray]:
        scores, cache = _shared_scores(parameters, layout, encoded)
        outcomes = encoded.outcomes
        probabilities = _sigmoid_array(scores)
        errors = (probabilities - outcomes) / len(outcomes)
        loss = float(np.mean(np.logaddexp(0.0, scores) - outcomes * scores))
        gradient = np.zeros_like(parameters)
        gradient[0] = errors.sum()
        gradient[layout.composition] = np.asarray(
            encoded.composition.T @ errors
        ).ravel()
        composition = parameters[layout.composition]
        loss += 0.5 * config.main_l2 * float(np.dot(composition, composition))
        gradient[layout.composition] += config.main_l2 * composition
        _embedding_gradient(parameters, gradient, errors, layout, encoded, cache)
        for section in layout.embedding_sections:
            values = parameters[section]
            loss += 0.5 * config.embedding_l2 * float(np.dot(values, values))
            gradient[section] += config.embedding_l2 * values
        return loss, gradient

    result = minimize(
        objective,
        initial,
        method="L-BFGS-B",
        jac=True,
        options={"maxiter": maximum_iterations, "ftol": tolerance, "gtol": tolerance},
    )
    if not result.success or not np.all(np.isfinite(result.x)):
        raise Stage3ValidationError(
            "stage34b_optimizer_failure", "shared model optimizer did not converge"
        )
    return _shared_model_from_parameters(
        variant,
        config,
        vocabulary,
        embedding_feature_indexes,
        layout,
        result.x,
        int(result.nit),
    )


def predict_shared_probability(
    model: SharedInteractionModel, draft: MatchDraftObservation
) -> float:
    composition = vectorize_draft(draft, model.vocabulary)
    score = model.intercept + sum(
        model.composition_coefficients[index] * value
        for index, value in composition.items()
    )
    embedding_lookup = {
        feature_index: index
        for index, feature_index in enumerate(model.embedding_feature_indexes)
    }
    blue, red = _role_feature_indexes(draft, model.vocabulary, embedding_lookup)
    if model.config.synergy_dimension:
        synergy = np.asarray(model.synergy_embeddings)
        for left in range(len(POSITIONS)):
            for right in range(left + 1, len(POSITIONS)):
                if blue[left] >= 0 and blue[right] >= 0:
                    score += float(np.dot(synergy[blue[left]], synergy[blue[right]]))
                if red[left] >= 0 and red[right] >= 0:
                    score -= float(np.dot(synergy[red[left]], synergy[red[right]]))
    if model.config.counter_dimension:
        attack = np.asarray(model.counter_attack_embeddings)
        defense = np.asarray(model.counter_defense_embeddings)
        for role in range(len(POSITIONS)):
            if blue[role] >= 0 and red[role] >= 0:
                score += float(np.dot(attack[blue[role]], defense[red[role]]))
                score -= float(np.dot(attack[red[role]], defense[blue[role]]))
    return _sigmoid(score)


def select_shared_config(
    *,
    variant: str,
    configs: tuple[SharedModelConfig, ...],
    inner_folds: tuple[ChronologicalFold, ...],
    protocol: dict[str, Any],
    fit_counter: _FitCounter | None = None,
    phase: str = "inner_selection",
) -> tuple[SharedModelConfig, list[dict[str, Any]]]:
    results = []
    hyper = protocol["hyperparameter_policy"]
    for config in configs:
        losses = []
        fold_rows = []
        for fold in inner_folds:
            model = fit_shared_interaction_model(
                tuple(row.draft for row in fold.training),
                variant=variant,
                config=config,
                embedding_support_minimum=hyper[
                    "embedding_support_minimum_training_matches"
                ],
                seed=hyper["seed"],
                initialization_scope=f"{phase}|{fold.fold_id}",
                maximum_iterations=hyper["maximum_iterations"],
                tolerance=hyper["optimizer_tolerance"],
            )
            if fit_counter is not None:
                fit_counter.record(
                    phase=phase,
                    model=variant,
                    config_id=config.config_id,
                    optimizer_fit=True,
                )
            fold_losses = [
                _binary_log_loss(
                    predict_shared_probability(model, row.draft), row.draft.outcome
                )
                for row in fold.validation
            ]
            losses.append(mean(fold_losses))
            fold_rows.append(
                {
                    "fold": fold.fold_id,
                    "training_matches": len(fold.training),
                    "validation_matches": len(fold.validation),
                    "mean_validation_log_loss": mean(fold_losses),
                }
            )
        results.append(
            {
                "config_id": config.config_id,
                "mean_inner_fold_log_loss": mean(losses),
                "folds": fold_rows,
            }
        )
    best = min(row["mean_inner_fold_log_loss"] for row in results)
    tolerance = 1e-12
    tied_ids = {
        row["config_id"]
        for row in results
        if math.isclose(
            row["mean_inner_fold_log_loss"], best, rel_tol=0, abs_tol=tolerance
        )
    }
    tied = [config for config in configs if config.config_id in tied_ids]
    selected = min(tied, key=_selection_tie_key)
    return selected, results


def paired_bootstrap_intervals(
    records: tuple[_PredictionRow, ...],
    *,
    candidates: tuple[str, ...],
    baselines: tuple[str, ...],
    replicates: int,
    seed: int,
) -> dict[str, Any]:
    _validate_prediction_records(records)
    strata: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, row in enumerate(records):
        strata[(row.platform, row.outer_block)].append(index)
    if replicates < 1:
        raise Stage3ValidationError(
            "stage34b_bootstrap_replicates", "bootstrap replicates must be positive"
        )
    comparisons = tuple(
        (candidate, baseline, metric)
        for candidate in candidates
        for baseline in baselines
        for metric in ("log_loss", "brier_score")
    )
    differences = np.asarray(
        [
            [
                _metric_loss(metric, row.probabilities[candidate], row.outcome)
                - _metric_loss(metric, row.probabilities[baseline], row.outcome)
                for row in records
            ]
            for candidate, baseline, metric in comparisons
        ],
        dtype=np.float64,
    )
    rng = np.random.default_rng(seed)
    replicate_sums = np.zeros((len(comparisons), replicates), dtype=np.float64)
    for _, values in sorted(strata.items()):
        indexes = np.asarray(values, dtype=np.int64)
        probabilities = np.full(len(indexes), 1 / len(indexes), dtype=np.float64)
        counts = rng.multinomial(len(indexes), probabilities, size=replicates)
        replicate_sums += differences[:, indexes] @ counts.T
    samples = replicate_sums / len(records)
    sample_index = {comparison: index for index, comparison in enumerate(comparisons)}
    output: dict[str, Any] = {}
    for candidate in candidates:
        output[candidate] = {}
        for baseline in baselines:
            output[candidate][baseline] = {}
            for metric in ("log_loss", "brier_score"):
                point_values = [
                    _metric_loss(metric, row.probabilities[candidate], row.outcome)
                    - _metric_loss(metric, row.probabilities[baseline], row.outcome)
                    for row in records
                ]
                values = samples[sample_index[(candidate, baseline, metric)]].tolist()
                output[candidate][baseline][metric] = {
                    "orientation": "candidate_minus_baseline",
                    "point_difference": mean(point_values),
                    "lower_0_025": _quantile(values, 0.025),
                    "upper_0_975": _quantile(values, 0.975),
                    "replicates": replicates,
                    "seed": seed,
                    "strata": ["platform", "outer_block"],
                }
    return output


def evaluate_stage34b(
    rows: tuple[TimedDraft, ...],
    protocol: dict[str, Any],
    *,
    enforce_frozen_counts: bool,
    progress_callback: ProgressCallback | None = None,
) -> Stage34BEvaluation:
    """Execute Stage 3.4B-1; callers must enforce authorization for real data."""

    validate_stage34b_protocol(protocol)
    outer_folds = construct_outer_folds(
        rows, protocol, enforce_frozen_counts=enforce_frozen_counts
    )
    hyper = protocol["hyperparameter_policy"]
    minimum = protocol["validation"]["minimum_preceding_training"]
    grids = _candidate_grids(protocol)
    fit_counter = _FitCounter(progress_callback)
    prediction_rows: list[_PredictionRow] = []
    selections: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for outer in outer_folds:
        inner = construct_inner_folds(
            outer.training,
            outer.cutoff,
            fold_count=protocol["validation"]["inner"]["folds"],
            minimum_training_drafts=minimum["drafts"],
            minimum_per_platform=minimum["per_platform_drafts"],
            minimum_hours=minimum["hours"],
        )
        training_drafts = tuple(row.draft for row in outer.training)
        blue_rate = mean(row.outcome for row in training_drafts)
        fit_counter.record(
            phase="outer_baseline",
            model="training_fold_blue_win_rate_intercept",
            optimizer_fit=False,
        )
        role_rates = _fit_role_rate_baseline(
            training_drafts,
            minimum_support=hyper["rate_baseline_minimum_training_matches"],
        )
        fit_counter.record(
            phase="outer_baseline",
            model="fold_local_champion_role_rate_minimum_support_10",
            optimizer_fit=False,
        )
        stage_a = {}
        for variant in MODEL_VARIANTS:
            model = fit_composition_model(
                training_drafts,
                variant=variant,
                l2_strength=0.1,
                max_iterations=hyper["maximum_iterations"],
                tolerance=hyper["optimizer_tolerance"],
            )
            fit_counter.record(
                phase="outer_baseline", model=variant, optimizer_fit=True
            )
            stage_a[variant] = model
        candidates = {}
        for variant in MODEL_VARIANTS_B:
            selected, selection_rows = select_shared_config(
                variant=variant,
                configs=grids[variant],
                inner_folds=inner,
                protocol=protocol,
                fit_counter=fit_counter,
                phase=f"{outer.fold_id}_inner_selection",
            )
            model = fit_shared_interaction_model(
                training_drafts,
                variant=variant,
                config=selected,
                embedding_support_minimum=hyper[
                    "embedding_support_minimum_training_matches"
                ],
                seed=hyper["seed"],
                initialization_scope=f"{outer.fold_id}|outer_fit",
                maximum_iterations=hyper["maximum_iterations"],
                tolerance=hyper["optimizer_tolerance"],
            )
            fit_counter.record(
                phase="outer_candidate",
                model=variant,
                config_id=selected.config_id,
                optimizer_fit=True,
            )
            candidates[variant] = model
            selections[variant].append(
                {
                    "outer_block": outer.fold_id,
                    "selected_config_id": selected.config_id,
                    "candidates": selection_rows,
                }
            )
        for row in outer.validation:
            probabilities = {
                "constant_0_5": 0.5,
                "training_fold_blue_win_rate_intercept": blue_rate,
                "stage3_4a_composition_only_l2_0_1_no_intercept": (
                    predict_probability(stage_a["composition_only"], row.draft)
                ),
                "stage3_4a_composition_plus_direct_matchup_l2_0_1_no_intercept": (
                    predict_probability(
                        stage_a["composition_plus_lane_matchups"], row.draft
                    )
                ),
                "fold_local_champion_role_rate_minimum_support_10": (
                    _predict_role_rate(row.draft, role_rates, blue_rate)
                ),
                **{
                    variant: predict_shared_probability(model, row.draft)
                    for variant, model in candidates.items()
                },
            }
            prediction_rows.append(
                _PredictionRow(
                    private_key=_private_key(row),
                    platform=row.draft.platform,
                    outer_block=outer.fold_id,
                    outcome=row.draft.outcome,
                    probabilities=probabilities,
                )
            )
    ephemeral = tuple(prediction_rows)
    _validate_prediction_records(ephemeral)
    metrics = _aggregate_metrics(ephemeral, protocol["metrics"]["calibration_bins"])
    bootstrap = paired_bootstrap_intervals(
        ephemeral,
        candidates=MODEL_VARIANTS_B,
        baselines=BASELINES,
        replicates=protocol["metrics"]["bootstrap"]["replicates"],
        seed=protocol["metrics"]["bootstrap"]["seed"],
    )
    coverage = {
        variant: {
            "overall": 1.0,
            "by_role": {role: 1.0 for role in POSITIONS},
            "maximum_role_coverage_drop_vs_composition": 0.0,
        }
        for variant in MODEL_VARIANTS_B
    }
    gate_evaluation = evaluate_material_usefulness_gate(
        metrics=metrics,
        paired_intervals=bootstrap,
        coverage=coverage,
        protocol=protocol,
    )
    final_cutoff = _timestamp(
        protocol["validation"]["inner"]["final_selection_cutoff_utc"]
    )
    final_inner = construct_inner_folds(
        rows,
        final_cutoff,
        fold_count=protocol["validation"]["inner"]["folds"],
        minimum_training_drafts=minimum["drafts"],
        minimum_per_platform=minimum["per_platform_drafts"],
        minimum_hours=minimum["hours"],
    )
    final_models = {}
    final_selection = {}
    drafts = tuple(row.draft for row in rows)
    for variant in MODEL_VARIANTS_B:
        selected, candidate_rows = select_shared_config(
            variant=variant,
            configs=grids[variant],
            inner_folds=final_inner,
            protocol=protocol,
            fit_counter=fit_counter,
            phase="final_inner_selection",
        )
        model = fit_shared_interaction_model(
            drafts,
            variant=variant,
            config=selected,
            embedding_support_minimum=hyper[
                "embedding_support_minimum_training_matches"
            ],
            seed=hyper["seed"],
            initialization_scope="final_all_development",
            maximum_iterations=hyper["maximum_iterations"],
            tolerance=hyper["optimizer_tolerance"],
        )
        fit_counter.record(
            phase="final_candidate",
            model=variant,
            config_id=selected.config_id,
            optimizer_fit=True,
        )
        final_models[variant] = model
        final_selection[variant] = {
            "selected_config_id": selected.config_id,
            "candidates": candidate_rows,
        }
    expected_accounting = expected_fit_accounting(protocol)
    expected = expected_accounting["expected_predictive_training_operations"]
    if (
        fit_counter.count != expected
        or fit_counter.optimizer_fits
        != expected_accounting["predictive_optimizer_fits"]
        or fit_counter.analytic_operations
        != expected_accounting["analytic_baseline_training_operations"]
    ):
        raise Stage3ValidationError(
            "stage34b_fit_count_conflict", "Stage 3.4B fit count differs"
        )
    artifact = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": "patch26_15_nested_rolling_origin_development_not_holdout",
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": _sha256_json(protocol),
        "fit_accounting": {
            **expected_accounting,
            "observed_predictive_training_operations": fit_counter.count,
            "observed_predictive_optimizer_fits": fit_counter.optimizer_fits,
            "observed_analytic_baseline_training_operations": (
                fit_counter.analytic_operations
            ),
        },
        "outer_fold_counts": {
            fold.fold_id: {
                "training": len(fold.training),
                "test": len(fold.validation),
            }
            for fold in outer_folds
        },
        "outer_selections": dict(selections),
        "final_selection": final_selection,
        "metrics": metrics,
        "paired_bootstrap_intervals": bootstrap,
        "coverage_reconciliation": coverage,
        "material_usefulness_gate": protocol["material_usefulness_gate"],
        "material_usefulness_evaluation": gate_evaluation,
        "future_holdout_gate_passed": any(
            row["passes_all"] for row in gate_evaluation.values()
        ),
        "ready_for_recommendations": False,
        "candidate_contrasts": "mechanical_non_causal_only",
        "privacy": {
            "aggregate_only": True,
            "oof_rows_published": False,
            "match_or_player_identifiers_published": False,
            "external_paths_published": False,
        },
    }
    _reject_nonfinite(artifact)
    return Stage34BEvaluation(
        artifact=artifact, final_candidate_models=final_models
    )


def evaluate_material_usefulness_gate(
    *,
    metrics: dict[str, Any],
    paired_intervals: dict[str, Any],
    coverage: dict[str, Any],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    gate = protocol["material_usefulness_gate"]
    blue = "training_fold_blue_win_rate_intercept"
    composition = "stage3_4a_composition_only_l2_0_1_no_intercept"
    blocks = tuple(
        sorted(set(metrics[composition]) - {"overall", "eun1", "euw1"})
    )
    output = {}
    for candidate in MODEL_VARIANTS_B:
        candidate_metrics = metrics[candidate]
        overall = candidate_metrics["overall"]
        checks = {
            "log_loss_improvement_vs_blue_intercept": (
                metrics[blue]["overall"]["log_loss"] - overall["log_loss"]
                >= gate["log_loss_improvement_vs_blue_intercept_minimum"]
            ),
            "log_loss_improvement_vs_composition": (
                metrics[composition]["overall"]["log_loss"]
                - overall["log_loss"]
                >= gate["log_loss_improvement_vs_composition_minimum"]
            ),
            "paired_log_loss_upper_vs_blue_below_zero": (
                paired_intervals[candidate][blue]["log_loss"]["upper_0_975"]
                < 0
            ),
            "paired_log_loss_upper_vs_composition_below_zero": (
                paired_intervals[candidate][composition]["log_loss"][
                    "upper_0_975"
                ]
                < 0
            ),
            "brier_improvement_vs_blue_intercept": (
                metrics[blue]["overall"]["brier_score"]
                - overall["brier_score"]
                >= gate["overall_brier_improvement_vs_both_baselines_minimum"]
            ),
            "brier_improvement_vs_composition": (
                metrics[composition]["overall"]["brier_score"]
                - overall["brier_score"]
                >= gate["overall_brier_improvement_vs_both_baselines_minimum"]
            ),
            "each_platform_improves_vs_composition": all(
                metrics[composition][platform]["log_loss"]
                - candidate_metrics[platform]["log_loss"]
                >= gate[
                    "each_platform_log_loss_improvement_vs_composition_minimum"
                ]
                for platform in ("eun1", "euw1")
            ),
            "no_platform_worse_than_blue_intercept": all(
                candidate_metrics[platform]["log_loss"]
                <= metrics[blue][platform]["log_loss"]
                for platform in ("eun1", "euw1")
            ),
            "ece": (
                overall["expected_calibration_error"] <= gate["ece_maximum"]
            ),
            "calibration_slope": (
                overall["calibration_slope"] is not None
                and gate["calibration_slope_minimum"]
                <= overall["calibration_slope"]
                <= gate["calibration_slope_maximum"]
            ),
            "calibration_intercept": (
                abs(overall["calibration_intercept"])
                <= gate["calibration_intercept_absolute_maximum"]
            ),
            "coverage": coverage[candidate]["overall"]
            >= gate["coverage_minimum"],
            "role_coverage": (
                coverage[candidate][
                    "maximum_role_coverage_drop_vs_composition"
                ]
                <= gate["role_coverage_maximum_drop"]
            ),
            "chronological_direction_repeats": sum(
                candidate_metrics[block]["log_loss"]
                < metrics[composition][block]["log_loss"]
                for block in blocks
            )
            >= gate["chronological_blocks_with_improvement_minimum"],
        }
        output[candidate] = {
            "checks": checks,
            "passes_all": all(checks.values()),
            "interpretation": "patch26.15_rolling_origin_development_gate_only",
        }
    return output


def write_stage34b_artifact(artifact: dict[str, Any], output_directory: Path) -> Path:
    validate_stage34b_artifact(artifact)
    payload = _json_bytes(artifact)
    target = output_directory / "development_results.json"
    if target.exists():
        if target.read_bytes() == payload:
            return target
        raise Stage3ValidationError(
            "stage34b_immutable_output_conflict", "unequal Stage 3.4B output exists"
        )
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            dir=output_directory.parent, prefix=f".{output_directory.name}."
        )
    )
    try:
        (staging / "development_results.json").write_bytes(payload)
        os.replace(staging, output_directory)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


def validate_stage34b_artifact(artifact: dict[str, Any]) -> None:
    if (
        artifact.get("schema_version") != RESULT_SCHEMA_VERSION
        or artifact.get("protocol_id") != PROTOCOL_ID
        or artifact.get("privacy")
        != {
            "aggregate_only": True,
            "oof_rows_published": False,
            "match_or_player_identifiers_published": False,
            "external_paths_published": False,
        }
    ):
        raise Stage3ValidationError(
            "stage34b_artifact_contract", "Stage 3.4B artifact contract differs"
        )
    stack = [artifact]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if set(value) & FORBIDDEN_PUBLIC_KEYS:
                raise Stage3ValidationError(
                    "stage34b_artifact_identifier",
                    "Stage 3.4B artifact contains a private identifier field",
                )
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    _reject_nonfinite(artifact)


@dataclass(frozen=True)
class _ParameterLayout:
    composition_count: int
    embedding_count: int
    synergy_dimension: int
    counter_dimension: int

    @property
    def composition(self) -> slice:
        return slice(1, 1 + self.composition_count)

    @property
    def synergy(self) -> slice:
        start = self.composition.stop
        return slice(start, start + self.embedding_count * self.synergy_dimension)

    @property
    def counter_attack(self) -> slice:
        start = self.synergy.stop
        return slice(start, start + self.embedding_count * self.counter_dimension)

    @property
    def counter_defense(self) -> slice:
        start = self.counter_attack.stop
        return slice(start, start + self.embedding_count * self.counter_dimension)

    @property
    def total(self) -> int:
        return self.counter_defense.stop

    @property
    def embedding_sections(self) -> tuple[slice, ...]:
        return tuple(
            section
            for section in (self.synergy, self.counter_attack, self.counter_defense)
            if section.stop > section.start
        )


@dataclass(frozen=True)
class _EncodedDrafts:
    outcomes: np.ndarray
    composition: Any
    blue: np.ndarray
    red: np.ndarray


def _encode_drafts(
    drafts: tuple[MatchDraftObservation, ...],
    vocabulary: FeatureVocabulary,
    embedding_lookup: dict[int, int],
) -> _EncodedDrafts:
    from scipy.sparse import csr_matrix

    row_indexes = []
    column_indexes = []
    data = []
    blue = np.full((len(drafts), len(POSITIONS)), -1, dtype=np.int64)
    red = np.full_like(blue, -1)
    for row_index, draft in enumerate(drafts):
        for column, value in vectorize_draft(draft, vocabulary).items():
            row_indexes.append(row_index)
            column_indexes.append(column)
            data.append(value)
        blue[row_index], red[row_index] = _role_feature_indexes(
            draft, vocabulary, embedding_lookup
        )
    return _EncodedDrafts(
        outcomes=np.asarray([row.outcome for row in drafts], dtype=np.float64),
        composition=csr_matrix(
            (data, (row_indexes, column_indexes)),
            shape=(len(drafts), len(vocabulary.keys)),
            dtype=np.float64,
        ),
        blue=blue,
        red=red,
    )


def _shared_scores(
    parameters: np.ndarray, layout: _ParameterLayout, encoded: _EncodedDrafts
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    scores = np.asarray(encoded.composition @ parameters[layout.composition]).ravel()
    scores += parameters[0]
    cache = {}
    if layout.synergy_dimension:
        synergy = parameters[layout.synergy].reshape(
            layout.embedding_count, layout.synergy_dimension
        )
        cache["synergy"] = synergy
        for left in range(len(POSITIONS)):
            for right in range(left + 1, len(POSITIONS)):
                scores += _pair_dot(
                    synergy, encoded.blue[:, left], encoded.blue[:, right]
                )
                scores -= _pair_dot(
                    synergy, encoded.red[:, left], encoded.red[:, right]
                )
    if layout.counter_dimension:
        attack = parameters[layout.counter_attack].reshape(
            layout.embedding_count, layout.counter_dimension
        )
        defense = parameters[layout.counter_defense].reshape(
            layout.embedding_count, layout.counter_dimension
        )
        cache["attack"] = attack
        cache["defense"] = defense
        for role in range(len(POSITIONS)):
            scores += _cross_dot(
                attack, defense, encoded.blue[:, role], encoded.red[:, role]
            )
            scores -= _cross_dot(
                attack, defense, encoded.red[:, role], encoded.blue[:, role]
            )
    return scores, cache


def _embedding_gradient(
    parameters: np.ndarray,
    gradient: np.ndarray,
    errors: np.ndarray,
    layout: _ParameterLayout,
    encoded: _EncodedDrafts,
    cache: dict[str, np.ndarray],
) -> None:
    if layout.synergy_dimension:
        values = cache["synergy"]
        target = gradient[layout.synergy].reshape(values.shape)
        for left in range(len(POSITIONS)):
            for right in range(left + 1, len(POSITIONS)):
                _add_pair_gradient(
                    target,
                    values,
                    encoded.blue[:, left],
                    encoded.blue[:, right],
                    errors,
                    1.0,
                )
                _add_pair_gradient(
                    target,
                    values,
                    encoded.red[:, left],
                    encoded.red[:, right],
                    errors,
                    -1.0,
                )
    if layout.counter_dimension:
        attack = cache["attack"]
        defense = cache["defense"]
        attack_gradient = gradient[layout.counter_attack].reshape(attack.shape)
        defense_gradient = gradient[layout.counter_defense].reshape(defense.shape)
        for role in range(len(POSITIONS)):
            _add_cross_gradient(
                attack_gradient,
                defense_gradient,
                attack,
                defense,
                encoded.blue[:, role],
                encoded.red[:, role],
                errors,
                1.0,
            )
            _add_cross_gradient(
                attack_gradient,
                defense_gradient,
                attack,
                defense,
                encoded.red[:, role],
                encoded.blue[:, role],
                errors,
                -1.0,
            )


def _pair_dot(values: np.ndarray, left: np.ndarray, right: np.ndarray) -> np.ndarray:
    output = np.zeros(len(left), dtype=np.float64)
    valid = (left >= 0) & (right >= 0)
    output[valid] = np.sum(values[left[valid]] * values[right[valid]], axis=1)
    return output


def _cross_dot(
    first: np.ndarray, second: np.ndarray, left: np.ndarray, right: np.ndarray
) -> np.ndarray:
    output = np.zeros(len(left), dtype=np.float64)
    valid = (left >= 0) & (right >= 0)
    output[valid] = np.sum(first[left[valid]] * second[right[valid]], axis=1)
    return output


def _add_pair_gradient(
    target: np.ndarray,
    values: np.ndarray,
    left: np.ndarray,
    right: np.ndarray,
    errors: np.ndarray,
    sign: float,
) -> None:
    valid = (left >= 0) & (right >= 0)
    weights = (sign * errors[valid])[:, None]
    np.add.at(target, left[valid], weights * values[right[valid]])
    np.add.at(target, right[valid], weights * values[left[valid]])


def _add_cross_gradient(
    first_target: np.ndarray,
    second_target: np.ndarray,
    first: np.ndarray,
    second: np.ndarray,
    left: np.ndarray,
    right: np.ndarray,
    errors: np.ndarray,
    sign: float,
) -> None:
    valid = (left >= 0) & (right >= 0)
    weights = (sign * errors[valid])[:, None]
    np.add.at(first_target, left[valid], weights * second[right[valid]])
    np.add.at(second_target, right[valid], weights * first[left[valid]])


def _shared_model_from_parameters(
    variant: str,
    config: SharedModelConfig,
    vocabulary: FeatureVocabulary,
    embedding_indexes: tuple[int, ...],
    layout: _ParameterLayout,
    parameters: np.ndarray,
    iterations: int,
) -> SharedInteractionModel:
    def rows(section: slice, dimension: int) -> tuple[tuple[float, ...], ...]:
        if not dimension:
            return ()
        array = parameters[section].reshape(layout.embedding_count, dimension)
        return tuple(tuple(float(value) for value in row) for row in array)

    return SharedInteractionModel(
        variant=variant,
        config=config,
        vocabulary=vocabulary,
        embedding_feature_indexes=embedding_indexes,
        intercept=float(parameters[0]),
        composition_coefficients=tuple(
            float(value) for value in parameters[layout.composition]
        ),
        synergy_embeddings=rows(layout.synergy, layout.synergy_dimension),
        counter_attack_embeddings=rows(
            layout.counter_attack, layout.counter_dimension
        ),
        counter_defense_embeddings=rows(
            layout.counter_defense, layout.counter_dimension
        ),
        optimizer_iterations=iterations,
        optimizer_status="converged",
    )


def _role_feature_indexes(
    draft: MatchDraftObservation,
    vocabulary: FeatureVocabulary,
    embedding_lookup: dict[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    blue_map = {row.role: row.champion_id for row in draft.allied}
    red_map = {row.role: row.champion_id for row in draft.opposing}

    def indexes(values: dict[str, int]) -> np.ndarray:
        output = []
        for role in POSITIONS:
            feature = vocabulary.index.get(("composition", role, values[role], 0))
            output.append(embedding_lookup.get(feature, -1))
        return np.asarray(output, dtype=np.int64)

    return indexes(blue_map), indexes(red_map)


def _fit_role_rate_baseline(
    drafts: tuple[MatchDraftObservation, ...], *, minimum_support: int
) -> dict[tuple[str, int], tuple[int, int]]:
    counts: dict[tuple[str, int], list[int]] = defaultdict(lambda: [0, 0])
    for draft in drafts:
        for row in draft.allied:
            counts[(row.role, row.champion_id)][0] += draft.outcome
            counts[(row.role, row.champion_id)][1] += 1
        for row in draft.opposing:
            counts[(row.role, row.champion_id)][0] += 1 - draft.outcome
            counts[(row.role, row.champion_id)][1] += 1
    return {
        key: (wins, games)
        for key, (wins, games) in counts.items()
        if games >= minimum_support
    }


def _predict_role_rate(
    draft: MatchDraftObservation,
    statistics: dict[tuple[str, int], tuple[int, int]],
    fallback: float,
) -> float:
    def rate(role: str, champion: int) -> float:
        wins, games = statistics.get((role, champion), (0, 0))
        return wins / games if games else fallback

    blue = mean(rate(row.role, row.champion_id) for row in draft.allied)
    red = mean(rate(row.role, row.champion_id) for row in draft.opposing)
    return min(max(fallback + (blue - red) / 2, 0.0), 1.0)


def _aggregate_metrics(
    records: tuple[_PredictionRow, ...], calibration_bins: int
) -> dict[str, Any]:
    output = {}
    scopes: dict[str, list[_PredictionRow]] = {
        "overall": list(records),
        "eun1": [row for row in records if row.platform == "eun1"],
        "euw1": [row for row in records if row.platform == "euw1"],
    }
    for block in sorted({row.outer_block for row in records}):
        scopes[block] = [row for row in records if row.outer_block == block]
    for policy in ALL_POLICIES:
        output[policy] = {
            scope: _metric_summary(rows, policy, calibration_bins)
            for scope, rows in scopes.items()
        }
    return output


def _metric_summary(
    rows: list[_PredictionRow], policy: str, calibration_bins: int
) -> dict[str, Any]:
    probabilities = [row.probabilities[policy] for row in rows]
    outcomes = [row.outcome for row in rows]
    calibration = _calibration_intercept_slope(probabilities, outcomes)
    return {
        "matches": len(rows),
        "log_loss": mean(
            _binary_log_loss(probability, outcome)
            for probability, outcome in zip(probabilities, outcomes, strict=True)
        ),
        "brier_score": mean(
            (probability - outcome) ** 2
            for probability, outcome in zip(probabilities, outcomes, strict=True)
        ),
        "calibration_intercept": calibration[0],
        "calibration_slope": calibration[1],
        "calibration_slope_undefined_reason": calibration[2],
        "expected_calibration_error": _ece(
            probabilities, outcomes, calibration_bins
        ),
        "coverage": 1.0,
        "prediction_mean": mean(probabilities),
        "prediction_population_standard_deviation": pstdev(probabilities),
        "prediction_minimum": min(probabilities),
        "prediction_maximum": max(probabilities),
    }


def _calibration_intercept_slope(
    probabilities: list[float], outcomes: list[int]
) -> tuple[float, float | None, str | None]:
    logits = np.asarray([_logit(value) for value in probabilities])
    targets = np.asarray(outcomes, dtype=np.float64)
    if np.ptp(logits) <= 1e-15:
        return _logit(float(targets.mean())), None, "constant_prediction_logit"

    def objective(values: np.ndarray) -> tuple[float, np.ndarray]:
        scores = values[0] + values[1] * logits
        probability = _sigmoid_array(scores)
        loss = float(np.mean(np.logaddexp(0.0, scores) - targets * scores))
        errors = probability - targets
        return loss, np.asarray([errors.mean(), np.mean(errors * logits)])

    result = minimize(
        objective,
        np.asarray([0.0, 1.0]),
        method="L-BFGS-B",
        jac=True,
        options={"maxiter": 100, "ftol": 1e-10, "gtol": 1e-10},
    )
    if not result.success or not np.all(np.isfinite(result.x)):
        raise Stage3ValidationError(
            "stage34b_calibration_failure", "calibration metric regression failed"
        )
    return float(result.x[0]), float(result.x[1]), None


def _ece(probabilities: list[float], outcomes: list[int], bins: int) -> float:
    total = len(probabilities)
    value = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        selected = [
            position
            for position, probability in enumerate(probabilities)
            if lower <= probability < upper or (index == bins - 1 and probability == 1)
        ]
        if selected:
            predicted = mean(probabilities[position] for position in selected)
            observed = mean(outcomes[position] for position in selected)
            value += len(selected) / total * abs(predicted - observed)
    return value


def _validate_prediction_records(records: tuple[_PredictionRow, ...]) -> None:
    if not records:
        raise Stage3ValidationError(
            "stage34b_empty_predictions", "Stage 3.4B predictions are empty"
        )
    keys = [row.private_key for row in records]
    if len(keys) != len(set(keys)):
        raise Stage3ValidationError(
            "stage34b_duplicate_prediction", "a test match was predicted twice"
        )
    for row in records:
        if set(row.probabilities) != set(ALL_POLICIES):
            raise Stage3ValidationError(
                "stage34b_paired_prediction_set",
                "paired policies do not share identical test rows",
            )
        if any(
            not math.isfinite(value) or not 0 <= value <= 1
            for value in row.probabilities.values()
        ):
            raise Stage3ValidationError(
                "stage34b_invalid_prediction", "Stage 3.4B prediction is invalid"
            )


def _validate_timed_rows(rows: tuple[TimedDraft, ...]) -> tuple[TimedDraft, ...]:
    keys = [_private_key(row) for row in rows]
    if len(keys) != len(set(keys)):
        raise Stage3ValidationError(
            "stage34b_duplicate_timed_draft", "timed drafts contain duplicates"
        )
    if any(
        row.game_creation.tzinfo is None
        or row.game_creation.utcoffset() != timedelta(0)
        or row.draft.queue_id != 420
        or row.draft.public_patch != "26.15"
        for row in rows
    ):
        raise Stage3ValidationError(
            "stage34b_timed_scope_conflict", "timed draft scope differs"
        )
    for row in rows:
        _validate_draft_structure(row.draft)
    return tuple(
        sorted(rows, key=lambda row: (row.game_creation, *_private_key(row)))
    )


def _validate_draft_structure(draft: MatchDraftObservation) -> None:
    allied_roles = [row.role for row in draft.allied]
    opposing_roles = [row.role for row in draft.opposing]
    champions = [
        row.champion_id for row in (*draft.allied, *draft.opposing)
    ]
    if (
        draft.platform not in {"eun1", "euw1"}
        or draft.queue_id != 420
        or draft.public_patch != "26.15"
        or draft.outcome not in {0, 1}
        or draft.allied_team_id >= draft.opposing_team_id
        or sorted(allied_roles) != sorted(POSITIONS)
        or sorted(opposing_roles) != sorted(POSITIONS)
        or len(champions) != 10
        or len(set(champions)) != 10
        or any(champion <= 0 for champion in champions)
    ):
        raise Stage3ValidationError(
            "stage34b_draft_structure", "Stage 3.4B draft structure differs"
        )


def _validate_chronological_fold(
    training: tuple[TimedDraft, ...],
    validation: tuple[TimedDraft, ...],
    cutoff: datetime,
    *,
    minimum_drafts: int,
    minimum_per_platform: int,
    minimum_hours: int,
) -> None:
    if not training or not validation:
        raise Stage3ValidationError(
            "stage34b_empty_chronological_fold", "chronological fold is empty"
        )
    if max(row.game_creation for row in training) >= min(
        row.game_creation for row in validation
    ):
        raise Stage3ValidationError(
            "stage34b_chronology_leakage", "training does not precede validation"
        )
    if any(row.game_creation >= cutoff for row in training) or any(
        row.game_creation < cutoff for row in validation
    ):
        raise Stage3ValidationError(
            "stage34b_cutoff_conflict", "chronological cutoff was not enforced"
        )
    if len(training) < minimum_drafts:
        raise Stage3ValidationError(
            "stage34b_training_minimum", "chronological training count is too small"
        )
    counts = Counter(row.draft.platform for row in training)
    if any(counts[platform] < minimum_per_platform for platform in ("eun1", "euw1")):
        raise Stage3ValidationError(
            "stage34b_platform_training_minimum",
            "chronological platform training count is too small",
        )
    span = cutoff - min(row.game_creation for row in training)
    if span < timedelta(hours=minimum_hours):
        raise Stage3ValidationError(
            "stage34b_training_span_minimum", "chronological training span is too short"
        )
    train_times = {row.game_creation for row in training}
    validation_times = {row.game_creation for row in validation}
    if train_times & validation_times:
        raise Stage3ValidationError(
            "stage34b_identical_timestamp_split", "identical timestamps cross a fold"
        )


def _candidate_grids(
    protocol: dict[str, Any],
) -> dict[str, tuple[SharedModelConfig, ...]]:
    output = {}
    for variant in MODEL_VARIANTS_B:
        rows = protocol["candidate_models"][variant]["grid"]
        output[variant] = tuple(
            SharedModelConfig(
                config_id=str(row["id"]),
                main_l2=float(row["main_l2"]),
                embedding_l2=float(row["embedding_l2"]),
                synergy_dimension=int(row["synergy_dimension"]),
                counter_dimension=int(row["counter_dimension"]),
            )
            for row in rows
        )
    return output


def _validate_variant_config(variant: str, config: SharedModelConfig) -> None:
    expected = {
        "composition_with_side_intercept": (False, False),
        "shared_allied_synergy": (True, False),
        "shared_lane_counter": (False, True),
        "combined_shared_interactions": (True, True),
    }[variant]
    actual = (config.synergy_dimension > 0, config.counter_dimension > 0)
    if actual != expected or config.main_l2 <= 0 or config.embedding_l2 < 0:
        raise Stage3ValidationError(
            "stage34b_variant_config_conflict", "candidate configuration differs"
        )


def _selection_tie_key(config: SharedModelConfig) -> tuple[Any, ...]:
    return (
        -config.embedding_l2,
        -config.main_l2,
        config.synergy_dimension + config.counter_dimension,
        config.config_id,
    )


def _initialization_seed(
    seed: int, config: SharedModelConfig, scope: str
) -> int:
    payload = f"{seed}|{config.config_id}|{scope}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _private_key(row: TimedDraft) -> tuple[str, str]:
    return row.draft.platform, row.draft.match_id


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise Stage3ValidationError(
            "stage34b_timestamp_invalid", "Stage 3.4B timestamp is invalid"
        ) from error
    return parsed.astimezone(UTC)


def _metric_loss(metric: str, probability: float, outcome: int) -> float:
    if metric == "log_loss":
        return _binary_log_loss(probability, outcome)
    if metric == "brier_score":
        return (probability - outcome) ** 2
    raise ValueError("unknown paired metric")


def _binary_log_loss(probability: float, outcome: int) -> float:
    selected = probability if outcome else 1 - probability
    if selected <= 0 or not math.isfinite(selected):
        raise Stage3ValidationError(
            "stage34b_undefined_log_loss", "Stage 3.4B log loss is undefined"
        )
    return -math.log(selected)


def _sigmoid(value: float) -> float:
    if value >= 0:
        negative = math.exp(-value)
        return 1 / (1 + negative)
    positive = math.exp(value)
    return positive / (1 + positive)


def _sigmoid_array(values: np.ndarray) -> np.ndarray:
    output = np.empty_like(values)
    positive = values >= 0
    output[positive] = 1 / (1 + np.exp(-values[positive]))
    exponent = np.exp(values[~positive])
    output[~positive] = exponent / (1 + exponent)
    return output


def _logit(probability: float) -> float:
    if not 0 < probability < 1 or not math.isfinite(probability):
        raise Stage3ValidationError(
            "stage34b_logit_invalid", "Stage 3.4B probability has undefined logit"
        )
    return math.log(probability / (1 - probability))


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise Stage3ValidationError(
            "stage34b_nonfinite", "Stage 3.4B value is non-finite"
        )
    if isinstance(value, dict):
        for nested in value.values():
            _reject_nonfinite(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_nonfinite(nested)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()
