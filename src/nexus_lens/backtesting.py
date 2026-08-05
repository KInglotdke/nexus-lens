"""Offline, patch-forward Stage 3.3C policy backtesting.

This module intentionally implements evaluation mechanics, not an approved model.
Every fitted statistic is derived from strictly earlier public patches and every
published result remains experimental and policy-unresolved.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.draft_aggregation import Stage33AInput, load_stage3_3a_input
from nexus_lens.draft_observations import ParticipantDraftObservation

BACKTEST_SCHEMA_VERSION = "stage3.3c-v1"
BACKTEST_METRICS_SCHEMA_VERSION = "stage3.3c-metrics-v1"
BACKTEST_QUALITY_SCHEMA_VERSION = "stage3.3c-quality-v1"
BACKTEST_POLICY_VERSION = "stage3.3c-reference-policy-v1"
BACKTEST_CODE_VERSION = "stage3.3c-engine-v1"
QUEUE_ID = 420
OUTPUT_FILES = ("backtest_metrics.json", "backtest_report.md", "quality_report.json")
POLICIES = (
    "naive_empirical",
    "champion_role_baseline",
    "shrunk_directional_matchup",
    "shrunk_directional_synergy",
    "matchup_synergy_combination",
)


@dataclass(frozen=True)
class BacktestConfig:
    """Explicit, non-production evaluation parameters."""

    analysis_region: str
    evaluation_patches: tuple[str, ...]
    policies: tuple[str, ...] = POLICIES
    prior_equivalent_games: float = 10.0
    calibration_bins: int = 10
    clip_min: float | None = None
    clip_max: float | None = None
    bootstrap_replicates: int = 0
    bootstrap_seed: int = 33_003


@dataclass(frozen=True)
class FoldSplit:
    evaluation_patch: str
    training_patches: tuple[str, ...]
    training_match_ids: frozenset[str]
    evaluation_match_ids: frozenset[str]


@dataclass(frozen=True)
class PredictionRecord:
    policy: str
    match_id: str
    public_patch: str
    platform: str
    role: str
    champion_id: int
    outcome: int
    probability: float | None
    training_evidence: int
    champion_role_training_games: int
    missing_reason: str | None


@dataclass(frozen=True)
class _FittedPolicies:
    global_wins: int
    global_games: int
    role_stats: dict[tuple[int, str], tuple[int, int]]
    matchup_stats: dict[tuple[int, str, int, str], tuple[int, int]]
    synergy_stats: dict[tuple[int, str, int, str], tuple[int, int]]
    training_match_ids: frozenset[str]

    @property
    def global_probability(self) -> float:
        return self.global_wins / self.global_games


@dataclass
class BacktestDataset:
    run_id: str
    output_directory: Path
    metrics: dict[str, Any]
    quality_report: dict[str, Any]
    metadata: dict[str, Any]
    markdown_report: str


def patch_tuple(public_patch: str) -> tuple[int, int]:
    """Parse a public ``major.minor`` patch without accepting loose variants."""

    parts = public_patch.split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise Stage3ValidationError(
            "invalid_public_patch", "public patches must be numeric major.minor"
        )
    return int(parts[0]), int(parts[1])


def validate_config(config: BacktestConfig) -> None:
    if not config.analysis_region.strip():
        raise Stage3ValidationError(
            "missing_analysis_region", "analysis region must be explicit"
        )
    if not config.evaluation_patches:
        raise Stage3ValidationError(
            "missing_evaluation_patch", "at least one evaluation patch is required"
        )
    if len(set(config.evaluation_patches)) != len(config.evaluation_patches):
        raise Stage3ValidationError(
            "duplicate_evaluation_patch", "evaluation patches must be unique"
        )
    for patch in config.evaluation_patches:
        patch_tuple(patch)
    unknown = set(config.policies) - set(POLICIES)
    if (
        unknown
        or not config.policies
        or len(set(config.policies)) != len(config.policies)
    ):
        raise Stage3ValidationError(
            "invalid_reference_policy", "reference policies must be unique and known"
        )
    if (
        not math.isfinite(config.prior_equivalent_games)
        or config.prior_equivalent_games <= 0
    ):
        raise Stage3ValidationError(
            "invalid_prior_strength", "experimental prior strength must be positive"
        )
    if config.calibration_bins < 1:
        raise Stage3ValidationError(
            "invalid_calibration_bins", "calibration bin count must be positive"
        )
    if (config.clip_min is None) != (config.clip_max is None):
        raise Stage3ValidationError(
            "incomplete_clipping", "both clipping boundaries or neither are required"
        )
    if (
        config.clip_min is not None
        and config.clip_max is not None
        and (
            not math.isfinite(config.clip_min)
            or not math.isfinite(config.clip_max)
            or not 0 < config.clip_min < config.clip_max < 1
        )
    ):
        raise Stage3ValidationError(
            "invalid_clipping", "clipping boundaries must satisfy 0 < min < max < 1"
        )
    if config.bootstrap_replicates < 0:
        raise Stage3ValidationError(
            "invalid_bootstrap", "bootstrap replicates must be nonnegative"
        )


def build_rolling_origin_splits(
    stage33a: Stage33AInput, evaluation_patches: Iterable[str]
) -> list[FoldSplit]:
    """Create match-level folds whose training patches are strictly earlier."""

    patch_by_match: dict[str, str] = {}
    for match in stage33a.matches:
        if match.match_id in patch_by_match:
            raise Stage3ValidationError(
                "duplicate_match", "a match occurs more than once in Stage 3.3A"
            )
        patch_by_match[match.match_id] = match.public_patch
    available = set(patch_by_match.values())
    folds: list[FoldSplit] = []
    for evaluation_patch in evaluation_patches:
        evaluation_key = patch_tuple(evaluation_patch)
        if evaluation_patch not in available:
            raise Stage3ValidationError(
                "evaluation_patch_missing", "evaluation patch is absent from the input"
            )
        training_patches = tuple(
            sorted(
                (patch for patch in available if patch_tuple(patch) < evaluation_key),
                key=patch_tuple,
            )
        )
        if not training_patches:
            raise Stage3ValidationError(
                "training_window_empty",
                "an evaluation patch requires at least one strictly earlier patch",
            )
        training_ids = frozenset(
            match_id
            for match_id, patch in patch_by_match.items()
            if patch in training_patches
        )
        evaluation_ids = frozenset(
            match_id
            for match_id, patch in patch_by_match.items()
            if patch == evaluation_patch
        )
        if training_ids & evaluation_ids:
            raise Stage3ValidationError(
                "match_split_overlap", "a match cannot occur in both sides of a fold"
            )
        folds.append(
            FoldSplit(
                evaluation_patch=evaluation_patch,
                training_patches=training_patches,
                training_match_ids=training_ids,
                evaluation_match_ids=evaluation_ids,
            )
        )
    return folds


def fit_reference_policies(
    participants: list[ParticipantDraftObservation], split: FoldSplit
) -> _FittedPolicies:
    """Fit sufficient statistics from training matches only."""

    participant_lookup = {
        (row.match_id, row.participant_id): row for row in participants
    }
    role_counts: dict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
    matchup_counts: dict[tuple[int, str, int, str], list[int]] = defaultdict(
        lambda: [0, 0]
    )
    synergy_counts: dict[tuple[int, str, int, str], list[int]] = defaultdict(
        lambda: [0, 0]
    )
    team_members = _team_members(participants)
    global_wins = 0
    global_games = 0
    for row in participants:
        if row.match_id not in split.training_match_ids:
            continue
        if row.public_patch not in split.training_patches:
            raise Stage3ValidationError(
                "training_patch_conflict", "training rows disagree with fold patches"
            )
        if not row.role_analysis_eligibility or row.analysis_position is None:
            continue
        outcome = int(row.win)
        global_wins += outcome
        global_games += 1
        role_key = (row.champion_id, row.analysis_position)
        _increment(role_counts[role_key], outcome)
        opponent = _resolved_opponent(row, participant_lookup)
        if row.matchup_eligibility and opponent is not None:
            matchup_key = (
                row.champion_id,
                row.analysis_position,
                opponent.champion_id,
                opponent.analysis_position,
            )
            _increment(matchup_counts[matchup_key], outcome)
        if row.synergy_eligibility:
            for ally in _eligible_allies(row, team_members):
                synergy_key = (
                    row.champion_id,
                    row.analysis_position,
                    ally.champion_id,
                    ally.analysis_position,
                )
                _increment(synergy_counts[synergy_key], outcome)
    if global_games == 0:
        raise Stage3ValidationError(
            "no_training_observations", "no role-eligible training observations exist"
        )
    return _FittedPolicies(
        global_wins=global_wins,
        global_games=global_games,
        role_stats=_freeze_counts(role_counts),
        matchup_stats=_freeze_counts(matchup_counts),
        synergy_stats=_freeze_counts(synergy_counts),
        training_match_ids=split.training_match_ids,
    )


def predict_reference_policy(
    *,
    policy: str,
    fitted: _FittedPolicies,
    row: ParticipantDraftObservation,
    participant_lookup: dict[tuple[str, int], ParticipantDraftObservation],
    team_members: dict[tuple[str, int], list[ParticipantDraftObservation]],
    prior_equivalent_games: float,
) -> tuple[float | None, int, str | None]:
    """Predict without reading the evaluation outcome."""

    if row.match_id in fitted.training_match_ids:
        raise Stage3ValidationError(
            "evaluation_match_in_training", "evaluation match appeared during fitting"
        )
    if not row.role_analysis_eligibility or row.analysis_position is None:
        return None, 0, "role_ineligible"
    global_probability = fitted.global_probability
    role_key = (row.champion_id, row.analysis_position)
    role_stat = fitted.role_stats.get(role_key)
    if policy == "naive_empirical":
        return global_probability, fitted.global_games, None
    if policy == "champion_role_baseline":
        if role_stat is None:
            return None, 0, "unseen_champion_role"
        return role_stat[0] / role_stat[1], role_stat[1], None
    baseline = (
        role_stat[0] / role_stat[1] if role_stat is not None else global_probability
    )
    if policy in {"shrunk_directional_matchup", "matchup_synergy_combination"}:
        matchup = _matchup_prediction(
            row,
            participant_lookup,
            fitted,
            baseline,
            prior_equivalent_games,
        )
        if policy == "shrunk_directional_matchup":
            return matchup
    else:
        matchup = (None, 0, "not_requested")
    synergy = _synergy_prediction(
        row, team_members, fitted, baseline, prior_equivalent_games
    )
    if policy == "shrunk_directional_synergy":
        return synergy
    if policy == "matchup_synergy_combination":
        if matchup[0] is None:
            return None, matchup[1], matchup[2]
        if synergy[0] is None:
            return None, synergy[1], synergy[2]
        return (
            (matchup[0] + synergy[0]) / 2,
            matchup[1] + synergy[1],
            None,
        )
    raise Stage3ValidationError("unknown_policy", "reference policy is unknown")


def calculate_metrics(
    records: list[PredictionRecord],
    *,
    calibration_bins: int,
    bootstrap_replicates: int = 0,
    bootstrap_seed: int = 33_003,
) -> dict[str, Any]:
    """Calculate finite aggregate metrics and match-cluster bootstrap intervals."""

    if calibration_bins < 1:
        raise ValueError("calibration_bins must be positive")
    evaluated = [record for record in records if record.probability is not None]
    probabilities = [record.probability for record in evaluated]
    if any(
        probability is None
        or not math.isfinite(probability)
        or not 0 <= probability <= 1
        for probability in probabilities
    ):
        raise ValueError("predictions must be finite probabilities")
    outcomes = [record.outcome for record in evaluated]
    if any(outcome not in {0, 1} for outcome in outcomes):
        raise ValueError("outcomes must be binary")
    brier = _mean_or_none(
        [
            (float(probability) - outcome) ** 2
            for probability, outcome in zip(probabilities, outcomes, strict=True)
        ]
    )
    log_losses: list[float] = []
    log_loss_reason = None
    for probability, outcome in zip(probabilities, outcomes, strict=True):
        probability = float(probability)
        selected = probability if outcome else 1 - probability
        if selected <= 0:
            log_loss_reason = "undefined_without_explicit_clipping"
            log_losses = []
            break
        log_losses.append(-math.log(selected))
    accuracy = _mean_or_none(
        [
            float((float(probability) >= 0.5) == bool(outcome))
            for probability, outcome in zip(probabilities, outcomes, strict=True)
        ]
    )
    bins = calibration_table(evaluated, calibration_bins)
    ece = (
        sum(
            item["count"]
            / len(evaluated)
            * abs(item["mean_probability"] - item["observed_rate"])
            for item in bins
            if item["count"]
        )
        if evaluated
        else None
    )
    result: dict[str, Any] = {
        "candidate_rows": len(records),
        "evaluated_rows": len(evaluated),
        "abstained_rows": len(records) - len(evaluated),
        "coverage": len(evaluated) / len(records) if records else None,
        "abstention_rate": (
            (len(records) - len(evaluated)) / len(records) if records else None
        ),
        "log_loss": _mean_or_none(log_losses),
        "log_loss_undefined_reason": log_loss_reason,
        "brier_score": brier,
        "accuracy_at_0_5": accuracy,
        "expected_calibration_error": ece,
        "calibration_bins": bins,
        "missing_reasons": dict(
            sorted(
                Counter(
                    row.missing_reason for row in records if row.missing_reason
                ).items()
            )
        ),
        "confidence_intervals": {},
    }
    if bootstrap_replicates:
        result["confidence_intervals"] = _cluster_bootstrap(
            evaluated,
            replicates=bootstrap_replicates,
            seed=bootstrap_seed,
        )
    _reject_nonfinite(result)
    return result


def calibration_table(
    records: list[PredictionRecord], bin_count: int
) -> list[dict[str, Any]]:
    """Create deterministic equal-width bins, including probability one in the last."""

    buckets: list[list[PredictionRecord]] = [[] for _ in range(bin_count)]
    for record in records:
        if record.probability is None:
            continue
        index = min(int(record.probability * bin_count), bin_count - 1)
        buckets[index].append(record)
    output = []
    for index, bucket in enumerate(buckets):
        output.append(
            {
                "bin_index": index,
                "lower_inclusive": index / bin_count,
                "upper_inclusive": (index + 1) / bin_count
                if index == bin_count - 1
                else None,
                "upper_exclusive": (index + 1) / bin_count
                if index < bin_count - 1
                else None,
                "count": len(bucket),
                "mean_probability": _mean_or_none(
                    [float(row.probability) for row in bucket]
                ),
                "observed_rate": _mean_or_none([float(row.outcome) for row in bucket]),
            }
        )
    return output


def build_backtest_dataset(
    *,
    stage33a: Stage33AInput,
    output_root: Path,
    config: BacktestConfig,
) -> BacktestDataset:
    """Build deterministic folds, predictions, and aggregate-only reports."""

    validate_config(config)
    platform = _validate_stage33a(stage33a)
    splits = build_rolling_origin_splits(stage33a, config.evaluation_patches)
    config_payload = _config_payload(config)
    config_hash = _sha256_json(config_payload)
    run_id = f"{stage33a.run_id}__stage3.3c__{config_hash[:12]}"
    output_directory = (
        output_root / f"schema={BACKTEST_SCHEMA_VERSION}" / f"run={run_id}"
    )
    participant_lookup = {
        (row.match_id, row.participant_id): row for row in stage33a.participants
    }
    team_members = _team_members(stage33a.participants)
    fold_results = []
    leakage_checks = []
    for fold_index, split in enumerate(splits):
        fitted = fit_reference_policies(stage33a.participants, split)
        evaluation_rows = sorted(
            (
                row
                for row in stage33a.participants
                if row.match_id in split.evaluation_match_ids
            ),
            key=lambda row: (row.match_id, row.participant_id),
        )
        policy_results = []
        for policy_index, policy in enumerate(config.policies):
            predictions = _prediction_records(
                policy=policy,
                fitted=fitted,
                evaluation_rows=evaluation_rows,
                participant_lookup=participant_lookup,
                team_members=team_members,
                config=config,
            )
            overall = calculate_metrics(
                predictions,
                calibration_bins=config.calibration_bins,
                bootstrap_replicates=config.bootstrap_replicates,
                bootstrap_seed=config.bootstrap_seed + fold_index * 100 + policy_index,
            )
            slices = _metric_slices(predictions, config.calibration_bins)
            policy_results.append(
                {
                    "policy": policy,
                    "status": "experimental_reference_policy_unresolved",
                    "overall": overall,
                    "slices": slices,
                }
            )
        fold_results.append(
            {
                "evaluation_patch": split.evaluation_patch,
                "training_patches": list(split.training_patches),
                "training_match_count": len(split.training_match_ids),
                "evaluation_match_count": len(split.evaluation_match_ids),
                "training_match_set_sha256": _hash_string_set(split.training_match_ids),
                "evaluation_match_set_sha256": _hash_string_set(
                    split.evaluation_match_ids
                ),
                "policy_results": policy_results,
            }
        )
        leakage_checks.append(
            {
                "evaluation_patch": split.evaluation_patch,
                "training_precedes_evaluation": all(
                    patch_tuple(patch) < patch_tuple(split.evaluation_patch)
                    for patch in split.training_patches
                ),
                "match_sets_disjoint": not bool(
                    split.training_match_ids & split.evaluation_match_ids
                ),
                "fit_source_is_training_match_set": (
                    fitted.training_match_ids == split.training_match_ids
                ),
                "platform_isolated": True,
                "queue_420_only": True,
            }
        )
    if not all(
        all(value for key, value in check.items() if key != "evaluation_patch")
        for check in leakage_checks
    ):
        raise Stage3ValidationError(
            "leakage_check_failed", "a leakage invariant failed"
        )
    metrics = {
        "schema_version": BACKTEST_METRICS_SCHEMA_VERSION,
        "policy_contract_version": BACKTEST_POLICY_VERSION,
        "status": "experimental_smoke_test_policy_unresolved",
        "production_recommendation_eligible": False,
        "platform": platform,
        "analysis_region": config.analysis_region,
        "queue_id": QUEUE_ID,
        "folds": fold_results,
    }
    quality = {
        "quality_report_schema_version": BACKTEST_QUALITY_SCHEMA_VERSION,
        "ready_for_experimental_publication": True,
        "ready_for_policy_selection": False,
        "ready_for_recommendations": False,
        "smoke_test_only": True,
        "statistically_sufficient": False,
        "leakage_checks": leakage_checks,
        "invariant_failures": [],
        "privacy": {
            "aggregate_only": True,
            "raw_identifiers_present": False,
            "player_keys_present": False,
            "match_identifiers_present": False,
        },
    }
    markdown = _markdown_report(metrics, quality, config)
    output_hashes = {
        "backtest_metrics.json": _sha256_bytes(_json_bytes(metrics)),
        "backtest_report.md": _sha256_bytes(markdown.encode("utf-8")),
        "quality_report.json": _sha256_bytes(_json_bytes(quality)),
    }
    metadata = {
        "processing_schema_version": BACKTEST_SCHEMA_VERSION,
        "metrics_schema_version": BACKTEST_METRICS_SCHEMA_VERSION,
        "quality_report_schema_version": BACKTEST_QUALITY_SCHEMA_VERSION,
        "policy_contract_version": BACKTEST_POLICY_VERSION,
        "code_version": BACKTEST_CODE_VERSION,
        "code_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "run_id": run_id,
        "status": "experimental_smoke_test_policy_unresolved",
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
    return BacktestDataset(
        run_id=run_id,
        output_directory=output_directory,
        metrics=metrics,
        quality_report=quality,
        metadata=metadata,
        markdown_report=markdown,
    )


def run_stage3_3c(
    *,
    input_directory: Path,
    output_root: Path,
    config: BacktestConfig,
    validate_only: bool,
    expected_match_count: int,
) -> BacktestDataset:
    """Load verified Stage 3.3A lineage and optionally publish Stage 3.3C."""

    stage33a = load_stage3_3a_input(
        input_directory,
        expected_participant_count=expected_match_count * 10,
        expected_team_count=expected_match_count * 2,
        expected_match_count=expected_match_count,
    )
    dataset = build_backtest_dataset(
        stage33a=stage33a, output_root=output_root, config=config
    )
    _validate_existing_output(dataset)
    if not validate_only:
        write_backtest_dataset(dataset)
    return dataset


def write_backtest_dataset(dataset: BacktestDataset) -> Path:
    """Atomically publish once; an unequal existing run is immutable."""

    if not dataset.quality_report["ready_for_experimental_publication"]:
        raise Stage3ValidationError(
            "backtest_not_ready", "failed Stage 3.3C output cannot be published"
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
                "an unequal immutable Stage 3.3C run already exists",
            )
        os.replace(staging, target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _validate_stage33a(stage33a: Stage33AInput) -> str:
    match_ids = {row.match_id for row in stage33a.matches}
    if len(match_ids) != len(stage33a.matches):
        raise Stage3ValidationError("duplicate_match", "Stage 3.3A matches repeat")
    platforms = {row.platform for row in stage33a.matches}
    queues = {row.queue_id for row in stage33a.matches}
    if len(platforms) != 1:
        raise Stage3ValidationError(
            "cross_platform_input", "each backtest input must contain one platform"
        )
    if queues != {QUEUE_ID}:
        raise Stage3ValidationError(
            "non_ranked_solo_input", "Stage 3.3C accepts queue 420 only"
        )
    participant_keys = set()
    patch_by_match = {row.match_id: row.public_patch for row in stage33a.matches}
    for row in stage33a.participants:
        key = (row.match_id, row.participant_id)
        if key in participant_keys:
            raise Stage3ValidationError(
                "duplicate_participant", "participant keys must be unique"
            )
        participant_keys.add(key)
        if (
            row.match_id not in match_ids
            or patch_by_match[row.match_id] != row.public_patch
        ):
            raise Stage3ValidationError(
                "participant_match_split_conflict",
                "participant patch assignment disagrees with its match",
            )
        if row.platform not in platforms or row.queue_id != QUEUE_ID:
            raise Stage3ValidationError(
                "participant_scope_conflict", "participant scope disagrees with match"
            )
    return next(iter(platforms))


def _prediction_records(
    *,
    policy: str,
    fitted: _FittedPolicies,
    evaluation_rows: list[ParticipantDraftObservation],
    participant_lookup: dict[tuple[str, int], ParticipantDraftObservation],
    team_members: dict[tuple[str, int], list[ParticipantDraftObservation]],
    config: BacktestConfig,
) -> list[PredictionRecord]:
    records = []
    for row in evaluation_rows:
        probability, evidence, missing = predict_reference_policy(
            policy=policy,
            fitted=fitted,
            row=row,
            participant_lookup=participant_lookup,
            team_members=team_members,
            prior_equivalent_games=config.prior_equivalent_games,
        )
        if probability is not None and config.clip_min is not None:
            probability = min(max(probability, config.clip_min), config.clip_max)  # type: ignore[arg-type]
        role = row.analysis_position or "UNRESOLVED"
        role_stat = fitted.role_stats.get((row.champion_id, role))
        records.append(
            PredictionRecord(
                policy=policy,
                match_id=row.match_id,
                public_patch=row.public_patch,
                platform=row.platform,
                role=role,
                champion_id=row.champion_id,
                outcome=int(row.win),
                probability=probability,
                training_evidence=evidence,
                champion_role_training_games=role_stat[1] if role_stat else 0,
                missing_reason=missing,
            )
        )
    return records


def _matchup_prediction(
    row: ParticipantDraftObservation,
    participant_lookup: dict[tuple[str, int], ParticipantDraftObservation],
    fitted: _FittedPolicies,
    baseline: float,
    prior_strength: float,
) -> tuple[float | None, int, str | None]:
    if not row.matchup_eligibility:
        return None, 0, "matchup_ineligible"
    opponent = _resolved_opponent(row, participant_lookup)
    if opponent is None or row.analysis_position is None:
        return None, 0, "unresolved_directional_opponent"
    key = (
        row.champion_id,
        row.analysis_position,
        opponent.champion_id,
        opponent.analysis_position,
    )
    wins, games = fitted.matchup_stats.get(key, (0, 0))
    return (wins + baseline * prior_strength) / (games + prior_strength), games, None


def _synergy_prediction(
    row: ParticipantDraftObservation,
    team_members: dict[tuple[str, int], list[ParticipantDraftObservation]],
    fitted: _FittedPolicies,
    baseline: float,
    prior_strength: float,
) -> tuple[float | None, int, str | None]:
    if not row.synergy_eligibility or row.analysis_position is None:
        return None, 0, "synergy_ineligible"
    probabilities = []
    evidence = 0
    for ally in _eligible_allies(row, team_members):
        key = (
            row.champion_id,
            row.analysis_position,
            ally.champion_id,
            ally.analysis_position,
        )
        wins, games = fitted.synergy_stats.get(key, (0, 0))
        probabilities.append(
            (wins + baseline * prior_strength) / (games + prior_strength)
        )
        evidence += games
    if not probabilities:
        return None, 0, "no_eligible_allies"
    return mean(probabilities), evidence, None


def _resolved_opponent(
    row: ParticipantDraftObservation,
    participant_lookup: dict[tuple[str, int], ParticipantDraftObservation],
) -> ParticipantDraftObservation | None:
    if row.lane_opponent_participant_id is None:
        return None
    opponent = participant_lookup.get((row.match_id, row.lane_opponent_participant_id))
    if opponent is None or opponent.analysis_position is None:
        return None
    return opponent


def _eligible_allies(
    row: ParticipantDraftObservation,
    team_members: dict[tuple[str, int], list[ParticipantDraftObservation]],
) -> list[ParticipantDraftObservation]:
    return [
        ally
        for ally in team_members.get((row.match_id, row.team_id), [])
        if ally.participant_id != row.participant_id
        and ally.analysis_position is not None
        and ally.role_analysis_eligibility
    ]


def _team_members(
    participants: list[ParticipantDraftObservation],
) -> dict[tuple[str, int], list[ParticipantDraftObservation]]:
    members: dict[tuple[str, int], list[ParticipantDraftObservation]] = defaultdict(
        list
    )
    for row in participants:
        members[(row.match_id, row.team_id)].append(row)
    for rows in members.values():
        rows.sort(key=lambda row: row.participant_id)
    return dict(members)


def _metric_slices(
    records: list[PredictionRecord], calibration_bins: int
) -> dict[str, list[dict[str, Any]]]:
    dimensions: dict[str, dict[str, list[PredictionRecord]]] = {
        "public_patch": defaultdict(list),
        "platform": defaultdict(list),
        "role": defaultdict(list),
        "champion_frequency": defaultdict(list),
        "training_evidence": defaultdict(list),
    }
    for row in records:
        dimensions["public_patch"][row.public_patch].append(row)
        dimensions["platform"][row.platform].append(row)
        dimensions["role"][row.role].append(row)
        dimensions["champion_frequency"][
            evidence_bucket(row.champion_role_training_games)
        ].append(row)
        dimensions["training_evidence"][evidence_bucket(row.training_evidence)].append(
            row
        )
    output = {}
    for dimension, groups in dimensions.items():
        output[dimension] = [
            {
                "value": value,
                "metrics": calculate_metrics(rows, calibration_bins=calibration_bins),
            }
            for value, rows in sorted(groups.items())
        ]
    return output


def evidence_bucket(sample_size: int) -> str:
    if sample_size == 0:
        return "0_unseen"
    if sample_size == 1:
        return "1"
    if sample_size < 5:
        return "2_4"
    if sample_size < 10:
        return "5_9"
    if sample_size < 20:
        return "10_19"
    return "20_plus"


def _cluster_bootstrap(
    records: list[PredictionRecord], *, replicates: int, seed: int
) -> dict[str, Any]:
    by_match: dict[str, list[PredictionRecord]] = defaultdict(list)
    for row in records:
        by_match[row.match_id].append(row)
    match_ids = sorted(by_match)
    if not match_ids:
        return {}
    rng = random.Random(seed)
    samples: dict[str, list[float]] = defaultdict(list)
    for _ in range(replicates):
        sampled = [rng.choice(match_ids) for _ in match_ids]
        rows = [row for match_id in sampled for row in by_match[match_id]]
        probabilities = [float(row.probability) for row in rows]
        outcomes = [row.outcome for row in rows]
        samples["brier_score"].append(
            mean(
                (probability - outcome) ** 2
                for probability, outcome in zip(probabilities, outcomes, strict=True)
            )
        )
        samples["accuracy_at_0_5"].append(
            mean(
                float((probability >= 0.5) == bool(outcome))
                for probability, outcome in zip(probabilities, outcomes, strict=True)
            )
        )
        if all(
            (probability if outcome else 1 - probability) > 0
            for probability, outcome in zip(probabilities, outcomes, strict=True)
        ):
            samples["log_loss"].append(
                mean(
                    -math.log(probability if outcome else 1 - probability)
                    for probability, outcome in zip(
                        probabilities, outcomes, strict=True
                    )
                )
            )
    return {
        name: {
            "method": "deterministic_match_cluster_percentile",
            "replicates": replicates,
            "seed": seed,
            "lower_0_025": _quantile(values, 0.025),
            "upper_0_975": _quantile(values, 0.975),
        }
        for name, values in sorted(samples.items())
    }


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _markdown_report(
    metrics: dict[str, Any], quality: dict[str, Any], config: BacktestConfig
) -> str:
    lines = [
        "# Nexus Lens Stage 3.3C backtest report",
        "",
        "Status: **experimental smoke test; policy unresolved**.",
        "",
        "This report validates offline chronological mechanics. It does not select a "
        "baseline, prior strength, threshold, policy, or recommendation.",
        "",
        f"- Platform: `{metrics['platform']}`",
        f"- Analysis region: `{metrics['analysis_region']}`",
        f"- Queue: `{metrics['queue_id']}` (Ranked Solo/Duo)",
        f"- Explicit clipping: `{config.clip_min}` to `{config.clip_max}`",
        f"- Calibration bins: `{config.calibration_bins}`",
        "- Accuracy threshold: `0.5`",
        "- Confidence intervals: deterministic match-cluster bootstrap",
        "",
    ]
    for fold in metrics["folds"]:
        lines.extend(
            [
                f"## Evaluate {fold['evaluation_patch']}",
                "",
                f"Training patches: `{', '.join(fold['training_patches'])}`.",
                "Training matches: "
                f"{fold['training_match_count']}; evaluation matches: "
                f"{fold['evaluation_match_count']}.",
                "",
                "| Reference policy | Coverage | Log loss | Brier | Accuracy | ECE |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for result in fold["policy_results"]:
            overall = result["overall"]
            lines.append(
                (
                    "| {policy} | {coverage} | {log_loss} | {brier} | "
                    "{accuracy} | {ece} |"
                ).format(
                    policy=result["policy"],
                    coverage=_format_metric(overall["coverage"]),
                    log_loss=_format_metric(overall["log_loss"]),
                    brier=_format_metric(overall["brier_score"]),
                    accuracy=_format_metric(overall["accuracy_at_0_5"]),
                    ece=_format_metric(overall["expected_calibration_error"]),
                )
            )
        lines.append("")
    lines.extend(
        [
            "Leakage gate: all training patches precede evaluation patches, match sets "
            "are disjoint, fitting uses the recorded training set only, and platform "
            "and queue scopes are isolated.",
            "",
            f"Ready for policy selection: `{quality['ready_for_policy_selection']}`.",
            f"Ready for recommendations: `{quality['ready_for_recommendations']}`.",
            "",
        ]
    )
    return "\n".join(lines)


def _format_metric(value: float | None) -> str:
    return "null" if value is None else f"{value:.6f}"


def _config_payload(config: BacktestConfig) -> dict[str, Any]:
    return {
        "analysis_region": config.analysis_region,
        "evaluation_patches": list(config.evaluation_patches),
        "policies": list(config.policies),
        "experimental_prior_equivalent_games": config.prior_equivalent_games,
        "calibration_bins": config.calibration_bins,
        "clip_min": config.clip_min,
        "clip_max": config.clip_max,
        "bootstrap_replicates": config.bootstrap_replicates,
        "bootstrap_seed": config.bootstrap_seed,
        "accuracy_threshold": 0.5,
        "fit_scope": "strictly_earlier_public_patches_only",
        "split_unit": "match",
        "policy_status": "unresolved",
    }


def _validate_existing_output(dataset: BacktestDataset) -> None:
    target = dataset.output_directory
    if not target.exists():
        return
    metadata_path = target / "metadata.json"
    if not metadata_path.is_file():
        raise Stage3ValidationError(
            "immutable_output_incomplete", "existing Stage 3.3C output is incomplete"
        )
    try:
        existing = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stage3ValidationError(
            "immutable_output_invalid", "existing Stage 3.3C metadata is invalid"
        ) from exc
    if existing != dataset.metadata:
        raise Stage3ValidationError(
            "immutable_output_lineage_conflict",
            "existing Stage 3.3C metadata disagrees with current lineage",
        )


def _write_dataset(dataset: BacktestDataset, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "backtest_metrics.json").write_bytes(_json_bytes(dataset.metrics))
    (directory / "quality_report.json").write_bytes(_json_bytes(dataset.quality_report))
    (directory / "backtest_report.md").write_text(
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


def _hash_string_set(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value))


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _increment(bucket: list[int], outcome: int) -> None:
    bucket[0] += outcome
    bucket[1] += 1


def _freeze_counts(
    values: dict[tuple[Any, ...], list[int]],
) -> dict[tuple[Any, ...], tuple[int, int]]:
    return {key: (value[0], value[1]) for key, value in values.items()}


def _mean_or_none(values: list[float]) -> float | None:
    return mean(values) if values else None


def _reject_nonfinite(value: Any) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("metrics cannot contain NaN or infinity")
