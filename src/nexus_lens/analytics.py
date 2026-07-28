"""Offline Stage 3.2 analytical features derived from Stage 3.1 tables."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from nexus_lens.canonical import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalBan,
    CanonicalMatch,
    CanonicalParticipant,
    CanonicalTeam,
    Stage3ValidationError,
)
from nexus_lens.formulas import (
    FORMULA_CONTRACT,
    FORMULA_CONTRACT_VERSION,
    RATIO_UNIT,
    complete_sum,
    duration_minutes,
    kda,
    per_minute,
    ratio,
    total_cs,
)

ANALYTICAL_SCHEMA_VERSION = "stage3.2-v1"
ANALYTICAL_QUALITY_SCHEMA_VERSION = "stage3.2-quality-v1"
EXPECTED_MATCH_COUNT = 100
EXPECTED_PARTICIPANT_COUNT = 1_000
EXPECTED_TEAM_COUNT = 200
EXPECTED_PATCH_COUNTS = {"26.13": 53, "26.14": 47}
EXPECTED_PARTICIPANTS_PER_MATCH = 10
EXPECTED_TEAMS_PER_MATCH = 2
SHORT_GAME_THRESHOLD_SECONDS = 300
CANONICAL_POSITIONS = frozenset({"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"})
SHARE_TOLERANCE = 1e-9
REQUIRED_STAGE3_1_FILES = (
    "bans.jsonl",
    "matches.jsonl",
    "metadata.json",
    "participants.jsonl",
    "quality_report.json",
    "teams.jsonl",
)
PARTICIPANT_DERIVED_METRICS = (
    "kda",
    "kill_participation",
    "total_cs",
    "cs_per_minute",
    "gold_per_minute",
    "team_gold_share",
    "damage_to_champions_per_minute",
    "team_champion_damage_share",
    "damage_taken_per_minute",
    "damage_mitigated_per_minute",
    "vision_score_per_minute",
    "total_heal_per_minute",
    "teammate_healing_per_minute",
    "teammate_shielding_per_minute",
)
TEAM_DERIVED_METRICS = (
    "team_kills",
    "team_deaths",
    "team_assists",
    "team_gold",
    "team_champion_damage",
    "team_cs",
)


class AnalyticalModel(BaseModel):
    """Strict base for deterministic Stage 3.2 records."""

    model_config = ConfigDict(extra="forbid")


class ParticipantMatchFeature(AnalyticalModel):
    match_id: str
    participant_id: int
    player_key: str
    public_patch: str
    team_id: int
    champion_id: int
    champion_name: str
    win: bool
    team_position: str | None
    individual_position: str | None
    analysis_position: str | None
    analysis_position_source: str
    position_disagreement: bool
    analytical_eligibility: bool
    exclusion_reasons: list[str]
    role_aggregation_eligibility: bool
    role_exclusion_reasons: list[str]
    game_duration_seconds: int
    short_game: bool
    kills: int | None
    deaths: int | None
    assists: int | None
    kda: float | None
    team_kills: int | None
    kill_participation: float | None
    total_minions_killed: int | None
    neutral_minions_killed: int | None
    total_cs: int | None
    cs_per_minute: float | None
    gold_earned: int | None
    team_gold: int | None
    gold_per_minute: float | None
    team_gold_share: float | None
    total_damage_to_champions: int | None
    team_champion_damage: int | None
    damage_to_champions_per_minute: float | None
    team_champion_damage_share: float | None
    physical_damage_to_champions: int | None
    magic_damage_to_champions: int | None
    true_damage_to_champions: int | None
    damage_taken: int | None
    damage_taken_per_minute: float | None
    damage_mitigated: int | None
    damage_mitigated_per_minute: float | None
    vision_score: int | None
    vision_score_per_minute: float | None
    wards_placed: int | None
    wards_killed: int | None
    control_wards_placed: int | None
    control_wards_purchased: int | None
    total_heal: int | None
    total_heal_per_minute: float | None
    total_heals_on_teammates: int | None
    teammate_healing_per_minute: float | None
    total_damage_shielded_on_teammates: int | None
    teammate_shielding_per_minute: float | None
    processing_schema_version: str


class TeamMatchFeature(AnalyticalModel):
    match_id: str
    public_patch: str
    team_id: int
    win: bool
    team_kills: int | None
    team_deaths: int | None
    team_assists: int | None
    team_gold: int | None
    team_champion_damage: int | None
    team_cs: int | None
    champion_kills: int | None
    champion_first: bool | None
    tower_kills: int | None
    tower_first: bool | None
    inhibitor_kills: int | None
    inhibitor_first: bool | None
    dragon_kills: int | None
    dragon_first: bool | None
    rift_herald_kills: int | None
    rift_herald_first: bool | None
    baron_kills: int | None
    baron_first: bool | None
    game_duration_seconds: int
    short_game: bool
    analytical_eligibility: bool
    exclusion_reasons: list[str]
    processing_schema_version: str


class MatchAnalysisContext(AnalyticalModel):
    match_id: str
    public_patch: str
    queue_id: int
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
    position_quality_ok: bool
    analytical_eligibility: bool
    exclusion_reasons: list[str]
    role_aggregation_eligibility: bool
    role_exclusion_reasons: list[str]
    processing_schema_version: str


@dataclass
class AnalyticalDataset:
    run_id: str
    input_directory: Path
    output_directory: Path
    participant_features: list[ParticipantMatchFeature]
    team_features: list[TeamMatchFeature]
    match_contexts: list[MatchAnalysisContext]
    quality_report: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Stage31Input:
    run_id: str
    input_directory: Path
    matches: list[CanonicalMatch]
    participants: list[CanonicalParticipant]
    teams: list[CanonicalTeam]
    bans: list[CanonicalBan]
    lineage_hashes: dict[str, str]


def build_retained_analytical_dataset(
    *,
    input_directory: Path,
    output_root: Path,
    expected_match_count: int = EXPECTED_MATCH_COUNT,
    expected_participant_count: int = EXPECTED_PARTICIPANT_COUNT,
    expected_team_count: int = EXPECTED_TEAM_COUNT,
    expected_patch_counts: dict[str, int] | None = None,
) -> AnalyticalDataset:
    """Load a compatible Stage 3.1 run and build Stage 3.2 rows without writing."""

    expected_patches = dict(expected_patch_counts or EXPECTED_PATCH_COUNTS)
    source = load_stage3_1_input(
        input_directory,
        expected_match_count=expected_match_count,
        expected_participant_count=expected_participant_count,
        expected_team_count=expected_team_count,
        expected_patch_counts=expected_patches,
    )
    output_directory = (
        output_root / f"schema={ANALYTICAL_SCHEMA_VERSION}" / f"run={source.run_id}"
    )
    _validate_existing_output_lineage(
        output_directory,
        run_id=source.run_id,
        lineage_hashes=source.lineage_hashes,
    )
    return build_analytical_dataset(
        run_id=source.run_id,
        input_directory=source.input_directory,
        output_directory=output_directory,
        matches=source.matches,
        participants=source.participants,
        teams=source.teams,
        lineage_hashes=source.lineage_hashes,
        expected_match_count=expected_match_count,
        expected_participant_count=expected_participant_count,
        expected_team_count=expected_team_count,
        expected_patch_counts=expected_patches,
    )


def load_stage3_1_input(
    input_directory: Path,
    *,
    expected_match_count: int,
    expected_participant_count: int,
    expected_team_count: int,
    expected_patch_counts: dict[str, int],
) -> Stage31Input:
    """Validate schema, lineage metadata, and all required Stage 3.1 files."""

    missing = [
        name
        for name in REQUIRED_STAGE3_1_FILES
        if not (input_directory / name).is_file()
    ]
    if missing:
        raise Stage3ValidationError(
            "stage3_1_input_missing", "required Stage 3.1 files are missing"
        )
    lineage_hashes = {
        name: hashlib.sha256((input_directory / name).read_bytes()).hexdigest()
        for name in REQUIRED_STAGE3_1_FILES
    }
    metadata = _load_json_object(input_directory / "metadata.json", "metadata")
    quality = _load_json_object(
        input_directory / "quality_report.json", "quality_report"
    )
    if metadata.get("processing_schema_version") != CANONICAL_SCHEMA_VERSION:
        raise Stage3ValidationError(
            "incompatible_stage3_1_schema", "Stage 3.1 schema version is incompatible"
        )
    if quality.get("quality_report_schema_version") != "stage3.1-quality-v1":
        raise Stage3ValidationError(
            "incompatible_stage3_1_quality_schema",
            "Stage 3.1 quality schema version is incompatible",
        )
    if quality.get("ready_for_stage_3_2") is not True or quality.get(
        "invariant_failures"
    ):
        raise Stage3ValidationError(
            "stage3_1_not_ready", "Stage 3.1 quality gate is not satisfied"
        )
    run_id = metadata.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise Stage3ValidationError("stage3_1_metadata", "Stage 3.1 run ID is missing")
    expected_rows = {
        "bans": 1_000 if expected_match_count == 100 else None,
        "matches": expected_match_count,
        "participants": expected_participant_count,
        "teams": expected_team_count,
    }
    metadata_rows = metadata.get("row_counts")
    if not isinstance(metadata_rows, dict):
        raise Stage3ValidationError(
            "stage3_1_metadata", "Stage 3.1 row counts are missing"
        )
    for name, expected in expected_rows.items():
        if expected is not None and metadata_rows.get(name) != expected:
            raise Stage3ValidationError(
                "stage3_1_row_count", "Stage 3.1 metadata row counts disagree"
            )
    if metadata.get("match_counts_by_public_patch") != expected_patch_counts:
        raise Stage3ValidationError(
            "stage3_1_patch_counts", "Stage 3.1 metadata patch counts disagree"
        )
    matches = _load_jsonl_models(input_directory / "matches.jsonl", CanonicalMatch)
    participants = _load_jsonl_models(
        input_directory / "participants.jsonl", CanonicalParticipant
    )
    teams = _load_jsonl_models(input_directory / "teams.jsonl", CanonicalTeam)
    bans = _load_jsonl_models(input_directory / "bans.jsonl", CanonicalBan)
    if (
        len(matches) != expected_match_count
        or len(participants) != expected_participant_count
        or len(teams) != expected_team_count
        or len(bans) != metadata_rows.get("bans")
    ):
        raise Stage3ValidationError(
            "stage3_1_row_count", "Stage 3.1 physical row counts disagree"
        )
    if any(
        row.processing_schema_version != CANONICAL_SCHEMA_VERSION
        for rows in (matches, participants, teams, bans)
        for row in rows
    ):
        raise Stage3ValidationError(
            "incompatible_stage3_1_row_schema", "Stage 3.1 row schema is incompatible"
        )
    match_ids = [row.match_id for row in matches]
    participant_keys = [(row.match_id, row.participant_id) for row in participants]
    team_keys = [(row.match_id, row.team_id) for row in teams]
    if len(match_ids) != len(set(match_ids)):
        raise Stage3ValidationError(
            "duplicate_match", "Stage 3.1 matches are duplicated"
        )
    if len(participant_keys) != len(set(participant_keys)):
        raise Stage3ValidationError(
            "duplicate_participant", "Stage 3.1 participant keys are duplicated"
        )
    if len(team_keys) != len(set(team_keys)):
        raise Stage3ValidationError(
            "duplicate_team", "Stage 3.1 team keys are duplicated"
        )
    approved_ids = set(match_ids)
    if (
        {row.match_id for row in participants} != approved_ids
        or {row.match_id for row in teams} != approved_ids
        or {row.match_id for row in bans} != approved_ids
    ):
        raise Stage3ValidationError(
            "stage3_1_key_lineage", "Stage 3.1 table match IDs do not reconcile"
        )
    if dict(sorted(Counter(row.public_patch for row in matches).items())) != dict(
        sorted(expected_patch_counts.items())
    ):
        raise Stage3ValidationError(
            "stage3_1_patch_counts", "Stage 3.1 physical patch counts disagree"
        )
    return Stage31Input(
        run_id=run_id,
        input_directory=input_directory,
        matches=matches,
        participants=participants,
        teams=teams,
        bans=bans,
        lineage_hashes=lineage_hashes,
    )


def build_analytical_dataset(
    *,
    run_id: str,
    input_directory: Path,
    output_directory: Path,
    matches: list[CanonicalMatch],
    participants: list[CanonicalParticipant],
    teams: list[CanonicalTeam],
    lineage_hashes: dict[str, str],
    expected_match_count: int,
    expected_participant_count: int,
    expected_team_count: int,
    expected_patch_counts: dict[str, int],
) -> AnalyticalDataset:
    """Derive deterministic analytical rows and aggregate validation findings."""

    quality = _quality_accumulator()
    match_by_id = {row.match_id: row for row in matches}
    participants_by_match: dict[str, list[CanonicalParticipant]] = defaultdict(list)
    teams_by_match: dict[str, list[CanonicalTeam]] = defaultdict(list)
    for participant in participants:
        participants_by_match[participant.match_id].append(participant)
    for team in teams:
        teams_by_match[team.match_id].append(team)

    duplicate_match_count = len(matches) - len(match_by_id)
    participant_keys = [(row.match_id, row.participant_id) for row in participants]
    team_keys = [(row.match_id, row.team_id) for row in teams]
    quality["duplicate_match_keys"] = duplicate_match_count
    quality["duplicate_participant_keys"] = len(participant_keys) - len(
        set(participant_keys)
    )
    quality["duplicate_team_keys"] = len(team_keys) - len(set(team_keys))

    contexts: list[MatchAnalysisContext] = []
    team_features: list[TeamMatchFeature] = []
    participant_features: list[ParticipantMatchFeature] = []
    team_feature_lookup: dict[tuple[str, int], TeamMatchFeature] = {}
    context_lookup: dict[str, MatchAnalysisContext] = {}

    for match in sorted(matches, key=lambda row: row.match_id):
        match_participants = sorted(
            participants_by_match.get(match.match_id, []),
            key=lambda row: row.participant_id,
        )
        match_teams = sorted(
            teams_by_match.get(match.match_id, []), key=lambda row: row.team_id
        )
        context = _match_context(match, match_participants, match_teams, quality)
        contexts.append(context)
        context_lookup[match.match_id] = context
        for team in match_teams:
            members = [row for row in match_participants if row.team_id == team.team_id]
            feature = _team_feature(match, context, team, members)
            team_features.append(feature)
            team_feature_lookup[(match.match_id, team.team_id)] = feature

    for participant in sorted(
        participants, key=lambda row: (row.match_id, row.participant_id)
    ):
        match = match_by_id.get(participant.match_id)
        context = context_lookup.get(participant.match_id)
        team = team_feature_lookup.get((participant.match_id, participant.team_id))
        if match is None or context is None or team is None:
            quality["skipped_or_error_categories"]["missing_parent_row"] += 1
            continue
        feature = _participant_feature(match, context, participant, team, quality)
        participant_features.append(feature)

    participant_features.sort(key=lambda row: (row.match_id, row.participant_id))
    team_features.sort(key=lambda row: (row.match_id, row.team_id))
    contexts.sort(key=lambda row: row.match_id)
    _validate_reconciliation(participant_features, team_features, contexts, quality)
    quality_report = _finalize_quality_report(
        quality=quality,
        matches=matches,
        participant_features=participant_features,
        team_features=team_features,
        contexts=contexts,
        expected_match_count=expected_match_count,
        expected_participant_count=expected_participant_count,
        expected_team_count=expected_team_count,
        expected_patch_counts=expected_patch_counts,
        input_participant_count=len(participants),
        input_team_count=len(teams),
        lineage_hashes=lineage_hashes,
        input_directory=input_directory,
        output_directory=output_directory,
    )
    metadata = {
        "processing_schema_version": ANALYTICAL_SCHEMA_VERSION,
        "quality_report_schema_version": ANALYTICAL_QUALITY_SCHEMA_VERSION,
        "formula_contract_version": FORMULA_CONTRACT_VERSION,
        "formula_contract": FORMULA_CONTRACT,
        "ratio_unit": RATIO_UNIT,
        "run_id": run_id,
        "input": {
            "directory": input_directory.as_posix(),
            "processing_schema_version": CANONICAL_SCHEMA_VERSION,
            "sha256": dict(sorted(lineage_hashes.items())),
        },
        "row_counts": {
            "match_analysis_context": len(contexts),
            "participant_match_features": len(participant_features),
            "team_match_features": len(team_features),
        },
        "match_counts_by_public_patch": dict(
            sorted(Counter(row.public_patch for row in contexts).items())
        ),
        "storage_format": "deterministic-jsonl",
        "privacy": {
            "approved_player_identifier": "player_key",
            "additional_identity_fields": False,
            "aggregate_quality_report": True,
        },
    }
    return AnalyticalDataset(
        run_id=run_id,
        input_directory=input_directory,
        output_directory=output_directory,
        participant_features=participant_features,
        team_features=team_features,
        match_contexts=contexts,
        quality_report=quality_report,
        metadata=metadata,
    )


def write_analytical_dataset(dataset: AnalyticalDataset) -> Path:
    """Stage and atomically publish one complete Stage 3.2 run."""

    if not dataset.quality_report["ready_for_stage_3_3_analysis_validation"]:
        raise Stage3ValidationError(
            "stage3_2_invariant_failure",
            "analytical output was not written because validation failed",
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


def run_stage3_2(
    *,
    input_directory: Path,
    output_root: Path,
    validate_only: bool,
    expected_match_count: int = EXPECTED_MATCH_COUNT,
    expected_participant_count: int = EXPECTED_PARTICIPANT_COUNT,
    expected_team_count: int = EXPECTED_TEAM_COUNT,
    expected_patch_counts: dict[str, int] | None = None,
) -> AnalyticalDataset:
    """Build, validate, and optionally publish Stage 3.2 output."""

    dataset = build_retained_analytical_dataset(
        input_directory=input_directory,
        output_root=output_root,
        expected_match_count=expected_match_count,
        expected_participant_count=expected_participant_count,
        expected_team_count=expected_team_count,
        expected_patch_counts=expected_patch_counts,
    )
    if not validate_only:
        write_analytical_dataset(dataset)
    return dataset


def _match_context(
    match: CanonicalMatch,
    participants: list[CanonicalParticipant],
    teams: list[CanonicalTeam],
    quality: dict[str, Any],
) -> MatchAnalysisContext:
    participant_ids = [row.participant_id for row in participants]
    team_ids = [row.team_id for row in teams]
    participants_complete = len(
        participants
    ) == EXPECTED_PARTICIPANTS_PER_MATCH and len(participant_ids) == len(
        set(participant_ids)
    )
    teams_complete = len(teams) == EXPECTED_TEAMS_PER_MATCH and len(team_ids) == len(
        set(team_ids)
    )
    position_details = [_position_policy(row) for row in participants]
    disagreement_count = sum(item[2] for item in position_details)
    fallback_count = sum(
        item[1] == "individual_position_fallback" for item in position_details
    )
    unresolved_count = sum(item[0] is None for item in position_details)
    positions_complete = unresolved_count == 0
    position_quality_ok = (
        positions_complete and disagreement_count == 0 and fallback_count == 0
    )
    short_game = 0 < match.game_duration_seconds < SHORT_GAME_THRESHOLD_SECONDS
    if short_game != match.is_remake_or_short_game:
        quality["source_flag_conflicts"]["short_game"] += 1
    exclusion_reasons: list[str] = []
    if match.game_duration_seconds <= 0:
        exclusion_reasons.append("invalid_duration")
    if short_game:
        exclusion_reasons.append("short_game")
    if not participants_complete:
        exclusion_reasons.append("incomplete_participants")
    if not teams_complete:
        exclusion_reasons.append("incomplete_teams")
    role_reasons = list(exclusion_reasons)
    if disagreement_count:
        role_reasons.append("position_disagreement")
    if fallback_count:
        role_reasons.append("position_fallback")
    if unresolved_count:
        role_reasons.append("unresolved_position")
    quality["position_disagreements"] += disagreement_count
    quality["position_fallbacks"] += fallback_count
    quality["unresolved_positions"] += unresolved_count
    return MatchAnalysisContext(
        match_id=match.match_id,
        public_patch=match.public_patch,
        queue_id=match.queue_id,
        game_duration_seconds=match.game_duration_seconds,
        short_game=short_game,
        participant_count=len(participants),
        team_count=len(teams),
        participants_complete=participants_complete,
        teams_complete=teams_complete,
        positions_complete=positions_complete,
        position_disagreement_count=disagreement_count,
        position_fallback_count=fallback_count,
        unresolved_position_count=unresolved_count,
        position_quality_ok=position_quality_ok,
        analytical_eligibility=not exclusion_reasons,
        exclusion_reasons=exclusion_reasons,
        role_aggregation_eligibility=not role_reasons,
        role_exclusion_reasons=role_reasons,
        processing_schema_version=ANALYTICAL_SCHEMA_VERSION,
    )


def _team_feature(
    match: CanonicalMatch,
    context: MatchAnalysisContext,
    team: CanonicalTeam,
    participants: list[CanonicalParticipant],
) -> TeamMatchFeature:
    participant_cs = [
        total_cs(row.total_minions_killed, row.neutral_minions_killed)
        for row in participants
    ]
    complete_team = len(participants) == 5

    def team_sum(values: list[int | None]) -> int | None:
        return complete_sum(values) if complete_team else None

    return TeamMatchFeature(
        match_id=match.match_id,
        public_patch=match.public_patch,
        team_id=team.team_id,
        win=team.win,
        team_kills=team_sum([row.kills for row in participants]),
        team_deaths=team_sum([row.deaths for row in participants]),
        team_assists=team_sum([row.assists for row in participants]),
        team_gold=team_sum([row.gold_earned for row in participants]),
        team_champion_damage=team_sum(
            [row.total_damage_dealt_to_champions for row in participants]
        ),
        team_cs=team_sum(participant_cs),
        champion_kills=team.champion_kills,
        champion_first=team.champion_first,
        tower_kills=team.tower_kills,
        tower_first=team.tower_first,
        inhibitor_kills=team.inhibitor_kills,
        inhibitor_first=team.inhibitor_first,
        dragon_kills=team.dragon_kills,
        dragon_first=team.dragon_first,
        rift_herald_kills=team.rift_herald_kills,
        rift_herald_first=team.rift_herald_first,
        baron_kills=team.baron_kills,
        baron_first=team.baron_first,
        game_duration_seconds=match.game_duration_seconds,
        short_game=context.short_game,
        analytical_eligibility=context.analytical_eligibility,
        exclusion_reasons=list(context.exclusion_reasons),
        processing_schema_version=ANALYTICAL_SCHEMA_VERSION,
    )


def _participant_feature(
    match: CanonicalMatch,
    context: MatchAnalysisContext,
    participant: CanonicalParticipant,
    team: TeamMatchFeature,
    quality: dict[str, Any],
) -> ParticipantMatchFeature:
    minutes = duration_minutes(match.game_duration_seconds)
    participant_cs = total_cs(
        participant.total_minions_killed, participant.neutral_minions_killed
    )
    if minutes is None:
        quality["zero_or_invalid_denominators"]["per_minute_invalid_duration"] += 1
    if participant.deaths == 0:
        quality["formula_conventions"]["kda_zero_death_rows"] += 1
    if team.team_kills is not None and team.team_kills <= 0:
        quality["zero_or_invalid_denominators"][
            "kill_participation_nonpositive_team_kills"
        ] += 1
    if team.team_gold is not None and team.team_gold <= 0:
        quality["zero_or_invalid_denominators"][
            "team_gold_share_nonpositive_team_gold"
        ] += 1
    if team.team_champion_damage is not None and team.team_champion_damage <= 0:
        quality["zero_or_invalid_denominators"][
            "team_damage_share_nonpositive_team_damage"
        ] += 1
    analysis_position, position_source, disagreement = _position_policy(participant)
    role_reasons = list(context.exclusion_reasons)
    if disagreement:
        role_reasons.append("position_disagreement")
    if position_source == "individual_position_fallback":
        role_reasons.append("position_fallback")
    if analysis_position is None:
        role_reasons.append("unresolved_position")
    return ParticipantMatchFeature(
        match_id=participant.match_id,
        participant_id=participant.participant_id,
        player_key=participant.player_key,
        public_patch=match.public_patch,
        team_id=participant.team_id,
        champion_id=participant.champion_id,
        champion_name=participant.champion_name,
        win=participant.win,
        team_position=participant.team_position,
        individual_position=participant.individual_position,
        analysis_position=analysis_position,
        analysis_position_source=position_source,
        position_disagreement=disagreement,
        analytical_eligibility=context.analytical_eligibility,
        exclusion_reasons=list(context.exclusion_reasons),
        role_aggregation_eligibility=not role_reasons,
        role_exclusion_reasons=role_reasons,
        game_duration_seconds=match.game_duration_seconds,
        short_game=context.short_game,
        kills=participant.kills,
        deaths=participant.deaths,
        assists=participant.assists,
        kda=kda(participant.kills, participant.deaths, participant.assists),
        team_kills=team.team_kills,
        kill_participation=ratio(
            _optional_add(participant.kills, participant.assists), team.team_kills
        ),
        total_minions_killed=participant.total_minions_killed,
        neutral_minions_killed=participant.neutral_minions_killed,
        total_cs=participant_cs,
        cs_per_minute=per_minute(participant_cs, minutes),
        gold_earned=participant.gold_earned,
        team_gold=team.team_gold,
        gold_per_minute=per_minute(participant.gold_earned, minutes),
        team_gold_share=ratio(participant.gold_earned, team.team_gold),
        total_damage_to_champions=participant.total_damage_dealt_to_champions,
        team_champion_damage=team.team_champion_damage,
        damage_to_champions_per_minute=per_minute(
            participant.total_damage_dealt_to_champions, minutes
        ),
        team_champion_damage_share=ratio(
            participant.total_damage_dealt_to_champions,
            team.team_champion_damage,
        ),
        physical_damage_to_champions=(participant.physical_damage_dealt_to_champions),
        magic_damage_to_champions=participant.magic_damage_dealt_to_champions,
        true_damage_to_champions=participant.true_damage_dealt_to_champions,
        damage_taken=participant.total_damage_taken,
        damage_taken_per_minute=per_minute(participant.total_damage_taken, minutes),
        damage_mitigated=participant.damage_self_mitigated,
        damage_mitigated_per_minute=per_minute(
            participant.damage_self_mitigated, minutes
        ),
        vision_score=participant.vision_score,
        vision_score_per_minute=per_minute(participant.vision_score, minutes),
        wards_placed=participant.wards_placed,
        wards_killed=participant.wards_killed,
        control_wards_placed=participant.control_wards_placed,
        control_wards_purchased=participant.control_wards_purchased,
        total_heal=participant.total_heal,
        total_heal_per_minute=per_minute(participant.total_heal, minutes),
        total_heals_on_teammates=participant.total_heals_on_teammates,
        teammate_healing_per_minute=per_minute(
            participant.total_heals_on_teammates, minutes
        ),
        total_damage_shielded_on_teammates=(
            participant.total_damage_shielded_on_teammates
        ),
        teammate_shielding_per_minute=per_minute(
            participant.total_damage_shielded_on_teammates, minutes
        ),
        processing_schema_version=ANALYTICAL_SCHEMA_VERSION,
    )


def _position_policy(
    participant: CanonicalParticipant,
) -> tuple[str | None, str, bool]:
    team_position = participant.team_position
    individual_position = participant.individual_position
    team_valid = team_position in CANONICAL_POSITIONS
    individual_valid = individual_position in CANONICAL_POSITIONS
    disagreement = bool(
        team_valid and individual_valid and team_position != individual_position
    )
    if team_valid:
        return team_position, "team_position", disagreement
    if individual_valid:
        return individual_position, "individual_position_fallback", False
    return None, "unresolved", False


def _validate_reconciliation(
    participants: list[ParticipantMatchFeature],
    teams: list[TeamMatchFeature],
    contexts: list[MatchAnalysisContext],
    quality: dict[str, Any],
) -> None:
    team_lookup = {(row.match_id, row.team_id): row for row in teams}
    participant_groups: dict[tuple[str, int], list[ParticipantMatchFeature]] = (
        defaultdict(list)
    )
    teams_by_match: dict[str, list[TeamMatchFeature]] = defaultdict(list)
    for participant in participants:
        participant_groups[(participant.match_id, participant.team_id)].append(
            participant
        )
        team = team_lookup.get((participant.match_id, participant.team_id))
        if team is None:
            quality["reconciliation_failures"]["missing_participant_team"] += 1
            continue
        if (
            participant.team_kills != team.team_kills
            or participant.team_gold != team.team_gold
            or participant.team_champion_damage != team.team_champion_damage
        ):
            quality["reconciliation_failures"][
                "participant_denominator_team_mismatch"
            ] += 1
        if participant.win != team.win:
            quality["reconciliation_failures"]["participant_team_win_mismatch"] += 1
    for team in teams:
        teams_by_match[team.match_id].append(team)
        if team.team_kills != team.champion_kills:
            quality["reconciliation_failures"][
                "team_kills_vs_canonical_champion_kills"
            ] += 1
        members = participant_groups.get((team.match_id, team.team_id), [])
        if len(members) != 5:
            quality["reconciliation_failures"]["team_participant_count"] += 1
        _validate_share_sum(
            [row.team_gold_share for row in members],
            team.team_gold,
            "gold_share_sum",
            quality,
        )
        _validate_share_sum(
            [row.team_champion_damage_share for row in members],
            team.team_champion_damage,
            "damage_share_sum",
            quality,
        )
    for _match_id, match_teams in teams_by_match.items():
        if len(match_teams) != 2:
            continue
        first, second = match_teams
        if (
            first.team_kills is not None
            and second.team_deaths is not None
            and first.team_kills != second.team_deaths
        ):
            quality["reconciliation_failures"]["team_kills_vs_opponent_deaths"] += 1
        if (
            second.team_kills is not None
            and first.team_deaths is not None
            and second.team_kills != first.team_deaths
        ):
            quality["reconciliation_failures"]["team_kills_vs_opponent_deaths"] += 1
    context_ids = {row.match_id for row in contexts}
    if set(teams_by_match) - context_ids:
        quality["reconciliation_failures"]["team_without_context"] += 1


def _validate_share_sum(
    shares: list[float | None],
    denominator: int | None,
    category: str,
    quality: dict[str, Any],
) -> None:
    if denominator is None or denominator <= 0:
        if any(value is not None for value in shares):
            quality["reconciliation_failures"][category] += 1
        return
    if any(value is None for value in shares) or not math.isclose(
        sum(value for value in shares if value is not None),
        1.0,
        rel_tol=SHARE_TOLERANCE,
        abs_tol=SHARE_TOLERANCE,
    ):
        quality["reconciliation_failures"][category] += 1


def _quality_accumulator() -> dict[str, Any]:
    return {
        "duplicate_match_keys": 0,
        "duplicate_participant_keys": 0,
        "duplicate_team_keys": 0,
        "formula_conventions": Counter({"kda_zero_death_rows": 0}),
        "position_disagreements": 0,
        "position_fallbacks": 0,
        "range_validation_failures": Counter(),
        "reconciliation_failures": Counter(),
        "skipped_or_error_categories": Counter(),
        "source_flag_conflicts": Counter(),
        "unresolved_positions": 0,
        "zero_or_invalid_denominators": Counter(
            {
                "kill_participation_nonpositive_team_kills": 0,
                "per_minute_invalid_duration": 0,
                "team_damage_share_nonpositive_team_damage": 0,
                "team_gold_share_nonpositive_team_gold": 0,
            }
        ),
    }


def _finalize_quality_report(
    *,
    quality: dict[str, Any],
    matches: list[CanonicalMatch],
    participant_features: list[ParticipantMatchFeature],
    team_features: list[TeamMatchFeature],
    contexts: list[MatchAnalysisContext],
    expected_match_count: int,
    expected_participant_count: int,
    expected_team_count: int,
    expected_patch_counts: dict[str, int],
    input_participant_count: int,
    input_team_count: int,
    lineage_hashes: dict[str, str],
    input_directory: Path,
    output_directory: Path,
) -> dict[str, Any]:
    invariant_failures: Counter[str] = Counter()
    if len(contexts) != expected_match_count:
        invariant_failures["match_context_row_count"] += 1
    if len(participant_features) != expected_participant_count:
        invariant_failures["participant_feature_row_count"] += 1
    if len(team_features) != expected_team_count:
        invariant_failures["team_feature_row_count"] += 1
    for name in (
        "duplicate_match_keys",
        "duplicate_participant_keys",
        "duplicate_team_keys",
    ):
        if quality[name]:
            invariant_failures[name] += quality[name]
    patch_counts = dict(sorted(Counter(row.public_patch for row in contexts).items()))
    if patch_counts != dict(sorted(expected_patch_counts.items())):
        invariant_failures["patch_counts"] += 1
    source_ids = {row.match_id for row in matches}
    if (
        {row.match_id for row in contexts} != source_ids
        or {row.match_id for row in participant_features} != source_ids
        or {row.match_id for row in team_features} != source_ids
    ):
        invariant_failures["source_match_lineage"] += 1
    null_counts = {
        "participant_match_features": {
            field: sum(getattr(row, field) is None for row in participant_features)
            for field in PARTICIPANT_DERIVED_METRICS
        },
        "team_match_features": {
            field: sum(getattr(row, field) is None for row in team_features)
            for field in TEAM_DERIVED_METRICS
        },
    }
    ranges = {
        "participant_match_features": {
            field: _metric_range(participant_features, field)
            for field in PARTICIPANT_DERIVED_METRICS
        },
        "team_match_features": {
            field: _metric_range(team_features, field) for field in TEAM_DERIVED_METRICS
        },
    }
    for field in (
        "kill_participation",
        "team_gold_share",
        "team_champion_damage_share",
    ):
        invalid = sum(
            value is not None and not 0 <= value <= 1
            for value in (getattr(row, field) for row in participant_features)
        )
        if invalid:
            quality["range_validation_failures"][field] += invalid
    for field in PARTICIPANT_DERIVED_METRICS:
        invalid = sum(
            value is not None and value < 0
            for value in (getattr(row, field) for row in participant_features)
        )
        if invalid:
            quality["range_validation_failures"][f"negative_{field}"] += invalid
    for field in TEAM_DERIVED_METRICS:
        invalid = sum(
            value is not None and value < 0
            for value in (getattr(row, field) for row in team_features)
        )
        if invalid:
            quality["range_validation_failures"][f"negative_{field}"] += invalid
    if quality["range_validation_failures"]:
        invariant_failures["derived_metric_ranges"] += sum(
            quality["range_validation_failures"].values()
        )
    records: list[BaseModel] = [*participant_features, *team_features, *contexts]
    nonfinite_count = sum(_count_nonfinite(row.model_dump()) for row in records)
    if nonfinite_count:
        invariant_failures["nonfinite_values"] += nonfinite_count
    eligible_matches = sum(row.analytical_eligibility for row in contexts)
    role_eligible_matches = sum(row.role_aggregation_eligibility for row in contexts)
    eligible_participants = sum(
        row.analytical_eligibility for row in participant_features
    )
    role_eligible_participants = sum(
        row.role_aggregation_eligibility for row in participant_features
    )
    exclusion_reasons = Counter(
        reason for row in contexts for reason in row.exclusion_reasons
    )
    role_exclusion_reasons = Counter(
        reason for row in contexts for reason in row.role_exclusion_reasons
    )
    output_files = {
        "match_analysis_context": "match_analysis_context.jsonl",
        "metadata": "metadata.json",
        "participant_match_features": "participant_match_features.jsonl",
        "quality_report": "quality_report.json",
        "team_match_features": "team_match_features.jsonl",
    }
    return {
        "processing_schema_version": ANALYTICAL_SCHEMA_VERSION,
        "quality_report_schema_version": ANALYTICAL_QUALITY_SCHEMA_VERSION,
        "formula_contract_version": FORMULA_CONTRACT_VERSION,
        "ratio_unit": RATIO_UNIT,
        "input": {
            "directory": input_directory.as_posix(),
            "processing_schema_version": CANONICAL_SCHEMA_VERSION,
            "row_counts": {
                "matches": len(matches),
                "participants": input_participant_count,
                "teams": input_team_count,
            },
            "sha256": dict(sorted(lineage_hashes.items())),
        },
        "output": {
            "directory": output_directory.as_posix(),
            "files": output_files,
            "row_counts": {
                "match_analysis_context": len(contexts),
                "participant_match_features": len(participant_features),
                "team_match_features": len(team_features),
            },
            "match_counts_by_public_patch": patch_counts,
        },
        "eligibility": {
            "analytically_eligible_matches": eligible_matches,
            "analytically_ineligible_matches": len(contexts) - eligible_matches,
            "analytically_eligible_participants": eligible_participants,
            "analytically_ineligible_participants": len(participant_features)
            - eligible_participants,
            "match_exclusion_reasons": _sorted_counter(exclusion_reasons),
            "role_aggregation_eligible_matches": role_eligible_matches,
            "role_aggregation_ineligible_matches": len(contexts)
            - role_eligible_matches,
            "role_aggregation_eligible_participants": role_eligible_participants,
            "role_aggregation_ineligible_participants": len(participant_features)
            - role_eligible_participants,
            "role_exclusion_reasons": _sorted_counter(role_exclusion_reasons),
            "short_games": sum(row.short_game for row in contexts),
            "short_game_rule": (
                f"0 < game_duration_seconds < {SHORT_GAME_THRESHOLD_SECONDS}; "
                "non-positive duration is invalid_duration, not short_game"
            ),
        },
        "positions": {
            "disagreements": quality["position_disagreements"],
            "fallbacks": quality["position_fallbacks"],
            "unresolved": quality["unresolved_positions"],
            "analysis_position_policy": (
                "recognized team_position; otherwise recognized individual_position "
                "as an explicit role-ineligible fallback; otherwise null"
            ),
        },
        "derived_metric_null_counts": null_counts,
        "zero_or_invalid_denominators": _sorted_counter(
            quality["zero_or_invalid_denominators"]
        ),
        "formula_conventions": _sorted_counter(quality["formula_conventions"]),
        "derived_metric_ranges": ranges,
        "range_validation_failures": _sorted_counter(
            quality["range_validation_failures"]
        ),
        "reconciliation_failures": _sorted_counter(quality["reconciliation_failures"]),
        "source_flag_conflicts": _sorted_counter(quality["source_flag_conflicts"]),
        "skipped_or_error_categories": _sorted_counter(
            quality["skipped_or_error_categories"]
        ),
        "invariant_failures": _sorted_counter(invariant_failures),
        "privacy": {
            "approved_player_identifier": "player_key",
            "additional_identity_fields": False,
            "aggregate_report_only": True,
        },
        "ready_for_stage_3_3_analysis_validation": not invariant_failures,
    }


def _write_staged_dataset(dataset: AnalyticalDataset, directory: Path) -> None:
    _write_lines(
        directory / "participant_match_features.jsonl",
        dataset.participant_features,
    )
    _write_lines(directory / "team_match_features.jsonl", dataset.team_features)
    _write_lines(directory / "match_analysis_context.jsonl", dataset.match_contexts)
    _write_json(directory / "metadata.json", dataset.metadata)
    _write_json(directory / "quality_report.json", dataset.quality_report)


def _validate_existing_output_lineage(
    output_directory: Path,
    *,
    run_id: str,
    lineage_hashes: dict[str, str],
) -> None:
    if not output_directory.exists():
        return
    metadata_path = output_directory / "metadata.json"
    if not metadata_path.is_file():
        raise Stage3ValidationError(
            "existing_stage3_2_lineage",
            "existing Stage 3.2 output has no lineage metadata",
        )
    metadata = _load_json_object(metadata_path, "existing_stage3_2_metadata")
    recorded_input = metadata.get("input")
    if (
        metadata.get("processing_schema_version") != ANALYTICAL_SCHEMA_VERSION
        or metadata.get("run_id") != run_id
        or not isinstance(recorded_input, dict)
        or recorded_input.get("sha256") != dict(sorted(lineage_hashes.items()))
    ):
        raise Stage3ValidationError(
            "stage3_1_lineage_changed",
            "Stage 3.1 hashes differ from existing Stage 3.2 lineage",
        )


def _write_lines(path: Path, rows: list[BaseModel]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                )
            )
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            value,
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_jsonl_models(path: Path, model: type[BaseModel]) -> list[Any]:
    rows: list[Any] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise Stage3ValidationError(
            "stage3_1_input_read", "Stage 3.1 table could not be read"
        ) from None
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line, object_pairs_hook=_unique_object)
            rows.append(model.model_validate(value))
        except (json.JSONDecodeError, ValidationError):
            raise Stage3ValidationError(
                "stage3_1_input_schema", "Stage 3.1 table row is invalid"
            ) from None
    return rows


def _load_json_object(path: Path, source: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise Stage3ValidationError(
            f"stage3_1_{source}", f"Stage 3.1 {source} is invalid"
        ) from None
    if not isinstance(value, dict):
        raise Stage3ValidationError(
            f"stage3_1_{source}", f"Stage 3.1 {source} must be an object"
        )
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object key", key, 0)
        result[key] = value
    return result


def _optional_add(left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    return left + right


def _metric_range(rows: list[Any], field: str) -> dict[str, Any]:
    values = [getattr(row, field) for row in rows if getattr(row, field) is not None]
    return {
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
    }


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
