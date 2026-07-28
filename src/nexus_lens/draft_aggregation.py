"""Stage 3.3B transparent matchup and ally-synergy aggregation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from scipy.stats import beta as beta_distribution

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.draft_observations import (
    DRAFT_OBSERVATION_SCHEMA_VERSION,
    DRAFT_POLICY_VERSION,
    DRAFT_QUALITY_SCHEMA_VERSION,
    MatchDraftContext,
    ParticipantDraftObservation,
    TeamDraftObservation,
)

AGGREGATION_SCHEMA_VERSION = "stage3.3b-v1"
AGGREGATION_QUALITY_SCHEMA_VERSION = "stage3.3b-quality-v1"
AGGREGATION_POLICY_VERSION = "stage3.3b-aggregation-policy-v1"
STATISTICAL_PRIMITIVES_VERSION = "stage3.3b-statistics-v1"
PATCH_DECAY = 0.8
MAX_PREVIOUS_PATCHES = 5
PROVISIONAL_MINIMUM_PRACTICAL_ADVANTAGE = 0.01
MODERATE_EVIDENCE_PROBABILITY = 0.90
STRONG_EVIDENCE_PROBABILITY = 0.95
ROLE_VIABILITY_SHARE = 0.05
ROLE_VIABILITY_MINIMUM_GAMES = 50
EXPECTED_PARTICIPANT_COUNT = 1_000
EXPECTED_TEAM_COUNT = 200
EXPECTED_MATCH_COUNT = 100
STATISTICAL_STATUS_UNRESOLVED = "not_evaluated_policy_unresolved"
REQUIRED_STAGE3_3A_FILES = (
    "draft_observation_quality_report.json",
    "match_draft_context.jsonl",
    "metadata.json",
    "participant_draft_observations.jsonl",
    "team_draft_observations.jsonl",
)
REPRODUCTION_COMMAND = (
    ".\\.venv\\Scripts\\python.exe scripts\\build_draft_aggregates.py "
    "--input-run "
    "data/processed/stage3/schema=stage3.3a-v1/"
    "run=20260722T125547567196Z-population"
)


class AggregationModel(BaseModel):
    """Strict base for deterministic Stage 3.3B output rows."""

    model_config = ConfigDict(extra="forbid")


class SufficientStatistics(AggregationModel):
    observed_games: int
    wins: int
    losses: int
    raw_win_rate: float | None
    weighted_wins: float
    weighted_losses: float
    weighted_win_rate: float | None
    sum_weights: float
    sum_squared_weights: float
    effective_sample_size: float | None


class PatchSpecificStatistics(AggregationModel):
    public_patch: str
    patch_age: int
    patch_weight: float
    observed_games: int
    wins: int
    losses: int
    raw_win_rate: float | None


class CumulativePatchWindow(AggregationModel):
    oldest_patch_age: int
    considered_patches: list[str]
    observed_patches: list[str]
    input_missing_patch_ages: list[int]
    input_missing_patches: list[str]
    statistics: SufficientStatistics


class StatisticsBundle(AggregationModel):
    statistics: SufficientStatistics
    patch_specific: list[PatchSpecificStatistics]
    cumulative_patch_windows: list[CumulativePatchWindow]


class BaselineComponent(AggregationModel):
    exclusion_kind: str
    excluded_champion_id: int
    excluded_position: str
    availability_status: str
    sparsity_status: str
    statistics: SufficientStatistics
    patch_specific: list[PatchSpecificStatistics]
    cumulative_patch_windows: list[CumulativePatchWindow]


class PairExclusionComponent(AggregationModel):
    counterpart_champion_id: int
    counterpart_champion_name: str | None
    counterpart_position: str
    availability_status: str
    sparsity_status: str
    statistics: SufficientStatistics
    patch_specific: list[PatchSpecificStatistics]
    cumulative_patch_windows: list[CumulativePatchWindow]


class PosteriorFields(AggregationModel):
    baseline_probability: float | None
    prior_equivalent_games: float | None
    minimum_practical_advantage: float
    prior_expected_wins: float | None
    prior_expected_losses: float | None
    posterior_alpha: float | None
    posterior_beta: float | None
    posterior_mean: float | None
    posterior_advantage: float | None
    posterior_probability_practical_advantage: float | None
    evidence_tier: str | None


class MatchupAggregate(AggregationModel):
    logical_key: str
    focal_champion_id: int
    focal_champion_name: str | None
    focal_position: str
    opponent_champion_id: int
    opponent_champion_name: str | None
    opponent_position: str
    platform: str
    region: str | None
    region_lineage_status: str
    rank_bracket: str | None
    collection_stratum: str | None
    rank_lineage_status: str
    queue_id: int
    target_patch: str
    statistics: SufficientStatistics
    patch_specific: list[PatchSpecificStatistics]
    cumulative_patch_windows: list[CumulativePatchWindow]
    focal_leave_opponent_out: BaselineComponent
    opponent_leave_focal_out: BaselineComponent
    posterior: PosteriorFields
    statistical_status: str
    visible_observation: bool
    recommendation_eligibility: bool | None
    source_eligibility_policy: str
    source_observation_count: int
    source_participant_observations_sha256: str
    source_run_id: str
    source_stage3_3a_schema_version: str
    processing_schema_version: str


class SynergyAggregate(AggregationModel):
    logical_key: str
    focal_champion_id: int
    focal_champion_name: str | None
    focal_position: str
    ally_champion_id: int
    ally_champion_name: str | None
    ally_position: str
    platform: str
    region: str | None
    region_lineage_status: str
    rank_bracket: str | None
    collection_stratum: str | None
    rank_lineage_status: str
    queue_id: int
    target_patch: str
    statistics: SufficientStatistics
    patch_specific: list[PatchSpecificStatistics]
    cumulative_patch_windows: list[CumulativePatchWindow]
    focal_without_ally: BaselineComponent
    ally_without_focal: BaselineComponent
    posterior: PosteriorFields
    statistical_status: str
    visible_observation: bool
    recommendation_eligibility: bool | None
    source_eligibility_policy: str
    source_observation_count: int
    source_participant_observations_sha256: str
    source_run_id: str
    source_stage3_3a_schema_version: str
    processing_schema_version: str


class ChampionRoleSufficientStatistics(AggregationModel):
    logical_key: str
    champion_id: int
    champion_name: str | None
    analysis_position: str
    platform: str
    region: str | None
    region_lineage_status: str
    rank_bracket: str | None
    collection_stratum: str | None
    rank_lineage_status: str
    queue_id: int
    target_patch: str
    role_eligible: StatisticsBundle
    matchup_eligible: StatisticsBundle
    synergy_eligible: StatisticsBundle
    opponent_exclusion_components: list[PairExclusionComponent]
    ally_exclusion_components: list[PairExclusionComponent]
    champion_role_share: float | None
    champion_role_eligible_games: int
    champion_all_role_eligible_games: int
    meets_five_percent_share: bool
    meets_fifty_game_minimum: bool
    provisional_role_viability_rule_satisfied: bool
    authoritative_role_viability: bool
    source_participant_observations_sha256: str
    source_run_id: str
    source_stage3_3a_schema_version: str
    processing_schema_version: str


class BetaBinomialResult(AggregationModel):
    baseline_probability: float
    prior_equivalent_games: float
    observed_wins: float
    observed_losses: float
    minimum_practical_advantage: float
    practical_advantage_threshold: float
    prior_expected_wins: float
    prior_expected_losses: float
    posterior_alpha: float
    posterior_beta: float
    posterior_mean: float
    posterior_advantage: float
    posterior_probability_practical_advantage: float
    evidence_tier: Literal[
        "insufficient_evidence", "moderate_evidence", "strong_evidence"
    ]


@dataclass(frozen=True)
class Stage33AInput:
    run_id: str
    input_directory: Path
    stage3_1_directory: Path
    stage3_2_directory: Path
    participants: list[ParticipantDraftObservation]
    teams: list[TeamDraftObservation]
    matches: list[MatchDraftContext]
    lineage_hashes: dict[str, str]
    stage3_1_hashes: dict[str, str]
    stage3_2_hashes: dict[str, str]


@dataclass
class AggregationDataset:
    run_id: str
    input_directory: Path
    stage3_1_directory: Path
    stage3_2_directory: Path
    output_directory: Path
    matchup_aggregates: list[MatchupAggregate]
    synergy_aggregates: list[SynergyAggregate]
    champion_role_statistics: list[ChampionRoleSufficientStatistics]
    quality_report: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class _Context:
    platform: str
    region: str | None
    region_lineage_status: str
    rank_bracket: str | None
    collection_stratum: str | None
    rank_lineage_status: str
    queue_id: int

    def values(self) -> tuple[Any, ...]:
        return (
            self.platform,
            self.region,
            self.region_lineage_status,
            self.rank_bracket,
            self.collection_stratum,
            self.rank_lineage_status,
            self.queue_id,
        )


@dataclass(frozen=True)
class _PairContribution:
    match_id: str
    team_id: int
    public_patch: str
    win: bool
    focal_champion_id: int
    focal_position: str
    counterpart_champion_id: int
    counterpart_position: str
    context: _Context


@dataclass(frozen=True)
class _RoleContribution:
    match_id: str
    team_id: int
    public_patch: str
    win: bool
    champion_id: int
    position: str
    context: _Context
    role_eligible: bool
    matchup_eligible: bool
    synergy_eligible: bool
    opponent: tuple[int, str] | None
    allies: tuple[tuple[int, str], ...]


def beta_survival_probability(alpha: float, beta: float, threshold: float) -> float:
    """Return P(X > threshold) for a proper Beta(alpha, beta) distribution.

    SciPy's survival function is used directly instead of ``1 - cdf``. Thresholds
    at or below zero return one; thresholds at or above one return zero.
    """

    alpha = _finite_number("alpha", alpha)
    beta = _finite_number("beta", beta)
    threshold = _finite_number("threshold", threshold)
    if alpha <= 0 or beta <= 0:
        raise ValueError("alpha and beta must be positive")
    if threshold <= 0:
        return 1.0
    if threshold >= 1:
        return 0.0
    probability = float(beta_distribution.sf(threshold, alpha, beta))
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("SciPy returned an invalid beta survival probability")
    return probability


def evidence_tier(probability: float) -> str:
    """Apply the approved 0.90 and 0.95 evidence boundaries."""

    probability = _finite_number("probability", probability)
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between zero and one")
    if probability < MODERATE_EVIDENCE_PROBABILITY:
        return "insufficient_evidence"
    if probability < STRONG_EVIDENCE_PROBABILITY:
        return "moderate_evidence"
    return "strong_evidence"


def beta_binomial_posterior(
    *,
    baseline_probability: float,
    prior_equivalent_games: float,
    observed_wins: float,
    observed_losses: float,
    minimum_practical_advantage: float = PROVISIONAL_MINIMUM_PRACTICAL_ADVANTAGE,
) -> BetaBinomialResult:
    """Calculate a caller-parameterized beta-binomial posterior.

    ``prior_equivalent_games`` is mandatory. A zero-strength prior is accepted only
    when both observed wins and observed losses are positive, which leaves a proper
    Beta posterior. No production baseline or prior strength is selected here.
    """

    baseline = _finite_number("baseline_probability", baseline_probability)
    prior_strength = _finite_number("prior_equivalent_games", prior_equivalent_games)
    wins = _finite_number("observed_wins", observed_wins)
    losses = _finite_number("observed_losses", observed_losses)
    advantage = _finite_number(
        "minimum_practical_advantage", minimum_practical_advantage
    )
    if not 0 < baseline < 1:
        raise ValueError("baseline_probability must be strictly between zero and one")
    if prior_strength < 0:
        raise ValueError("prior_equivalent_games must be nonnegative")
    if wins < 0 or losses < 0:
        raise ValueError("observed wins and losses must be nonnegative")
    if advantage < 0:
        raise ValueError("minimum_practical_advantage must be nonnegative")
    prior_wins = baseline * prior_strength
    prior_losses = (1 - baseline) * prior_strength
    posterior_alpha = prior_wins + wins
    posterior_beta = prior_losses + losses
    if posterior_alpha <= 0 or posterior_beta <= 0:
        raise ValueError(
            "zero prior strength requires positive observed wins and losses "
            "to form a proper beta posterior"
        )
    posterior_mean = posterior_alpha / (posterior_alpha + posterior_beta)
    threshold = baseline + advantage
    probability = beta_survival_probability(posterior_alpha, posterior_beta, threshold)
    return BetaBinomialResult(
        baseline_probability=baseline,
        prior_equivalent_games=prior_strength,
        observed_wins=wins,
        observed_losses=losses,
        minimum_practical_advantage=advantage,
        practical_advantage_threshold=threshold,
        prior_expected_wins=prior_wins,
        prior_expected_losses=prior_losses,
        posterior_alpha=posterior_alpha,
        posterior_beta=posterior_beta,
        posterior_mean=posterior_mean,
        posterior_advantage=posterior_mean - baseline,
        posterior_probability_practical_advantage=probability,
        evidence_tier=evidence_tier(probability),
    )


def run_stage3_3b(
    *,
    input_directory: Path,
    output_root: Path,
    validate_only: bool,
    target_patch: str | None = None,
    minimum_practical_advantage: float = PROVISIONAL_MINIMUM_PRACTICAL_ADVANTAGE,
    expected_participant_count: int = EXPECTED_PARTICIPANT_COUNT,
    expected_team_count: int = EXPECTED_TEAM_COUNT,
    expected_match_count: int = EXPECTED_MATCH_COUNT,
) -> AggregationDataset:
    """Load Stage 3.3A, build aggregates, and optionally publish them."""

    stage33a = load_stage3_3a_input(
        input_directory,
        expected_participant_count=expected_participant_count,
        expected_team_count=expected_team_count,
        expected_match_count=expected_match_count,
    )
    output_directory = (
        output_root / f"schema={AGGREGATION_SCHEMA_VERSION}" / f"run={stage33a.run_id}"
    )
    _validate_existing_output_lineage(output_directory, stage33a)
    dataset = build_aggregation_dataset(
        stage33a=stage33a,
        output_directory=output_directory,
        target_patch=target_patch,
        minimum_practical_advantage=minimum_practical_advantage,
    )
    if not validate_only:
        write_aggregation_dataset(dataset)
    return dataset


def load_stage3_3a_input(
    input_directory: Path,
    *,
    expected_participant_count: int,
    expected_team_count: int,
    expected_match_count: int,
) -> Stage33AInput:
    """Load Stage 3.3A and verify all recorded prior-stage lineage."""

    missing = [
        name
        for name in REQUIRED_STAGE3_3A_FILES
        if not (input_directory / name).is_file()
    ]
    if missing:
        raise Stage3ValidationError(
            "stage3_3a_input_missing", "required Stage 3.3A files are missing"
        )
    hashes = _hash_files(input_directory, REQUIRED_STAGE3_3A_FILES)
    metadata = _load_json_object(
        input_directory / "metadata.json", "stage3_3a_metadata"
    )
    quality = _load_json_object(
        input_directory / "draft_observation_quality_report.json",
        "stage3_3a_quality",
    )
    if metadata.get("processing_schema_version") != DRAFT_OBSERVATION_SCHEMA_VERSION:
        raise Stage3ValidationError(
            "incompatible_stage3_3a_schema", "Stage 3.3A schema is incompatible"
        )
    if metadata.get("draft_policy_version") != DRAFT_POLICY_VERSION:
        raise Stage3ValidationError(
            "incompatible_stage3_3a_policy", "Stage 3.3A policy is incompatible"
        )
    if quality.get("quality_report_schema_version") != DRAFT_QUALITY_SCHEMA_VERSION:
        raise Stage3ValidationError(
            "incompatible_stage3_3a_quality",
            "Stage 3.3A quality schema is incompatible",
        )
    if (
        quality.get("ready_for_matchup_synergy_aggregation") is not True
        or quality.get("invariant_failures")
        or quality.get("reconciliation_failures")
    ):
        raise Stage3ValidationError(
            "stage3_3a_not_ready", "Stage 3.3A quality gate is not satisfied"
        )
    output = metadata.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("sha256"), dict):
        raise Stage3ValidationError(
            "stage3_3a_lineage", "Stage 3.3A output hashes are missing"
        )
    for name, expected_hash in output["sha256"].items():
        if hashes.get(name) != expected_hash:
            raise Stage3ValidationError(
                "stage3_3a_hash_conflict", "Stage 3.3A physical hashes disagree"
            )
    inputs = metadata.get("inputs")
    if not isinstance(inputs, dict):
        raise Stage3ValidationError(
            "stage3_3a_lineage", "Stage 3.3A prior-stage lineage is missing"
        )
    stage31_directory, stage31_hashes = _verify_prior_lineage(inputs, "stage3_1")
    stage32_directory, stage32_hashes = _verify_prior_lineage(inputs, "stage3_2")
    participants = _load_jsonl_models(
        input_directory / "participant_draft_observations.jsonl",
        ParticipantDraftObservation,
        "stage3_3a_participants",
    )
    teams = _load_jsonl_models(
        input_directory / "team_draft_observations.jsonl",
        TeamDraftObservation,
        "stage3_3a_teams",
    )
    matches = _load_jsonl_models(
        input_directory / "match_draft_context.jsonl",
        MatchDraftContext,
        "stage3_3a_matches",
    )
    if (
        len(participants) != expected_participant_count
        or len(teams) != expected_team_count
        or len(matches) != expected_match_count
    ):
        raise Stage3ValidationError(
            "stage3_3a_row_count", "Stage 3.3A physical row counts disagree"
        )
    _validate_stage33a_keys(participants, teams, matches)
    run_id = metadata.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise Stage3ValidationError(
            "stage3_3a_metadata", "Stage 3.3A run ID is missing"
        )
    return Stage33AInput(
        run_id=run_id,
        input_directory=input_directory,
        stage3_1_directory=stage31_directory,
        stage3_2_directory=stage32_directory,
        participants=participants,
        teams=teams,
        matches=matches,
        lineage_hashes=hashes,
        stage3_1_hashes=stage31_hashes,
        stage3_2_hashes=stage32_hashes,
    )


def build_aggregation_dataset(
    *,
    stage33a: Stage33AInput,
    output_directory: Path,
    target_patch: str | None = None,
    minimum_practical_advantage: float = PROVISIONAL_MINIMUM_PRACTICAL_ADVANTAGE,
) -> AggregationDataset:
    """Create factual aggregates without policy-filled posteriors."""

    practical_advantage = _finite_number(
        "minimum_practical_advantage", minimum_practical_advantage
    )
    if practical_advantage < 0:
        raise Stage3ValidationError(
            "invalid_practical_advantage",
            "minimum practical advantage must be nonnegative",
        )
    available_patches = sorted(
        {row.public_patch for row in stage33a.participants},
        key=_patch_tuple,
        reverse=True,
    )
    if not available_patches:
        raise Stage3ValidationError(
            "stage3_3a_patch_coverage", "Stage 3.3A has no public patches"
        )
    selected_target = target_patch or available_patches[0]
    target_tuple = _patch_tuple(selected_target)
    patch_ages = {
        patch: _patch_age(target_tuple, _patch_tuple(patch))
        for patch in available_patches
    }
    if any(age < 0 for age in patch_ages.values()):
        raise Stage3ValidationError(
            "target_patch_before_input",
            "target patch is older than a Stage 3.3A input patch",
        )
    scoped_patches = {
        patch: age for patch, age in patch_ages.items() if age <= MAX_PREVIOUS_PATCHES
    }
    if not scoped_patches:
        raise Stage3ValidationError(
            "target_patch_window_empty", "no input patch is inside the target window"
        )
    max_window_age = max(scoped_patches.values())
    champion_names = _champion_names(stage33a.participants)
    participant_lookup = {
        (row.match_id, row.participant_id): row for row in stage33a.participants
    }
    team_members: dict[tuple[str, int], list[ParticipantDraftObservation]] = (
        defaultdict(list)
    )
    for row in stage33a.participants:
        team_members[(row.match_id, row.team_id)].append(row)
    for members in team_members.values():
        members.sort(key=lambda row: row.participant_id)

    matchup_contributions, matchup_skips = _matchup_contributions(
        stage33a.participants,
        participant_lookup,
        scoped_patches,
    )
    synergy_contributions, synergy_skips = _synergy_contributions(
        stage33a.participants,
        team_members,
        scoped_patches,
    )
    role_contributions, role_skips = _role_contributions(
        stage33a.participants,
        participant_lookup,
        team_members,
        scoped_patches,
    )
    source_hash = stage33a.lineage_hashes["participant_draft_observations.jsonl"]

    matchup_groups: dict[tuple[Any, ...], list[_PairContribution]] = defaultdict(list)
    for contribution in matchup_contributions:
        matchup_groups[_pair_group_key(contribution)].append(contribution)
    synergy_groups: dict[tuple[Any, ...], list[_PairContribution]] = defaultdict(list)
    for contribution in synergy_contributions:
        synergy_groups[_pair_group_key(contribution)].append(contribution)
    role_groups: dict[tuple[Any, ...], list[_RoleContribution]] = defaultdict(list)
    for contribution in role_contributions:
        role_groups[_role_group_key(contribution)].append(contribution)

    matchup_aggregates = [
        _matchup_aggregate(
            key,
            rows,
            role_groups,
            champion_names,
            target_patch=selected_target,
            max_window_age=max_window_age,
            input_patch_ages=scoped_patches,
            minimum_practical_advantage=practical_advantage,
            source_hash=source_hash,
            run_id=stage33a.run_id,
        )
        for key, rows in sorted(matchup_groups.items(), key=lambda item: item[0])
    ]
    synergy_aggregates = [
        _synergy_aggregate(
            key,
            rows,
            role_groups,
            champion_names,
            target_patch=selected_target,
            max_window_age=max_window_age,
            input_patch_ages=scoped_patches,
            minimum_practical_advantage=practical_advantage,
            source_hash=source_hash,
            run_id=stage33a.run_id,
        )
        for key, rows in sorted(synergy_groups.items(), key=lambda item: item[0])
    ]
    champion_role_statistics = _champion_role_statistics(
        role_groups,
        champion_names,
        target_patch=selected_target,
        max_window_age=max_window_age,
        input_patch_ages=scoped_patches,
        source_hash=source_hash,
        run_id=stage33a.run_id,
    )
    validation = _validate_aggregates(
        stage33a=stage33a,
        matchup_contributions=matchup_contributions,
        synergy_contributions=synergy_contributions,
        role_contributions=role_contributions,
        matchup_aggregates=matchup_aggregates,
        synergy_aggregates=synergy_aggregates,
        champion_role_statistics=champion_role_statistics,
        target_patch=selected_target,
        max_window_age=max_window_age,
        input_patch_ages=scoped_patches,
    )
    quality_report = _quality_report(
        stage33a=stage33a,
        output_directory=output_directory,
        target_patch=selected_target,
        max_window_age=max_window_age,
        input_patch_ages=scoped_patches,
        matchup_contributions=matchup_contributions,
        synergy_contributions=synergy_contributions,
        role_contributions=role_contributions,
        matchup_aggregates=matchup_aggregates,
        synergy_aggregates=synergy_aggregates,
        champion_role_statistics=champion_role_statistics,
        matchup_skips=matchup_skips,
        synergy_skips=synergy_skips,
        role_skips=role_skips,
        validation=validation,
    )
    output_hashes = _output_content_hashes(
        matchup_aggregates,
        synergy_aggregates,
        champion_role_statistics,
        quality_report,
    )
    metadata = _metadata(
        stage33a=stage33a,
        output_directory=output_directory,
        target_patch=selected_target,
        max_window_age=max_window_age,
        practical_advantage=practical_advantage,
        matchup_count=len(matchup_aggregates),
        synergy_count=len(synergy_aggregates),
        role_count=len(champion_role_statistics),
        output_hashes=output_hashes,
    )
    return AggregationDataset(
        run_id=stage33a.run_id,
        input_directory=stage33a.input_directory,
        stage3_1_directory=stage33a.stage3_1_directory,
        stage3_2_directory=stage33a.stage3_2_directory,
        output_directory=output_directory,
        matchup_aggregates=matchup_aggregates,
        synergy_aggregates=synergy_aggregates,
        champion_role_statistics=champion_role_statistics,
        quality_report=quality_report,
        metadata=metadata,
    )


def write_aggregation_dataset(dataset: AggregationDataset) -> Path:
    """Stage and atomically publish an invariant-clean Stage 3.3B run."""

    if not dataset.quality_report["ready_for_calibration"]:
        raise Stage3ValidationError(
            "stage3_3b_invariant_failure",
            "aggregates were not written because validation failed",
        )
    target = dataset.output_directory
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}."))
    try:
        _write_staged_dataset(dataset, staging)
        if target.exists() and _directories_equal(staging, target):
            return target
        if target.exists():
            backup = target.with_name(f".{target.name}.previous")
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(target, backup)
            try:
                os.replace(staging, target)
            except BaseException:
                os.replace(backup, target)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(staging, target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _matchup_contributions(
    participants: list[ParticipantDraftObservation],
    participant_lookup: dict[tuple[str, int], ParticipantDraftObservation],
    scoped_patches: dict[str, int],
) -> tuple[list[_PairContribution], Counter[str]]:
    rows: list[_PairContribution] = []
    skipped: Counter[str] = Counter()
    seen: set[tuple[Any, ...]] = set()
    for participant in participants:
        if not participant.matchup_eligibility:
            skipped["stage3_3a_matchup_ineligible"] += 1
            continue
        if participant.public_patch not in scoped_patches:
            skipped["outside_six_patch_window"] += 1
            continue
        if (
            participant.analysis_position is None
            or participant.lane_opponent_participant_id is None
            or participant.lane_opponent_champion_id is None
            or participant.lane_opponent_resolution_status != "resolved_unique"
        ):
            raise Stage3ValidationError(
                "eligible_matchup_missing_key",
                "eligible Stage 3.3A matchup lacks a resolved position opponent",
            )
        opponent = participant_lookup.get(
            (participant.match_id, participant.lane_opponent_participant_id)
        )
        if opponent is None or opponent.analysis_position is None:
            raise Stage3ValidationError(
                "eligible_matchup_missing_opponent",
                "eligible Stage 3.3A matchup opponent is missing",
            )
        contribution = _PairContribution(
            match_id=participant.match_id,
            team_id=participant.team_id,
            public_patch=participant.public_patch,
            win=participant.win,
            focal_champion_id=participant.champion_id,
            focal_position=participant.analysis_position,
            counterpart_champion_id=opponent.champion_id,
            counterpart_position=opponent.analysis_position,
            context=_context(participant),
        )
        source_key = (participant.match_id, *_pair_group_key(contribution))
        if source_key in seen:
            raise Stage3ValidationError(
                "duplicate_directional_matchup",
                "a match contributes twice to one directional matchup",
            )
        seen.add(source_key)
        rows.append(contribution)
    return sorted(rows, key=_contribution_sort_key), skipped


def _synergy_contributions(
    participants: list[ParticipantDraftObservation],
    team_members: dict[tuple[str, int], list[ParticipantDraftObservation]],
    scoped_patches: dict[str, int],
) -> tuple[list[_PairContribution], Counter[str]]:
    rows: list[_PairContribution] = []
    skipped: Counter[str] = Counter()
    seen: set[tuple[Any, ...]] = set()
    for participant in participants:
        if not participant.synergy_eligibility:
            skipped["stage3_3a_synergy_ineligible"] += 1
            continue
        if participant.public_patch not in scoped_patches:
            skipped["outside_six_patch_window"] += 1
            continue
        members = team_members[(participant.match_id, participant.team_id)]
        if not participant.team_structure_valid or len(members) != 5:
            raise Stage3ValidationError(
                "eligible_synergy_invalid_team",
                "eligible Stage 3.3A synergy has invalid team structure",
            )
        if participant.analysis_position is None:
            skipped["focal_position_unavailable"] += len(members) - 1
            continue
        for ally in members:
            if ally.participant_id == participant.participant_id:
                continue
            if ally.analysis_position is None:
                skipped["ally_position_unavailable"] += 1
                continue
            contribution = _PairContribution(
                match_id=participant.match_id,
                team_id=participant.team_id,
                public_patch=participant.public_patch,
                win=participant.win,
                focal_champion_id=participant.champion_id,
                focal_position=participant.analysis_position,
                counterpart_champion_id=ally.champion_id,
                counterpart_position=ally.analysis_position,
                context=_context(participant),
            )
            source_key = (
                participant.match_id,
                participant.team_id,
                *_pair_group_key(contribution),
            )
            if source_key in seen:
                raise Stage3ValidationError(
                    "duplicate_directional_synergy",
                    "a team-match contributes twice to one directional synergy",
                )
            seen.add(source_key)
            rows.append(contribution)
    return sorted(rows, key=_contribution_sort_key), skipped


def _role_contributions(
    participants: list[ParticipantDraftObservation],
    participant_lookup: dict[tuple[str, int], ParticipantDraftObservation],
    team_members: dict[tuple[str, int], list[ParticipantDraftObservation]],
    scoped_patches: dict[str, int],
) -> tuple[list[_RoleContribution], Counter[str]]:
    rows: list[_RoleContribution] = []
    skipped: Counter[str] = Counter()
    for participant in participants:
        if participant.public_patch not in scoped_patches:
            skipped["outside_six_patch_window"] += 1
            continue
        if participant.analysis_position is None:
            skipped["analysis_position_unavailable"] += 1
            continue
        opponent: tuple[int, str] | None = None
        if (
            participant.matchup_eligibility
            and participant.lane_opponent_participant_id is not None
        ):
            opponent_row = participant_lookup[
                (participant.match_id, participant.lane_opponent_participant_id)
            ]
            if opponent_row.analysis_position is None:
                raise Stage3ValidationError(
                    "eligible_matchup_missing_position",
                    "eligible matchup opponent position is missing",
                )
            opponent = (opponent_row.champion_id, opponent_row.analysis_position)
        allies = tuple(
            sorted(
                (
                    ally.champion_id,
                    ally.analysis_position,
                )
                for ally in team_members[(participant.match_id, participant.team_id)]
                if ally.participant_id != participant.participant_id
                and ally.analysis_position is not None
            )
        )
        rows.append(
            _RoleContribution(
                match_id=participant.match_id,
                team_id=participant.team_id,
                public_patch=participant.public_patch,
                win=participant.win,
                champion_id=participant.champion_id,
                position=participant.analysis_position,
                context=_context(participant),
                role_eligible=participant.role_analysis_eligibility,
                matchup_eligible=participant.matchup_eligibility,
                synergy_eligible=participant.synergy_eligibility
                and participant.team_structure_valid,
                opponent=opponent,
                allies=allies,
            )
        )
    return sorted(rows, key=_role_contribution_sort_key), skipped


def _matchup_aggregate(
    key: tuple[Any, ...],
    rows: list[_PairContribution],
    role_groups: dict[tuple[Any, ...], list[_RoleContribution]],
    champion_names: dict[int, str | None],
    *,
    target_patch: str,
    max_window_age: int,
    input_patch_ages: dict[str, int],
    minimum_practical_advantage: float,
    source_hash: str,
    run_id: str,
) -> MatchupAggregate:
    focal_id, focal_position, opponent_id, opponent_position, *context_values = key
    context = _context_from_values(tuple(context_values))
    bundle = _statistics_bundle(
        rows,
        target_patch=target_patch,
        max_window_age=max_window_age,
        input_patch_ages=input_patch_ages,
    )
    focal_role_rows = role_groups.get((focal_id, focal_position, *context.values()), [])
    opponent_role_rows = role_groups.get(
        (opponent_id, opponent_position, *context.values()), []
    )
    focal_baseline_rows = [
        row
        for row in focal_role_rows
        if row.matchup_eligible
        and row.opponent is not None
        and row.opponent != (opponent_id, opponent_position)
    ]
    opponent_baseline_rows = [
        row
        for row in opponent_role_rows
        if row.matchup_eligible
        and row.opponent is not None
        and row.opponent != (focal_id, focal_position)
    ]
    return MatchupAggregate(
        logical_key=_logical_key(("matchup", *key, target_patch)),
        focal_champion_id=focal_id,
        focal_champion_name=champion_names[focal_id],
        focal_position=focal_position,
        opponent_champion_id=opponent_id,
        opponent_champion_name=champion_names[opponent_id],
        opponent_position=opponent_position,
        **_context_dict(context),
        target_patch=target_patch,
        statistics=bundle.statistics,
        patch_specific=bundle.patch_specific,
        cumulative_patch_windows=bundle.cumulative_patch_windows,
        focal_leave_opponent_out=_baseline_component(
            focal_baseline_rows,
            exclusion_kind="focal_champion_role_excluding_opponent",
            excluded_champion_id=opponent_id,
            excluded_position=opponent_position,
            target_patch=target_patch,
            max_window_age=max_window_age,
            input_patch_ages=input_patch_ages,
        ),
        opponent_leave_focal_out=_baseline_component(
            opponent_baseline_rows,
            exclusion_kind="opponent_champion_role_excluding_focal",
            excluded_champion_id=focal_id,
            excluded_position=focal_position,
            target_patch=target_patch,
            max_window_age=max_window_age,
            input_patch_ages=input_patch_ages,
        ),
        posterior=_unevaluated_posterior(minimum_practical_advantage),
        statistical_status=STATISTICAL_STATUS_UNRESOLVED,
        visible_observation=True,
        recommendation_eligibility=None,
        source_eligibility_policy=(
            "Stage 3.3A matchup_eligibility and resolved_unique lane opponent"
        ),
        source_observation_count=len(rows),
        source_participant_observations_sha256=source_hash,
        source_run_id=run_id,
        source_stage3_3a_schema_version=DRAFT_OBSERVATION_SCHEMA_VERSION,
        processing_schema_version=AGGREGATION_SCHEMA_VERSION,
    )


def _synergy_aggregate(
    key: tuple[Any, ...],
    rows: list[_PairContribution],
    role_groups: dict[tuple[Any, ...], list[_RoleContribution]],
    champion_names: dict[int, str | None],
    *,
    target_patch: str,
    max_window_age: int,
    input_patch_ages: dict[str, int],
    minimum_practical_advantage: float,
    source_hash: str,
    run_id: str,
) -> SynergyAggregate:
    focal_id, focal_position, ally_id, ally_position, *context_values = key
    context = _context_from_values(tuple(context_values))
    bundle = _statistics_bundle(
        rows,
        target_patch=target_patch,
        max_window_age=max_window_age,
        input_patch_ages=input_patch_ages,
    )
    focal_role_rows = role_groups.get((focal_id, focal_position, *context.values()), [])
    ally_role_rows = role_groups.get((ally_id, ally_position, *context.values()), [])
    focal_baseline_rows = [
        row
        for row in focal_role_rows
        if row.synergy_eligible and (ally_id, ally_position) not in row.allies
    ]
    ally_baseline_rows = [
        row
        for row in ally_role_rows
        if row.synergy_eligible and (focal_id, focal_position) not in row.allies
    ]
    return SynergyAggregate(
        logical_key=_logical_key(("synergy", *key, target_patch)),
        focal_champion_id=focal_id,
        focal_champion_name=champion_names[focal_id],
        focal_position=focal_position,
        ally_champion_id=ally_id,
        ally_champion_name=champion_names[ally_id],
        ally_position=ally_position,
        **_context_dict(context),
        target_patch=target_patch,
        statistics=bundle.statistics,
        patch_specific=bundle.patch_specific,
        cumulative_patch_windows=bundle.cumulative_patch_windows,
        focal_without_ally=_baseline_component(
            focal_baseline_rows,
            exclusion_kind="focal_champion_role_without_ally",
            excluded_champion_id=ally_id,
            excluded_position=ally_position,
            target_patch=target_patch,
            max_window_age=max_window_age,
            input_patch_ages=input_patch_ages,
        ),
        ally_without_focal=_baseline_component(
            ally_baseline_rows,
            exclusion_kind="ally_champion_role_without_focal",
            excluded_champion_id=focal_id,
            excluded_position=focal_position,
            target_patch=target_patch,
            max_window_age=max_window_age,
            input_patch_ages=input_patch_ages,
        ),
        posterior=_unevaluated_posterior(minimum_practical_advantage),
        statistical_status=STATISTICAL_STATUS_UNRESOLVED,
        visible_observation=True,
        recommendation_eligibility=None,
        source_eligibility_policy=(
            "Stage 3.3A synergy_eligibility and valid five-player team"
        ),
        source_observation_count=len(rows),
        source_participant_observations_sha256=source_hash,
        source_run_id=run_id,
        source_stage3_3a_schema_version=DRAFT_OBSERVATION_SCHEMA_VERSION,
        processing_schema_version=AGGREGATION_SCHEMA_VERSION,
    )


def _champion_role_statistics(
    role_groups: dict[tuple[Any, ...], list[_RoleContribution]],
    champion_names: dict[int, str | None],
    *,
    target_patch: str,
    max_window_age: int,
    input_patch_ages: dict[str, int],
    source_hash: str,
    run_id: str,
) -> list[ChampionRoleSufficientStatistics]:
    champion_context_totals: Counter[tuple[Any, ...]] = Counter()
    for key, rows in role_groups.items():
        champion_id, _position, *context_values = key
        champion_context_totals[(champion_id, *context_values)] += sum(
            row.role_eligible for row in rows
        )
    output: list[ChampionRoleSufficientStatistics] = []
    for key, rows in sorted(role_groups.items(), key=lambda item: item[0]):
        champion_id, position, *context_values = key
        context = _context_from_values(tuple(context_values))
        role_rows = [row for row in rows if row.role_eligible]
        matchup_rows = [row for row in rows if row.matchup_eligible]
        synergy_rows = [row for row in rows if row.synergy_eligible]
        role_bundle = _statistics_bundle(
            role_rows,
            target_patch=target_patch,
            max_window_age=max_window_age,
            input_patch_ages=input_patch_ages,
        )
        opponent_pairs = sorted(
            {row.opponent for row in matchup_rows if row.opponent is not None}
        )
        ally_pairs = sorted({ally for row in synergy_rows for ally in row.allies})
        opponent_components = [
            _pair_exclusion_component(
                [
                    row
                    for row in matchup_rows
                    if row.opponent is not None and row.opponent != pair
                ],
                pair=pair,
                champion_names=champion_names,
                target_patch=target_patch,
                max_window_age=max_window_age,
                input_patch_ages=input_patch_ages,
            )
            for pair in opponent_pairs
        ]
        ally_components = [
            _pair_exclusion_component(
                [row for row in synergy_rows if pair not in row.allies],
                pair=pair,
                champion_names=champion_names,
                target_patch=target_patch,
                max_window_age=max_window_age,
                input_patch_ages=input_patch_ages,
            )
            for pair in ally_pairs
        ]
        champion_total = champion_context_totals[(champion_id, *context.values())]
        role_games = len(role_rows)
        share = role_games / champion_total if champion_total else None
        meets_share = share is not None and share >= ROLE_VIABILITY_SHARE
        meets_games = role_games >= ROLE_VIABILITY_MINIMUM_GAMES
        output.append(
            ChampionRoleSufficientStatistics(
                logical_key=_logical_key(
                    (
                        "champion_role",
                        champion_id,
                        position,
                        *context.values(),
                        target_patch,
                    )
                ),
                champion_id=champion_id,
                champion_name=champion_names[champion_id],
                analysis_position=position,
                **_context_dict(context),
                target_patch=target_patch,
                role_eligible=role_bundle,
                matchup_eligible=_statistics_bundle(
                    matchup_rows,
                    target_patch=target_patch,
                    max_window_age=max_window_age,
                    input_patch_ages=input_patch_ages,
                ),
                synergy_eligible=_statistics_bundle(
                    synergy_rows,
                    target_patch=target_patch,
                    max_window_age=max_window_age,
                    input_patch_ages=input_patch_ages,
                ),
                opponent_exclusion_components=opponent_components,
                ally_exclusion_components=ally_components,
                champion_role_share=share,
                champion_role_eligible_games=role_games,
                champion_all_role_eligible_games=champion_total,
                meets_five_percent_share=meets_share,
                meets_fifty_game_minimum=meets_games,
                provisional_role_viability_rule_satisfied=meets_share and meets_games,
                authoritative_role_viability=False,
                source_participant_observations_sha256=source_hash,
                source_run_id=run_id,
                source_stage3_3a_schema_version=DRAFT_OBSERVATION_SCHEMA_VERSION,
                processing_schema_version=AGGREGATION_SCHEMA_VERSION,
            )
        )
    return output


def _statistics_bundle(
    rows: list[_PairContribution] | list[_RoleContribution],
    *,
    target_patch: str,
    max_window_age: int,
    input_patch_ages: dict[str, int],
) -> StatisticsBundle:
    patch_specific = _patch_specific(rows, input_patch_ages)
    windows = _cumulative_windows(
        rows,
        target_patch=target_patch,
        max_window_age=max_window_age,
        input_patch_ages=input_patch_ages,
    )
    return StatisticsBundle(
        statistics=_sufficient_statistics(rows, input_patch_ages),
        patch_specific=patch_specific,
        cumulative_patch_windows=windows,
    )


def _baseline_component(
    rows: list[_RoleContribution],
    *,
    exclusion_kind: str,
    excluded_champion_id: int,
    excluded_position: str,
    target_patch: str,
    max_window_age: int,
    input_patch_ages: dict[str, int],
) -> BaselineComponent:
    bundle = _statistics_bundle(
        rows,
        target_patch=target_patch,
        max_window_age=max_window_age,
        input_patch_ages=input_patch_ages,
    )
    return BaselineComponent(
        exclusion_kind=exclusion_kind,
        excluded_champion_id=excluded_champion_id,
        excluded_position=excluded_position,
        availability_status=_availability_status(len(rows)),
        sparsity_status=STATISTICAL_STATUS_UNRESOLVED,
        statistics=bundle.statistics,
        patch_specific=bundle.patch_specific,
        cumulative_patch_windows=bundle.cumulative_patch_windows,
    )


def _pair_exclusion_component(
    rows: list[_RoleContribution],
    *,
    pair: tuple[int, str],
    champion_names: dict[int, str | None],
    target_patch: str,
    max_window_age: int,
    input_patch_ages: dict[str, int],
) -> PairExclusionComponent:
    bundle = _statistics_bundle(
        rows,
        target_patch=target_patch,
        max_window_age=max_window_age,
        input_patch_ages=input_patch_ages,
    )
    return PairExclusionComponent(
        counterpart_champion_id=pair[0],
        counterpart_champion_name=champion_names[pair[0]],
        counterpart_position=pair[1],
        availability_status=_availability_status(len(rows)),
        sparsity_status=STATISTICAL_STATUS_UNRESOLVED,
        statistics=bundle.statistics,
        patch_specific=bundle.patch_specific,
        cumulative_patch_windows=bundle.cumulative_patch_windows,
    )


def _sufficient_statistics(
    rows: list[_PairContribution] | list[_RoleContribution],
    input_patch_ages: dict[str, int],
) -> SufficientStatistics:
    games = len(rows)
    wins = sum(row.win for row in rows)
    losses = games - wins
    weights = [PATCH_DECAY ** input_patch_ages[row.public_patch] for row in rows]
    weighted_wins = sum(
        weight for row, weight in zip(rows, weights, strict=True) if row.win
    )
    sum_weights = sum(weights)
    weighted_losses = sum_weights - weighted_wins
    sum_squared_weights = sum(weight * weight for weight in weights)
    return SufficientStatistics(
        observed_games=games,
        wins=wins,
        losses=losses,
        raw_win_rate=wins / games if games else None,
        weighted_wins=weighted_wins,
        weighted_losses=weighted_losses,
        weighted_win_rate=weighted_wins / sum_weights if sum_weights else None,
        sum_weights=sum_weights,
        sum_squared_weights=sum_squared_weights,
        effective_sample_size=(
            (sum_weights * sum_weights) / sum_squared_weights
            if sum_squared_weights
            else None
        ),
    )


def _patch_specific(
    rows: list[_PairContribution] | list[_RoleContribution],
    input_patch_ages: dict[str, int],
) -> list[PatchSpecificStatistics]:
    by_patch: dict[str, list[_PairContribution] | list[_RoleContribution]] = (
        defaultdict(list)
    )
    for row in rows:
        by_patch[row.public_patch].append(row)
    output: list[PatchSpecificStatistics] = []
    for patch in sorted(by_patch, key=lambda item: input_patch_ages[item]):
        patch_rows = by_patch[patch]
        wins = sum(row.win for row in patch_rows)
        games = len(patch_rows)
        output.append(
            PatchSpecificStatistics(
                public_patch=patch,
                patch_age=input_patch_ages[patch],
                patch_weight=PATCH_DECAY ** input_patch_ages[patch],
                observed_games=games,
                wins=wins,
                losses=games - wins,
                raw_win_rate=wins / games,
            )
        )
    return output


def _cumulative_windows(
    rows: list[_PairContribution] | list[_RoleContribution],
    *,
    target_patch: str,
    max_window_age: int,
    input_patch_ages: dict[str, int],
) -> list[CumulativePatchWindow]:
    target = _patch_tuple(target_patch)
    windows: list[CumulativePatchWindow] = []
    for oldest_age in range(max_window_age + 1):
        considered = [_patch_at_age(target, age) for age in range(oldest_age + 1)]
        window_rows = [
            row for row in rows if input_patch_ages[row.public_patch] <= oldest_age
        ]
        input_missing_ages = [
            age for age, patch in enumerate(considered) if patch not in input_patch_ages
        ]
        observed_patches = sorted(
            {row.public_patch for row in window_rows},
            key=lambda patch: input_patch_ages[patch],
        )
        windows.append(
            CumulativePatchWindow(
                oldest_patch_age=oldest_age,
                considered_patches=considered,
                observed_patches=observed_patches,
                input_missing_patch_ages=input_missing_ages,
                input_missing_patches=[considered[age] for age in input_missing_ages],
                statistics=_sufficient_statistics(window_rows, input_patch_ages),
            )
        )
    return windows


def _unevaluated_posterior(minimum_practical_advantage: float) -> PosteriorFields:
    return PosteriorFields(
        baseline_probability=None,
        prior_equivalent_games=None,
        minimum_practical_advantage=minimum_practical_advantage,
        prior_expected_wins=None,
        prior_expected_losses=None,
        posterior_alpha=None,
        posterior_beta=None,
        posterior_mean=None,
        posterior_advantage=None,
        posterior_probability_practical_advantage=None,
        evidence_tier=None,
    )


def _validate_aggregates(
    *,
    stage33a: Stage33AInput,
    matchup_contributions: list[_PairContribution],
    synergy_contributions: list[_PairContribution],
    role_contributions: list[_RoleContribution],
    matchup_aggregates: list[MatchupAggregate],
    synergy_aggregates: list[SynergyAggregate],
    champion_role_statistics: list[ChampionRoleSufficientStatistics],
    target_patch: str,
    max_window_age: int,
    input_patch_ages: dict[str, int],
) -> dict[str, Counter[str]]:
    reconciliation: Counter[str] = Counter()
    invariants: Counter[str] = Counter()
    matchup_keys = [row.logical_key for row in matchup_aggregates]
    synergy_keys = [row.logical_key for row in synergy_aggregates]
    role_keys = [row.logical_key for row in champion_role_statistics]
    if len(matchup_keys) != len(set(matchup_keys)):
        invariants["duplicate_matchup_aggregate_key"] += 1
    if len(synergy_keys) != len(set(synergy_keys)):
        invariants["duplicate_synergy_aggregate_key"] += 1
    if len(role_keys) != len(set(role_keys)):
        invariants["duplicate_champion_role_key"] += 1

    expected_matchup = Counter(_pair_group_key(row) for row in matchup_contributions)
    expected_synergy = Counter(_pair_group_key(row) for row in synergy_contributions)
    actual_matchup = {
        _aggregate_pair_key(row): row.source_observation_count
        for row in matchup_aggregates
    }
    actual_synergy = {
        _aggregate_pair_key(row): row.source_observation_count
        for row in synergy_aggregates
    }
    if dict(expected_matchup) != actual_matchup:
        reconciliation["matchup_contribution_totals"] += 1
    if dict(expected_synergy) != actual_synergy:
        reconciliation["synergy_contribution_totals"] += 1

    matchup_aggregate_lookup = {
        _aggregate_pair_key(row): row for row in matchup_aggregates
    }
    for row in matchup_aggregates:
        reverse_key = (
            row.opponent_champion_id,
            row.opponent_position,
            row.focal_champion_id,
            row.focal_position,
            row.platform,
            row.region,
            row.region_lineage_status,
            row.rank_bracket,
            row.collection_stratum,
            row.rank_lineage_status,
            row.queue_id,
        )
        reverse = matchup_aggregate_lookup.get(reverse_key)
        if reverse is None:
            reconciliation["missing_reciprocal_matchup_aggregate"] += 1
        elif (
            row.statistics.observed_games != reverse.statistics.observed_games
            or row.statistics.wins != reverse.statistics.losses
            or row.statistics.losses != reverse.statistics.wins
        ):
            reconciliation["reciprocal_matchup_aggregate_outcomes"] += 1

    matchup_sources = [
        (row.match_id, *_pair_group_key(row)) for row in matchup_contributions
    ]
    synergy_sources = [
        (row.match_id, row.team_id, *_pair_group_key(row))
        for row in synergy_contributions
    ]
    if len(matchup_sources) != len(set(matchup_sources)):
        invariants["duplicate_directional_matchup_contribution"] += 1
    if len(synergy_sources) != len(set(synergy_sources)):
        invariants["duplicate_directional_synergy_contribution"] += 1

    matchup_source_lookup = {
        (
            row.match_id,
            row.focal_champion_id,
            row.focal_position,
            row.counterpart_champion_id,
            row.counterpart_position,
            *row.context.values(),
        ): row
        for row in matchup_contributions
    }
    for row in matchup_contributions:
        reverse = matchup_source_lookup.get(
            (
                row.match_id,
                row.counterpart_champion_id,
                row.counterpart_position,
                row.focal_champion_id,
                row.focal_position,
                *row.context.values(),
            )
        )
        if reverse is None:
            reconciliation["missing_reciprocal_matchup"] += 1
        elif reverse.win == row.win:
            reconciliation["reciprocal_matchup_outcome"] += 1

    for aggregate in [*matchup_aggregates, *synergy_aggregates]:
        _validate_statistics_bundle(
            aggregate.statistics,
            aggregate.patch_specific,
            aggregate.cumulative_patch_windows,
            source_count=aggregate.source_observation_count,
            target_patch=target_patch,
            max_window_age=max_window_age,
            input_patch_ages=input_patch_ages,
            reconciliation=reconciliation,
        )
        if (
            aggregate.statistical_status != STATISTICAL_STATUS_UNRESOLVED
            or aggregate.recommendation_eligibility is not None
            or any(
                value is not None
                for field, value in aggregate.posterior.model_dump().items()
                if field != "minimum_practical_advantage"
            )
        ):
            invariants["production_posterior_evaluated"] += 1

    for aggregate in matchup_aggregates:
        expected_focal = [
            row
            for row in role_contributions
            if _role_matches_aggregate_focal(row, aggregate)
            and row.matchup_eligible
            and row.opponent is not None
            and row.opponent
            != (aggregate.opponent_champion_id, aggregate.opponent_position)
        ]
        expected_opponent = [
            row
            for row in role_contributions
            if _role_matches_aggregate_opponent(row, aggregate)
            and row.matchup_eligible
            and row.opponent is not None
            and row.opponent != (aggregate.focal_champion_id, aggregate.focal_position)
        ]
        if aggregate.focal_leave_opponent_out.statistics.observed_games != len(
            expected_focal
        ):
            reconciliation["focal_matchup_exclusion"] += 1
        if aggregate.opponent_leave_focal_out.statistics.observed_games != len(
            expected_opponent
        ):
            reconciliation["opponent_matchup_exclusion"] += 1

    for aggregate in synergy_aggregates:
        expected_focal = [
            row
            for row in role_contributions
            if _role_matches_synergy_focal(row, aggregate)
            and row.synergy_eligible
            and (aggregate.ally_champion_id, aggregate.ally_position) not in row.allies
        ]
        expected_ally = [
            row
            for row in role_contributions
            if _role_matches_synergy_ally(row, aggregate)
            and row.synergy_eligible
            and (aggregate.focal_champion_id, aggregate.focal_position)
            not in row.allies
        ]
        if aggregate.focal_without_ally.statistics.observed_games != len(
            expected_focal
        ):
            reconciliation["focal_synergy_exclusion"] += 1
        if aggregate.ally_without_focal.statistics.observed_games != len(expected_ally):
            reconciliation["ally_synergy_exclusion"] += 1

    eligible_matchup = sum(row.matchup_eligibility for row in stage33a.participants)
    scoped_eligible_matchup = sum(
        row.matchup_eligibility and row.public_patch in input_patch_ages
        for row in stage33a.participants
    )
    if len(matchup_contributions) != scoped_eligible_matchup:
        reconciliation["stage3_3a_matchup_eligibility"] += 1
    if any(
        not row.matchup_eligibility
        for row in stage33a.participants
        if _participant_in_matchup_contributions(row, matchup_contributions)
    ):
        invariants["ineligible_matchup_contribution"] += 1
    if eligible_matchup < scoped_eligible_matchup:
        invariants["eligible_matchup_count"] += 1

    records: list[BaseModel] = [
        *matchup_aggregates,
        *synergy_aggregates,
        *champion_role_statistics,
    ]
    nonfinite = sum(_count_nonfinite(row.model_dump()) for row in records)
    if nonfinite:
        invariants["nonfinite_values"] += nonfinite
    return {"reconciliation": reconciliation, "invariants": invariants}


def _validate_statistics_bundle(
    statistics: SufficientStatistics,
    patch_specific: list[PatchSpecificStatistics],
    windows: list[CumulativePatchWindow],
    *,
    source_count: int,
    target_patch: str,
    max_window_age: int,
    input_patch_ages: dict[str, int],
    reconciliation: Counter[str],
) -> None:
    if (
        statistics.observed_games != source_count
        or statistics.wins + statistics.losses != source_count
        or sum(row.observed_games for row in patch_specific) != source_count
        or sum(row.wins for row in patch_specific) != statistics.wins
    ):
        reconciliation["aggregate_patch_totals"] += 1
    if len(windows) != max_window_age + 1:
        reconciliation["cumulative_window_count"] += 1
        return
    for index, window in enumerate(windows):
        if window.oldest_patch_age != index:
            reconciliation["cumulative_window_order"] += 1
        expected_patches = [
            _patch_at_age(_patch_tuple(target_patch), age) for age in range(index + 1)
        ]
        if window.considered_patches != expected_patches:
            reconciliation["cumulative_window_patches"] += 1
        stats = window.statistics
        if stats.wins + stats.losses != stats.observed_games:
            reconciliation["cumulative_window_outcomes"] += 1
        if not math.isclose(
            stats.weighted_wins + stats.weighted_losses,
            stats.sum_weights,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            reconciliation["weighted_outcomes"] += 1
        expected_missing = [
            age
            for age, patch in enumerate(expected_patches)
            if patch not in input_patch_ages
        ]
        if window.input_missing_patch_ages != expected_missing:
            reconciliation["missing_patch_ages"] += 1
        expected_eff = (
            stats.sum_weights**2 / stats.sum_squared_weights
            if stats.sum_squared_weights
            else None
        )
        if not _optional_close(stats.effective_sample_size, expected_eff):
            reconciliation["effective_sample_size"] += 1
    if windows[-1].statistics != statistics:
        reconciliation["final_window_total"] += 1
    for patch in patch_specific:
        expected_weight = PATCH_DECAY**patch.patch_age
        if patch.patch_weight != expected_weight:
            reconciliation["patch_weight"] += 1


def _quality_report(
    *,
    stage33a: Stage33AInput,
    output_directory: Path,
    target_patch: str,
    max_window_age: int,
    input_patch_ages: dict[str, int],
    matchup_contributions: list[_PairContribution],
    synergy_contributions: list[_PairContribution],
    role_contributions: list[_RoleContribution],
    matchup_aggregates: list[MatchupAggregate],
    synergy_aggregates: list[SynergyAggregate],
    champion_role_statistics: list[ChampionRoleSufficientStatistics],
    matchup_skips: Counter[str],
    synergy_skips: Counter[str],
    role_skips: Counter[str],
    validation: dict[str, Counter[str]],
) -> dict[str, Any]:
    matchup_sizes = [row.statistics.observed_games for row in matchup_aggregates]
    synergy_sizes = [row.statistics.observed_games for row in synergy_aggregates]
    matchup_effective = [
        row.statistics.effective_sample_size
        for row in matchup_aggregates
        if row.statistics.effective_sample_size is not None
    ]
    synergy_effective = [
        row.statistics.effective_sample_size
        for row in synergy_aggregates
        if row.statistics.effective_sample_size is not None
    ]
    focal_matchup_status = Counter(
        row.focal_leave_opponent_out.availability_status for row in matchup_aggregates
    )
    opponent_matchup_status = Counter(
        row.opponent_leave_focal_out.availability_status for row in matchup_aggregates
    )
    focal_synergy_status = Counter(
        row.focal_without_ally.availability_status for row in synergy_aggregates
    )
    ally_synergy_status = Counter(
        row.ally_without_focal.availability_status for row in synergy_aggregates
    )
    platform_counts = Counter(row.platform for row in stage33a.matches)
    role_counts = Counter(
        row.analysis_position
        for row in stage33a.participants
        if row.analysis_position is not None
    )
    viable_rows = sum(
        row.provisional_role_viability_rule_satisfied
        for row in champion_role_statistics
    )
    reconciliation = validation["reconciliation"]
    invariants = validation["invariants"]
    return {
        "processing_schema_version": AGGREGATION_SCHEMA_VERSION,
        "quality_report_schema_version": AGGREGATION_QUALITY_SCHEMA_VERSION,
        "aggregation_policy_version": AGGREGATION_POLICY_VERSION,
        "statistical_primitives_version": STATISTICAL_PRIMITIVES_VERSION,
        "input": {
            "directory": stage33a.input_directory.as_posix(),
            "schema_version": DRAFT_OBSERVATION_SCHEMA_VERSION,
            "row_counts": {
                "participant_draft_observations": len(stage33a.participants),
                "team_draft_observations": len(stage33a.teams),
                "match_draft_context": len(stage33a.matches),
            },
            "sha256": dict(sorted(stage33a.lineage_hashes.items())),
        },
        "eligible_observations": {
            "stage3_3a_matchup_eligible_participants": sum(
                row.matchup_eligibility for row in stage33a.participants
            ),
            "matchup_directional_contributions": len(matchup_contributions),
            "stage3_3a_synergy_eligible_participants": sum(
                row.synergy_eligibility for row in stage33a.participants
            ),
            "synergy_directional_contributions": len(synergy_contributions),
            "role_eligible_participants": sum(
                row.role_analysis_eligibility for row in stage33a.participants
            ),
            "role_contribution_rows": len(role_contributions),
            "matchup_skips": _sorted_counter(matchup_skips),
            "synergy_skips": _sorted_counter(synergy_skips),
            "role_skips": _sorted_counter(role_skips),
        },
        "outputs": {
            "directory": output_directory.as_posix(),
            "row_counts": {
                "matchup_aggregates": len(matchup_aggregates),
                "synergy_aggregates": len(synergy_aggregates),
                "champion_role_sufficient_statistics": len(champion_role_statistics),
            },
        },
        "sample_size_distributions": {
            "matchup_observed_games": _integer_distribution(matchup_sizes),
            "synergy_observed_games": _integer_distribution(synergy_sizes),
            "matchup_effective_sample_size": _float_distribution(matchup_effective),
            "synergy_effective_sample_size": _float_distribution(synergy_effective),
        },
        "patch_windows": {
            "target_patch": target_patch,
            "patch_decay": PATCH_DECAY,
            "max_previous_patches": MAX_PREVIOUS_PATCHES,
            "oldest_available_patch_age": max_window_age,
            "available_patches_by_age": dict(
                sorted(input_patch_ages.items(), key=lambda item: item[1])
            ),
            "missing_patch_ages": [
                age
                for age in range(max_window_age + 1)
                if age not in input_patch_ages.values()
            ],
            "evidence_based_early_stopping": False,
        },
        "baseline_component_availability": {
            "matchup_focal_leave_opponent_out": _sorted_counter(focal_matchup_status),
            "matchup_opponent_leave_focal_out": _sorted_counter(
                opponent_matchup_status
            ),
            "synergy_focal_without_ally": _sorted_counter(focal_synergy_status),
            "synergy_ally_without_focal": _sorted_counter(ally_synergy_status),
            "sparsity_threshold_status": STATISTICAL_STATUS_UNRESOLVED,
        },
        "champion_role_coverage": {
            "rows": len(champion_role_statistics),
            "champions": len({row.champion_id for row in champion_role_statistics}),
            "positions": _sorted_counter(role_counts),
            "provisional_rule_satisfied_rows": viable_rows,
            "share_threshold": ROLE_VIABILITY_SHARE,
            "minimum_games": ROLE_VIABILITY_MINIMUM_GAMES,
            "authoritative_role_viability": False,
            "sample_limitation": (
                "100 matches cannot establish authoritative full-roster role viability"
            ),
        },
        "lineage_coverage": {
            "platforms": _sorted_counter(platform_counts),
            "explicit_region_rows": sum(
                row.region is not None for row in stage33a.matches
            ),
            "rank_bracket_rows": sum(
                row.rank_bracket is not None for row in stage33a.matches
            ),
            "collection_stratum_rows": sum(
                row.collection_stratum is not None for row in stage33a.matches
            ),
        },
        "unresolved_policy_counts": {
            "matchup_posteriors_not_evaluated": len(matchup_aggregates),
            "synergy_posteriors_not_evaluated": len(synergy_aggregates),
            "counter_labels": 0,
            "strong_synergy_labels": 0,
        },
        "reconciliation_failures": _sorted_counter(reconciliation),
        "invariant_failures": _sorted_counter(invariants),
        "nonfinite_values": sum(
            _count_nonfinite(row.model_dump())
            for row in [
                *matchup_aggregates,
                *synergy_aggregates,
                *champion_role_statistics,
            ]
        ),
        "privacy": {
            "raw_riot_identifiers": False,
            "player_keys_in_outputs": False,
            "aggregate_report_only": True,
        },
        "limitations": {
            "counter_recommendations_supported": False,
            "causal_synergy_claims_supported": False,
            "posterior_evidence_evaluated": False,
            "reason": (
                "baseline combination, prior strength, calibration, and minimum "
                "effective-sample policies remain unresolved"
            ),
        },
        "ready_for_calibration": not reconciliation and not invariants,
    }


def _metadata(
    *,
    stage33a: Stage33AInput,
    output_directory: Path,
    target_patch: str,
    max_window_age: int,
    practical_advantage: float,
    matchup_count: int,
    synergy_count: int,
    role_count: int,
    output_hashes: dict[str, str],
) -> dict[str, Any]:
    return {
        "processing_schema_version": AGGREGATION_SCHEMA_VERSION,
        "quality_report_schema_version": AGGREGATION_QUALITY_SCHEMA_VERSION,
        "aggregation_policy_version": AGGREGATION_POLICY_VERSION,
        "statistical_primitives_version": STATISTICAL_PRIMITIVES_VERSION,
        "source_stage3_3a_policy_version": DRAFT_POLICY_VERSION,
        "run_id": stage33a.run_id,
        "generation_timestamp": _deterministic_run_timestamp(stage33a.run_id),
        "timestamp_policy": "deterministic UTC timestamp parsed from source run ID",
        "inputs": {
            "stage3_1": {
                "directory": stage33a.stage3_1_directory.as_posix(),
                "sha256": dict(sorted(stage33a.stage3_1_hashes.items())),
            },
            "stage3_2": {
                "directory": stage33a.stage3_2_directory.as_posix(),
                "sha256": dict(sorted(stage33a.stage3_2_hashes.items())),
            },
            "stage3_3a": {
                "directory": stage33a.input_directory.as_posix(),
                "sha256": dict(sorted(stage33a.lineage_hashes.items())),
            },
        },
        "generation_configuration": {
            "target_patch": target_patch,
            "target_patch_source": (
                "CLI when supplied; otherwise newest numeric public patch in input"
            ),
            "patch_decay": PATCH_DECAY,
            "max_previous_patches": MAX_PREVIOUS_PATCHES,
            "oldest_available_patch_age": max_window_age,
            "directional_matchups": True,
            "directional_synergies": True,
            "patch_window_early_stopping": False,
            "minimum_practical_advantage": practical_advantage,
            "posterior_population_evaluation": False,
        },
        "approved_parameters": {
            "patch_decay": PATCH_DECAY,
            "max_previous_patches": MAX_PREVIOUS_PATCHES,
            "minimum_practical_advantage": {
                "value": practical_advantage,
                "status": "provisional_pending_backtesting",
            },
            "evidence_probability_boundaries": {
                "moderate": MODERATE_EVIDENCE_PROBABILITY,
                "strong": STRONG_EVIDENCE_PROBABILITY,
            },
            "role_viability": {
                "champion_role_share": ROLE_VIABILITY_SHARE,
                "minimum_eligible_games": ROLE_VIABILITY_MINIMUM_GAMES,
                "authoritative_for_current_sample": False,
            },
        },
        "unresolved_parameters": {
            "baseline_combination_formula": None,
            "prior_equivalent_games": None,
            "minimum_effective_sample_size": None,
            "adaptive_window_stopping_rule": None,
            "calibration_method": None,
            "major_change_history_policy": None,
            "causal_synergy_controls": None,
        },
        "numerical_implementation": {
            "beta_survival_probability": "scipy.stats.beta.sf",
            "reason": (
                "survival function used directly for numerical stability near one"
            ),
            "custom_approximation": False,
        },
        "deterministic_ordering_policy": {
            "aggregate_rows": "logical group tuple ascending",
            "patches": "numeric public patch age ascending from target",
            "nested_exclusions": "counterpart champion ID then position",
            "serialization": "UTF-8 sorted-key JSON; NaN and infinity rejected",
        },
        "row_counts": {
            "matchup_aggregates": matchup_count,
            "synergy_aggregates": synergy_count,
            "champion_role_sufficient_statistics": role_count,
        },
        "output": {
            "directory": output_directory.as_posix(),
            "sha256": dict(sorted(output_hashes.items())),
            "metadata_hash_excluded_reason": "metadata cannot contain its own hash",
        },
        "storage_format": "deterministic-jsonl",
        "privacy": {
            "player_identifiers_in_outputs": False,
            "aggregate_reports_only": True,
        },
        "reproduction_command": REPRODUCTION_COMMAND,
    }


def _output_content_hashes(
    matchup_aggregates: list[MatchupAggregate],
    synergy_aggregates: list[SynergyAggregate],
    champion_role_statistics: list[ChampionRoleSufficientStatistics],
    quality_report: dict[str, Any],
) -> dict[str, str]:
    content = {
        "aggregation_quality_report.json": _json_bytes(quality_report),
        "champion_role_sufficient_statistics.jsonl": _rows_bytes(
            champion_role_statistics
        ),
        "matchup_aggregates.jsonl": _rows_bytes(matchup_aggregates),
        "synergy_aggregates.jsonl": _rows_bytes(synergy_aggregates),
    }
    return {
        name: hashlib.sha256(value).hexdigest()
        for name, value in sorted(content.items())
    }


def _write_staged_dataset(dataset: AggregationDataset, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _write_bytes(
        directory / "matchup_aggregates.jsonl",
        _rows_bytes(dataset.matchup_aggregates),
    )
    _write_bytes(
        directory / "synergy_aggregates.jsonl",
        _rows_bytes(dataset.synergy_aggregates),
    )
    _write_bytes(
        directory / "champion_role_sufficient_statistics.jsonl",
        _rows_bytes(dataset.champion_role_statistics),
    )
    _write_bytes(
        directory / "aggregation_quality_report.json",
        _json_bytes(dataset.quality_report),
    )
    _write_bytes(directory / "metadata.json", _json_bytes(dataset.metadata))


def _validate_existing_output_lineage(
    output_directory: Path, stage33a: Stage33AInput
) -> None:
    if not output_directory.exists():
        return
    metadata_path = output_directory / "metadata.json"
    if not metadata_path.is_file():
        raise Stage3ValidationError(
            "existing_stage3_3b_lineage",
            "existing Stage 3.3B output has no lineage metadata",
        )
    metadata = _load_json_object(metadata_path, "existing_stage3_3b_metadata")
    inputs = metadata.get("inputs")
    stage33a_input = inputs.get("stage3_3a") if isinstance(inputs, dict) else None
    if (
        metadata.get("processing_schema_version") != AGGREGATION_SCHEMA_VERSION
        or metadata.get("run_id") != stage33a.run_id
        or not isinstance(stage33a_input, dict)
        or stage33a_input.get("sha256") != dict(sorted(stage33a.lineage_hashes.items()))
    ):
        raise Stage3ValidationError(
            "stage3_3a_lineage_changed",
            "Stage 3.3A hashes differ from existing Stage 3.3B lineage",
        )


def _verify_prior_lineage(
    inputs: dict[str, Any], stage_name: str
) -> tuple[Path, dict[str, str]]:
    item = inputs.get(stage_name)
    if (
        not isinstance(item, dict)
        or not isinstance(item.get("directory"), str)
        or not isinstance(item.get("sha256"), dict)
    ):
        raise Stage3ValidationError(
            "stage3_3a_lineage", f"Stage 3.3A {stage_name} lineage is invalid"
        )
    directory = Path(item["directory"])
    expected_hashes = item["sha256"]
    if not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in expected_hashes.items()
    ):
        raise Stage3ValidationError(
            "stage3_3a_lineage", f"Stage 3.3A {stage_name} hashes are invalid"
        )
    actual = _hash_files(directory, tuple(sorted(expected_hashes)))
    if actual != expected_hashes:
        raise Stage3ValidationError(
            "prior_stage_hash_conflict",
            f"{stage_name} artifacts differ from Stage 3.3A lineage",
        )
    return directory, actual


def _validate_stage33a_keys(
    participants: list[ParticipantDraftObservation],
    teams: list[TeamDraftObservation],
    matches: list[MatchDraftContext],
) -> None:
    participant_keys = [(row.match_id, row.participant_id) for row in participants]
    team_keys = [(row.match_id, row.team_id) for row in teams]
    match_keys = [row.match_id for row in matches]
    if len(participant_keys) != len(set(participant_keys)):
        raise Stage3ValidationError(
            "duplicate_stage3_3a_participant", "Stage 3.3A participant keys duplicate"
        )
    if len(team_keys) != len(set(team_keys)):
        raise Stage3ValidationError(
            "duplicate_stage3_3a_team", "Stage 3.3A team keys duplicate"
        )
    if len(match_keys) != len(set(match_keys)):
        raise Stage3ValidationError(
            "duplicate_stage3_3a_match", "Stage 3.3A match keys duplicate"
        )
    source_ids = {row.match_id for row in matches}
    if {row.match_id for row in participants} != source_ids or {
        row.match_id for row in teams
    } != source_ids:
        raise Stage3ValidationError(
            "stage3_3a_match_lineage", "Stage 3.3A tables have different match IDs"
        )
    if any(
        row.processing_schema_version != DRAFT_OBSERVATION_SCHEMA_VERSION
        for row in [*participants, *teams, *matches]
    ):
        raise Stage3ValidationError(
            "incompatible_stage3_3a_row_schema", "Stage 3.3A row schema is incompatible"
        )


def _context(row: ParticipantDraftObservation) -> _Context:
    return _Context(
        platform=row.platform,
        region=row.region,
        region_lineage_status=row.region_lineage_status,
        rank_bracket=row.rank_bracket,
        collection_stratum=row.collection_stratum,
        rank_lineage_status=row.rank_lineage_status,
        queue_id=row.queue_id,
    )


def _context_from_values(values: tuple[Any, ...]) -> _Context:
    return _Context(
        platform=values[0],
        region=values[1],
        region_lineage_status=values[2],
        rank_bracket=values[3],
        collection_stratum=values[4],
        rank_lineage_status=values[5],
        queue_id=values[6],
    )


def _context_dict(context: _Context) -> dict[str, Any]:
    return {
        "platform": context.platform,
        "region": context.region,
        "region_lineage_status": context.region_lineage_status,
        "rank_bracket": context.rank_bracket,
        "collection_stratum": context.collection_stratum,
        "rank_lineage_status": context.rank_lineage_status,
        "queue_id": context.queue_id,
    }


def _pair_group_key(row: _PairContribution) -> tuple[Any, ...]:
    return (
        row.focal_champion_id,
        row.focal_position,
        row.counterpart_champion_id,
        row.counterpart_position,
        *row.context.values(),
    )


def _aggregate_pair_key(row: MatchupAggregate | SynergyAggregate) -> tuple[Any, ...]:
    counterpart_id = (
        row.opponent_champion_id
        if isinstance(row, MatchupAggregate)
        else row.ally_champion_id
    )
    counterpart_position = (
        row.opponent_position
        if isinstance(row, MatchupAggregate)
        else row.ally_position
    )
    return (
        row.focal_champion_id,
        row.focal_position,
        counterpart_id,
        counterpart_position,
        row.platform,
        row.region,
        row.region_lineage_status,
        row.rank_bracket,
        row.collection_stratum,
        row.rank_lineage_status,
        row.queue_id,
    )


def _role_group_key(row: _RoleContribution) -> tuple[Any, ...]:
    return (row.champion_id, row.position, *row.context.values())


def _contribution_sort_key(row: _PairContribution) -> tuple[Any, ...]:
    return (*_pair_group_key(row), row.match_id, row.team_id)


def _role_contribution_sort_key(row: _RoleContribution) -> tuple[Any, ...]:
    return (*_role_group_key(row), row.match_id, row.team_id)


def _role_matches_aggregate_focal(
    row: _RoleContribution, aggregate: MatchupAggregate
) -> bool:
    return (
        row.champion_id == aggregate.focal_champion_id
        and row.position == aggregate.focal_position
        and row.context.values()
        == (
            aggregate.platform,
            aggregate.region,
            aggregate.region_lineage_status,
            aggregate.rank_bracket,
            aggregate.collection_stratum,
            aggregate.rank_lineage_status,
            aggregate.queue_id,
        )
    )


def _role_matches_aggregate_opponent(
    row: _RoleContribution, aggregate: MatchupAggregate
) -> bool:
    return (
        row.champion_id == aggregate.opponent_champion_id
        and row.position == aggregate.opponent_position
        and row.context.values()
        == (
            aggregate.platform,
            aggregate.region,
            aggregate.region_lineage_status,
            aggregate.rank_bracket,
            aggregate.collection_stratum,
            aggregate.rank_lineage_status,
            aggregate.queue_id,
        )
    )


def _role_matches_synergy_focal(
    row: _RoleContribution, aggregate: SynergyAggregate
) -> bool:
    return (
        row.champion_id == aggregate.focal_champion_id
        and row.position == aggregate.focal_position
        and row.context.values()
        == (
            aggregate.platform,
            aggregate.region,
            aggregate.region_lineage_status,
            aggregate.rank_bracket,
            aggregate.collection_stratum,
            aggregate.rank_lineage_status,
            aggregate.queue_id,
        )
    )


def _role_matches_synergy_ally(
    row: _RoleContribution, aggregate: SynergyAggregate
) -> bool:
    return (
        row.champion_id == aggregate.ally_champion_id
        and row.position == aggregate.ally_position
        and row.context.values()
        == (
            aggregate.platform,
            aggregate.region,
            aggregate.region_lineage_status,
            aggregate.rank_bracket,
            aggregate.collection_stratum,
            aggregate.rank_lineage_status,
            aggregate.queue_id,
        )
    )


def _participant_in_matchup_contributions(
    participant: ParticipantDraftObservation,
    contributions: list[_PairContribution],
) -> bool:
    return any(
        row.match_id == participant.match_id
        and row.team_id == participant.team_id
        and row.focal_champion_id == participant.champion_id
        for row in contributions
    )


def _champion_names(
    participants: list[ParticipantDraftObservation],
) -> dict[int, str | None]:
    names: dict[int, set[str]] = defaultdict(set)
    champion_ids = {row.champion_id for row in participants}
    for row in participants:
        if row.champion_name is not None:
            names[row.champion_id].add(row.champion_name)
    conflicts = {
        champion_id: values for champion_id, values in names.items() if len(values) > 1
    }
    if conflicts:
        raise Stage3ValidationError(
            "champion_name_conflict", "one champion ID has conflicting names"
        )
    return {
        champion_id: next(iter(names[champion_id]), None)
        for champion_id in sorted(champion_ids)
    }


def _patch_tuple(public_patch: str) -> tuple[int, int]:
    parts = public_patch.split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise Stage3ValidationError(
            "invalid_public_patch", "public patch must contain numeric major.minor"
        )
    return int(parts[0]), int(parts[1])


def _patch_age(target: tuple[int, int], patch: tuple[int, int]) -> int:
    if target[0] != patch[0]:
        raise Stage3ValidationError(
            "cross_major_patch_policy_unresolved",
            "cross-major patch history requires an approved major-change policy",
        )
    return target[1] - patch[1]


def _patch_at_age(target: tuple[int, int], age: int) -> str:
    minor = target[1] - age
    if minor < 0:
        raise Stage3ValidationError(
            "invalid_patch_window", "patch window crosses below minor zero"
        )
    return f"{target[0]}.{minor}"


def _availability_status(games: int) -> str:
    return "available" if games else "unavailable_no_leave_pair_out_observations"


def _logical_key(values: tuple[Any, ...]) -> str:
    return json.dumps(
        values, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _finite_number(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _optional_close(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def _integer_distribution(values: list[int]) -> dict[str, Any]:
    return {
        "count": len(values),
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
        "median": median(values) if values else None,
        "value_counts": _sorted_numeric_counter(Counter(values)),
    }


def _float_distribution(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
        "median": median(values) if values else None,
        "value_counts": dict(
            sorted(
                Counter(format(value, ".17g") for value in values).items(),
                key=lambda item: float(item[0]),
            )
        ),
    }


def _sorted_numeric_counter(counter: Counter[int]) -> dict[str, int]:
    return {str(key): counter[key] for key in sorted(counter)}


def _sorted_counter(counter: Counter[Any]) -> dict[str, int]:
    return dict(sorted((str(key), value) for key, value in counter.items()))


def _hash_files(directory: Path, names: tuple[str, ...]) -> dict[str, str]:
    try:
        return {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in sorted(names)
        }
    except OSError:
        raise Stage3ValidationError(
            "input_hash_failure", "an input artifact could not be hashed"
        ) from None


def _load_jsonl_models(path: Path, model: type[BaseModel], category: str) -> list[Any]:
    rows: list[Any] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise Stage3ValidationError(
            category, "an input table could not be read"
        ) from None
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line, object_pairs_hook=_unique_object)
            rows.append(model.model_validate(value))
        except (json.JSONDecodeError, ValidationError):
            raise Stage3ValidationError(category, "an input row is invalid") from None
    return rows


def _load_json_object(path: Path, category: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise Stage3ValidationError(
            category, "an input JSON object is invalid"
        ) from None
    if not isinstance(value, dict):
        raise Stage3ValidationError(category, "an input JSON value must be an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object key", key, 0)
        result[key] = value
    return result


def _rows_bytes(rows: list[BaseModel]) -> bytes:
    return "".join(
        json.dumps(
            row.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
        for row in rows
    ).encode()


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _count_nonfinite(value: Any) -> int:
    if isinstance(value, float):
        return int(not math.isfinite(value))
    if isinstance(value, dict):
        return sum(_count_nonfinite(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_nonfinite(item) for item in value)
    return 0


def _deterministic_run_timestamp(run_id: str) -> str:
    prefix = run_id.split("-", 1)[0]
    try:
        parsed = datetime.strptime(prefix, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise Stage3ValidationError(
            "invalid_source_run_id", "source run ID has no deterministic timestamp"
        ) from None
    return parsed.isoformat()


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
