"""Stage 3.3A factual draft observations for future champion-select analysis."""

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
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from nexus_lens.analytics import (
    ANALYTICAL_QUALITY_SCHEMA_VERSION,
    ANALYTICAL_SCHEMA_VERSION,
    CANONICAL_POSITIONS,
    EXPECTED_MATCH_COUNT,
    EXPECTED_PARTICIPANT_COUNT,
    EXPECTED_TEAM_COUNT,
    FORMULA_CONTRACT_VERSION,
    MatchAnalysisContext,
    ParticipantMatchFeature,
    Stage31Input,
    TeamMatchFeature,
    load_stage3_1_input,
    stage3_1_patch_counts,
)
from nexus_lens.canonical import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalBan,
    CanonicalMatch,
    CanonicalParticipant,
    Stage3ValidationError,
)

DRAFT_OBSERVATION_SCHEMA_VERSION = "stage3.3a-v1"
DRAFT_QUALITY_SCHEMA_VERSION = "stage3.3a-quality-v1"
DRAFT_POLICY_VERSION = "stage3.3a-draft-policy-v1"
EXPECTED_BAN_COUNT = 1_000
EXPECTED_PARTICIPANTS_PER_TEAM = 5
EXPECTED_TEAMS_PER_MATCH = 2
REQUIRED_STAGE3_2_FILES = (
    "match_analysis_context.jsonl",
    "metadata.json",
    "participant_match_features.jsonl",
    "quality_report.json",
    "team_match_features.jsonl",
)
REPRODUCTION_COMMAND = (
    ".\\.venv\\Scripts\\python.exe scripts\\build_draft_observations.py "
    "--stage3-1-run "
    "data/processed/stage3/schema=stage3.1-v1/"
    "run=20260722T125547567196Z-population "
    "--stage3-2-run "
    "data/processed/stage3/schema=stage3.2-v1/"
    "run=20260722T125547567196Z-population"
)


class DraftModel(BaseModel):
    """Strict base for immutable Stage 3.3A output rows."""

    model_config = ConfigDict(extra="forbid")


class DraftBan(DraftModel):
    pick_turn: int | None
    champion_id: int | None


class DraftTeamComposition(DraftModel):
    team_id: int
    champion_ids: list[int]
    win: bool


class DraftTeamBans(DraftModel):
    team_id: int
    bans: list[DraftBan]


class ParticipantDraftObservation(DraftModel):
    match_id: str
    participant_id: int
    player_key: str
    champion_id: int
    champion_name: str | None
    team_id: int
    win: bool
    team_win: bool
    enemy_team_win: bool
    public_patch: str
    queue_id: int
    platform: str
    region: str | None
    region_lineage_status: str
    rank_bracket: str | None
    collection_stratum: str | None
    rank_lineage_status: str
    game_duration_seconds: int
    short_game: bool
    team_position: str | None
    individual_position: str | None
    analysis_position: str | None
    analysis_position_source: str
    position_disagreement: bool
    position_pairing_eligibility: bool
    allied_champion_ids: list[int]
    enemy_champion_ids: list[int]
    lane_opponent_participant_id: int | None
    lane_opponent_champion_id: int | None
    lane_opponent_champion_name: str | None
    lane_opponent_resolution_status: str
    lane_opponent_resolution_reason: str
    general_analysis_eligibility: bool
    role_analysis_eligibility: bool
    matchup_eligibility: bool
    matchup_exclusion_reasons: list[str]
    synergy_eligibility: bool
    synergy_exclusion_reasons: list[str]
    team_structure_valid: bool
    opponent_team_structure_valid: bool
    source_run_id: str
    source_stage3_1_schema_version: str
    source_stage3_2_schema_version: str
    processing_schema_version: str


class TeamDraftObservation(DraftModel):
    match_id: str
    team_id: int
    win: bool
    opponent_team_id: int
    opponent_win: bool
    public_patch: str
    queue_id: int
    platform: str
    region: str | None
    region_lineage_status: str
    rank_bracket: str | None
    collection_stratum: str | None
    rank_lineage_status: str
    game_duration_seconds: int
    short_game: bool
    champion_ids: list[int]
    opponent_champion_ids: list[int]
    bans: list[DraftBan]
    opponent_bans: list[DraftBan]
    team_structure_valid: bool
    opponent_team_structure_valid: bool
    positions_complete: bool
    all_lane_opponents_resolved: bool
    general_analysis_eligibility: bool
    matchup_eligibility: bool
    matchup_exclusion_reasons: list[str]
    synergy_eligibility: bool
    synergy_exclusion_reasons: list[str]
    source_run_id: str
    source_stage3_1_schema_version: str
    source_stage3_2_schema_version: str
    processing_schema_version: str


class MatchDraftContext(DraftModel):
    match_id: str
    public_patch: str
    queue_id: int
    platform: str
    region: str | None
    region_lineage_status: str
    rank_bracket: str | None
    collection_stratum: str | None
    rank_lineage_status: str
    game_duration_seconds: int
    short_game: bool
    participant_count: int
    team_count: int
    participants_complete: bool
    teams_complete: bool
    positions_complete: bool
    position_disagreement_count: int
    position_fallback_count: int
    unresolved_position_count: int
    team_compositions: list[DraftTeamComposition]
    team_bans: list[DraftTeamBans]
    resolved_lane_opponent_pairs: int
    all_five_lane_opponent_pairs_resolved: bool
    general_analysis_eligibility: bool
    general_exclusion_reasons: list[str]
    matchup_eligibility: bool
    matchup_exclusion_reasons: list[str]
    synergy_eligibility: bool
    synergy_exclusion_reasons: list[str]
    source_run_id: str
    source_stage3_1_schema_version: str
    source_stage3_2_schema_version: str
    processing_schema_version: str


@dataclass(frozen=True)
class Stage32Input:
    run_id: str
    input_directory: Path
    participant_features: list[ParticipantMatchFeature]
    team_features: list[TeamMatchFeature]
    match_contexts: list[MatchAnalysisContext]
    lineage_hashes: dict[str, str]


@dataclass
class DraftObservationDataset:
    run_id: str
    stage3_1_directory: Path
    stage3_2_directory: Path
    output_directory: Path
    participant_observations: list[ParticipantDraftObservation]
    team_observations: list[TeamDraftObservation]
    match_contexts: list[MatchDraftContext]
    quality_report: dict[str, Any]
    metadata: dict[str, Any]


def run_stage3_3a(
    *,
    stage3_1_directory: Path,
    stage3_2_directory: Path,
    output_root: Path,
    validate_only: bool,
    expected_match_count: int = EXPECTED_MATCH_COUNT,
    expected_participant_count: int = EXPECTED_PARTICIPANT_COUNT,
    expected_team_count: int = EXPECTED_TEAM_COUNT,
    expected_ban_count: int = EXPECTED_BAN_COUNT,
    expected_patch_counts: dict[str, int] | None = None,
) -> DraftObservationDataset:
    """Validate prior stages, build observations, and optionally publish them."""

    expected_patches = dict(
        expected_patch_counts
        if expected_patch_counts is not None
        else stage3_1_patch_counts(
            stage3_1_directory, expected_match_count=expected_match_count
        )
    )
    stage31 = load_stage3_1_input(
        stage3_1_directory,
        expected_match_count=expected_match_count,
        expected_participant_count=expected_participant_count,
        expected_team_count=expected_team_count,
        expected_patch_counts=expected_patches,
    )
    if len(stage31.bans) != expected_ban_count:
        raise Stage3ValidationError(
            "stage3_1_ban_count", "Stage 3.1 ban row count disagrees"
        )
    stage32 = load_stage3_2_input(
        stage3_2_directory,
        stage3_1_hashes=stage31.lineage_hashes,
        expected_match_count=expected_match_count,
        expected_participant_count=expected_participant_count,
        expected_team_count=expected_team_count,
        expected_patch_counts=expected_patches,
    )
    if stage31.run_id != stage32.run_id:
        raise Stage3ValidationError(
            "prior_stage_run_conflict", "Stage 3.1 and Stage 3.2 run IDs disagree"
        )
    output_directory = (
        output_root
        / f"schema={DRAFT_OBSERVATION_SCHEMA_VERSION}"
        / f"run={stage31.run_id}"
    )
    _validate_existing_output_lineage(
        output_directory,
        stage3_1_hashes=stage31.lineage_hashes,
        stage3_2_hashes=stage32.lineage_hashes,
        run_id=stage31.run_id,
    )
    dataset = build_draft_observation_dataset(
        stage31=stage31,
        stage32=stage32,
        output_directory=output_directory,
        expected_match_count=expected_match_count,
        expected_participant_count=expected_participant_count,
        expected_team_count=expected_team_count,
        expected_patch_counts=expected_patches,
    )
    if not validate_only:
        write_draft_observation_dataset(dataset)
    return dataset


def load_stage3_2_input(
    input_directory: Path,
    *,
    stage3_1_hashes: dict[str, str],
    expected_match_count: int,
    expected_participant_count: int,
    expected_team_count: int,
    expected_patch_counts: dict[str, int],
) -> Stage32Input:
    """Load Stage 3.2 only when schema, quality, and Stage 3.1 lineage agree."""

    missing = [
        name
        for name in REQUIRED_STAGE3_2_FILES
        if not (input_directory / name).is_file()
    ]
    if missing:
        raise Stage3ValidationError(
            "stage3_2_input_missing", "required Stage 3.2 files are missing"
        )
    hashes = _hash_files(input_directory, REQUIRED_STAGE3_2_FILES)
    metadata = _load_json_object(input_directory / "metadata.json", "stage3_2_metadata")
    quality = _load_json_object(
        input_directory / "quality_report.json", "stage3_2_quality"
    )
    if metadata.get("processing_schema_version") != ANALYTICAL_SCHEMA_VERSION:
        raise Stage3ValidationError(
            "incompatible_stage3_2_schema", "Stage 3.2 schema is incompatible"
        )
    if metadata.get("formula_contract_version") != FORMULA_CONTRACT_VERSION:
        raise Stage3ValidationError(
            "incompatible_formula_contract",
            "Stage 3.2 formula contract is incompatible",
        )
    if (
        quality.get("quality_report_schema_version")
        != ANALYTICAL_QUALITY_SCHEMA_VERSION
    ):
        raise Stage3ValidationError(
            "incompatible_stage3_2_quality", "Stage 3.2 quality schema is incompatible"
        )
    if quality.get(
        "ready_for_stage_3_3_analysis_validation"
    ) is not True or quality.get("invariant_failures"):
        raise Stage3ValidationError(
            "stage3_2_not_ready", "Stage 3.2 quality gate is not satisfied"
        )
    recorded_stage31 = metadata.get("input")
    if not isinstance(recorded_stage31, dict) or recorded_stage31.get("sha256") != dict(
        sorted(stage3_1_hashes.items())
    ):
        raise Stage3ValidationError(
            "stage3_2_lineage_conflict", "Stage 3.2 does not match Stage 3.1 hashes"
        )
    run_id = metadata.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise Stage3ValidationError("stage3_2_metadata", "Stage 3.2 run ID is missing")
    participants = _load_jsonl_models(
        input_directory / "participant_match_features.jsonl",
        ParticipantMatchFeature,
        "stage3_2_participants",
    )
    teams = _load_jsonl_models(
        input_directory / "team_match_features.jsonl",
        TeamMatchFeature,
        "stage3_2_teams",
    )
    contexts = _load_jsonl_models(
        input_directory / "match_analysis_context.jsonl",
        MatchAnalysisContext,
        "stage3_2_contexts",
    )
    if (
        len(participants) != expected_participant_count
        or len(teams) != expected_team_count
        or len(contexts) != expected_match_count
    ):
        raise Stage3ValidationError(
            "stage3_2_row_count", "Stage 3.2 physical row counts disagree"
        )
    if metadata.get("match_counts_by_public_patch") != expected_patch_counts:
        raise Stage3ValidationError(
            "stage3_2_patch_counts", "Stage 3.2 patch counts disagree"
        )
    participant_keys = [(row.match_id, row.participant_id) for row in participants]
    team_keys = [(row.match_id, row.team_id) for row in teams]
    context_ids = [row.match_id for row in contexts]
    if len(participant_keys) != len(set(participant_keys)):
        raise Stage3ValidationError(
            "duplicate_stage3_2_participant", "Stage 3.2 participant keys duplicate"
        )
    if len(team_keys) != len(set(team_keys)):
        raise Stage3ValidationError(
            "duplicate_stage3_2_team", "Stage 3.2 team keys duplicate"
        )
    if len(context_ids) != len(set(context_ids)):
        raise Stage3ValidationError(
            "duplicate_stage3_2_context", "Stage 3.2 match keys duplicate"
        )
    if any(
        row.processing_schema_version != ANALYTICAL_SCHEMA_VERSION
        for rows in (participants, teams, contexts)
        for row in rows
    ):
        raise Stage3ValidationError(
            "incompatible_stage3_2_row_schema", "Stage 3.2 row schema is incompatible"
        )
    return Stage32Input(
        run_id=run_id,
        input_directory=input_directory,
        participant_features=participants,
        team_features=teams,
        match_contexts=contexts,
        lineage_hashes=hashes,
    )


def build_draft_observation_dataset(
    *,
    stage31: Stage31Input,
    stage32: Stage32Input,
    output_directory: Path,
    expected_match_count: int,
    expected_participant_count: int,
    expected_team_count: int,
    expected_patch_counts: dict[str, int],
) -> DraftObservationDataset:
    """Transform compatible prior-stage rows into factual draft observations."""

    quality = _quality_accumulator()
    matches = {row.match_id: row for row in stage31.matches}
    canonical_participants = {
        (row.match_id, row.participant_id): row for row in stage31.participants
    }
    features = {
        (row.match_id, row.participant_id): row for row in stage32.participant_features
    }
    team_features = {(row.match_id, row.team_id): row for row in stage32.team_features}
    contexts = {row.match_id: row for row in stage32.match_contexts}
    _validate_prior_stage_reconciliation(
        stage31, stage32, canonical_participants, features, quality
    )
    participants_by_match: dict[str, list[CanonicalParticipant]] = defaultdict(list)
    bans_by_team: dict[tuple[str, int], list[CanonicalBan]] = defaultdict(list)
    for row in stage31.participants:
        participants_by_match[row.match_id].append(row)
    for row in stage31.bans:
        bans_by_team[(row.match_id, row.team_id)].append(row)

    participant_observations: list[ParticipantDraftObservation] = []
    team_observations: list[TeamDraftObservation] = []
    match_contexts: list[MatchDraftContext] = []
    for match_id in sorted(matches):
        match = matches[match_id]
        context = contexts[match_id]
        participants = sorted(
            participants_by_match[match_id], key=lambda row: row.participant_id
        )
        team_ids = sorted({row.team_id for row in participants})
        members_by_team = {
            team_id: [row for row in participants if row.team_id == team_id]
            for team_id in team_ids
        }
        valid_match_structure = (
            len(participants) == 10
            and len(team_ids) == EXPECTED_TEAMS_PER_MATCH
            and all(
                len(members) == EXPECTED_PARTICIPANTS_PER_TEAM
                for members in members_by_team.values()
            )
        )
        resolutions = _resolve_lane_opponents(
            participants,
            members_by_team,
            features,
            valid_match_structure=valid_match_structure,
        )
        observations_for_match = _participant_observations(
            stage31.run_id,
            match,
            context,
            participants,
            members_by_team,
            features,
            team_features,
            resolutions,
            quality,
        )
        participant_observations.extend(observations_for_match)
        reciprocal_pairs = _resolved_reciprocal_pairs(
            observations_for_match, canonical_participants, quality
        )
        all_pairs_resolved = valid_match_structure and len(reciprocal_pairs) == 5
        match_team_observations = _team_observations(
            stage31.run_id,
            match,
            context,
            members_by_team,
            team_features,
            bans_by_team,
            observations_for_match,
            all_pairs_resolved,
            valid_match_structure,
        )
        team_observations.extend(match_team_observations)
        match_contexts.append(
            _match_draft_context(
                stage31.run_id,
                match,
                context,
                members_by_team,
                bans_by_team,
                valid_match_structure,
                len(reciprocal_pairs),
                all_pairs_resolved,
            )
        )

    participant_observations.sort(key=lambda row: (row.match_id, row.participant_id))
    team_observations.sort(key=lambda row: (row.match_id, row.team_id))
    match_contexts.sort(key=lambda row: row.match_id)
    _validate_outputs(
        stage31,
        stage32,
        participant_observations,
        team_observations,
        match_contexts,
        bans_by_team,
        quality,
    )
    quality_report = _finalize_quality_report(
        stage31=stage31,
        stage32=stage32,
        participant_observations=participant_observations,
        team_observations=team_observations,
        match_contexts=match_contexts,
        quality=quality,
        output_directory=output_directory,
        expected_match_count=expected_match_count,
        expected_participant_count=expected_participant_count,
        expected_team_count=expected_team_count,
        expected_patch_counts=expected_patch_counts,
    )
    output_hashes = _output_content_hashes(
        participant_observations,
        team_observations,
        match_contexts,
        quality_report,
    )
    metadata = _build_metadata(
        stage31=stage31,
        stage32=stage32,
        output_directory=output_directory,
        output_hashes=output_hashes,
        participant_count=len(participant_observations),
        team_count=len(team_observations),
        match_count=len(match_contexts),
    )
    return DraftObservationDataset(
        run_id=stage31.run_id,
        stage3_1_directory=stage31.input_directory,
        stage3_2_directory=stage32.input_directory,
        output_directory=output_directory,
        participant_observations=participant_observations,
        team_observations=team_observations,
        match_contexts=match_contexts,
        quality_report=quality_report,
        metadata=metadata,
    )


def write_draft_observation_dataset(dataset: DraftObservationDataset) -> Path:
    """Stage and atomically publish an invariant-clean Stage 3.3A run."""

    if not dataset.quality_report["ready_for_matchup_synergy_aggregation"]:
        raise Stage3ValidationError(
            "stage3_3a_invariant_failure",
            "draft observations were not written because validation failed",
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


def _validate_prior_stage_reconciliation(
    stage31: Stage31Input,
    stage32: Stage32Input,
    canonical: dict[tuple[str, int], CanonicalParticipant],
    features: dict[tuple[str, int], ParticipantMatchFeature],
    quality: dict[str, Any],
) -> None:
    if set(canonical) != set(features):
        raise Stage3ValidationError(
            "prior_stage_participant_lineage",
            "Stage 3.1 and Stage 3.2 participant keys disagree",
        )
    stage31_match_ids = {row.match_id for row in stage31.matches}
    if {row.match_id for row in stage32.match_contexts} != stage31_match_ids:
        raise Stage3ValidationError(
            "prior_stage_match_lineage", "Stage 3.1 and Stage 3.2 match keys disagree"
        )
    for key, canonical_row in canonical.items():
        feature = features[key]
        if (
            feature.player_key != canonical_row.player_key
            or feature.champion_id != canonical_row.champion_id
            or feature.team_id != canonical_row.team_id
            or feature.win != canonical_row.win
        ):
            raise Stage3ValidationError(
                "prior_stage_participant_conflict",
                "Stage 3.1 and Stage 3.2 participant facts disagree",
            )
        if feature.champion_name != canonical_row.champion_name:
            quality["prior_stage_reconciliation"]["champion_name"] += 1


def _resolve_lane_opponents(
    participants: list[CanonicalParticipant],
    members_by_team: dict[int, list[CanonicalParticipant]],
    features: dict[tuple[str, int], ParticipantMatchFeature],
    *,
    valid_match_structure: bool,
) -> dict[tuple[str, int], tuple[CanonicalParticipant | None, str, str]]:
    resolutions = {}
    team_ids = sorted(members_by_team)
    for participant in participants:
        key = (participant.match_id, participant.participant_id)
        feature = features[key]
        if not valid_match_structure or len(team_ids) != 2:
            resolutions[key] = (
                None,
                "invalid_team_structure",
                "normal_five_vs_five_structure_is_not_available",
            )
            continue
        if not _position_pairing_eligible(feature):
            resolutions[key] = (
                None,
                "participant_position_ineligible",
                _position_ineligible_reason(feature),
            )
            continue
        opponent_team_id = next(
            team for team in team_ids if team != participant.team_id
        )
        candidates = [
            candidate
            for candidate in members_by_team[opponent_team_id]
            if _position_pairing_eligible(
                features[(candidate.match_id, candidate.participant_id)]
            )
            and features[
                (candidate.match_id, candidate.participant_id)
            ].analysis_position
            == feature.analysis_position
        ]
        if len(candidates) == 1:
            candidate = candidates[0]
            reciprocal_candidates = [
                teammate
                for teammate in members_by_team[participant.team_id]
                if _position_pairing_eligible(
                    features[(teammate.match_id, teammate.participant_id)]
                )
                and features[
                    (teammate.match_id, teammate.participant_id)
                ].analysis_position
                == feature.analysis_position
            ]
            if len(reciprocal_candidates) == 1:
                resolutions[key] = (
                    candidate,
                    "resolved_unique",
                    "exactly_one_reciprocal_eligible_opponent_with_same_position",
                )
            else:
                resolutions[key] = (
                    None,
                    "nonreciprocal_position_group",
                    "opposing_candidate_cannot_resolve_a_unique_reciprocal_pair",
                )
        elif not candidates:
            resolutions[key] = (
                None,
                "missing_unique_opponent",
                "no_eligible_opponent_with_same_analysis_position",
            )
        else:
            resolutions[key] = (
                None,
                "ambiguous_opponent_position",
                "multiple_eligible_opponents_with_same_analysis_position",
            )
    return resolutions


def _participant_observations(
    run_id: str,
    match: CanonicalMatch,
    context: MatchAnalysisContext,
    participants: list[CanonicalParticipant],
    members_by_team: dict[int, list[CanonicalParticipant]],
    features: dict[tuple[str, int], ParticipantMatchFeature],
    team_features: dict[tuple[str, int], TeamMatchFeature],
    resolutions: dict[tuple[str, int], tuple[CanonicalParticipant | None, str, str]],
    quality: dict[str, Any],
) -> list[ParticipantDraftObservation]:
    observations = []
    team_ids = sorted(members_by_team)
    for participant in participants:
        key = (participant.match_id, participant.participant_id)
        feature = features[key]
        team = team_features[(participant.match_id, participant.team_id)]
        opponent_team_id = (
            next((value for value in team_ids if value != participant.team_id), None)
            if len(team_ids) == 2
            else None
        )
        team_members = members_by_team.get(participant.team_id, [])
        opponent_members = members_by_team.get(opponent_team_id, [])
        team_valid = len(team_members) == EXPECTED_PARTICIPANTS_PER_TEAM
        opponent_valid = len(opponent_members) == EXPECTED_PARTICIPANTS_PER_TEAM
        allied_ids = (
            [
                row.champion_id
                for row in sorted(team_members, key=lambda item: item.participant_id)
                if row.participant_id != participant.participant_id
            ]
            if team_valid
            else []
        )
        enemy_ids = [
            row.champion_id
            for row in sorted(opponent_members, key=lambda item: item.participant_id)
        ]
        opponent, resolution_status, resolution_reason = resolutions[key]
        matchup_reasons = list(feature.exclusion_reasons)
        if not feature.role_aggregation_eligibility:
            matchup_reasons.extend(feature.role_exclusion_reasons)
        if resolution_status != "resolved_unique":
            matchup_reasons.append("lane_opponent_unresolved")
        matchup_reasons = _unique_reasons(matchup_reasons)
        synergy_reasons = list(feature.exclusion_reasons)
        if not team_valid:
            synergy_reasons.append("invalid_team_structure")
        synergy_reasons = _unique_reasons(synergy_reasons)
        quality["lane_resolution_statuses"][resolution_status] += 1
        if participant.champion_name is None:
            quality["missing_champion_names"] += 1
        observations.append(
            ParticipantDraftObservation(
                match_id=participant.match_id,
                participant_id=participant.participant_id,
                player_key=participant.player_key,
                champion_id=participant.champion_id,
                champion_name=participant.champion_name,
                team_id=participant.team_id,
                win=participant.win,
                team_win=team.win,
                enemy_team_win=not team.win,
                public_patch=match.public_patch,
                queue_id=match.queue_id,
                platform=match.platform,
                region=None,
                region_lineage_status="platform_available_region_not_explicit",
                rank_bracket=None,
                collection_stratum=None,
                rank_lineage_status="unavailable_in_stage3_inputs",
                game_duration_seconds=match.game_duration_seconds,
                short_game=context.short_game,
                team_position=feature.team_position,
                individual_position=feature.individual_position,
                analysis_position=feature.analysis_position,
                analysis_position_source=feature.analysis_position_source,
                position_disagreement=feature.position_disagreement,
                position_pairing_eligibility=_position_pairing_eligible(feature),
                allied_champion_ids=allied_ids,
                enemy_champion_ids=enemy_ids,
                lane_opponent_participant_id=(
                    opponent.participant_id if opponent is not None else None
                ),
                lane_opponent_champion_id=(
                    opponent.champion_id if opponent is not None else None
                ),
                lane_opponent_champion_name=(
                    opponent.champion_name if opponent is not None else None
                ),
                lane_opponent_resolution_status=resolution_status,
                lane_opponent_resolution_reason=resolution_reason,
                general_analysis_eligibility=feature.analytical_eligibility,
                role_analysis_eligibility=feature.role_aggregation_eligibility,
                matchup_eligibility=not matchup_reasons,
                matchup_exclusion_reasons=matchup_reasons,
                synergy_eligibility=not synergy_reasons,
                synergy_exclusion_reasons=synergy_reasons,
                team_structure_valid=team_valid,
                opponent_team_structure_valid=opponent_valid,
                source_run_id=run_id,
                source_stage3_1_schema_version=CANONICAL_SCHEMA_VERSION,
                source_stage3_2_schema_version=ANALYTICAL_SCHEMA_VERSION,
                processing_schema_version=DRAFT_OBSERVATION_SCHEMA_VERSION,
            )
        )
    return observations


def _resolved_reciprocal_pairs(
    observations: list[ParticipantDraftObservation],
    canonical: dict[tuple[str, int], CanonicalParticipant],
    quality: dict[str, Any],
) -> set[frozenset[int]]:
    by_participant = {row.participant_id: row for row in observations}
    pairs: set[frozenset[int]] = set()
    for row in observations:
        if row.lane_opponent_resolution_status != "resolved_unique":
            continue
        if row.lane_opponent_participant_id is None:
            quality["reconciliation_failures"]["lane_opponent_reciprocity"] += 1
            continue
        opponent = by_participant.get(row.lane_opponent_participant_id)
        if opponent is None:
            quality["reconciliation_failures"]["lane_opponent_reciprocity"] += 1
            continue
        source = canonical[(row.match_id, row.participant_id)]
        if (
            opponent.lane_opponent_participant_id != row.participant_id
            or opponent.lane_opponent_champion_id != source.champion_id
        ):
            quality["reconciliation_failures"]["lane_opponent_reciprocity"] += 1
            continue
        pairs.add(frozenset({row.participant_id, opponent.participant_id}))
    return pairs


def _team_observations(
    run_id: str,
    match: CanonicalMatch,
    context: MatchAnalysisContext,
    members_by_team: dict[int, list[CanonicalParticipant]],
    team_features: dict[tuple[str, int], TeamMatchFeature],
    bans_by_team: dict[tuple[str, int], list[CanonicalBan]],
    participant_observations: list[ParticipantDraftObservation],
    all_pairs_resolved: bool,
    valid_match_structure: bool,
) -> list[TeamDraftObservation]:
    rows = []
    team_ids = sorted(members_by_team)
    for team_id in team_ids:
        opponent_team_id = next(value for value in team_ids if value != team_id)
        team = team_features[(match.match_id, team_id)]
        opponent = team_features[(match.match_id, opponent_team_id)]
        members = sorted(members_by_team[team_id], key=lambda item: item.participant_id)
        opponents = sorted(
            members_by_team[opponent_team_id], key=lambda item: item.participant_id
        )
        member_observations = [
            row for row in participant_observations if row.team_id == team_id
        ]
        matchup_reasons = list(context.exclusion_reasons)
        if not all_pairs_resolved:
            matchup_reasons.append("incomplete_lane_opponent_pairs")
        matchup_reasons = _unique_reasons(matchup_reasons)
        synergy_reasons = list(context.exclusion_reasons)
        if not valid_match_structure:
            synergy_reasons.append("invalid_team_structure")
        synergy_reasons = _unique_reasons(synergy_reasons)
        rows.append(
            TeamDraftObservation(
                match_id=match.match_id,
                team_id=team_id,
                win=team.win,
                opponent_team_id=opponent_team_id,
                opponent_win=opponent.win,
                public_patch=match.public_patch,
                queue_id=match.queue_id,
                platform=match.platform,
                region=None,
                region_lineage_status="platform_available_region_not_explicit",
                rank_bracket=None,
                collection_stratum=None,
                rank_lineage_status="unavailable_in_stage3_inputs",
                game_duration_seconds=match.game_duration_seconds,
                short_game=context.short_game,
                champion_ids=[row.champion_id for row in members],
                opponent_champion_ids=[row.champion_id for row in opponents],
                bans=_draft_bans(bans_by_team[(match.match_id, team_id)]),
                opponent_bans=_draft_bans(
                    bans_by_team[(match.match_id, opponent_team_id)]
                ),
                team_structure_valid=len(members) == EXPECTED_PARTICIPANTS_PER_TEAM,
                opponent_team_structure_valid=len(opponents)
                == EXPECTED_PARTICIPANTS_PER_TEAM,
                positions_complete=context.positions_complete,
                all_lane_opponents_resolved=all_pairs_resolved,
                general_analysis_eligibility=team.analytical_eligibility,
                matchup_eligibility=(
                    not matchup_reasons
                    and all(row.matchup_eligibility for row in member_observations)
                ),
                matchup_exclusion_reasons=matchup_reasons,
                synergy_eligibility=not synergy_reasons,
                synergy_exclusion_reasons=synergy_reasons,
                source_run_id=run_id,
                source_stage3_1_schema_version=CANONICAL_SCHEMA_VERSION,
                source_stage3_2_schema_version=ANALYTICAL_SCHEMA_VERSION,
                processing_schema_version=DRAFT_OBSERVATION_SCHEMA_VERSION,
            )
        )
    return rows


def _match_draft_context(
    run_id: str,
    match: CanonicalMatch,
    context: MatchAnalysisContext,
    members_by_team: dict[int, list[CanonicalParticipant]],
    bans_by_team: dict[tuple[str, int], list[CanonicalBan]],
    valid_match_structure: bool,
    resolved_pairs: int,
    all_pairs_resolved: bool,
) -> MatchDraftContext:
    team_ids = sorted(members_by_team)
    matchup_reasons = list(context.exclusion_reasons)
    if not all_pairs_resolved:
        matchup_reasons.append("incomplete_lane_opponent_pairs")
    synergy_reasons = list(context.exclusion_reasons)
    if not valid_match_structure:
        synergy_reasons.append("invalid_team_structure")
    return MatchDraftContext(
        match_id=match.match_id,
        public_patch=match.public_patch,
        queue_id=match.queue_id,
        platform=match.platform,
        region=None,
        region_lineage_status="platform_available_region_not_explicit",
        rank_bracket=None,
        collection_stratum=None,
        rank_lineage_status="unavailable_in_stage3_inputs",
        game_duration_seconds=match.game_duration_seconds,
        short_game=context.short_game,
        participant_count=context.participant_count,
        team_count=context.team_count,
        participants_complete=context.participants_complete,
        teams_complete=context.teams_complete,
        positions_complete=context.positions_complete,
        position_disagreement_count=context.position_disagreement_count,
        position_fallback_count=context.position_fallback_count,
        unresolved_position_count=context.unresolved_position_count,
        team_compositions=[
            DraftTeamComposition(
                team_id=team_id,
                champion_ids=[
                    row.champion_id
                    for row in sorted(
                        members_by_team[team_id], key=lambda item: item.participant_id
                    )
                ],
                win=any(row.win for row in members_by_team[team_id]),
            )
            for team_id in team_ids
        ],
        team_bans=[
            DraftTeamBans(
                team_id=team_id,
                bans=_draft_bans(bans_by_team[(match.match_id, team_id)]),
            )
            for team_id in team_ids
        ],
        resolved_lane_opponent_pairs=resolved_pairs,
        all_five_lane_opponent_pairs_resolved=all_pairs_resolved,
        general_analysis_eligibility=context.analytical_eligibility,
        general_exclusion_reasons=list(context.exclusion_reasons),
        matchup_eligibility=not _unique_reasons(matchup_reasons),
        matchup_exclusion_reasons=_unique_reasons(matchup_reasons),
        synergy_eligibility=not _unique_reasons(synergy_reasons),
        synergy_exclusion_reasons=_unique_reasons(synergy_reasons),
        source_run_id=run_id,
        source_stage3_1_schema_version=CANONICAL_SCHEMA_VERSION,
        source_stage3_2_schema_version=ANALYTICAL_SCHEMA_VERSION,
        processing_schema_version=DRAFT_OBSERVATION_SCHEMA_VERSION,
    )


def _validate_outputs(
    stage31: Stage31Input,
    stage32: Stage32Input,
    participants: list[ParticipantDraftObservation],
    teams: list[TeamDraftObservation],
    contexts: list[MatchDraftContext],
    bans_by_team: dict[tuple[str, int], list[CanonicalBan]],
    quality: dict[str, Any],
) -> None:
    participant_keys = [(row.match_id, row.participant_id) for row in participants]
    team_keys = [(row.match_id, row.team_id) for row in teams]
    context_keys = [row.match_id for row in contexts]
    quality["duplicates"]["participant"] = len(participant_keys) - len(
        set(participant_keys)
    )
    quality["duplicates"]["team"] = len(team_keys) - len(set(team_keys))
    quality["duplicates"]["match"] = len(context_keys) - len(set(context_keys))
    canonical = {
        (row.match_id, row.participant_id): row for row in stage31.participants
    }
    matches = {row.match_id: row for row in stage31.matches}
    source_participants_by_team: dict[tuple[str, int], list[CanonicalParticipant]] = (
        defaultdict(list)
    )
    for source in stage31.participants:
        source_participants_by_team[(source.match_id, source.team_id)].append(source)
    participant_observations_by_match: dict[str, list[ParticipantDraftObservation]] = (
        defaultdict(list)
    )
    team_observations_by_match: dict[str, list[TeamDraftObservation]] = defaultdict(
        list
    )
    for row in participants:
        participant_observations_by_match[row.match_id].append(row)
    for row in teams:
        team_observations_by_match[row.match_id].append(row)
    features = {
        (row.match_id, row.participant_id): row for row in stage32.participant_features
    }
    for row in participants:
        key = (row.match_id, row.participant_id)
        source = canonical[key]
        feature = features[key]
        team_members = sorted(
            source_participants_by_team[(row.match_id, row.team_id)],
            key=lambda item: item.participant_id,
        )
        opponent_members = sorted(
            [
                item
                for item in stage31.participants
                if item.match_id == row.match_id and item.team_id != row.team_id
            ],
            key=lambda item: item.participant_id,
        )
        expected_allies = (
            [
                item.champion_id
                for item in team_members
                if item.participant_id != row.participant_id
            ]
            if len(team_members) == EXPECTED_PARTICIPANTS_PER_TEAM
            else []
        )
        expected_enemies = [item.champion_id for item in opponent_members]
        if row.allied_champion_ids != expected_allies:
            quality["reconciliation_failures"]["allied_composition"] += 1
        if row.enemy_champion_ids != expected_enemies:
            quality["reconciliation_failures"]["enemy_composition"] += 1
        if source.champion_id in row.allied_champion_ids:
            quality["reconciliation_failures"]["participant_as_own_ally"] += 1
        if source.champion_id in row.enemy_champion_ids:
            quality["reconciliation_failures"]["participant_as_own_enemy"] += 1
        if row.team_structure_valid and len(row.allied_champion_ids) != 4:
            quality["reconciliation_failures"]["ally_count"] += 1
        if row.opponent_team_structure_valid and len(row.enemy_champion_ids) != 5:
            quality["reconciliation_failures"]["enemy_count"] += 1
        if row.public_patch != matches[row.match_id].public_patch:
            quality["reconciliation_failures"]["participant_patch"] += 1
        if (
            row.general_analysis_eligibility != feature.analytical_eligibility
            or row.role_analysis_eligibility != feature.role_aggregation_eligibility
        ):
            quality["reconciliation_failures"]["participant_eligibility"] += 1
    canonical_bans = {
        key: [(item.pick_turn, item.champion_id) for item in _sorted_bans(value)]
        for key, value in bans_by_team.items()
    }
    for row in teams:
        observed = [(item.pick_turn, item.champion_id) for item in row.bans]
        if observed != canonical_bans[(row.match_id, row.team_id)]:
            quality["reconciliation_failures"]["team_bans"] += 1
        expected_team = [
            item.champion_id
            for item in sorted(
                source_participants_by_team[(row.match_id, row.team_id)],
                key=lambda item: item.participant_id,
            )
        ]
        expected_opponent = [
            item.champion_id
            for item in sorted(
                [
                    item
                    for item in stage31.participants
                    if item.match_id == row.match_id and item.team_id != row.team_id
                ],
                key=lambda item: item.participant_id,
            )
        ]
        if row.champion_ids != expected_team:
            quality["reconciliation_failures"]["team_composition"] += 1
        if row.opponent_champion_ids != expected_opponent:
            quality["reconciliation_failures"]["opponent_composition"] += 1
        if row.win == row.opponent_win:
            quality["reconciliation_failures"]["team_outcomes"] += 1
    for context in contexts:
        match_participants = participant_observations_by_match[context.match_id]
        match_teams = team_observations_by_match[context.match_id]
        if context.participants_complete and len(match_participants) != 10:
            quality["reconciliation_failures"]["participants_per_match"] += 1
        if context.teams_complete and len(match_teams) != 2:
            quality["reconciliation_failures"]["teams_per_match"] += 1
        expected_compositions = [
            (row.team_id, row.champion_ids, row.win)
            for row in sorted(match_teams, key=lambda item: item.team_id)
        ]
        observed_compositions = [
            (row.team_id, row.champion_ids, row.win)
            for row in context.team_compositions
        ]
        if observed_compositions != expected_compositions:
            quality["reconciliation_failures"]["match_compositions"] += 1
        expected_bans = [
            (
                row.team_id,
                [(item.pick_turn, item.champion_id) for item in row.bans],
            )
            for row in sorted(match_teams, key=lambda item: item.team_id)
        ]
        observed_bans = [
            (
                row.team_id,
                [(item.pick_turn, item.champion_id) for item in row.bans],
            )
            for row in context.team_bans
        ]
        if observed_bans != expected_bans:
            quality["reconciliation_failures"]["match_bans"] += 1
    nonfinite = sum(
        _count_nonfinite(row.model_dump()) for row in [*participants, *teams, *contexts]
    )
    quality["nonfinite_values"] = nonfinite


def _finalize_quality_report(
    *,
    stage31: Stage31Input,
    stage32: Stage32Input,
    participant_observations: list[ParticipantDraftObservation],
    team_observations: list[TeamDraftObservation],
    match_contexts: list[MatchDraftContext],
    quality: dict[str, Any],
    output_directory: Path,
    expected_match_count: int,
    expected_participant_count: int,
    expected_team_count: int,
    expected_patch_counts: dict[str, int],
) -> dict[str, Any]:
    invariants: Counter[str] = Counter()
    if len(participant_observations) != expected_participant_count:
        invariants["participant_row_count"] += 1
    if len(team_observations) != expected_team_count:
        invariants["team_row_count"] += 1
    if len(match_contexts) != expected_match_count:
        invariants["match_row_count"] += 1
    for category, count in quality["duplicates"].items():
        if count:
            invariants[f"duplicate_{category}_keys"] += count
    if quality["reconciliation_failures"]:
        invariants["reconciliation_failures"] += sum(
            quality["reconciliation_failures"].values()
        )
    if quality["nonfinite_values"]:
        invariants["nonfinite_values"] += quality["nonfinite_values"]
    patch_counts = dict(
        sorted(Counter(row.public_patch for row in match_contexts).items())
    )
    if patch_counts != dict(sorted(expected_patch_counts.items())):
        invariants["patch_counts"] += 1
    lane_statuses = _sorted_counter(quality["lane_resolution_statuses"])
    matchup_eligible = sum(row.matchup_eligibility for row in participant_observations)
    synergy_eligible = sum(row.synergy_eligibility for row in participant_observations)
    valid_teams = sum(row.team_structure_valid for row in team_observations)
    all_pair_matches = sum(
        row.all_five_lane_opponent_pairs_resolved for row in match_contexts
    )
    explicit_no_bans = sum(
        item.champion_id == -1 for row in team_observations for item in row.bans
    )
    return {
        "processing_schema_version": DRAFT_OBSERVATION_SCHEMA_VERSION,
        "quality_report_schema_version": DRAFT_QUALITY_SCHEMA_VERSION,
        "draft_policy_version": DRAFT_POLICY_VERSION,
        "inputs": {
            "stage3_1": {
                "directory": stage31.input_directory.as_posix(),
                "schema_version": CANONICAL_SCHEMA_VERSION,
                "sha256": dict(sorted(stage31.lineage_hashes.items())),
            },
            "stage3_2": {
                "directory": stage32.input_directory.as_posix(),
                "schema_version": ANALYTICAL_SCHEMA_VERSION,
                "sha256": dict(sorted(stage32.lineage_hashes.items())),
            },
        },
        "outputs": {
            "directory": output_directory.as_posix(),
            "row_counts": {
                "participant_draft_observations": len(participant_observations),
                "team_draft_observations": len(team_observations),
                "match_draft_context": len(match_contexts),
            },
            "match_counts_by_public_patch": patch_counts,
        },
        "structure": {
            "participant_observations_with_four_allies": sum(
                len(row.allied_champion_ids) == 4 for row in participant_observations
            ),
            "participant_observations_with_five_enemies": sum(
                len(row.enemy_champion_ids) == 5 for row in participant_observations
            ),
            "valid_team_observations": valid_teams,
            "matches_with_all_five_lane_opponent_pairs": all_pair_matches,
        },
        "lane_opponents": {
            "resolution_statuses": lane_statuses,
            "resolved_participant_observations": lane_statuses.get(
                "resolved_unique", 0
            ),
            "unresolved_participant_observations": len(participant_observations)
            - lane_statuses.get("resolved_unique", 0),
            "matches_with_all_five_pairs": all_pair_matches,
        },
        "eligibility": {
            "general_analysis_eligible_participants": sum(
                row.general_analysis_eligibility for row in participant_observations
            ),
            "role_analysis_eligible_participants": sum(
                row.role_analysis_eligibility for row in participant_observations
            ),
            "matchup_eligible_participants": matchup_eligible,
            "matchup_ineligible_participants": len(participant_observations)
            - matchup_eligible,
            "synergy_eligible_participants": synergy_eligible,
            "synergy_ineligible_participants": len(participant_observations)
            - synergy_eligible,
            "factual_ally_relationships": sum(
                len(row.allied_champion_ids) for row in participant_observations
            ),
        },
        "positions": {
            "disagreements": sum(
                row.position_disagreement for row in participant_observations
            ),
            "fallbacks": sum(
                row.analysis_position_source == "individual_position_fallback"
                for row in participant_observations
            ),
            "unresolved": sum(
                row.analysis_position is None for row in participant_observations
            ),
        },
        "lineage_availability": {
            "platforms": _sorted_counter(
                Counter(row.platform for row in match_contexts)
            ),
            "explicit_region_rows": sum(
                row.region is not None for row in match_contexts
            ),
            "rank_bracket_rows": sum(
                row.rank_bracket is not None for row in match_contexts
            ),
            "collection_stratum_rows": sum(
                row.collection_stratum is not None for row in match_contexts
            ),
        },
        "bans": {
            "rows": sum(len(row.bans) for row in team_observations),
            "explicit_no_ban_rows": explicit_no_bans,
        },
        "missing_champion_names": quality["missing_champion_names"],
        "prior_stage_reconciliation": _sorted_counter(
            quality["prior_stage_reconciliation"]
        ),
        "reconciliation_failures": _sorted_counter(quality["reconciliation_failures"]),
        "nonfinite_values": quality["nonfinite_values"],
        "skipped_or_error_categories": _sorted_counter(
            quality["skipped_or_error_categories"]
        ),
        "invariant_failures": _sorted_counter(invariants),
        "privacy": {
            "approved_player_identifier": "player_key",
            "additional_identity_fields": False,
            "aggregate_report_only": True,
        },
        "limitations": {
            "authoritative_role_viability": False,
            "reason": (
                "100 matches cannot establish full-roster champion-role viability "
                "under the approved 5-percent and 50-game requirements"
            ),
            "pick_order_reconstructed": False,
            "statistical_conclusions_computed": False,
        },
        "ready_for_matchup_synergy_aggregation": not invariants,
    }


def _build_metadata(
    *,
    stage31: Stage31Input,
    stage32: Stage32Input,
    output_directory: Path,
    output_hashes: dict[str, str],
    participant_count: int,
    team_count: int,
    match_count: int,
) -> dict[str, Any]:
    return {
        "processing_schema_version": DRAFT_OBSERVATION_SCHEMA_VERSION,
        "quality_report_schema_version": DRAFT_QUALITY_SCHEMA_VERSION,
        "draft_policy_version": DRAFT_POLICY_VERSION,
        "formula_contract_version": FORMULA_CONTRACT_VERSION,
        "run_id": stage31.run_id,
        "generation_timestamp": _deterministic_run_timestamp(stage31.run_id),
        "timestamp_policy": "deterministic UTC timestamp parsed from source run ID",
        "inputs": {
            "stage3_1": {
                "directory": stage31.input_directory.as_posix(),
                "sha256": dict(sorted(stage31.lineage_hashes.items())),
            },
            "stage3_2": {
                "directory": stage32.input_directory.as_posix(),
                "sha256": dict(sorted(stage32.lineage_hashes.items())),
            },
        },
        "generation_configuration": {
            "recognized_roles": sorted(CANONICAL_POSITIONS),
            "lane_opponent_policy": (
                "exactly one position-eligible opposing participant with the same "
                "Stage 3.2 analysis_position"
            ),
            "position_pairing_policy": (
                "team_position source, recognized position, and no disagreement"
            ),
            "allied_champion_order": "participant_id ascending, self excluded",
            "enemy_champion_order": "participant_id ascending",
            "ban_order": "pick_turn ascending, null last, champion_id tie-break",
            "pick_order_reconstruction": False,
            "rank_lineage_source": "unavailable in Stage 3.1/3.2 inputs",
        },
        "row_counts": {
            "participant_draft_observations": participant_count,
            "team_draft_observations": team_count,
            "match_draft_context": match_count,
        },
        "output": {
            "directory": output_directory.as_posix(),
            "sha256": dict(sorted(output_hashes.items())),
            "metadata_hash_excluded_reason": "metadata cannot contain its own hash",
        },
        "reproduction_command": REPRODUCTION_COMMAND,
        "storage_format": "deterministic-jsonl",
        "privacy": {
            "approved_player_identifier": "player_key",
            "additional_identity_fields": False,
        },
    }


def _output_content_hashes(
    participants: list[ParticipantDraftObservation],
    teams: list[TeamDraftObservation],
    contexts: list[MatchDraftContext],
    quality_report: dict[str, Any],
) -> dict[str, str]:
    contents = {
        "participant_draft_observations.jsonl": _rows_bytes(participants),
        "team_draft_observations.jsonl": _rows_bytes(teams),
        "match_draft_context.jsonl": _rows_bytes(contexts),
        "draft_observation_quality_report.json": _json_bytes(quality_report),
    }
    return {
        name: hashlib.sha256(content).hexdigest() for name, content in contents.items()
    }


def _write_staged_dataset(dataset: DraftObservationDataset, directory: Path) -> None:
    _write_bytes(
        directory / "participant_draft_observations.jsonl",
        _rows_bytes(dataset.participant_observations),
    )
    _write_bytes(
        directory / "team_draft_observations.jsonl",
        _rows_bytes(dataset.team_observations),
    )
    _write_bytes(
        directory / "match_draft_context.jsonl",
        _rows_bytes(dataset.match_contexts),
    )
    _write_bytes(
        directory / "draft_observation_quality_report.json",
        _json_bytes(dataset.quality_report),
    )
    _write_bytes(directory / "metadata.json", _json_bytes(dataset.metadata))


def _validate_existing_output_lineage(
    output_directory: Path,
    *,
    stage3_1_hashes: dict[str, str],
    stage3_2_hashes: dict[str, str],
    run_id: str,
) -> None:
    if not output_directory.exists():
        return
    metadata_path = output_directory / "metadata.json"
    if not metadata_path.is_file():
        raise Stage3ValidationError(
            "existing_stage3_3a_lineage", "existing output has no lineage metadata"
        )
    metadata = _load_json_object(metadata_path, "existing_stage3_3a_metadata")
    inputs = metadata.get("inputs")
    stage31 = inputs.get("stage3_1") if isinstance(inputs, dict) else None
    stage32 = inputs.get("stage3_2") if isinstance(inputs, dict) else None
    if (
        metadata.get("processing_schema_version") != DRAFT_OBSERVATION_SCHEMA_VERSION
        or metadata.get("run_id") != run_id
        or not isinstance(stage31, dict)
        or not isinstance(stage32, dict)
        or stage31.get("sha256") != dict(sorted(stage3_1_hashes.items()))
        or stage32.get("sha256") != dict(sorted(stage3_2_hashes.items()))
    ):
        raise Stage3ValidationError(
            "prior_stage_lineage_changed",
            "prior-stage hashes differ from existing Stage 3.3A lineage",
        )


def _position_pairing_eligible(feature: ParticipantMatchFeature) -> bool:
    return bool(
        feature.analysis_position in CANONICAL_POSITIONS
        and feature.analysis_position_source == "team_position"
        and not feature.position_disagreement
    )


def _position_ineligible_reason(feature: ParticipantMatchFeature) -> str:
    if feature.position_disagreement:
        return "participant_position_disagreement"
    if feature.analysis_position_source == "individual_position_fallback":
        return "participant_position_fallback"
    return "participant_position_missing_or_unrecognized"


def _draft_bans(rows: list[CanonicalBan]) -> list[DraftBan]:
    return [
        DraftBan(pick_turn=row.pick_turn, champion_id=row.champion_id)
        for row in _sorted_bans(rows)
    ]


def _sorted_bans(rows: list[CanonicalBan]) -> list[CanonicalBan]:
    return sorted(
        rows,
        key=lambda row: (
            row.pick_turn if row.pick_turn is not None else 10_000,
            row.champion_id if row.champion_id is not None else -1,
        ),
    )


def _quality_accumulator() -> dict[str, Any]:
    return {
        "duplicates": Counter(),
        "lane_resolution_statuses": Counter(),
        "missing_champion_names": 0,
        "nonfinite_values": 0,
        "prior_stage_reconciliation": Counter(),
        "reconciliation_failures": Counter(),
        "skipped_or_error_categories": Counter(),
    }


def _deterministic_run_timestamp(run_id: str) -> str:
    prefix = run_id.removesuffix("-population")
    try:
        value = datetime.strptime(prefix, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise Stage3ValidationError(
            "run_timestamp", "source run ID does not contain a valid UTC timestamp"
        ) from None
    return value.isoformat()


def _unique_reasons(reasons: list[str]) -> list[str]:
    return list(dict.fromkeys(reasons))


def _hash_files(directory: Path, names: tuple[str, ...]) -> dict[str, str]:
    return {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in names
    }


def _load_jsonl_models(path: Path, model: type[BaseModel], category: str) -> list[Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise Stage3ValidationError(
            category, "prior-stage table could not be read"
        ) from None
    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rows.append(
                model.model_validate(json.loads(line, object_pairs_hook=_unique_object))
            )
        except (json.JSONDecodeError, ValidationError):
            raise Stage3ValidationError(
                category, "prior-stage row is invalid"
            ) from None
    return rows


def _load_json_object(path: Path, category: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise Stage3ValidationError(category, "prior-stage JSON is invalid") from None
    if not isinstance(value, dict):
        raise Stage3ValidationError(category, "prior-stage JSON must be an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object key", key, 0)
        result[key] = value
    return result


def _rows_bytes(rows: list[BaseModel]) -> bytes:
    text = "".join(
        json.dumps(
            row.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
        for row in rows
    )
    return text.encode("utf-8")


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
    ).encode("utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
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


def _sorted_counter(counter: Counter[Any]) -> dict[str, int]:
    return dict(sorted((str(key), value) for key, value in counter.items()))


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
