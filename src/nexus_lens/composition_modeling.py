"""Stage 3.4A offline composition-aware modelling harness.

The harness uses antisymmetric match-level features and deterministic L2 logistic
regression. It is experimental, non-calibrating, and does not produce recommendations.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Literal

import numpy as np
from scipy.optimize import minimize
from scipy.sparse import csr_matrix

from nexus_lens.backtesting import (
    PredictionRecord,
    calculate_metrics,
    evidence_bucket,
    patch_tuple,
)
from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.draft_aggregation import Stage33AInput, load_stage3_3a_input
from nexus_lens.draft_observations import ParticipantDraftObservation

COMPOSITION_SCHEMA_VERSION = "stage3.4a-v1"
COMPOSITION_METRICS_SCHEMA_VERSION = "stage3.4a-metrics-v1"
COMPOSITION_MODEL_SCHEMA_VERSION = "stage3.4a-model-v1"
COMPOSITION_QUALITY_SCHEMA_VERSION = "stage3.4a-quality-v1"
COMPOSITION_POLICY_VERSION = "stage3.4a-experimental-policy-v1"
COMPOSITION_CODE_VERSION = "stage3.4a-engine-v1"
QUEUE_ID = 420
POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
MODEL_VARIANTS = ("composition_only", "composition_plus_lane_matchups")
POLICIES = (
    "naive_0_5",
    "champion_role_baseline",
    "composition_only",
    "composition_plus_lane_matchups",
)
OUTPUT_FILES = (
    "composition_metrics.json",
    "model_artifacts.json",
    "quality_report.json",
    "composition_report.md",
)

FeatureKey = tuple[str, str, int, int]


@dataclass(frozen=True)
class CompositionConfig:
    analysis_region: str
    training_patch: str = "26.14"
    evaluation_patch: str = "26.15"
    l2_grid: tuple[float, ...] = (0.01, 0.1, 1.0)
    composition_only_l2: float | None = None
    composition_plus_lane_matchups_l2: float | None = None
    cv_folds: int = 3
    seed: int = 34_001
    calibration_bins: int = 10
    bootstrap_replicates: int = 200
    bootstrap_seed: int = 34_101
    max_iterations: int = 500
    optimizer_tolerance: float = 1e-9
    max_publication_bytes: int = 5_000_000
    minimum_free_space_reserve_bytes: int = 15_371_520_659


@dataclass(frozen=True)
class ChampionRoleAssignment:
    role: str
    champion_id: int


@dataclass(frozen=True)
class MatchDraftObservation:
    match_id: str
    public_patch: str
    platform: str
    queue_id: int
    allied_team_id: int
    opposing_team_id: int
    allied: tuple[ChampionRoleAssignment, ...]
    opposing: tuple[ChampionRoleAssignment, ...]
    outcome: int


@dataclass(frozen=True)
class DraftCorpus:
    observations: tuple[MatchDraftObservation, ...]
    match_scope: dict[str, tuple[str, str]]
    exclusions: dict[str, str]
    platform: str


@dataclass(frozen=True)
class FeatureVocabulary:
    keys: tuple[FeatureKey, ...]
    index: dict[FeatureKey, int]
    include_lane_matchups: bool


@dataclass(frozen=True)
class FittedCompositionModel:
    variant: str
    vocabulary: FeatureVocabulary
    coefficients: tuple[float, ...]
    feature_training_counts: tuple[int, ...]
    l2_strength: float
    optimizer_iterations: int
    optimizer_status: str
    training_match_set_sha256: str


@dataclass(frozen=True)
class CounterfactualResult:
    model_variant: str
    replaced_side: str
    replaced_role: str
    original_champion_id: int
    candidate_champion_id: int
    original_probability: float
    candidate_probability: float
    probability_difference: float
    unchanged_nine_slots_sha256: str
    interpretation: str = "mechanical_non_causal_not_a_recommendation"


@dataclass(frozen=True)
class StoragePreflight:
    observed_free_bytes: int
    estimated_publication_bytes: int
    minimum_reserve_bytes: int
    projected_free_after_publication_bytes: int
    materially_reduces_collection_headroom: bool


@dataclass
class CompositionDataset:
    run_id: str
    output_directory: Path
    metrics: dict[str, Any]
    model_artifacts: dict[str, Any]
    quality_report: dict[str, Any]
    metadata: dict[str, Any]
    markdown_report: str
    storage_preflight: StoragePreflight


def validate_config(config: CompositionConfig) -> None:
    if not config.analysis_region.strip():
        raise Stage3ValidationError(
            "missing_analysis_region", "analysis region must be explicit"
        )
    if patch_tuple(config.training_patch) >= patch_tuple(config.evaluation_patch):
        raise Stage3ValidationError(
            "non_chronological_patches",
            "training patch must be strictly earlier than evaluation patch",
        )
    if not config.l2_grid or any(
        not math.isfinite(value) or value <= 0 for value in config.l2_grid
    ):
        raise Stage3ValidationError(
            "invalid_l2_grid", "all L2 strengths must be finite and positive"
        )
    if len(set(config.l2_grid)) != len(config.l2_grid):
        raise Stage3ValidationError(
            "duplicate_l2_strength", "L2 strengths must be unique"
        )
    frozen_strengths = (
        config.composition_only_l2,
        config.composition_plus_lane_matchups_l2,
    )
    if (frozen_strengths[0] is None) != (frozen_strengths[1] is None):
        raise Stage3ValidationError(
            "incomplete_frozen_l2",
            "both model strengths must be frozen together or neither",
        )
    if any(
        value is not None and (not math.isfinite(value) or value <= 0)
        for value in frozen_strengths
    ):
        raise Stage3ValidationError(
            "invalid_frozen_l2", "frozen L2 strengths must be finite and positive"
        )
    if config.cv_folds < 2:
        raise Stage3ValidationError(
            "invalid_cv_folds", "at least two folds are required"
        )
    if config.calibration_bins < 1 or config.bootstrap_replicates < 0:
        raise Stage3ValidationError(
            "invalid_metric_configuration", "metric configuration is invalid"
        )
    if (
        config.max_iterations < 1
        or not math.isfinite(config.optimizer_tolerance)
        or config.optimizer_tolerance <= 0
    ):
        raise Stage3ValidationError(
            "invalid_optimizer_configuration", "optimizer settings are invalid"
        )
    if config.max_publication_bytes < 1 or config.minimum_free_space_reserve_bytes < 0:
        raise Stage3ValidationError(
            "invalid_storage_configuration", "storage limits are invalid"
        )


def build_match_draft_corpus(stage33a: Stage33AInput) -> DraftCorpus:
    """Build one deterministic team-100-oriented observation per complete match."""

    match_scope: dict[str, tuple[str, str]] = {}
    platforms = set()
    for match in stage33a.matches:
        if match.match_id in match_scope:
            raise Stage3ValidationError(
                "duplicate_match", "Stage 3.3A contains a duplicate match"
            )
        if match.queue_id != QUEUE_ID:
            raise Stage3ValidationError(
                "non_ranked_solo_input", "Stage 3.4A accepts queue 420 only"
            )
        match_scope[match.match_id] = (match.public_patch, match.platform)
        platforms.add(match.platform)
    if len(platforms) != 1:
        raise Stage3ValidationError(
            "cross_platform_input", "each Stage 3.4A input must contain one platform"
        )
    grouped: dict[str, list[ParticipantDraftObservation]] = defaultdict(list)
    participant_keys = set()
    for row in stage33a.participants:
        key = (row.match_id, row.participant_id)
        if key in participant_keys:
            raise Stage3ValidationError(
                "duplicate_participant", "participant rows cannot multiply a match"
            )
        participant_keys.add(key)
        if row.match_id not in match_scope:
            raise Stage3ValidationError(
                "participant_without_match", "participant match context is missing"
            )
        patch, platform = match_scope[row.match_id]
        if (
            row.public_patch != patch
            or row.platform != platform
            or row.queue_id != QUEUE_ID
        ):
            raise Stage3ValidationError(
                "participant_scope_conflict",
                "participant patch, platform, or queue disagrees with its match",
            )
        grouped[row.match_id].append(row)
    observations = []
    exclusions = {}
    for match_id in sorted(match_scope):
        rows = grouped.get(match_id, [])
        reason = _draft_exclusion_reason(rows)
        if reason is not None:
            exclusions[match_id] = reason
            continue
        teams: dict[int, list[ParticipantDraftObservation]] = defaultdict(list)
        for row in rows:
            teams[row.team_id].append(row)
        team_ids = sorted(teams)
        allied_team_id, opposing_team_id = team_ids
        allied = _assignments(teams[allied_team_id])
        opposing = _assignments(teams[opposing_team_id])
        allied_outcomes = {row.win for row in teams[allied_team_id]}
        opposing_outcomes = {row.win for row in teams[opposing_team_id]}
        if (
            len(allied_outcomes) != 1
            or len(opposing_outcomes) != 1
            or next(iter(allied_outcomes)) == next(iter(opposing_outcomes))
        ):
            raise Stage3ValidationError(
                "team_outcome_conflict", "team outcomes must be consistent and opposite"
            )
        patch, platform = match_scope[match_id]
        observations.append(
            MatchDraftObservation(
                match_id=match_id,
                public_patch=patch,
                platform=platform,
                queue_id=QUEUE_ID,
                allied_team_id=allied_team_id,
                opposing_team_id=opposing_team_id,
                allied=allied,
                opposing=opposing,
                outcome=int(next(iter(allied_outcomes))),
            )
        )
    return DraftCorpus(
        observations=tuple(observations),
        match_scope=match_scope,
        exclusions=exclusions,
        platform=next(iter(platforms)),
    )


def build_feature_vocabulary(
    drafts: Iterable[MatchDraftObservation], *, include_lane_matchups: bool
) -> FeatureVocabulary:
    """Fit the vocabulary from the supplied training drafts only."""

    keys = set()
    for draft in drafts:
        for assignment in (*draft.allied, *draft.opposing):
            keys.add(("composition", assignment.role, assignment.champion_id, 0))
        if include_lane_matchups:
            allied = _role_map(draft.allied)
            opposing = _role_map(draft.opposing)
            for role in POSITIONS:
                low, high = sorted((allied[role], opposing[role]))
                if low != high:
                    keys.add(("lane_matchup", role, low, high))
    ordered = tuple(sorted(keys))
    return FeatureVocabulary(
        keys=ordered,
        index={key: index for index, key in enumerate(ordered)},
        include_lane_matchups=include_lane_matchups,
    )


def vectorize_draft(
    draft: MatchDraftObservation, vocabulary: FeatureVocabulary
) -> dict[int, float]:
    """Return sparse antisymmetric features; unseen keys contribute zero."""

    values: dict[int, float] = defaultdict(float)
    for sign, assignments in ((1.0, draft.allied), (-1.0, draft.opposing)):
        for assignment in assignments:
            key = ("composition", assignment.role, assignment.champion_id, 0)
            index = vocabulary.index.get(key)
            if index is not None:
                values[index] += sign
    if vocabulary.include_lane_matchups:
        allied = _role_map(draft.allied)
        opposing = _role_map(draft.opposing)
        for role in POSITIONS:
            allied_champion = allied[role]
            opposing_champion = opposing[role]
            low, high = sorted((allied_champion, opposing_champion))
            if low == high:
                continue
            index = vocabulary.index.get(("lane_matchup", role, low, high))
            if index is not None:
                values[index] += 1.0 if allied_champion == low else -1.0
    return {index: value for index, value in values.items() if value}


def swap_teams(draft: MatchDraftObservation) -> MatchDraftObservation:
    return MatchDraftObservation(
        match_id=draft.match_id,
        public_patch=draft.public_patch,
        platform=draft.platform,
        queue_id=draft.queue_id,
        allied_team_id=draft.opposing_team_id,
        opposing_team_id=draft.allied_team_id,
        allied=draft.opposing,
        opposing=draft.allied,
        outcome=1 - draft.outcome,
    )


def fit_composition_model(
    drafts: tuple[MatchDraftObservation, ...],
    *,
    variant: str,
    l2_strength: float,
    max_iterations: int,
    tolerance: float,
) -> FittedCompositionModel:
    """Fit deterministic no-intercept L2 logistic regression with SciPy."""

    if variant not in MODEL_VARIANTS:
        raise Stage3ValidationError("unknown_model_variant", "model variant is unknown")
    if not drafts:
        raise Stage3ValidationError("empty_training_data", "training drafts are empty")
    vocabulary = build_feature_vocabulary(
        drafts, include_lane_matchups=variant == "composition_plus_lane_matchups"
    )
    if not vocabulary.keys:
        raise Stage3ValidationError("empty_vocabulary", "training vocabulary is empty")
    matrix = _matrix(drafts, vocabulary)
    outcomes = np.asarray([draft.outcome for draft in drafts], dtype=np.float64)

    def objective(coefficients: np.ndarray) -> tuple[float, np.ndarray]:
        logits = matrix @ coefficients
        loss = float(
            np.mean(np.logaddexp(0.0, logits) - outcomes * logits)
            + 0.5 * l2_strength * np.dot(coefficients, coefficients)
        )
        probabilities = _sigmoid_array(logits)
        gradient = np.asarray(matrix.T @ (probabilities - outcomes)).ravel()
        gradient = gradient / len(outcomes) + l2_strength * coefficients
        return loss, gradient

    result = minimize(
        objective,
        np.zeros(len(vocabulary.keys), dtype=np.float64),
        method="L-BFGS-B",
        jac=True,
        options={"maxiter": max_iterations, "ftol": tolerance, "gtol": tolerance},
    )
    if not result.success or not np.all(np.isfinite(result.x)):
        raise Stage3ValidationError(
            "optimizer_failure", "deterministic logistic optimization did not converge"
        )
    return FittedCompositionModel(
        variant=variant,
        vocabulary=vocabulary,
        coefficients=tuple(float(value) for value in result.x),
        feature_training_counts=tuple(
            int(value) for value in np.asarray(matrix.getnnz(axis=0)).ravel()
        ),
        l2_strength=l2_strength,
        optimizer_iterations=int(result.nit),
        optimizer_status="converged",
        training_match_set_sha256=_hash_string_set(draft.match_id for draft in drafts),
    )


def predict_probability(
    model: FittedCompositionModel, draft: MatchDraftObservation
) -> float:
    sparse = vectorize_draft(draft, model.vocabulary)
    logit = sum(model.coefficients[index] * value for index, value in sparse.items())
    return _sigmoid(logit)


def select_l2_strength(
    drafts: tuple[MatchDraftObservation, ...],
    *,
    variant: str,
    l2_grid: tuple[float, ...],
    cv_folds: int,
    seed: int,
    max_iterations: int,
    tolerance: float,
) -> tuple[float, list[dict[str, Any]]]:
    """Select regularization using match-grouped training-patch folds only."""

    if len(drafts) < cv_folds:
        raise Stage3ValidationError(
            "insufficient_cv_matches", "training matches are fewer than CV folds"
        )
    folds: dict[int, list[MatchDraftObservation]] = defaultdict(list)
    ordered_for_assignment = sorted(
        drafts,
        key=lambda draft: (
            hashlib.sha256(f"{seed}:{draft.match_id}".encode()).digest(),
            draft.match_id,
        ),
    )
    for index, draft in enumerate(ordered_for_assignment):
        folds[index % cv_folds].append(draft)
    results = []
    for strength in sorted(l2_grid):
        losses = []
        validation_count = 0
        vocabulary_hashes = []
        for fold_index in range(cv_folds):
            validation = tuple(sorted(folds[fold_index], key=lambda row: row.match_id))
            training = tuple(
                sorted(
                    (
                        row
                        for index, rows in folds.items()
                        if index != fold_index
                        for row in rows
                    ),
                    key=lambda row: row.match_id,
                )
            )
            model = fit_composition_model(
                training,
                variant=variant,
                l2_strength=strength,
                max_iterations=max_iterations,
                tolerance=tolerance,
            )
            probabilities = [predict_probability(model, row) for row in validation]
            losses.extend(
                _binary_log_loss(probability, row.outcome)
                for probability, row in zip(probabilities, validation, strict=True)
            )
            validation_count += len(validation)
            vocabulary_hashes.append(_hash_feature_keys(model.vocabulary.keys))
        results.append(
            {
                "l2_strength": strength,
                "mean_log_loss": mean(losses),
                "validation_match_count": validation_count,
                "fold_count": cv_folds,
                "fold_vocabulary_sha256": vocabulary_hashes,
                "fit_scope": "training_patch_match_grouped_only",
            }
        )
    best_loss = min(row["mean_log_loss"] for row in results)
    tied = [
        row
        for row in results
        if math.isclose(row["mean_log_loss"], best_loss, rel_tol=0, abs_tol=1e-12)
    ]
    selected = max(row["l2_strength"] for row in tied)
    return selected, results


def fit_champion_role_baseline(
    drafts: tuple[MatchDraftObservation, ...],
) -> dict[tuple[int, str], tuple[int, int]]:
    counts: dict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
    for draft in drafts:
        for assignment in draft.allied:
            bucket = counts[(assignment.champion_id, assignment.role)]
            bucket[0] += draft.outcome
            bucket[1] += 1
        for assignment in draft.opposing:
            bucket = counts[(assignment.champion_id, assignment.role)]
            bucket[0] += 1 - draft.outcome
            bucket[1] += 1
    return {key: (value[0], value[1]) for key, value in counts.items()}


def predict_champion_role_baseline(
    draft: MatchDraftObservation,
    statistics: dict[tuple[int, str], tuple[int, int]],
) -> float:
    def rate(assignment: ChampionRoleAssignment) -> float:
        wins, games = statistics.get((assignment.champion_id, assignment.role), (0, 0))
        return wins / games if games else 0.5

    allied_rate = mean(rate(assignment) for assignment in draft.allied)
    opposing_rate = mean(rate(assignment) for assignment in draft.opposing)
    return 0.5 + (allied_rate - opposing_rate) / 2


def evaluate_counterfactual(
    *,
    model: FittedCompositionModel,
    draft: MatchDraftObservation,
    side: Literal["allied", "opposing"],
    role: str,
    candidate_champion_id: int,
) -> CounterfactualResult:
    """Replace exactly one slot while holding the other nine fixed."""

    if role not in POSITIONS or candidate_champion_id <= 0:
        raise Stage3ValidationError(
            "invalid_counterfactual", "counterfactual role or champion is invalid"
        )
    original_assignments = draft.allied if side == "allied" else draft.opposing
    original = _role_map(original_assignments)[role]
    replacement = tuple(
        ChampionRoleAssignment(
            role=item.role,
            champion_id=(
                candidate_champion_id if item.role == role else item.champion_id
            ),
        )
        for item in original_assignments
    )
    candidate = MatchDraftObservation(
        match_id=draft.match_id,
        public_patch=draft.public_patch,
        platform=draft.platform,
        queue_id=draft.queue_id,
        allied_team_id=draft.allied_team_id,
        opposing_team_id=draft.opposing_team_id,
        allied=replacement if side == "allied" else draft.allied,
        opposing=replacement if side == "opposing" else draft.opposing,
        outcome=draft.outcome,
    )
    unchanged = [
        (slot_side, assignment.role, assignment.champion_id)
        for slot_side, assignments in (
            ("allied", draft.allied),
            ("opposing", draft.opposing),
        )
        for assignment in assignments
        if not (slot_side == side and assignment.role == role)
    ]
    original_probability = predict_probability(model, draft)
    candidate_probability = predict_probability(model, candidate)
    return CounterfactualResult(
        model_variant=model.variant,
        replaced_side=side,
        replaced_role=role,
        original_champion_id=original,
        candidate_champion_id=candidate_champion_id,
        original_probability=original_probability,
        candidate_probability=candidate_probability,
        probability_difference=candidate_probability - original_probability,
        unchanged_nine_slots_sha256=_sha256_json(unchanged),
    )


def build_composition_dataset(
    *,
    stage33a: Stage33AInput,
    output_root: Path,
    config: CompositionConfig,
) -> CompositionDataset:
    validate_config(config)
    corpus = build_match_draft_corpus(stage33a)
    training = tuple(
        row for row in corpus.observations if row.public_patch == config.training_patch
    )
    evaluation = tuple(
        row
        for row in corpus.observations
        if row.public_patch == config.evaluation_patch
    )
    training_ids = {row.match_id for row in training}
    evaluation_ids = {row.match_id for row in evaluation}
    if not training or not evaluation:
        raise Stage3ValidationError(
            "missing_model_fold", "training or evaluation draft fold is empty"
        )
    if training_ids & evaluation_ids:
        raise Stage3ValidationError(
            "match_split_overlap", "a match occurs in training and evaluation"
        )
    available_patches = {patch for patch, _ in corpus.match_scope.values()}
    if (
        config.training_patch not in available_patches
        or config.evaluation_patch not in available_patches
    ):
        raise Stage3ValidationError(
            "configured_patch_missing", "configured patches are absent from input"
        )
    selected_strengths = {}
    cv_results = {}
    models = {}
    for variant in MODEL_VARIANTS:
        frozen = (
            config.composition_only_l2
            if variant == "composition_only"
            else config.composition_plus_lane_matchups_l2
        )
        if frozen is None:
            selected, results = select_l2_strength(
                training,
                variant=variant,
                l2_grid=config.l2_grid,
                cv_folds=config.cv_folds,
                seed=config.seed,
                max_iterations=config.max_iterations,
                tolerance=config.optimizer_tolerance,
            )
        else:
            selected = frozen
            results = [
                {
                    "l2_strength": frozen,
                    "selection_status": "frozen_before_evaluation",
                    "validation_match_count": 0,
                    "fold_count": 0,
                    "fold_vocabulary_sha256": [],
                    "fit_scope": "no_evaluation_tuning",
                }
            ]
        selected_strengths[variant] = selected
        cv_results[variant] = results
        models[variant] = fit_composition_model(
            training,
            variant=variant,
            l2_strength=selected,
            max_iterations=config.max_iterations,
            tolerance=config.optimizer_tolerance,
        )
    baseline = fit_champion_role_baseline(training)
    _validate_model_invariants(training, evaluation, models)
    evaluation_all_ids = {
        match_id
        for match_id, (patch, _) in corpus.match_scope.items()
        if patch == config.evaluation_patch
    }
    policy_results = []
    for policy_index, policy in enumerate(POLICIES):
        records = _prediction_records(
            policy=policy,
            corpus=corpus,
            evaluation=evaluation,
            evaluation_all_ids=evaluation_all_ids,
            baseline=baseline,
            models=models,
            training_count=len(training),
        )
        overall = calculate_metrics(
            records,
            calibration_bins=config.calibration_bins,
            bootstrap_replicates=config.bootstrap_replicates,
            bootstrap_seed=config.bootstrap_seed + policy_index,
        )
        policy_results.append(
            {
                "policy": policy,
                "status": "experimental_non_calibrating_policy_unresolved",
                "overall": overall,
                "slices": _metric_slices(records, config.calibration_bins),
            }
        )
    config_payload = _config_payload(config)
    config_hash = _sha256_json(config_payload)
    run_id = f"{stage33a.run_id}__stage3.4a__{config_hash[:12]}"
    output_directory = (
        output_root / f"schema={COMPOSITION_SCHEMA_VERSION}" / f"run={run_id}"
    )
    metrics = {
        "schema_version": COMPOSITION_METRICS_SCHEMA_VERSION,
        "status": "experimental_non_calibrating_policy_unresolved",
        "production_recommendation_eligible": False,
        "platform": corpus.platform,
        "analysis_region": config.analysis_region,
        "queue_id": QUEUE_ID,
        "training_patch": config.training_patch,
        "evaluation_patch": config.evaluation_patch,
        "training_match_count": len(training),
        "evaluation_match_count": len(evaluation_all_ids),
        "evaluation_eligible_draft_count": len(evaluation),
        "policy_results": policy_results,
    }
    model_artifacts = _model_artifacts(
        corpus=corpus,
        config=config,
        training=training,
        baseline=baseline,
        models=models,
        selected_strengths=selected_strengths,
        cv_results=cv_results,
    )
    quality = _quality_report(
        corpus=corpus,
        config=config,
        training=training,
        evaluation=evaluation,
        evaluation_all_ids=evaluation_all_ids,
        models=models,
    )
    markdown = _markdown_report(metrics, quality, selected_strengths)
    output_hashes = {
        "composition_metrics.json": _sha256_bytes(_json_bytes(metrics)),
        "model_artifacts.json": _sha256_bytes(_json_bytes(model_artifacts)),
        "quality_report.json": _sha256_bytes(_json_bytes(quality)),
        "composition_report.md": _sha256_bytes(markdown.encode("utf-8")),
    }
    metadata = {
        "processing_schema_version": COMPOSITION_SCHEMA_VERSION,
        "metrics_schema_version": COMPOSITION_METRICS_SCHEMA_VERSION,
        "model_schema_version": COMPOSITION_MODEL_SCHEMA_VERSION,
        "quality_report_schema_version": COMPOSITION_QUALITY_SCHEMA_VERSION,
        "policy_version": COMPOSITION_POLICY_VERSION,
        "code_version": COMPOSITION_CODE_VERSION,
        "code_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "run_id": run_id,
        "status": "experimental_non_calibrating_policy_unresolved",
        "source": {
            "stage3_3a_run_id": stage33a.run_id,
            "stage3_3a_directory": stage33a.input_directory.as_posix(),
            "stage3_3a_sha256": dict(sorted(stage33a.lineage_hashes.items())),
            "stage3_1_sha256": dict(sorted(stage33a.stage3_1_hashes.items())),
            "stage3_2_sha256": dict(sorted(stage33a.stage3_2_hashes.items())),
        },
        "parameters": config_payload,
        "output": {"directory": output_directory.as_posix(), "sha256": output_hashes},
    }
    publication_bytes = sum(
        len(payload)
        for payload in (
            _json_bytes(metrics),
            _json_bytes(model_artifacts),
            _json_bytes(quality),
            markdown.encode("utf-8"),
            _json_bytes(metadata),
        )
    )
    storage = _storage_preflight(output_root, publication_bytes, config)
    return CompositionDataset(
        run_id=run_id,
        output_directory=output_directory,
        metrics=metrics,
        model_artifacts=model_artifacts,
        quality_report=quality,
        metadata=metadata,
        markdown_report=markdown,
        storage_preflight=storage,
    )


def run_stage3_4a(
    *,
    input_directory: Path,
    output_root: Path,
    config: CompositionConfig,
    validate_only: bool,
    expected_match_count: int,
) -> CompositionDataset:
    stage33a = load_stage3_3a_input(
        input_directory,
        expected_participant_count=expected_match_count * 10,
        expected_team_count=expected_match_count * 2,
        expected_match_count=expected_match_count,
    )
    dataset = build_composition_dataset(
        stage33a=stage33a, output_root=output_root, config=config
    )
    _validate_existing_output(dataset)
    if not validate_only:
        write_composition_dataset(dataset)
    return dataset


def write_composition_dataset(dataset: CompositionDataset) -> Path:
    if not dataset.quality_report["ready_for_experimental_publication"]:
        raise Stage3ValidationError(
            "composition_not_ready", "failed Stage 3.4A output cannot be published"
        )
    target = dataset.output_directory
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}."))
    try:
        _write_dataset(dataset, staging)
        if target.exists():
            if _directories_equal(staging, target):
                return target
            raise Stage3ValidationError(
                "immutable_output_conflict",
                "unequal immutable Stage 3.4A output exists",
            )
        os.replace(staging, target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _draft_exclusion_reason(rows: list[ParticipantDraftObservation]) -> str | None:
    if len(rows) != 10:
        return "participant_count_not_ten"
    teams = Counter(row.team_id for row in rows)
    if len(teams) != 2 or set(teams.values()) != {5}:
        return "team_structure_not_five_vs_five"
    for team_id in teams:
        team_rows = [row for row in rows if row.team_id == team_id]
        roles = [row.analysis_position for row in team_rows]
        if any(not row.role_analysis_eligibility for row in team_rows):
            return "role_analysis_ineligible"
        if set(roles) != set(POSITIONS) or len(roles) != len(set(roles)):
            return "roles_not_complete_and_unique"
    return None


def _assignments(
    rows: list[ParticipantDraftObservation],
) -> tuple[ChampionRoleAssignment, ...]:
    values = tuple(
        sorted(
            (
                ChampionRoleAssignment(
                    role=str(row.analysis_position), champion_id=row.champion_id
                )
                for row in rows
            ),
            key=lambda item: POSITIONS.index(item.role),
        )
    )
    if tuple(item.role for item in values) != POSITIONS:
        raise Stage3ValidationError("role_ordering_failure", "role ordering failed")
    return values


def _role_map(assignments: tuple[ChampionRoleAssignment, ...]) -> dict[str, int]:
    values = {item.role: item.champion_id for item in assignments}
    if set(values) != set(POSITIONS) or len(values) != len(assignments):
        raise Stage3ValidationError("invalid_draft_roles", "draft roles are incomplete")
    return values


def _matrix(
    drafts: tuple[MatchDraftObservation, ...], vocabulary: FeatureVocabulary
) -> csr_matrix:
    row_indices = []
    column_indices = []
    data = []
    for row_index, draft in enumerate(drafts):
        for column_index, value in vectorize_draft(draft, vocabulary).items():
            row_indices.append(row_index)
            column_indices.append(column_index)
            data.append(value)
    return csr_matrix(
        (data, (row_indices, column_indices)),
        shape=(len(drafts), len(vocabulary.keys)),
        dtype=np.float64,
    )


def _prediction_records(
    *,
    policy: str,
    corpus: DraftCorpus,
    evaluation: tuple[MatchDraftObservation, ...],
    evaluation_all_ids: set[str],
    baseline: dict[tuple[int, str], tuple[int, int]],
    models: dict[str, FittedCompositionModel],
    training_count: int,
) -> list[PredictionRecord]:
    evaluation_by_id = {row.match_id: row for row in evaluation}
    records = []
    for match_id in sorted(evaluation_all_ids):
        draft = evaluation_by_id.get(match_id)
        if draft is None:
            patch, platform = corpus.match_scope[match_id]
            records.append(
                PredictionRecord(
                    policy=policy,
                    match_id=match_id,
                    public_patch=patch,
                    platform=platform,
                    role="MATCH",
                    champion_id=0,
                    outcome=0,
                    probability=None,
                    training_evidence=0,
                    champion_role_training_games=0,
                    missing_reason=corpus.exclusions[match_id],
                )
            )
            continue
        role_counts = [
            baseline.get((item.champion_id, item.role), (0, 0))[1]
            for item in (*draft.allied, *draft.opposing)
        ]
        champion_evidence = min(role_counts)
        if policy == "naive_0_5":
            probability = 0.5
            training_evidence = training_count
        elif policy == "champion_role_baseline":
            probability = predict_champion_role_baseline(draft, baseline)
            training_evidence = champion_evidence
        elif policy in models:
            model = models[policy]
            probability = predict_probability(model, draft)
            training_evidence = _minimum_feature_evidence(draft, model, baseline)
        else:
            raise Stage3ValidationError(
                "unknown_policy", "evaluation policy is unknown"
            )
        records.append(
            PredictionRecord(
                policy=policy,
                match_id=match_id,
                public_patch=draft.public_patch,
                platform=draft.platform,
                role="MATCH",
                champion_id=0,
                outcome=draft.outcome,
                probability=probability,
                training_evidence=training_evidence,
                champion_role_training_games=champion_evidence,
                missing_reason=None,
            )
        )
    return records


def _minimum_feature_evidence(
    draft: MatchDraftObservation,
    model: FittedCompositionModel,
    baseline: dict[tuple[int, str], tuple[int, int]],
) -> int:
    if model.variant == "composition_only":
        counts = [
            baseline.get((item.champion_id, item.role), (0, 0))[1]
            for item in (*draft.allied, *draft.opposing)
        ]
        return min(counts)
    allied = _role_map(draft.allied)
    opposing = _role_map(draft.opposing)
    counts = []
    for role in POSITIONS:
        low, high = sorted((allied[role], opposing[role]))
        index = model.vocabulary.index.get(("lane_matchup", role, low, high))
        counts.append(model.feature_training_counts[index] if index is not None else 0)
    return min(counts)


def _metric_slices(
    records: list[PredictionRecord], calibration_bins: int
) -> dict[str, list[dict[str, Any]]]:
    dimensions: dict[str, dict[str, list[PredictionRecord]]] = {
        "public_patch": defaultdict(list),
        "platform": defaultdict(list),
        "minimum_champion_role_frequency": defaultdict(list),
        "training_evidence": defaultdict(list),
    }
    for row in records:
        dimensions["public_patch"][row.public_patch].append(row)
        dimensions["platform"][row.platform].append(row)
        dimensions["minimum_champion_role_frequency"][
            evidence_bucket(row.champion_role_training_games)
        ].append(row)
        dimensions["training_evidence"][evidence_bucket(row.training_evidence)].append(
            row
        )
    return {
        dimension: [
            {
                "value": value,
                "metrics": calculate_metrics(rows, calibration_bins=calibration_bins),
            }
            for value, rows in sorted(groups.items())
        ]
        for dimension, groups in dimensions.items()
    }


def _validate_model_invariants(
    training: tuple[MatchDraftObservation, ...],
    evaluation: tuple[MatchDraftObservation, ...],
    models: dict[str, FittedCompositionModel],
) -> None:
    training_ids = {row.match_id for row in training}
    if training_ids & {row.match_id for row in evaluation}:
        raise Stage3ValidationError("match_split_overlap", "folds overlap")
    for model in models.values():
        for draft in (*training, *evaluation):
            original = vectorize_draft(draft, model.vocabulary)
            swapped = vectorize_draft(swap_teams(draft), model.vocabulary)
            if original != {index: -value for index, value in swapped.items()}:
                raise Stage3ValidationError(
                    "team_swap_feature_failure", "team swap is not antisymmetric"
                )
            probability = predict_probability(model, draft)
            swapped_probability = predict_probability(model, swap_teams(draft))
            if not math.isclose(
                probability + swapped_probability, 1.0, rel_tol=0, abs_tol=1e-12
            ):
                raise Stage3ValidationError(
                    "team_swap_probability_failure",
                    "team swap probabilities are not complementary",
                )


def _model_artifacts(
    *,
    corpus: DraftCorpus,
    config: CompositionConfig,
    training: tuple[MatchDraftObservation, ...],
    baseline: dict[tuple[int, str], tuple[int, int]],
    models: dict[str, FittedCompositionModel],
    selected_strengths: dict[str, float],
    cv_results: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "schema_version": COMPOSITION_MODEL_SCHEMA_VERSION,
        "status": "experimental_non_calibrating_policy_unresolved",
        "platform": corpus.platform,
        "analysis_region": config.analysis_region,
        "queue_id": QUEUE_ID,
        "training_patch": config.training_patch,
        "training_match_count": len(training),
        "training_match_set_sha256": _hash_string_set(row.match_id for row in training),
        "outcome_feature_in_vocabulary": False,
        "intercept": None,
        "team_encoding": "antisymmetric_allied_plus_one_opposing_minus_one",
        "unseen_feature_behavior": "ignored_zero_coefficient_contribution",
        "champion_role_baseline": [
            {
                "champion_id": champion_id,
                "role": role,
                "wins": wins,
                "games": games,
                "raw_win_rate": wins / games,
            }
            for (champion_id, role), (wins, games) in sorted(baseline.items())
        ],
        "models": [
            {
                "variant": variant,
                "selected_l2_strength": selected_strengths[variant],
                "selection_metric": "match_grouped_training_patch_log_loss",
                "cv_results": cv_results[variant],
                "optimizer": "scipy.optimize.minimize_L-BFGS-B",
                "optimizer_iterations": models[variant].optimizer_iterations,
                "optimizer_status": models[variant].optimizer_status,
                "vocabulary_sha256": _hash_feature_keys(
                    models[variant].vocabulary.keys
                ),
                "training_match_set_sha256": models[variant].training_match_set_sha256,
                "coefficients": [
                    {
                        "feature": _feature_payload(key),
                        "coefficient": coefficient,
                        "training_match_count": models[variant].feature_training_counts[
                            index
                        ],
                    }
                    for index, (key, coefficient) in enumerate(
                        zip(
                            models[variant].vocabulary.keys,
                            models[variant].coefficients,
                            strict=True,
                        )
                    )
                ],
            }
            for variant in MODEL_VARIANTS
        ],
    }


def _quality_report(
    *,
    corpus: DraftCorpus,
    config: CompositionConfig,
    training: tuple[MatchDraftObservation, ...],
    evaluation: tuple[MatchDraftObservation, ...],
    evaluation_all_ids: set[str],
    models: dict[str, FittedCompositionModel],
) -> dict[str, Any]:
    exclusions = Counter(corpus.exclusions.values())
    return {
        "quality_report_schema_version": COMPOSITION_QUALITY_SCHEMA_VERSION,
        "status": "experimental_non_calibrating_policy_unresolved",
        "ready_for_experimental_publication": True,
        "ready_for_policy_selection": False,
        "ready_for_recommendations": False,
        "smoke_test_only": True,
        "statistically_sufficient": False,
        "match_counts": {
            "input": len(corpus.match_scope),
            "eligible": len(corpus.observations),
            "excluded": len(corpus.exclusions),
            "training_eligible": len(training),
            "evaluation_total": len(evaluation_all_ids),
            "evaluation_eligible": len(evaluation),
        },
        "exclusion_reasons": dict(sorted(exclusions.items())),
        "leakage_and_invariance": {
            "training_patch_strictly_earlier": patch_tuple(config.training_patch)
            < patch_tuple(config.evaluation_patch),
            "training_evaluation_match_sets_disjoint": not bool(
                {row.match_id for row in training}
                & {row.match_id for row in evaluation}
            ),
            "hyperparameter_selection_scope": "26.14_match_grouped_only",
            "vocabulary_fit_scope": "training_fold_only",
            "preprocessing": "none",
            "evaluation_outcome_in_feature_construction": False,
            "team_swap_probability_complement_checked": True,
            "role_aware_ordering_checked": True,
            "single_platform": corpus.platform,
            "queue_420_only": True,
            "random_seed": config.seed,
            "bootstrap_seed": config.bootstrap_seed,
        },
        "model_dimensions": {
            variant: len(model.vocabulary.keys) for variant, model in models.items()
        },
        "privacy": {
            "aggregate_or_model_parameter_only": True,
            "prediction_level_artifact": False,
            "raw_identifiers_present": False,
            "player_names_present": False,
            "player_keys_present": False,
            "match_identifiers_present": False,
        },
        "invariant_failures": [],
    }


def _markdown_report(
    metrics: dict[str, Any],
    quality: dict[str, Any],
    selected_strengths: dict[str, float],
) -> str:
    lines = [
        "# Nexus Lens Stage 3.4A composition smoke report",
        "",
        "Status: **experimental, non-calibrating, and policy-unresolved**.",
        "",
        "This match-level development fold does not select a winning model and does "
        "not produce recommendations or causal lane claims.",
        "",
        f"- Platform: `{metrics['platform']}`",
        f"- Analysis region: `{metrics['analysis_region']}`",
        f"- Queue: `{metrics['queue_id']}`",
        f"- Training patch: `{metrics['training_patch']}`",
        f"- Evaluation patch: `{metrics['evaluation_patch']}`",
        f"- Training eligible matches: `{metrics['training_match_count']}`",
        f"- Evaluation matches: `{metrics['evaluation_match_count']}`",
        f"- Evaluation eligible drafts: `{metrics['evaluation_eligible_draft_count']}`",
        "- Prediction clipping: `none`",
        "- Accuracy threshold: `0.5`",
        "",
        "Selected inside 26.14 match-grouped CV only:",
        "",
    ]
    for variant, strength in sorted(selected_strengths.items()):
        lines.append(f"- `{variant}` L2 strength: `{strength}`")
    lines.extend(
        [
            "",
            "| Policy | Coverage | Log loss | Brier | Accuracy | ECE |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for result in metrics["policy_results"]:
        overall = result["overall"]
        lines.append(
            (
                "| {policy} | {coverage} | {log_loss} | {brier} | {accuracy} | {ece} |"
            ).format(
                policy=result["policy"],
                coverage=_format_metric(overall["coverage"]),
                log_loss=_format_metric(overall["log_loss"]),
                brier=_format_metric(overall["brier_score"]),
                accuracy=_format_metric(overall["accuracy_at_0_5"]),
                ece=_format_metric(overall["expected_calibration_error"]),
            )
        )
    lines.extend(
        [
            "",
            "Team-swap complementarity and role-attached ordering invariance passed. "
            "All feature vocabularies and regularization choices were fitted using "
            "training-patch folds only.",
            "",
            f"Ready for policy selection: `{quality['ready_for_policy_selection']}`.",
            f"Ready for recommendations: `{quality['ready_for_recommendations']}`.",
            "",
        ]
    )
    return "\n".join(lines)


def _config_payload(config: CompositionConfig) -> dict[str, Any]:
    return {
        "analysis_region": config.analysis_region,
        "training_patch": config.training_patch,
        "evaluation_patch": config.evaluation_patch,
        "l2_grid": list(config.l2_grid),
        "frozen_l2_strengths": {
            "composition_only": config.composition_only_l2,
            "composition_plus_lane_matchups": (
                config.composition_plus_lane_matchups_l2
            ),
        },
        "cv_folds": config.cv_folds,
        "seed": config.seed,
        "calibration_bins": config.calibration_bins,
        "bootstrap_replicates": config.bootstrap_replicates,
        "bootstrap_seed": config.bootstrap_seed,
        "max_iterations": config.max_iterations,
        "optimizer_tolerance": config.optimizer_tolerance,
        "prediction_clipping": None,
        "max_publication_bytes": config.max_publication_bytes,
        "minimum_free_space_reserve_bytes": config.minimum_free_space_reserve_bytes,
        "split_unit": "match",
        "feature_fit_scope": "training_only",
        "hyperparameter_fit_scope": "training_patch_grouped_cv_only",
        "policy_status": "unresolved",
    }


def _storage_preflight(
    output_root: Path, publication_bytes: int, config: CompositionConfig
) -> StoragePreflight:
    if publication_bytes > config.max_publication_bytes:
        raise Stage3ValidationError(
            "publication_too_large", "Stage 3.4A publication exceeds its explicit cap"
        )
    probe = output_root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    projected = free - publication_bytes
    materially_reduces = publication_bytes > max(int(free * 0.001), 1_000_000)
    if projected < config.minimum_free_space_reserve_bytes or materially_reduces:
        raise Stage3ValidationError(
            "storage_reserve_failure",
            "publication would violate collection storage headroom",
        )
    return StoragePreflight(
        observed_free_bytes=free,
        estimated_publication_bytes=publication_bytes,
        minimum_reserve_bytes=config.minimum_free_space_reserve_bytes,
        projected_free_after_publication_bytes=projected,
        materially_reduces_collection_headroom=materially_reduces,
    )


def _feature_payload(key: FeatureKey) -> dict[str, Any]:
    kind, role, first, second = key
    if kind == "composition":
        return {"kind": kind, "role": role, "champion_id": first}
    return {
        "kind": kind,
        "role": role,
        "lower_champion_id": first,
        "higher_champion_id": second,
        "sign": "positive_when_allied_is_lower_id",
    }


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


def _binary_log_loss(probability: float, outcome: int) -> float:
    selected = probability if outcome else 1 - probability
    if selected <= 0 or not math.isfinite(selected):
        raise Stage3ValidationError("nonfinite_log_loss", "model log loss is invalid")
    return -math.log(selected)


def _hash_feature_keys(keys: Iterable[FeatureKey]) -> str:
    return _sha256_json([list(key) for key in keys])


def _hash_string_set(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        digest.update(value.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _format_metric(value: float | None) -> str:
    return "null" if value is None else f"{value:.6f}"


def _validate_existing_output(dataset: CompositionDataset) -> None:
    target = dataset.output_directory
    if not target.exists():
        return
    metadata_path = target / "metadata.json"
    if not metadata_path.is_file():
        raise Stage3ValidationError(
            "immutable_output_incomplete", "existing Stage 3.4A output is incomplete"
        )
    try:
        existing = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stage3ValidationError(
            "immutable_output_invalid", "existing Stage 3.4A metadata is invalid"
        ) from exc
    if existing != dataset.metadata:
        raise Stage3ValidationError(
            "immutable_output_lineage_conflict",
            "existing Stage 3.4A metadata disagrees with current lineage",
        )


def _write_dataset(dataset: CompositionDataset, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "composition_metrics.json").write_bytes(_json_bytes(dataset.metrics))
    (directory / "model_artifacts.json").write_bytes(
        _json_bytes(dataset.model_artifacts)
    )
    (directory / "quality_report.json").write_bytes(_json_bytes(dataset.quality_report))
    (directory / "composition_report.md").write_text(
        dataset.markdown_report, encoding="utf-8", newline="\n"
    )
    (directory / "metadata.json").write_bytes(_json_bytes(dataset.metadata))


def _directories_equal(left: Path, right: Path) -> bool:
    left_files = sorted(
        path.relative_to(left) for path in left.rglob("*") if path.is_file()
    )
    right_files = sorted(
        path.relative_to(right) for path in right.rglob("*") if path.is_file()
    )
    return left_files == right_files and all(
        (left / relative).read_bytes() == (right / relative).read_bytes()
        for relative in left_files
    )


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
