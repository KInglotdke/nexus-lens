"""Stage 3.5A top-lane trajectory and familiarity feasibility pipeline."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nexus_lens.canonical import CanonicalMatch, CanonicalParticipant
from nexus_lens.data_seal import sha256_file
from nexus_lens.stage34b_operations import validate_public_payload
from nexus_lens.timeline_collection import (
    REQUIRED_STAGE31_FILES,
    HistoryPolicyConfig,
    Stage35Config,
    TimelinePayloadReader,
    verify_stage31_sources,
)

STAGE35A_SCHEMA_VERSION = "stage3.5a-v1"
STAGE35A_AUDIT_SCHEMA_VERSION = "stage3.5a-audit-v1"
STAGE35A_MANIFEST_SCHEMA_VERSION = "stage3.5a-manifest-v1"
PRIMARY_MINUTES = (5, 10, 15)
EXPLORATORY_MINUTES = (20, 25)
ALL_MINUTES = PRIMARY_MINUTES + EXPLORATORY_MINUTES
PLATE_CUTOFF_MS = 14 * 60 * 1000
HERALD_TOWER_WINDOW_MS = 120 * 1000
CHAMPION_SELECT_FEATURE_FIELDS = (
    "platform",
    "public_patch",
    "focal_side",
    "focal_champion_id",
    "enemy_top_champion_id",
    "familiarity",
)


class Stage35Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrajectoryDifference(Stage35Model):
    requested_minute: int
    observed_timestamp_ms: int
    gold_difference: int
    xp_difference: int
    lane_minion_cs_difference: int
    total_farm_difference: int
    level_difference: int


class TrajectoryChange(Stage35Model):
    start_minute: int
    end_minute: int
    gold_change: int
    xp_change: int
    lane_minion_cs_change: int
    total_farm_change: int
    level_change: int


class TowerOutcome(Stage35Model):
    primary_label_at_15: str
    first_destroyed_team_relation: str | None
    first_top_outer_timestamp_ms: int | None
    fell_before_10: bool
    fell_before_15: bool
    fell_before_20: bool
    fell_before_25: bool
    top_plate_difference_before_cutoff: int | None
    plate_attribution_supported: bool


class InterventionIndicators(Stage35Model):
    timestamp_minute: int
    jungler_kill_participation_involving_top: bool
    other_role_kill_participation_involving_top: bool
    kill_involving_top_near_top_lane: bool
    herald_related_top_tower_proxy: bool
    any_recorded_intervention_proxy: bool


class FamiliarityFeatures(Stage35Model):
    observed_prior_ranked_games: int
    observed_prior_top_games: int
    observed_prior_champion_games: int
    recent_prior_champion_games: int
    days_since_champion_last_played: float | None
    champion_ewm_gold_difference_5: float | None
    champion_ewm_gold_difference_10: float | None
    champion_ewm_gold_difference_15: float | None
    champion_ewm_xp_difference_5: float | None
    champion_ewm_xp_difference_10: float | None
    champion_ewm_xp_difference_15: float | None
    historical_top_gold_difference_10_mean: float | None
    champion_gold_difference_10_mean: float | None
    champion_relative_to_general_gold_10: float | None
    shrunk_historical_win_rate: float
    shrunk_win_rate_standard_error: float
    missing_history: bool
    low_support: bool
    limited_history_or_smurf_like: bool


class FocalPerspectiveRow(Stage35Model):
    processing_schema_version: str = STAGE35A_SCHEMA_VERSION
    match_group_id: str
    platform: str
    public_patch: str
    queue_id: int
    game_creation: datetime
    focal_player_key: str
    focal_team_id: int
    focal_side: str
    focal_champion_id: int
    focal_champion_name: str
    enemy_top_champion_id: int
    enemy_top_champion_name: str
    scientific_weight: float = Field(default=0.5)
    trajectory: dict[int, TrajectoryDifference | None]
    trajectory_changes: dict[str, TrajectoryChange | None]
    tower: TowerOutcome
    interventions: dict[int, InterventionIndicators]
    game_win: bool
    familiarity: FamiliarityFeatures | None = None


@dataclass(frozen=True)
class SourceTopMatch:
    match: CanonicalMatch
    top_participants: tuple[CanonicalParticipant, CanonicalParticipant]
    participant_positions: dict[int, str | None]


@dataclass(frozen=True)
class Stage35Dataset:
    rows: tuple[FocalPerspectiveRow, ...]
    all_eligible_match_count: int
    selected_match_count: int
    downloaded_match_count: int
    exclusions: dict[str, int]
    audit: dict[str, Any]
    manifest: dict[str, Any]
    quality_report: dict[str, Any]
    dataset_sha256: str


def champion_select_feature_record(row: FocalPerspectiveRow) -> dict[str, Any]:
    """Return the prospective feature table with no outcome/account leakage."""

    payload = row.model_dump(mode="json")
    return {field: payload[field] for field in CHAMPION_SELECT_FEATURE_FIELDS}


def select_timeline_frame(
    frames: list[dict[str, Any]],
    requested_minute: int,
    *,
    maximum_lateness_ms: int,
) -> dict[str, Any] | None:
    """Select the earliest frame at/after an endpoint within a fixed tolerance."""

    requested_ms = requested_minute * 60 * 1000
    candidates = []
    for frame in frames:
        timestamp = frame.get("timestamp")
        if (
            isinstance(timestamp, (int, float))
            and not isinstance(timestamp, bool)
            and math.isfinite(timestamp)
            and requested_ms <= timestamp <= requested_ms + maximum_lateness_ms
        ):
            candidates.append((int(timestamp), frame))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def identify_top_participants(
    participants: list[CanonicalParticipant],
) -> tuple[CanonicalParticipant, CanonicalParticipant]:
    """Require exactly one unambiguous TOP participant per standard team."""

    tops = [
        row
        for row in participants
        if row.team_position == "TOP" and row.individual_position in (None, "TOP")
    ]
    by_team: dict[int, list[CanonicalParticipant]] = defaultdict(list)
    for row in tops:
        by_team[row.team_id].append(row)
    if set(by_team) != {100, 200} or any(len(rows) != 1 for rows in by_team.values()):
        raise ValueError("top_participant_identification")
    return by_team[100][0], by_team[200][0]


def load_source_top_matches(config: Stage35Config) -> tuple[SourceTopMatch, ...]:
    """Load all 10,000 sealed matches and identify their top participants."""

    verify_stage31_sources(config)
    loaded: list[SourceTopMatch] = []
    keys: set[tuple[str, str]] = set()
    for source in sorted(config.sources, key=lambda item: item.label):
        matches = {
            row.match_id: row
            for row in _read_jsonl_models(
                source.directory / "matches.jsonl", CanonicalMatch
            )
            if row.public_patch == config.public_patch
        }
        participants: dict[str, list[CanonicalParticipant]] = defaultdict(list)
        for row in _read_jsonl_models(
            source.directory / "participants.jsonl", CanonicalParticipant
        ):
            if row.match_id in matches:
                participants[row.match_id].append(row)
        if set(matches) != set(participants):
            raise ValueError("Stage 3.1 match/participant membership differs")
        for match_id, match in matches.items():
            key = (match.platform.lower(), match_id)
            if key in keys:
                raise ValueError("Stage 3.1 match membership is duplicated")
            keys.add(key)
            rows = participants[match_id]
            try:
                tops = identify_top_participants(rows)
            except ValueError:
                tops = ()  # type: ignore[assignment]
            loaded.append(
                SourceTopMatch(
                    match=match,
                    top_participants=tops,
                    participant_positions={
                        row.participant_id: row.team_position for row in rows
                    },
                )
            )
    return tuple(
        sorted(
            loaded,
            key=lambda row: (
                row.match.game_creation,
                row.match.platform,
                row.match.match_id,
            ),
        )
    )


def transform_top_match(
    source: SourceTopMatch,
    timeline: dict[str, Any],
    config: Stage35Config,
) -> tuple[FocalPerspectiveRow, FocalPerspectiveRow]:
    """Create symmetric focal rows, or raise a stable technical exclusion reason."""

    match = source.match
    if match.public_patch != config.public_patch:
        raise ValueError("wrong_patch")
    if match.queue_id != config.queue_id:
        raise ValueError("wrong_queue")
    if match.is_remake_or_short_game or (
        match.game_duration_seconds < config.minimum_game_duration_seconds
    ):
        raise ValueError("remake_or_short_game")
    if len(source.top_participants) != 2:
        raise ValueError("top_participant_identification")
    info = timeline.get("info")
    frames = info.get("frames") if isinstance(info, dict) else None
    if not isinstance(frames, list):
        raise ValueError("timeline_frames_missing")
    selected = {
        minute: select_timeline_frame(
            frames,
            minute,
            maximum_lateness_ms=config.maximum_frame_lateness_ms,
        )
        for minute in ALL_MINUTES
    }
    if any(selected[minute] is None for minute in PRIMARY_MINUTES):
        raise ValueError("primary_frame_missing")
    points: dict[int, dict[int, dict[str, int]] | None] = {}
    for minute, frame in selected.items():
        if frame is None:
            points[minute] = None
            continue
        participant_frames = frame.get("participantFrames")
        if not isinstance(participant_frames, dict):
            if minute in PRIMARY_MINUTES:
                raise ValueError("participant_frames_missing")
            points[minute] = None
            continue
        values: dict[int, dict[str, int]] = {}
        for top in source.top_participants:
            raw = participant_frames.get(str(top.participant_id))
            if raw is None:
                raw = participant_frames.get(top.participant_id)
            try:
                values[top.participant_id] = _frame_values(raw)
            except ValueError:
                if minute in PRIMARY_MINUTES:
                    raise ValueError("primary_target_invalid") from None
                values = {}
                break
        points[minute] = values or None
    rows = tuple(
        _build_perspective(
            source=source,
            focal=focal,
            opponent=opponent,
            frames=frames,
            selected=selected,
            points=points,
        )
        for focal, opponent in (
            (source.top_participants[0], source.top_participants[1]),
            (source.top_participants[1], source.top_participants[0]),
        )
    )
    validate_perspective_symmetry(rows)
    return rows  # type: ignore[return-value]


def validate_perspective_symmetry(
    rows: tuple[FocalPerspectiveRow, FocalPerspectiveRow],
) -> None:
    first, second = rows
    if (
        first.match_group_id != second.match_group_id
        or first.scientific_weight + second.scientific_weight != 1.0
        or first.game_win == second.game_win
        or first.focal_team_id == second.focal_team_id
    ):
        raise ValueError("perspective_group_symmetry")
    reverse_labels = {
        "enemy_top_outer_first": "allied_top_outer_first",
        "allied_top_outer_first": "enemy_top_outer_first",
        "neither_by_15": "neither_by_15",
    }
    if reverse_labels[first.tower.primary_label_at_15] != (
        second.tower.primary_label_at_15
    ):
        raise ValueError("tower_label_symmetry")
    for minute in ALL_MINUTES:
        left = first.trajectory[minute]
        right = second.trajectory[minute]
        if (left is None) != (right is None):
            raise ValueError("trajectory_availability_symmetry")
        if left is None or right is None:
            continue
        for field in (
            "gold_difference",
            "xp_difference",
            "lane_minion_cs_difference",
            "total_farm_difference",
            "level_difference",
        ):
            if getattr(left, field) != -getattr(right, field):
                raise ValueError("trajectory_value_symmetry")


def add_strict_history_features(
    rows: tuple[FocalPerspectiveRow, ...],
    policy: HistoryPolicyConfig,
) -> tuple[FocalPerspectiveRow, ...]:
    """Construct familiarity using only strictly earlier timestamp cohorts."""

    states: dict[str, dict[str, Any]] = {}
    enriched: list[FocalPerspectiveRow] = []
    by_time: dict[datetime, list[FocalPerspectiveRow]] = defaultdict(list)
    for row in rows:
        by_time[row.game_creation].append(row)
    for timestamp in sorted(by_time):
        cohort = sorted(
            by_time[timestamp], key=lambda row: (row.platform, row.match_group_id)
        )
        pending_updates: list[FocalPerspectiveRow] = []
        for row in cohort:
            state = states.get(row.focal_player_key, _new_history_state())
            feature = _history_feature(row, timestamp, state, policy)
            updated = row.model_copy(update={"familiarity": feature})
            enriched.append(updated)
            pending_updates.append(updated)
        for row in pending_updates:
            state = states.setdefault(row.focal_player_key, _new_history_state())
            _update_history_state(state, row, policy)
    return tuple(enriched)


def build_stage35a_dataset(config: Stage35Config) -> Stage35Dataset:
    """Load checksummed timelines and build deterministic Stage 3.5A outputs."""

    sources = load_source_top_matches(config)
    timelines = {
        platform.platform: TimelinePayloadReader(platform)
        for platform in config.platforms
    }
    eligible: list[tuple[FocalPerspectiveRow, FocalPerspectiveRow]] = []
    exclusions: Counter[str] = Counter()
    downloaded = 0
    for source in sources:
        platform = source.match.platform.lower()
        reader = timelines.get(platform)
        timeline = reader.load(source.match.match_id) if reader is not None else None
        if timeline is None:
            exclusions["timeline_not_downloaded"] += 1
            continue
        downloaded += 1
        try:
            eligible.append(transform_top_match(source, timeline, config))
        except ValueError as error:
            exclusions[str(error)] += 1
    selected_groups = eligible[: config.target_eligible_matches]
    flat = tuple(row for group in selected_groups for row in group)
    rows = add_strict_history_features(flat, config.history_policy)
    dataset_hash = _dataset_sha256(rows)
    quality = _build_quality(
        rows=rows,
        eligible_count=len(eligible),
        downloaded=downloaded,
        exclusions=exclusions,
        target=config.target_eligible_matches,
    )
    audit = _build_audit(
        rows=rows,
        eligible_count=len(eligible),
        downloaded=downloaded,
        source_count=len(sources),
        exclusions=exclusions,
        config=config,
    )
    manifest = _build_manifest(
        config=config,
        rows=rows,
        source_count=len(sources),
        eligible_count=len(eligible),
        dataset_hash=dataset_hash,
        quality=quality,
    )
    validate_public_payload(audit)
    validate_public_payload(manifest)
    validate_public_payload(quality)
    return Stage35Dataset(
        rows=rows,
        all_eligible_match_count=len(eligible),
        selected_match_count=len(selected_groups),
        downloaded_match_count=downloaded,
        exclusions=dict(sorted(exclusions.items())),
        audit=audit,
        manifest=manifest,
        quality_report=quality,
        dataset_sha256=dataset_hash,
    )


def write_stage35a_dataset(dataset: Stage35Dataset, config: Stage35Config) -> None:
    """Atomically publish private rows and aggregate-only repository artifacts."""

    private_payloads = {
        "focal_perspectives.jsonl": b"".join(
            _json_bytes(row.model_dump(mode="json")) for row in dataset.rows
        ),
        "private_manifest.json": _json_bytes(
            {
                "schema_version": STAGE35A_MANIFEST_SCHEMA_VERSION,
                "dataset_sha256": dataset.dataset_sha256,
                "match_count": dataset.selected_match_count,
                "focal_row_count": len(dataset.rows),
            }
        ),
    }
    report = _render_report(dataset.audit, dataset.quality_report)
    public_payloads = {
        "audit.json": _json_bytes(dataset.audit),
        "manifest.json": _json_bytes(dataset.manifest),
        "quality_report.json": _json_bytes(dataset.quality_report),
        "feasibility_report.md": report.encode("utf-8"),
    }
    if sum(map(len, public_payloads.values())) > (
        config.maximum_aggregate_publication_bytes
    ):
        raise ValueError("aggregate publication size ceiling exceeded")
    _atomic_publish_directory(config.private_output_directory, private_payloads)
    try:
        _atomic_publish_directory(config.aggregate_output_directory, public_payloads)
    except Exception:
        shutil.rmtree(config.private_output_directory, ignore_errors=True)
        raise


def _build_perspective(
    *,
    source: SourceTopMatch,
    focal: CanonicalParticipant,
    opponent: CanonicalParticipant,
    frames: list[dict[str, Any]],
    selected: dict[int, dict[str, Any] | None],
    points: dict[int, dict[int, dict[str, int]] | None],
) -> FocalPerspectiveRow:
    trajectory: dict[int, TrajectoryDifference | None] = {}
    for minute in ALL_MINUTES:
        point = points[minute]
        frame = selected[minute]
        if point is None or frame is None:
            trajectory[minute] = None
            continue
        mine = point[focal.participant_id]
        theirs = point[opponent.participant_id]
        trajectory[minute] = TrajectoryDifference(
            requested_minute=minute,
            observed_timestamp_ms=int(frame["timestamp"]),
            gold_difference=mine["gold"] - theirs["gold"],
            xp_difference=mine["xp"] - theirs["xp"],
            lane_minion_cs_difference=mine["lane_cs"] - theirs["lane_cs"],
            total_farm_difference=mine["farm"] - theirs["farm"],
            level_difference=mine["level"] - theirs["level"],
        )
    changes = {
        f"{start}_to_{end}": _trajectory_change(
            trajectory[start], trajectory[end], start, end
        )
        for start, end in zip(ALL_MINUTES, ALL_MINUTES[1:], strict=False)
    }
    tower = _tower_outcome(frames, focal.team_id)
    interventions = {
        minute: _intervention_indicators(
            frames=frames,
            cutoff_ms=minute * 60 * 1000,
            top_ids={focal.participant_id, opponent.participant_id},
            positions=source.participant_positions,
        )
        for minute in PRIMARY_MINUTES
    }
    return FocalPerspectiveRow(
        match_group_id=_match_group_id(
            source.match.platform.lower(), source.match.match_id
        ),
        platform=source.match.platform.lower(),
        public_patch=source.match.public_patch,
        queue_id=source.match.queue_id,
        game_creation=source.match.game_creation.astimezone(UTC),
        focal_player_key=focal.player_key,
        focal_team_id=focal.team_id,
        focal_side="BLUE" if focal.team_id == 100 else "RED",
        focal_champion_id=_required_int(focal.champion_id),
        focal_champion_name=_required_text(focal.champion_name),
        enemy_top_champion_id=_required_int(opponent.champion_id),
        enemy_top_champion_name=_required_text(opponent.champion_name),
        trajectory=trajectory,
        trajectory_changes=changes,
        tower=tower,
        interventions=interventions,
        game_win=focal.win,
    )


def _frame_values(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise ValueError("participant frame is missing")
    values = {
        "gold": _nonnegative_int(raw.get("totalGold")),
        "xp": _nonnegative_int(raw.get("xp")),
        "lane_cs": _nonnegative_int(raw.get("minionsKilled")),
        "jungle_cs": _nonnegative_int(raw.get("jungleMinionsKilled")),
        "level": _nonnegative_int(raw.get("level")),
    }
    if not 1 <= values["level"] <= 18:
        raise ValueError("participant level is impossible")
    values["farm"] = values["lane_cs"] + values["jungle_cs"]
    return values


def _trajectory_change(
    start: TrajectoryDifference | None,
    end: TrajectoryDifference | None,
    start_minute: int,
    end_minute: int,
) -> TrajectoryChange | None:
    if start is None or end is None:
        return None
    return TrajectoryChange(
        start_minute=start_minute,
        end_minute=end_minute,
        gold_change=end.gold_difference - start.gold_difference,
        xp_change=end.xp_difference - start.xp_difference,
        lane_minion_cs_change=(
            end.lane_minion_cs_difference - start.lane_minion_cs_difference
        ),
        total_farm_change=end.total_farm_difference - start.total_farm_difference,
        level_change=end.level_difference - start.level_difference,
    )


def _tower_outcome(frames: list[dict[str, Any]], focal_team_id: int) -> TowerOutcome:
    tower_events = []
    plate_events = []
    plate_supported = True
    for event in _events(frames):
        if (
            event.get("type") == "BUILDING_KILL"
            and event.get("buildingType") == "TOWER_BUILDING"
            and event.get("towerType") == "OUTER_TURRET"
            and event.get("laneType") == "TOP_LANE"
        ):
            team_id = event.get("teamId")
            timestamp = event.get("timestamp")
            if team_id in (100, 200) and _valid_timestamp(timestamp):
                tower_events.append((int(timestamp), int(team_id)))
        if event.get("type") == "TURRET_PLATE_DESTROYED":
            if event.get("laneType") != "TOP_LANE":
                continue
            team_id = event.get("teamId")
            timestamp = event.get("timestamp")
            if team_id not in (100, 200) or not _valid_timestamp(timestamp):
                plate_supported = False
            elif int(timestamp) <= PLATE_CUTOFF_MS:
                plate_events.append(int(team_id))
    tower_events.sort()
    first = tower_events[0] if tower_events else None
    first_timestamp = first[0] if first else None
    destroyed_team = first[1] if first else None
    if first is None or first[0] > 15 * 60 * 1000:
        label = "neither_by_15"
    elif destroyed_team == focal_team_id:
        label = "allied_top_outer_first"
    else:
        label = "enemy_top_outer_first"
    relation = None
    if destroyed_team is not None:
        relation = "allied" if destroyed_team == focal_team_id else "enemy"
    plate_difference = None
    if plate_supported:
        enemy_team = 200 if focal_team_id == 100 else 100
        plate_difference = plate_events.count(enemy_team) - plate_events.count(
            focal_team_id
        )
    return TowerOutcome(
        primary_label_at_15=label,
        first_destroyed_team_relation=relation,
        first_top_outer_timestamp_ms=first_timestamp,
        fell_before_10=first_timestamp is not None and first_timestamp <= 600_000,
        fell_before_15=first_timestamp is not None and first_timestamp <= 900_000,
        fell_before_20=first_timestamp is not None and first_timestamp <= 1_200_000,
        fell_before_25=first_timestamp is not None and first_timestamp <= 1_500_000,
        top_plate_difference_before_cutoff=plate_difference,
        plate_attribution_supported=plate_supported,
    )


def _intervention_indicators(
    *,
    frames: list[dict[str, Any]],
    cutoff_ms: int,
    top_ids: set[int],
    positions: dict[int, str | None],
) -> InterventionIndicators:
    jungle = False
    other = False
    near_top = False
    herald_kills: list[tuple[int, int | None]] = []
    top_towers: list[tuple[int, int | None]] = []
    for event in _events(frames):
        timestamp = event.get("timestamp")
        if not _valid_timestamp(timestamp) or int(timestamp) > cutoff_ms:
            continue
        timestamp = int(timestamp)
        if event.get("type") == "CHAMPION_KILL":
            involved = {
                value
                for value in (
                    event.get("killerId"),
                    event.get("victimId"),
                    *(event.get("assistingParticipantIds") or []),
                )
                if isinstance(value, int) and not isinstance(value, bool)
            }
            if involved & top_ids:
                third_party = involved - top_ids
                jungle = jungle or any(
                    positions.get(identifier) == "JUNGLE" for identifier in third_party
                )
                other = other or any(
                    positions.get(identifier) not in (None, "TOP", "JUNGLE")
                    for identifier in third_party
                )
                near_top = near_top or _near_top(event.get("position"))
        elif (
            event.get("type") == "ELITE_MONSTER_KILL"
            and event.get("monsterType") == "RIFTHERALD"
        ):
            herald_kills.append((timestamp, event.get("killerTeamId")))
        elif (
            event.get("type") == "BUILDING_KILL"
            and event.get("buildingType") == "TOWER_BUILDING"
            and event.get("laneType") == "TOP_LANE"
        ):
            top_towers.append((timestamp, event.get("teamId")))
    herald_proxy = any(
        0 <= tower_time - herald_time <= HERALD_TOWER_WINDOW_MS
        and herald_team in (100, 200)
        and destroyed_team in (100, 200)
        and herald_team != destroyed_team
        for herald_time, herald_team in herald_kills
        for tower_time, destroyed_team in top_towers
    )
    return InterventionIndicators(
        timestamp_minute=cutoff_ms // 60_000,
        jungler_kill_participation_involving_top=jungle,
        other_role_kill_participation_involving_top=other,
        kill_involving_top_near_top_lane=near_top,
        herald_related_top_tower_proxy=herald_proxy,
        any_recorded_intervention_proxy=jungle or other or near_top or herald_proxy,
    )


def _history_feature(
    row: FocalPerspectiveRow,
    timestamp: datetime,
    state: dict[str, Any],
    policy: HistoryPolicyConfig,
) -> FamiliarityFeatures:
    champion = state["champions"].get(row.focal_champion_id, _new_champion_state())
    prior_games = state["games"]
    champion_games = champion["games"]
    recent = sum(
        1
        for prior_timestamp in champion["timestamps"]
        if 0
        < (timestamp - prior_timestamp).total_seconds()
        <= policy.recent_days * 86_400
    )
    last = champion["timestamps"][-1] if champion["timestamps"] else None
    days_since = (
        (timestamp - last).total_seconds() / 86_400 if last is not None else None
    )
    wins = state["wins"]
    posterior_n = prior_games + policy.shrinkage_prior_strength
    rate = (
        wins + policy.shrinkage_prior_mean * policy.shrinkage_prior_strength
    ) / posterior_n
    variance = rate * (1.0 - rate) / (posterior_n + 1.0)
    general_gold = _mean(state["gold10"])
    champion_gold = _mean(champion["gold10"])
    return FamiliarityFeatures(
        observed_prior_ranked_games=prior_games,
        observed_prior_top_games=prior_games,
        observed_prior_champion_games=champion_games,
        recent_prior_champion_games=recent,
        days_since_champion_last_played=days_since,
        champion_ewm_gold_difference_5=champion["ewm"].get("gold5"),
        champion_ewm_gold_difference_10=champion["ewm"].get("gold10"),
        champion_ewm_gold_difference_15=champion["ewm"].get("gold15"),
        champion_ewm_xp_difference_5=champion["ewm"].get("xp5"),
        champion_ewm_xp_difference_10=champion["ewm"].get("xp10"),
        champion_ewm_xp_difference_15=champion["ewm"].get("xp15"),
        historical_top_gold_difference_10_mean=general_gold,
        champion_gold_difference_10_mean=champion_gold,
        champion_relative_to_general_gold_10=(
            champion_gold - general_gold
            if champion_gold is not None and general_gold is not None
            else None
        ),
        shrunk_historical_win_rate=rate,
        shrunk_win_rate_standard_error=math.sqrt(max(0.0, variance)),
        missing_history=prior_games == 0,
        low_support=champion_games < policy.limited_history_threshold,
        limited_history_or_smurf_like=(prior_games < policy.limited_history_threshold),
    )


def _update_history_state(
    state: dict[str, Any],
    row: FocalPerspectiveRow,
    policy: HistoryPolicyConfig,
) -> None:
    state["games"] += 1
    state["wins"] += int(row.game_win)
    point10 = row.trajectory[10]
    if point10 is not None:
        state["gold10"].append(point10.gold_difference)
    champion = state["champions"].setdefault(
        row.focal_champion_id, _new_champion_state()
    )
    champion["games"] += 1
    champion["timestamps"].append(row.game_creation)
    if point10 is not None:
        champion["gold10"].append(point10.gold_difference)
    for minute in PRIMARY_MINUTES:
        point = row.trajectory[minute]
        if point is None:
            continue
        for prefix, value in (
            ("gold", point.gold_difference),
            ("xp", point.xp_difference),
        ):
            key = f"{prefix}{minute}"
            previous = champion["ewm"].get(key)
            champion["ewm"][key] = (
                float(value)
                if previous is None
                else policy.exponential_decay * previous
                + (1.0 - policy.exponential_decay) * value
            )


def _new_history_state() -> dict[str, Any]:
    return {
        "games": 0,
        "wins": 0,
        "gold10": [],
        "champions": {},
    }


def _new_champion_state() -> dict[str, Any]:
    return {"games": 0, "timestamps": [], "gold10": [], "ewm": {}}


def _build_quality(
    *,
    rows: tuple[FocalPerspectiveRow, ...],
    eligible_count: int,
    downloaded: int,
    exclusions: Counter[str],
    target: int,
) -> dict[str, Any]:
    groups = Counter(row.match_group_id for row in rows)
    group_weights: dict[str, float] = defaultdict(float)
    for row in rows:
        group_weights[row.match_group_id] += row.scientific_weight
    symmetric = all(count == 2 for count in groups.values())
    finite = _is_finite(rows)
    forbidden_feature_names = {
        "opponent_puuid",
        "opponent_player_key",
        "opponent_rank",
        "opponent_account_level",
        "opponent_mastery",
        "opponent_win_rate",
        "opponent_history",
        "game_win_feature",
    }
    observed_fields = set(FocalPerspectiveRow.model_fields)
    return {
        "schema_version": "stage3.5a-quality-v1",
        "downloaded_matches": downloaded,
        "eligible_matches_before_exact_target": eligible_count,
        "selected_matches": len(groups),
        "focal_perspective_rows": len(rows),
        "exact_target_reached": len(groups) == target,
        "perspective_rows_exactly_two_per_match": symmetric,
        "scientific_weight_per_match_equals_one": all(
            math.isclose(weight, 1.0) for weight in group_weights.values()
        ),
        "all_values_finite_or_explicitly_null": finite,
        "opponent_account_features_absent": not bool(
            observed_fields & forbidden_feature_names
        ),
        "game_win_not_an_input_feature": "game_win_feature" not in observed_fields,
        "exclusion_total": sum(exclusions.values()),
        "ready_for_stage3_5b_protocol_work": (
            len(groups) == target and symmetric and finite
        ),
        "predictive_models_fitted": 0,
    }


def _build_audit(
    *,
    rows: tuple[FocalPerspectiveRow, ...],
    eligible_count: int,
    downloaded: int,
    source_count: int,
    exclusions: Counter[str],
    config: Stage35Config,
) -> dict[str, Any]:
    groups = {row.match_group_id for row in rows}
    platform_matches = Counter()
    seen_platform_group: set[tuple[str, str]] = set()
    for row in rows:
        key = (row.platform, row.match_group_id)
        if key not in seen_platform_group:
            seen_platform_group.add(key)
            platform_matches[row.platform] += 1
    targets: dict[str, Any] = {}
    for minute in ALL_MINUTES:
        for name in (
            "gold_difference",
            "xp_difference",
            "lane_minion_cs_difference",
            "total_farm_difference",
            "level_difference",
        ):
            values = [
                getattr(point, name)
                for row in rows
                if (point := row.trajectory[minute]) is not None
            ]
            targets[f"{minute}m_{name}"] = _distribution(values, len(rows))
    champions = Counter(row.focal_champion_name for row in rows)
    matchups = Counter(
        (row.focal_champion_name, row.enemy_top_champion_name) for row in rows
    )
    history = [row.familiarity for row in rows if row.familiarity is not None]
    unique_players = {row.focal_player_key for row in rows}
    tower_labels = Counter(row.tower.primary_label_at_15 for row in rows)
    wins = Counter("win" if row.game_win else "loss" for row in rows)
    target_correlations = _target_correlations(rows)
    survivor = _survivor_bias(rows)
    intervention = _intervention_audit(rows)
    history_budget = _history_budget(
        unique_player_count=len(unique_players),
        config=config,
        downloaded_timeline_count=downloaded,
    )
    return {
        "schema_version": STAGE35A_AUDIT_SCHEMA_VERSION,
        "scope": {
            "source_matches": source_count,
            "downloaded_timelines": downloaded,
            "eligible_matches_before_exact_target": eligible_count,
            "selected_core_matches": len(groups),
            "focal_perspective_rows": len(rows),
            "scientific_weight_per_row": 0.5,
            "platform_match_counts": dict(sorted(platform_matches.items())),
            "queue_id": config.queue_id,
            "public_patch": config.public_patch,
        },
        "exclusions": dict(sorted(exclusions.items())),
        "timestamp_target_distributions": targets,
        "target_correlations": target_correlations,
        "champion_support": {
            "unique_champions": len(champions),
            "rows_by_champion": dict(sorted(champions.items())),
            "support_distribution": _distribution(list(champions.values())),
        },
        "matchup_support": {
            "unique_directional_pairs": len(matchups),
            "pairs_with_at_least_2_rows": sum(
                value >= 2 for value in matchups.values()
            ),
            "pairs_with_at_least_5_rows": sum(
                value >= 5 for value in matchups.values()
            ),
            "pairs_with_at_least_10_rows": sum(
                value >= 10 for value in matchups.values()
            ),
            "support_distribution": _distribution(list(matchups.values())),
        },
        "tower": {
            "primary_label_rows": dict(sorted(tower_labels.items())),
            "plate_attribution_supported_rows": sum(
                row.tower.plate_attribution_supported for row in rows
            ),
            "first_top_outer_time_seconds": _distribution(
                [
                    row.tower.first_top_outer_timestamp_ms / 1000
                    for row in rows
                    if row.tower.first_top_outer_timestamp_ms is not None
                ],
                len(rows),
            ),
        },
        "game_outcome": {
            "focal_rows": dict(sorted(wins.items())),
            "trajectory_relationship": _outcome_relationship(rows),
            "secondary_target_only": True,
        },
        "intervention": intervention,
        "exploratory_survivor_bias": survivor,
        "focal_history": {
            "unique_focal_accounts": len(unique_players),
            "rows_with_at_least_1_prior_champion_game": sum(
                feature.observed_prior_champion_games >= 1 for feature in history
            ),
            "rows_with_at_least_5_prior_champion_games": sum(
                feature.observed_prior_champion_games >= 5 for feature in history
            ),
            "rows_with_at_least_10_prior_champion_games": sum(
                feature.observed_prior_champion_games >= 10 for feature in history
            ),
            "rows_with_at_least_20_prior_champion_games": sum(
                feature.observed_prior_champion_games >= 20 for feature in history
            ),
            "missing_history_rows": sum(feature.missing_history for feature in history),
            "limited_history_or_smurf_like_rows": sum(
                feature.limited_history_or_smurf_like for feature in history
            ),
            "left_truncated": True,
            "limitation": (
                "Only chronologically earlier matches inside the observed corpus are "
                "available; absence is not evidence of no prior player experience."
            ),
            "future_bounded_expansion_budget": history_budget,
        },
        "perspective_integrity": {
            "match_groups": len(groups),
            "rows": len(rows),
            "rows_per_match": 2 if rows else None,
            "total_weight": sum(row.scientific_weight for row in rows),
            "symmetric_quantities_negate": True,
            "win_and_tower_labels_reverse": True,
            "future_split_unit": "match_group_id",
            "future_bootstrap_unit": "match_group_id",
        },
        "privacy": {
            "aggregate_only": True,
            "opponent_account_features": False,
            "player_keys": False,
            "match_ids": False,
            "raw_external_paths": False,
            "credentials": False,
        },
        "model_fits": 0,
    }


def _build_manifest(
    *,
    config: Stage35Config,
    rows: tuple[FocalPerspectiveRow, ...],
    source_count: int,
    eligible_count: int,
    dataset_hash: str,
    quality: dict[str, Any],
) -> dict[str, Any]:
    source_files = {
        f"{source.label}:{name}": source.file_sha256[name]
        for source in config.sources
        for name in REQUIRED_STAGE31_FILES
    }
    source_bundle = _sha256_json(source_files)
    timeline_fingerprints = {}
    for platform in config.platforms:
        timeline_fingerprints[platform.platform] = _timeline_catalog_fingerprint(
            platform.checkpoint_database
        )
    executable_files = (
        Path(__file__),
        Path(__file__).with_name("timeline_collection.py"),
        Path(__file__).resolve().parents[2] / "scripts/collect_timelines.py",
        Path(__file__).resolve().parents[2] / "scripts/build_stage35a.py",
    )
    executable_hash = _sha256_json(
        {path.name: sha256_file(path) for path in executable_files}
    )
    return {
        "schema_version": STAGE35A_MANIFEST_SCHEMA_VERSION,
        "configuration": config.scientific_payload(),
        "source_match_count": source_count,
        "eligible_match_count_before_exact_target": eligible_count,
        "selected_match_count": len({row.match_group_id for row in rows}),
        "focal_perspective_row_count": len(rows),
        "source_bundle_sha256": source_bundle,
        "timeline_catalog_sha256": dict(sorted(timeline_fingerprints.items())),
        "private_dataset_sha256": dataset_hash,
        "executable_bundle_sha256": executable_hash,
        "quality_gates": quality,
        "publication": {
            "aggregate_only": True,
            "private_rows_committed": False,
            "model_fits": 0,
        },
    }


def _target_correlations(rows: tuple[FocalPerspectiveRow, ...]) -> dict[str, Any]:
    names = ("gold_difference", "xp_difference", "lane_minion_cs_difference")
    output = {}
    for minute in PRIMARY_MINUTES:
        for index, left in enumerate(names):
            for right in names[index + 1 :]:
                pairs = [
                    (getattr(point, left), getattr(point, right))
                    for row in rows
                    if (point := row.trajectory[minute]) is not None
                ]
                output[f"{minute}m_{left}__{right}"] = _correlation(pairs)
    return output


def _survivor_bias(rows: tuple[FocalPerspectiveRow, ...]) -> dict[str, Any]:
    output = {}
    for minute in EXPLORATORY_MINUTES:
        available = [row for row in rows if row.trajectory[minute] is not None]
        unavailable = [row for row in rows if row.trajectory[minute] is None]
        output[f"{minute}m"] = {
            "available_rows": len(available),
            "missing_rows": len(unavailable),
            "availability_rate": _ratio(len(available), len(rows)),
            "15m_gold_for_available": _distribution(
                [row.trajectory[15].gold_difference for row in available]  # type: ignore[union-attr]
            ),
            "15m_gold_for_unavailable": _distribution(
                [row.trajectory[15].gold_difference for row in unavailable]  # type: ignore[union-attr]
            ),
            "selection_warning": (
                "Availability conditions on surviving to the endpoint and is not "
                "representative of all eligible matches."
            ),
        }
    return output


def _intervention_audit(rows: tuple[FocalPerspectiveRow, ...]) -> dict[str, Any]:
    output = {}
    for minute in PRIMARY_MINUTES:
        flags = [row.interventions[minute] for row in rows]
        low = [
            row
            for row in rows
            if not row.interventions[minute].any_recorded_intervention_proxy
        ]
        output[f"{minute}m"] = {
            "any_proxy_rows": sum(
                flag.any_recorded_intervention_proxy for flag in flags
            ),
            "jungler_proxy_rows": sum(
                flag.jungler_kill_participation_involving_top for flag in flags
            ),
            "other_role_proxy_rows": sum(
                flag.other_role_kill_participation_involving_top for flag in flags
            ),
            "near_top_kill_proxy_rows": sum(
                flag.kill_involving_top_near_top_lane for flag in flags
            ),
            "herald_tower_proxy_rows": sum(
                flag.herald_related_top_tower_proxy for flag in flags
            ),
            "all_rows_gold": _distribution(
                [row.trajectory[minute].gold_difference for row in rows]  # type: ignore[union-attr]
            ),
            "low_recorded_intervention_gold": _distribution(
                [row.trajectory[minute].gold_difference for row in low]  # type: ignore[union-attr]
            ),
        }
    output["limitation"] = (
        "These event-derived proxies do not capture every gank; interventions without "
        "kills or supported frame events may be unobserved."
    )
    return output


def _outcome_relationship(rows: tuple[FocalPerspectiveRow, ...]) -> dict[str, Any]:
    output = {}
    for minute in PRIMARY_MINUTES:
        values = [
            (row.trajectory[minute].gold_difference, int(row.game_win))  # type: ignore[union-attr]
            for row in rows
        ]
        output[f"{minute}m_gold"] = {
            "correlation_with_game_win": _correlation(values),
            "mean_for_wins": _mean([value for value, win in values if win]),
            "mean_for_losses": _mean([value for value, win in values if not win]),
        }
    return output


def _history_budget(
    *,
    unique_player_count: int,
    config: Stage35Config,
    downloaded_timeline_count: int,
) -> dict[str, Any]:
    window = config.history_policy.proposed_maximum_prior_matches
    id_requests = unique_player_count * math.ceil(window / 100)
    upper_payload_requests = unique_player_count * window
    average_timeline_bytes = _average_timeline_bytes(config)
    return {
        "proposed_maximum_prior_matches": window,
        "proposed_maximum_prior_days": (
            config.history_policy.proposed_maximum_prior_days
        ),
        "match_id_request_estimate": id_requests,
        "match_payload_request_upper_bound_before_deduplication": (
            upper_payload_requests
        ),
        "timeline_request_upper_bound_before_deduplication": upper_payload_requests,
        "timeline_storage_upper_bound_bytes": round(
            average_timeline_bytes * upper_payload_requests
        ),
        "observed_timeline_files_for_size_estimate": downloaded_timeline_count,
        "requires_separate_authorization": True,
    }


def _average_timeline_bytes(config: Stage35Config) -> float:
    total = 0
    count = 0
    for platform in config.platforms:
        if not platform.raw_timeline_directory.exists():
            continue
        for path in platform.raw_timeline_directory.rglob("*.json"):
            if path.is_file():
                total += path.stat().st_size
                count += 1
    return total / count if count else 0.0


def _distribution(
    values: list[float | int], total: int | None = None
) -> dict[str, Any]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    denominator = len(clean) if total is None else total
    if not clean:
        return {
            "count": 0,
            "missing": denominator,
            "mean": None,
            "standard_deviation": None,
            "minimum": None,
            "p25": None,
            "median": None,
            "p75": None,
            "maximum": None,
        }
    ordered = sorted(clean)
    return {
        "count": len(clean),
        "missing": max(0, denominator - len(clean)),
        "mean": statistics.fmean(clean),
        "standard_deviation": statistics.pstdev(clean),
        "minimum": ordered[0],
        "p25": _quantile(ordered, 0.25),
        "median": _quantile(ordered, 0.5),
        "p75": _quantile(ordered, 0.75),
        "maximum": ordered[-1],
    }


def _correlation(pairs: list[tuple[float | int, float | int]]) -> float | None:
    if len(pairs) < 2:
        return None
    left = [float(pair[0]) for pair in pairs]
    right = [float(pair[1]) for pair in pairs]
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True)
    )
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return numerator / denominator if denominator else None


def _quantile(values: list[float], probability: float) -> float:
    if len(values) == 1:
        return values[0]
    position = probability * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def _events(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for frame in frames:
        raw_events = frame.get("events")
        if isinstance(raw_events, list):
            events.extend(event for event in raw_events if isinstance(event, dict))
    return events


def _near_top(position: Any) -> bool:
    if not isinstance(position, dict):
        return False
    x = position.get("x")
    y = position.get("y")
    if not all(isinstance(value, (int, float)) for value in (x, y)):
        return False
    return (x <= 6_000 and y >= 8_000) or (x <= 8_000 and y >= 10_000)


def _valid_timestamp(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _nonnegative_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("timeline value is invalid")
    return value


def _required_int(value: int | None) -> int:
    if value is None:
        raise ValueError("required champion identity is missing")
    return value


def _required_text(value: str | None) -> str:
    if value is None or not value.strip():
        raise ValueError("required champion identity is missing")
    return value


def _mean(values: list[float | int]) -> float | None:
    return statistics.fmean(values) if values else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _match_group_id(platform: str, match_id: str) -> str:
    digest = hashlib.sha256(
        f"nexus-lens:stage3.5a-v1:match\0{platform}\0{match_id}".encode()
    ).hexdigest()
    return f"match_group_{digest[:32]}"


def _dataset_sha256(rows: tuple[FocalPerspectiveRow, ...]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_json_bytes(row.model_dump(mode="json")))
    return digest.hexdigest()


def _timeline_catalog_fingerprint(path: Path) -> str:
    import sqlite3

    digest = hashlib.sha256()
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            """
            SELECT match_id,content_sha256,status FROM timelines
            ORDER BY match_id
            """
        )
        for match_id, content_sha256, status in rows:
            private = hashlib.sha256(match_id.encode()).hexdigest()
            digest.update(
                f"{private}\0{content_sha256 or ''}\0{status}\n".encode("ascii")
            )
    return digest.hexdigest()


def _read_jsonl_models(path: Path, model: type[BaseModel]) -> list[Any]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(model.model_validate_json(line))
    return rows


def _is_finite(value: Any) -> bool:
    if isinstance(value, BaseModel):
        return _is_finite(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return all(_is_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_is_finite(item) for item in value)
    if isinstance(value, float):
        return math.isfinite(value)
    return True


def _sha256_json(payload: Any) -> str:
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_publish_directory(path: Path, payloads: dict[str, bytes]) -> None:
    if path.exists():
        raise ValueError("Stage 3.5A output directory already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
    try:
        for name, payload in payloads.items():
            target = temporary / name
            target.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _render_report(audit: dict[str, Any], quality: dict[str, Any]) -> str:
    scope = audit["scope"]
    history = audit["focal_history"]
    return "\n".join(
        (
            "# Nexus Lens Stage 3.5A feasibility audit",
            "",
            "This is an aggregate-only data feasibility result. No predictive model "
            "was fitted and no recommendation policy is authorized.",
            "",
            "## Core retention",
            "",
            f"- Source matches: {scope['source_matches']}",
            f"- Downloaded timelines: {scope['downloaded_timelines']}",
            "- Eligible before exact stopping: "
            f"{scope['eligible_matches_before_exact_target']}",
            f"- Selected matches: {scope['selected_core_matches']}",
            f"- Focal-perspective rows: {scope['focal_perspective_rows']}",
            f"- Exact 10,000 target reached: {quality['exact_target_reached']}",
            "",
            "## History feasibility",
            "",
            f"- Unique focal accounts: {history['unique_focal_accounts']}",
            f"- Missing-history rows: {history['missing_history_rows']}",
            "- Within-corpus history is left-truncated and must not be interpreted "
            "as complete player history.",
            "",
            "## Interpretation limits",
            "",
            "The 20- and 25-minute endpoints condition on matches surviving to those "
            "times. Intervention flags are incomplete event-based proxies. Final game "
            "outcome is secondary and does not define lane success. Opponent account "
            "data is absent from the feature contract.",
            "",
        )
    )
