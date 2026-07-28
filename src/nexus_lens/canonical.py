"""Offline Stage 3.1 canonicalization of an approved Stage 2 population."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from nexus_lens.normalization import normalize_position
from nexus_lens.patches import resolve_patch
from nexus_lens.privacy import pseudonymize_puuid
from nexus_lens.schemas import RANKED_SOLO_QUEUE_ID, RiotMatch

CANONICAL_SCHEMA_VERSION = "stage3.1-v1"
QUALITY_REPORT_SCHEMA_VERSION = "stage3.1-quality-v1"
EXPECTED_MATCH_COUNT = 100
EXPECTED_PATCH_COUNTS = {"26.13": 53, "26.14": 47}
EXPECTED_PARTICIPANTS = 10
EXPECTED_TEAMS = 2
EXPECTED_BANS_PER_TEAM = 5
SHORT_GAME_THRESHOLD_SECONDS = 300
APPROVED_CHECKPOINT_STATUSES = frozenset(
    {"accepted", "already_cataloged_accepted", "already_cataloged_target"}
)
EXPECTED_GAME_MODE = "CLASSIC"


class CanonicalModel(BaseModel):
    """Strict base for stable Stage 3.1 output contracts."""

    model_config = ConfigDict(extra="forbid")


class CanonicalMatch(CanonicalModel):
    match_id: str
    public_patch: str
    game_version: str
    platform: str
    queue_id: int
    game_creation: datetime
    game_start_timestamp: datetime | None
    game_end_timestamp: datetime | None
    game_duration_seconds: int
    winning_team_id: int
    is_remake_or_short_game: bool
    source_payload_reference: str
    processing_schema_version: str


class CanonicalParticipant(CanonicalModel):
    match_id: str
    participant_id: int
    team_id: int
    player_key: str
    champion_id: int | None
    champion_name: str | None
    summoner_spell_1_id: int | None
    summoner_spell_2_id: int | None
    team_position: str | None
    individual_position: str | None
    lane: str | None
    role: str | None
    win: bool
    kills: int | None
    deaths: int | None
    assists: int | None
    total_minions_killed: int | None
    neutral_minions_killed: int | None
    gold_earned: int | None
    total_damage_dealt_to_champions: int | None
    physical_damage_dealt_to_champions: int | None
    magic_damage_dealt_to_champions: int | None
    true_damage_dealt_to_champions: int | None
    total_damage_taken: int | None
    damage_self_mitigated: int | None
    total_heal: int | None
    total_heals_on_teammates: int | None
    total_damage_shielded_on_teammates: int | None
    vision_score: int | None
    wards_placed: int | None
    wards_killed: int | None
    control_wards_placed: int | None
    control_wards_purchased: int | None
    champion_level: int | None
    item_0: int | None
    item_1: int | None
    item_2: int | None
    item_3: int | None
    item_4: int | None
    item_5: int | None
    item_6: int | None
    time_played_seconds: int | None
    challenge_objectives_stolen: int | None
    challenge_save_ally_from_death: int | None
    challenge_skillshots_dodged: int | None
    challenge_skillshots_hit: int | None
    challenge_solo_kills: int | None
    challenge_turret_plates_taken: int | None
    processing_schema_version: str


class CanonicalTeam(CanonicalModel):
    match_id: str
    team_id: int
    win: bool
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
    processing_schema_version: str


class CanonicalBan(CanonicalModel):
    match_id: str
    team_id: int
    pick_turn: int | None
    champion_id: int | None
    processing_schema_version: str


@dataclass(frozen=True)
class ApprovedPayload:
    match_id: str
    public_patch: str
    path: Path
    source_reference: str
    catalog: dict[str, Any]
    partition_record: dict[str, Any]


@dataclass
class CanonicalDataset:
    run_id: str
    input_manifest: str
    output_directory: Path
    matches: list[CanonicalMatch]
    participants: list[CanonicalParticipant]
    teams: list[CanonicalTeam]
    bans: list[CanonicalBan]
    quality_report: dict[str, Any]
    metadata: dict[str, Any]


class Stage3ValidationError(ValueError):
    """A sanitized integrity failure that never contains player identifiers."""

    def __init__(self, category: str, detail: str) -> None:
        self.category = category
        super().__init__(detail)


def build_retained_population_dataset(
    *,
    manifest_path: Path,
    checkpoint_path: Path,
    catalog_path: Path,
    raw_root: Path,
    processed_root: Path,
    output_root: Path,
    expected_match_count: int = EXPECTED_MATCH_COUNT,
    expected_patch_counts: dict[str, int] | None = None,
) -> CanonicalDataset:
    """Validate all retained inputs and build canonical rows without writing."""

    expected_patches = dict(expected_patch_counts or EXPECTED_PATCH_COUNTS)
    manifest = _load_json_object(manifest_path, "manifest")
    checkpoint = _load_json_object(checkpoint_path, "checkpoint")
    run_id, platform, approvals = _validate_approval_state(
        manifest,
        checkpoint,
        expected_match_count=expected_match_count,
        expected_patch_counts=expected_patches,
    )
    approved_payloads = _resolve_approved_payloads(
        approvals=approvals,
        manifest=manifest,
        manifest_path=manifest_path,
        catalog_path=catalog_path,
        raw_root=raw_root,
        processed_root=processed_root,
    )
    relative_manifest = _relative_reference(manifest_path, Path.cwd())
    output_directory = (
        output_root / f"schema={CANONICAL_SCHEMA_VERSION}" / f"run={run_id}"
    )
    return build_canonical_dataset(
        run_id=run_id,
        input_manifest=relative_manifest,
        platform=platform,
        approved_payloads=approved_payloads,
        output_directory=output_directory,
        expected_match_count=expected_match_count,
        expected_patch_counts=expected_patches,
    )


def build_canonical_dataset(
    *,
    run_id: str,
    input_manifest: str,
    platform: str,
    approved_payloads: list[ApprovedPayload],
    output_directory: Path,
    expected_match_count: int,
    expected_patch_counts: dict[str, int],
) -> CanonicalDataset:
    """Purely transform already-resolved approved payload references."""

    matches: list[CanonicalMatch] = []
    participants: list[CanonicalParticipant] = []
    teams: list[CanonicalTeam] = []
    bans: list[CanonicalBan] = []
    failures: Counter[str] = Counter()
    quality = _quality_accumulator()
    approved_ids = [item.match_id for item in approved_payloads]
    duplicate_approved = len(approved_ids) - len(set(approved_ids))
    quality["duplicate_match_ids"] = duplicate_approved
    if duplicate_approved:
        failures["duplicate_match"] += duplicate_approved

    for approved in sorted(approved_payloads, key=lambda item: item.match_id):
        try:
            transformed = _transform_payload(approved, platform, quality)
        except Stage3ValidationError as error:
            failures[error.category] += 1
            continue
        except ValidationError:
            failures["canonical_schema_validation"] += 1
            continue
        match_row, participant_rows, team_rows, ban_rows = transformed
        matches.append(match_row)
        participants.extend(participant_rows)
        teams.extend(team_rows)
        bans.extend(ban_rows)

    matches.sort(key=lambda row: row.match_id)
    participants.sort(key=lambda row: (row.match_id, row.participant_id))
    teams.sort(key=lambda row: (row.match_id, row.team_id))
    bans.sort(
        key=lambda row: (
            row.match_id,
            row.team_id,
            row.pick_turn if row.pick_turn is not None else 10_000,
            row.champion_id if row.champion_id is not None else -1,
        )
    )
    processed_patch_counts = dict(
        sorted(Counter(row.public_patch for row in matches).items())
    )
    if len(matches) != expected_match_count:
        failures["processed_match_count"] += 1
    if processed_patch_counts != dict(sorted(expected_patch_counts.items())):
        failures["processed_patch_counts"] += 1

    quality_report = _finalize_quality_report(
        quality=quality,
        failures=failures,
        input_count=len(approved_payloads),
        expected_match_count=expected_match_count,
        expected_patch_counts=expected_patch_counts,
        matches=matches,
        participants=participants,
        teams=teams,
        bans=bans,
        output_directory=output_directory,
    )
    metadata = {
        "processing_schema_version": CANONICAL_SCHEMA_VERSION,
        "quality_report_schema_version": QUALITY_REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "input_manifest": input_manifest,
        "queue_id": RANKED_SOLO_QUEUE_ID,
        "accepted_public_patches": sorted(expected_patch_counts),
        "match_counts_by_public_patch": processed_patch_counts,
        "row_counts": {
            "bans": len(bans),
            "matches": len(matches),
            "participants": len(participants),
            "teams": len(teams),
        },
        "storage_format": "deterministic-jsonl",
        "player_identifier": {
            "column": "player_key",
            "method": "sha256-project-scoped-truncated-128-bit",
            "raw_puuid_stored": False,
        },
    }
    return CanonicalDataset(
        run_id=run_id,
        input_manifest=input_manifest,
        output_directory=output_directory,
        matches=matches,
        participants=participants,
        teams=teams,
        bans=bans,
        quality_report=quality_report,
        metadata=metadata,
    )


def write_canonical_dataset(dataset: CanonicalDataset) -> Path:
    """Publish one complete deterministic dataset after all files are staged."""

    if not dataset.quality_report["ready_for_stage_3_2"]:
        raise Stage3ValidationError(
            "invariant_failure",
            "canonical output was not written because validation failed",
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


def _validate_approval_state(
    manifest: dict[str, Any],
    checkpoint: dict[str, Any],
    *,
    expected_match_count: int,
    expected_patch_counts: dict[str, int],
) -> tuple[str, str, dict[str, str]]:
    run_id = _required_string(manifest.get("run_id"), "manifest_run_id")
    if checkpoint.get("run_id") != run_id:
        raise Stage3ValidationError("run_id_conflict", "input run IDs disagree")
    summary = manifest.get("summary")
    config = manifest.get("configuration")
    checkpoint_config = checkpoint.get("config")
    matches = checkpoint.get("matches")
    if not all(
        isinstance(value, dict) for value in (summary, config, checkpoint_config)
    ):
        raise Stage3ValidationError(
            "malformed_manifest", "required input sections missing"
        )
    if not isinstance(matches, dict):
        raise Stage3ValidationError("malformed_checkpoint", "match state is missing")
    if (
        summary.get("target_reached") is not True
        or summary.get("completion_status") != "target_reached"
    ):
        raise Stage3ValidationError(
            "incomplete_population", "Stage 2 population did not reach its target"
        )
    platform = _required_string(config.get("platform"), "platform")
    if platform != checkpoint_config.get("platform") or platform != manifest.get(
        "platform"
    ):
        raise Stage3ValidationError(
            "platform_conflict", "input platform values disagree"
        )
    queue_values = {config.get("queue_id"), checkpoint_config.get("queue_id")}
    if queue_values != {RANKED_SOLO_QUEUE_ID}:
        raise Stage3ValidationError("queue_conflict", "approved queue is not 420")
    manifest_patches = manifest.get("accepted_public_patches")
    checkpoint_patches = checkpoint.get("accepted_public_patches")
    if set(manifest_patches or []) != set(expected_patch_counts) or set(
        checkpoint_patches or []
    ) != set(expected_patch_counts):
        raise Stage3ValidationError(
            "accepted_patch_conflict",
            "accepted patch window does not match expectation",
        )
    manifest_counts = summary.get("accepted_matches_by_public_patch")
    checkpoint_counts = checkpoint.get("accepted_match_counts_by_public_patch")
    if (
        manifest_counts != expected_patch_counts
        or checkpoint_counts != expected_patch_counts
    ):
        raise Stage3ValidationError(
            "approved_patch_counts", "approved patch counts do not match expectation"
        )
    for field in ("accepted_matches", "total_accepted_matches_credited"):
        if summary.get(field) != expected_match_count:
            raise Stage3ValidationError(
                "approved_match_count",
                "approved match total does not match expectation",
            )

    approvals: dict[str, str] = {}
    for match_id, record in matches.items():
        if not isinstance(record, dict):
            raise Stage3ValidationError("malformed_checkpoint", "invalid match state")
        if record.get("status") not in APPROVED_CHECKPOINT_STATUSES:
            continue
        public_patch = record.get("public_patch")
        if not isinstance(match_id, str) or not isinstance(public_patch, str):
            raise Stage3ValidationError(
                "malformed_checkpoint", "invalid approved state"
            )
        approvals[match_id] = public_patch
    if len(approvals) != expected_match_count:
        raise Stage3ValidationError(
            "approved_match_count",
            "checkpoint approval count does not match expectation",
        )
    if dict(sorted(Counter(approvals.values()).items())) != dict(
        sorted(expected_patch_counts.items())
    ):
        raise Stage3ValidationError(
            "approved_patch_counts",
            "checkpoint approval patches do not match expectation",
        )
    return run_id, platform, approvals


def _resolve_approved_payloads(
    *,
    approvals: dict[str, str],
    manifest: dict[str, Any],
    manifest_path: Path,
    catalog_path: Path,
    raw_root: Path,
    processed_root: Path,
) -> list[ApprovedPayload]:
    manifest_files = manifest.get("match_files")
    if not isinstance(manifest_files, list) or not all(
        isinstance(item, str) for item in manifest_files
    ):
        raise Stage3ValidationError("malformed_manifest", "match file list is invalid")
    if len(manifest_files) != len(set(manifest_files)):
        raise Stage3ValidationError(
            "duplicate_match", "manifest contains duplicate payload references"
        )
    direct_ids: set[str] = set()
    for relative in manifest_files:
        path = _confined_path(manifest_path.parent, relative)
        payload = _load_json_object(path, "payload")
        match_id = _payload_match_id(payload)
        if match_id not in approvals:
            raise Stage3ValidationError(
                "unapproved_payload",
                "manifest references a payload outside approval state",
            )
        if match_id in direct_ids:
            raise Stage3ValidationError(
                "duplicate_match", "multiple manifest payloads identify one match"
            )
        direct_ids.add(match_id)

    catalog_rows = _read_catalog(catalog_path, set(approvals))
    if set(catalog_rows) != set(approvals):
        raise Stage3ValidationError(
            "catalog_missing", "catalog does not contain every approved match"
        )
    routing_region = manifest.get("routing_region")
    if not isinstance(routing_region, str) or not routing_region:
        raise Stage3ValidationError(
            "malformed_manifest", "analysis routing region is missing"
        )
    approved_payloads: list[ApprovedPayload] = []
    for match_id, approved_patch in sorted(approvals.items()):
        catalog = catalog_rows[match_id]
        if (
            catalog.get("status") != "processed"
            or catalog.get("public_patch") != approved_patch
            or catalog.get("queue_id") != RANKED_SOLO_QUEUE_ID
        ):
            raise Stage3ValidationError(
                "catalog_conflict", "catalog metadata conflicts with approval state"
            )
        path = _locate_retained_payload(raw_root, match_id)
        partition_path = (
            processed_root
            / f"region={_safe_match_filename(routing_region)}"
            / f"patch={approved_patch}"
            / f"queue={RANKED_SOLO_QUEUE_ID}"
            / "matches"
            / f"{_safe_match_filename(match_id)}.json"
        )
        partition_record = _load_json_object(partition_path, "partition")
        if (
            partition_record.get("match_id") != match_id
            or partition_record.get("public_patch") != approved_patch
            or partition_record.get("queue_id") != RANKED_SOLO_QUEUE_ID
        ):
            raise Stage3ValidationError(
                "partition_conflict",
                "normalized partition conflicts with approval state",
            )
        approved_payloads.append(
            ApprovedPayload(
                match_id=match_id,
                public_patch=approved_patch,
                path=path,
                source_reference=_relative_reference(path, Path.cwd()),
                catalog=catalog,
                partition_record=partition_record,
            )
        )
    return approved_payloads


def _transform_payload(
    approved: ApprovedPayload,
    platform: str,
    quality: dict[str, Any],
) -> tuple[
    CanonicalMatch,
    list[CanonicalParticipant],
    list[CanonicalTeam],
    list[CanonicalBan],
]:
    raw_payload = _load_json_object(approved.path, "payload")
    try:
        raw_match = RiotMatch.model_validate(raw_payload)
    except ValidationError as error:
        category = _validation_error_category(error)
        raise Stage3ValidationError(
            category, "payload failed schema validation"
        ) from None
    if raw_match.metadata.matchId != approved.match_id:
        raise Stage3ValidationError(
            "payload_match_id_conflict",
            "payload identity conflicts with approval state",
        )
    info = raw_match.info
    if info.queueId != RANKED_SOLO_QUEUE_ID:
        quality["unexpected_queue_ids"][str(info.queueId)] += 1
        raise Stage3ValidationError("unexpected_queue", "payload queue is not 420")
    game_mode = info.gameMode or "<missing>"
    quality["game_mode_distribution"][game_mode] += 1
    if game_mode != EXPECTED_GAME_MODE:
        quality["unexpected_game_modes"][game_mode] += 1
        raise Stage3ValidationError(
            "unexpected_game_mode", "payload game mode is not CLASSIC"
        )
    if not info.platformId or info.platformId.lower() != platform.lower():
        raise Stage3ValidationError(
            "payload_platform_conflict", "payload platform conflicts with manifest"
        )
    if len(info.participants) != EXPECTED_PARTICIPANTS:
        raise Stage3ValidationError(
            "participant_count", "payload does not contain ten participants"
        )
    if len(info.teams) != EXPECTED_TEAMS:
        raise Stage3ValidationError("team_count", "payload does not contain two teams")
    critical_participant_fields = {
        "champion_id": lambda item: item.championId is None,
        "champion_name": lambda item: not item.championName,
        "participant_id": lambda item: item.participantId is None,
        "player_key_source": lambda item: not item.puuid or not item.puuid.strip(),
        "team_id": lambda item: item.teamId is None,
        "win": lambda item: item.win is None,
    }
    missing_critical = False
    for field, is_missing in critical_participant_fields.items():
        count = sum(is_missing(item) for item in info.participants)
        if count:
            quality["missing_critical_fields"][field] += count
            missing_critical = True
    if missing_critical:
        raise Stage3ValidationError(
            "missing_participant_critical_field",
            "a required participant field is missing",
        )
    participant_ids = [item.participantId for item in info.participants]
    duplicate_participant_ids = len(participant_ids) - len(set(participant_ids))
    if duplicate_participant_ids:
        quality["duplicate_participant_ids_within_matches"] += duplicate_participant_ids
        raise Stage3ValidationError(
            "duplicate_participant_id", "participant IDs are not unique within a match"
        )
    team_ids = [item.teamId for item in info.teams]
    if None in team_ids or len(set(team_ids)) != EXPECTED_TEAMS:
        raise Stage3ValidationError(
            "team_identity", "team IDs are missing or duplicate"
        )
    winners = [item.teamId for item in info.teams if item.win is True]
    losers = [item.teamId for item in info.teams if item.win is False]
    if len(winners) != 1 or len(losers) != 1:
        raise Stage3ValidationError(
            "winner_identity", "match lacks one winner and loser"
        )
    if info.gameDuration <= 0:
        quality["invalid_or_missing_duration"] += 1
        raise Stage3ValidationError("invalid_duration", "game duration is invalid")
    game_creation = _timestamp(info.gameCreation, "game_creation")
    game_start = _optional_timestamp(info.gameStartTimestamp, quality, "game_start")
    game_end = _optional_timestamp(info.gameEndTimestamp, quality, "game_end")
    if game_start and game_end and game_end < game_start:
        quality["invalid_timestamp_order"] += 1
        raise Stage3ValidationError(
            "invalid_timestamp", "game timestamps are out of order"
        )
    patch = resolve_patch(info.gameVersion, game_creation)
    if patch.status != "resolved" or patch.public_patch != approved.public_patch:
        raise Stage3ValidationError(
            "payload_patch_conflict", "payload patch conflicts with approval state"
        )
    if (
        approved.catalog.get("public_patch") != patch.public_patch
        or approved.catalog.get("api_game_version") != info.gameVersion
        or approved.catalog.get("patch_resolution_status") != "resolved"
        or approved.partition_record.get("public_patch") != patch.public_patch
        or approved.partition_record.get("api_game_version") != info.gameVersion
    ):
        raise Stage3ValidationError(
            "provenance_patch_conflict", "retained provenance patch values disagree"
        )
    if not info.gameVersion:
        raise Stage3ValidationError("missing_game_version", "game version is missing")
    is_short = info.gameDuration < SHORT_GAME_THRESHOLD_SECONDS
    if is_short:
        quality["remake_or_short_game_count"] += 1
    match_row = CanonicalMatch(
        match_id=approved.match_id,
        public_patch=patch.public_patch,
        game_version=info.gameVersion,
        platform=platform,
        queue_id=info.queueId,
        game_creation=game_creation,
        game_start_timestamp=game_start,
        game_end_timestamp=game_end,
        game_duration_seconds=info.gameDuration,
        winning_team_id=winners[0],
        is_remake_or_short_game=is_short,
        source_payload_reference=approved.source_reference,
        processing_schema_version=CANONICAL_SCHEMA_VERSION,
    )
    participant_rows = [
        _participant_row(approved.match_id, participant, quality)
        for participant in info.participants
    ]
    if any(row.team_id not in team_ids for row in participant_rows):
        raise Stage3ValidationError(
            "participant_team_conflict", "participant references an unknown team"
        )
    team_rows = [_team_row(approved.match_id, team) for team in info.teams]
    ban_rows: list[CanonicalBan] = []
    incomplete_bans = False
    for team in info.teams:
        if len(team.bans) < EXPECTED_BANS_PER_TEAM:
            incomplete_bans = True
            quality["missing_ban_slots"] += EXPECTED_BANS_PER_TEAM - len(team.bans)
        for ban in team.bans:
            if ban.championId == -1:
                quality["explicit_no_ban_rows"] += 1
            ban_rows.append(
                CanonicalBan(
                    match_id=approved.match_id,
                    team_id=team.teamId,
                    pick_turn=ban.pickTurn,
                    champion_id=ban.championId,
                    processing_schema_version=CANONICAL_SCHEMA_VERSION,
                )
            )
    if incomplete_bans:
        quality["matches_with_incomplete_bans"] += 1
    return match_row, participant_rows, team_rows, ban_rows


def _participant_row(
    match_id: str, participant: Any, quality: dict[str, Any]
) -> CanonicalParticipant:
    team_position = normalize_position(participant.teamPosition)
    individual_position = normalize_position(participant.individualPosition)
    quality["team_position_distribution"][team_position or "<missing>"] += 1
    quality["individual_position_distribution"][individual_position or "<missing>"] += 1
    if team_position is None:
        quality["missing_team_positions"] += 1
    if individual_position is None:
        quality["missing_individual_positions"] += 1
    if (
        team_position is not None
        and individual_position is not None
        and team_position != individual_position
    ):
        quality["ambiguous_positions"] += 1
    challenges = _extra(participant, "challenges") or {}
    if not challenges:
        quality["participants_missing_challenges"] += 1
    return CanonicalParticipant(
        match_id=match_id,
        participant_id=participant.participantId,
        team_id=participant.teamId,
        player_key=pseudonymize_puuid(participant.puuid),
        champion_id=participant.championId,
        champion_name=participant.championName,
        summoner_spell_1_id=participant.summoner1Id,
        summoner_spell_2_id=participant.summoner2Id,
        team_position=team_position,
        individual_position=individual_position,
        lane=participant.lane,
        role=participant.role,
        win=participant.win,
        kills=participant.kills,
        deaths=participant.deaths,
        assists=participant.assists,
        total_minions_killed=participant.totalMinionsKilled,
        neutral_minions_killed=participant.neutralMinionsKilled,
        gold_earned=participant.goldEarned,
        total_damage_dealt_to_champions=participant.totalDamageDealtToChampions,
        physical_damage_dealt_to_champions=_extra(
            participant, "physicalDamageDealtToChampions"
        ),
        magic_damage_dealt_to_champions=_extra(
            participant, "magicDamageDealtToChampions"
        ),
        true_damage_dealt_to_champions=_extra(
            participant, "trueDamageDealtToChampions"
        ),
        total_damage_taken=participant.totalDamageTaken,
        damage_self_mitigated=participant.damageSelfMitigated,
        total_heal=participant.totalHeal,
        total_heals_on_teammates=_extra(participant, "totalHealsOnTeammates"),
        total_damage_shielded_on_teammates=_extra(
            participant, "totalDamageShieldedOnTeammates"
        ),
        vision_score=participant.visionScore,
        wards_placed=participant.wardsPlaced,
        wards_killed=participant.wardsKilled,
        control_wards_placed=_extra(participant, "detectorWardsPlaced"),
        control_wards_purchased=_extra(participant, "visionWardsBoughtInGame"),
        champion_level=_extra(participant, "champLevel"),
        item_0=participant.item0,
        item_1=participant.item1,
        item_2=participant.item2,
        item_3=participant.item3,
        item_4=participant.item4,
        item_5=participant.item5,
        item_6=participant.item6,
        time_played_seconds=_extra(participant, "timePlayed"),
        challenge_objectives_stolen=_challenge(challenges, "objectivesStolen"),
        challenge_save_ally_from_death=_challenge(challenges, "saveAllyFromDeath"),
        challenge_skillshots_dodged=_challenge(challenges, "skillshotsDodged"),
        challenge_skillshots_hit=_challenge(challenges, "skillshotsHit"),
        challenge_solo_kills=_challenge(challenges, "soloKills"),
        challenge_turret_plates_taken=_challenge(challenges, "turretPlatesTaken"),
        processing_schema_version=CANONICAL_SCHEMA_VERSION,
    )


def _team_row(match_id: str, team: Any) -> CanonicalTeam:
    objectives = team.objectives
    return CanonicalTeam(
        match_id=match_id,
        team_id=team.teamId,
        win=team.win,
        champion_kills=_objective_value(objectives.champion, "kills"),
        champion_first=_objective_value(objectives.champion, "first"),
        tower_kills=_objective_value(objectives.tower, "kills"),
        tower_first=_objective_value(objectives.tower, "first"),
        inhibitor_kills=_objective_value(objectives.inhibitor, "kills"),
        inhibitor_first=_objective_value(objectives.inhibitor, "first"),
        dragon_kills=_objective_value(objectives.dragon, "kills"),
        dragon_first=_objective_value(objectives.dragon, "first"),
        rift_herald_kills=_objective_value(objectives.riftHerald, "kills"),
        rift_herald_first=_objective_value(objectives.riftHerald, "first"),
        baron_kills=_objective_value(objectives.baron, "kills"),
        baron_first=_objective_value(objectives.baron, "first"),
        processing_schema_version=CANONICAL_SCHEMA_VERSION,
    )


def _quality_accumulator() -> dict[str, Any]:
    return {
        "ambiguous_positions": 0,
        "duplicate_match_ids": 0,
        "duplicate_participant_ids_within_matches": 0,
        "explicit_no_ban_rows": 0,
        "game_mode_distribution": Counter(),
        "individual_position_distribution": Counter(),
        "invalid_or_missing_duration": 0,
        "invalid_timestamp_order": 0,
        "matches_with_incomplete_bans": 0,
        "missing_ban_slots": 0,
        "missing_critical_fields": Counter(),
        "missing_individual_positions": 0,
        "missing_optional_timestamps": Counter({"game_end": 0, "game_start": 0}),
        "missing_team_positions": 0,
        "participants_missing_challenges": 0,
        "remake_or_short_game_count": 0,
        "team_position_distribution": Counter(),
        "unexpected_game_modes": Counter(),
        "unexpected_queue_ids": Counter(),
    }


def _finalize_quality_report(
    *,
    quality: dict[str, Any],
    failures: Counter[str],
    input_count: int,
    expected_match_count: int,
    expected_patch_counts: dict[str, int],
    matches: list[CanonicalMatch],
    participants: list[CanonicalParticipant],
    teams: list[CanonicalTeam],
    bans: list[CanonicalBan],
    output_directory: Path,
) -> dict[str, Any]:
    participant_counts = Counter(row.match_id for row in participants)
    team_counts = Counter(row.match_id for row in teams)
    unexpected_participant_counts = sum(
        count != EXPECTED_PARTICIPANTS for count in participant_counts.values()
    )
    unexpected_team_counts = sum(
        count != EXPECTED_TEAMS for count in team_counts.values()
    )
    invariant_failures = dict(sorted(failures.items()))
    if unexpected_participant_counts:
        invariant_failures["canonical_participant_count"] = (
            unexpected_participant_counts
        )
    if unexpected_team_counts:
        invariant_failures["canonical_team_count"] = unexpected_team_counts
    output_names = {
        "bans": "bans.jsonl",
        "matches": "matches.jsonl",
        "metadata": "metadata.json",
        "participants": "participants.jsonl",
        "quality_report": "quality_report.json",
        "teams": "teams.jsonl",
    }
    expected_ban_slots = len(matches) * EXPECTED_BANS_PER_TEAM * EXPECTED_TEAMS
    incomplete_ban_matches = quality["matches_with_incomplete_bans"]
    return {
        "quality_report_schema_version": QUALITY_REPORT_SCHEMA_VERSION,
        "processing_schema_version": CANONICAL_SCHEMA_VERSION,
        "input": {
            "approved_matches": input_count,
            "expected_matches": expected_match_count,
            "expected_match_counts_by_public_patch": dict(
                sorted(expected_patch_counts.items())
            ),
        },
        "processed": {
            "match_counts_by_public_patch": dict(
                sorted(Counter(row.public_patch for row in matches).items())
            ),
            "matches": len(matches),
            "participants": len(participants),
            "teams": len(teams),
            "bans": len(bans),
        },
        "shape": {
            "participants_per_match_distribution": _count_distribution(
                participant_counts
            ),
            "teams_per_match_distribution": _count_distribution(team_counts),
            "matches_with_unexpected_participant_count": unexpected_participant_counts,
            "matches_with_unexpected_team_count": unexpected_team_counts,
            "duplicate_match_ids": quality["duplicate_match_ids"],
            "duplicate_participant_ids_within_matches": quality[
                "duplicate_participant_ids_within_matches"
            ],
        },
        "positions": {
            "missing_team_positions": quality["missing_team_positions"],
            "missing_individual_positions": quality["missing_individual_positions"],
            "ambiguous_team_vs_individual_positions": quality["ambiguous_positions"],
            "team_position_distribution": _sorted_counter(
                quality["team_position_distribution"]
            ),
            "individual_position_distribution": _sorted_counter(
                quality["individual_position_distribution"]
            ),
        },
        "bans": {
            "rows": len(bans),
            "expected_slots_per_match": EXPECTED_BANS_PER_TEAM * EXPECTED_TEAMS,
            "matches_with_incomplete_bans": incomplete_ban_matches,
            "matches_with_incomplete_bans_rate": (
                round(incomplete_ban_matches / len(matches), 6) if matches else None
            ),
            "missing_slots": quality["missing_ban_slots"],
            "missing_slot_rate": (
                round(quality["missing_ban_slots"] / expected_ban_slots, 6)
                if expected_ban_slots
                else None
            ),
            "explicit_no_ban_rows": quality["explicit_no_ban_rows"],
        },
        "timestamps_and_duration": {
            "invalid_or_missing_duration": quality["invalid_or_missing_duration"],
            "invalid_timestamp_order": quality["invalid_timestamp_order"],
            "missing_optional_timestamps": _sorted_counter(
                quality["missing_optional_timestamps"]
            ),
            "remake_or_short_game_count": quality["remake_or_short_game_count"],
            "short_game_rule": (
                f"game_duration_seconds < {SHORT_GAME_THRESHOLD_SECONDS}"
            ),
        },
        "payload_quality": {
            "expected_queue_id": RANKED_SOLO_QUEUE_ID,
            "expected_game_mode": EXPECTED_GAME_MODE,
            "participants_missing_challenges": quality[
                "participants_missing_challenges"
            ],
            "game_mode_distribution": _sorted_counter(
                quality["game_mode_distribution"]
            ),
            "unexpected_game_modes": _sorted_counter(quality["unexpected_game_modes"]),
            "unexpected_queue_ids": _sorted_counter(quality["unexpected_queue_ids"]),
            "missing_critical_fields": _sorted_counter(
                quality["missing_critical_fields"]
            ),
            "malformed_or_skipped_payloads_by_category": dict(sorted(failures.items())),
            "malformed_or_skipped_payloads_total": sum(failures.values()),
        },
        "invariant_failures": invariant_failures,
        "privacy": {
            "aggregate_report_only": True,
            "raw_puuid_stored": False,
            "player_key_method": "sha256-project-scoped-truncated-128-bit",
        },
        "outputs": {
            "directory": output_directory.as_posix(),
            "files": output_names,
            "storage_format": "deterministic-jsonl",
        },
        "ready_for_stage_3_2": not invariant_failures,
    }


def _write_staged_dataset(dataset: CanonicalDataset, directory: Path) -> None:
    _write_lines(directory / "matches.jsonl", dataset.matches)
    _write_lines(directory / "participants.jsonl", dataset.participants)
    _write_lines(directory / "teams.jsonl", dataset.teams)
    _write_lines(directory / "bans.jsonl", dataset.bans)
    _write_json(directory / "metadata.json", dataset.metadata)
    _write_json(directory / "quality_report.json", dataset.quality_report)


def _write_lines(path: Path, rows: list[CanonicalModel]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_catalog(path: Path, approved_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise Stage3ValidationError("catalog_missing", "processing catalog is missing")
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in approved_ids)
        rows = connection.execute(
            f"""
            SELECT match_id, status, api_game_version, public_patch,
                   patch_resolution_status, queue_id, source_snapshot
            FROM processed_matches WHERE match_id IN ({placeholders})
            """,
            sorted(approved_ids),
        ).fetchall()
    except sqlite3.Error:
        raise Stage3ValidationError(
            "catalog_invalid", "catalog could not be read"
        ) from None
    finally:
        if "connection" in locals():
            connection.close()
    return {str(row["match_id"]): dict(row) for row in rows}


def _locate_retained_payload(raw_root: Path, match_id: str) -> Path:
    filename = f"{_safe_match_filename(match_id)}.json"
    candidates = sorted(raw_root.glob(f"*/downloads/{filename}"))
    if not candidates:
        raise Stage3ValidationError(
            "payload_missing", "an approved retained payload is missing"
        )
    digests = {hashlib.sha256(path.read_bytes()).digest() for path in candidates}
    if len(digests) != 1:
        raise Stage3ValidationError(
            "payload_cache_conflict", "retained copies of a payload disagree"
        )
    return candidates[-1]


def _load_json_object(path: Path, source: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except FileNotFoundError:
        raise Stage3ValidationError(
            f"{source}_missing", f"required {source} is missing"
        ) from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise Stage3ValidationError(
            f"malformed_{source}", f"{source} is not valid JSON"
        ) from None
    if not isinstance(value, dict):
        raise Stage3ValidationError(
            f"malformed_{source}", f"{source} must be an object"
        )
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object key", key, 0)
        result[key] = value
    return result


def _payload_match_id(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata")
    match_id = metadata.get("matchId") if isinstance(metadata, dict) else None
    return _required_string(match_id, "payload_match_id")


def _timestamp(value: int, field: str) -> datetime:
    if value <= 0:
        raise Stage3ValidationError("invalid_timestamp", f"{field} is invalid")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError):
        raise Stage3ValidationError(
            "invalid_timestamp", f"{field} is invalid"
        ) from None


def _optional_timestamp(
    value: int | None, quality: dict[str, Any], field: str
) -> datetime | None:
    if value is None:
        quality["missing_optional_timestamps"][field] += 1
        return None
    return _timestamp(value, field)


def _validation_error_category(error: ValidationError) -> str:
    locations = {tuple(item["loc"]) for item in error.errors(include_url=False)}
    if any(location[:2] == ("info", "gameDuration") for location in locations):
        return "malformed_duration"
    if any(location[:2] == ("info", "gameCreation") for location in locations):
        return "malformed_timestamp"
    if any(location[:2] == ("info", "queueId") for location in locations):
        return "malformed_queue"
    return "schema_validation"


def _extra(model: Any, name: str) -> Any:
    return getattr(model, name, None)


def _challenge(challenges: dict[str, Any], name: str) -> int | None:
    value = challenges.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _objective_value(objective: Any, field: str) -> Any:
    return getattr(objective, field, None) if objective is not None else None


def _count_distribution(counts: Counter[str]) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in counts.values()).items()))


def _sorted_counter(counter: Counter[Any]) -> dict[str, int]:
    return dict(sorted((str(key), value) for key, value in counter.items()))


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Stage3ValidationError("missing_critical_field", f"{field} is missing")
    return value


def _confined_path(root: Path, relative: str) -> Path:
    root_resolved = root.resolve()
    path = (root / relative).resolve()
    if path != root_resolved and root_resolved not in path.parents:
        raise Stage3ValidationError("unsafe_path", "manifest path escapes its snapshot")
    return path


def _safe_match_filename(match_id: str) -> str:
    return "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in match_id
    )


def _relative_reference(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


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
