"""Pure transformation of validated Match-V5 payloads into Stage 1 records."""

from datetime import UTC, datetime

from nexus_lens.patches import resolve_patch
from nexus_lens.privacy import pseudonymize_puuid
from nexus_lens.schemas import (
    RANKED_SOLO_QUEUE_ID,
    MatchParticipant,
    MatchTeam,
    NormalizedBatch,
    NormalizedMatch,
    NormalizedParticipant,
    NormalizedTeam,
    RiotMatch,
    TeamObjective,
)

EXPECTED_PARTICIPANTS = 10
EXPECTED_TEAMS = 2
_POSITION_ALIASES = {
    "TOP": "TOP",
    "JUNGLE": "JUNGLE",
    "MIDDLE": "MIDDLE",
    "MID": "MIDDLE",
    "BOTTOM": "BOTTOM",
    "BOT": "BOTTOM",
    "UTILITY": "UTILITY",
    "SUPPORT": "UTILITY",
}


class NormalizationError(ValueError):
    """A non-sensitive, categorizable rejection of one raw match."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def normalize_position(position: str | None) -> str | None:
    """Canonicalize known team positions while retaining unknown values."""

    if position is None or not position.strip():
        return None
    cleaned = position.strip().upper()
    return _POSITION_ALIASES.get(cleaned, cleaned)


def normalize_match(raw_match: RiotMatch) -> NormalizedBatch:
    """Normalize one match or raise a domain-specific rejection."""

    info = raw_match.info
    if info.queueId != RANKED_SOLO_QUEUE_ID:
        raise NormalizationError(
            "wrong_queue",
            f"expected queueId {RANKED_SOLO_QUEUE_ID}, got {info.queueId}",
        )
    if len(info.participants) != EXPECTED_PARTICIPANTS:
        raise NormalizationError(
            "participant_count",
            f"expected {EXPECTED_PARTICIPANTS} participants, "
            f"got {len(info.participants)}",
        )
    if len(info.teams) != EXPECTED_TEAMS:
        raise NormalizationError(
            "team_count",
            f"expected {EXPECTED_TEAMS} teams, got {len(info.teams)}",
        )

    match_id = raw_match.metadata.matchId
    team_kills = _team_kill_totals(info.teams, info.participants)
    game_creation = _from_milliseconds(info.gameCreation)
    patch = resolve_patch(info.gameVersion, game_creation)
    match_record = NormalizedMatch(
        match_id=match_id,
        data_version=raw_match.metadata.dataVersion,
        game_creation=game_creation,
        game_start=_optional_timestamp(info.gameStartTimestamp),
        game_end=_optional_timestamp(info.gameEndTimestamp),
        game_duration_seconds=info.gameDuration,
        api_game_version=patch.api_game_version,
        api_patch=patch.api_patch,
        public_patch=patch.public_patch,
        patch_resolution_method=patch.method,
        patch_resolution_status=patch.status,
        queue_id=info.queueId,
        game_mode=info.gameMode,
        game_type=info.gameType,
        map_id=info.mapId,
        platform_id=info.platformId,
        participant_count=len(info.participants),
        team_count=len(info.teams),
    )
    participants = [
        _normalize_participant(match_id, participant, team_kills)
        for participant in info.participants
    ]
    teams = [_normalize_team(match_id, team) for team in info.teams]
    return NormalizedBatch(
        match=match_record,
        participants=participants,
        teams=teams,
    )


def _normalize_participant(
    match_id: str,
    participant: MatchParticipant,
    team_kills: dict[int, int],
) -> NormalizedParticipant:
    kills = participant.kills or 0
    deaths = participant.deaths or 0
    assists = participant.assists or 0
    lane_cs = participant.totalMinionsKilled or 0
    neutral_cs = participant.neutralMinionsKilled or 0
    total_team_kills = (
        team_kills.get(participant.teamId) if participant.teamId else None
    )
    if total_team_kills is None:
        kill_participation = None
    elif total_team_kills == 0:
        kill_participation = 0.0
    else:
        kill_participation = round((kills + assists) / total_team_kills, 6)

    styles = participant.perks.styles if participant.perks else []
    primary = styles[0] if styles else None
    secondary = styles[1] if len(styles) > 1 else None

    return NormalizedParticipant(
        match_id=match_id,
        participant_id=participant.participantId,
        team_id=participant.teamId,
        champion_id=participant.championId,
        champion_name=participant.championName,
        team_position=normalize_position(participant.teamPosition),
        individual_position=normalize_position(participant.individualPosition),
        legacy_role=participant.role,
        legacy_lane=participant.lane,
        win=participant.win,
        kills=kills,
        deaths=deaths,
        assists=assists,
        kda=round((kills + assists) / deaths, 6)
        if deaths
        else float(kills + assists),
        kill_participation=kill_participation,
        gold_earned=participant.goldEarned,
        total_minions_killed=lane_cs,
        neutral_minions_killed=neutral_cs,
        cs=lane_cs + neutral_cs,
        vision_score=participant.visionScore,
        wards_placed=participant.wardsPlaced,
        wards_killed=participant.wardsKilled,
        damage_to_champions=participant.totalDamageDealtToChampions,
        damage_taken=participant.totalDamageTaken,
        damage_mitigated=participant.damageSelfMitigated,
        healing_done=participant.totalHeal,
        time_ccing_others=participant.timeCCingOthers,
        summoner_spell_1_id=participant.summoner1Id,
        summoner_spell_2_id=participant.summoner2Id,
        primary_style_id=primary.style if primary else None,
        secondary_style_id=secondary.style if secondary else None,
        primary_perk_ids=_perk_ids(primary),
        secondary_perk_ids=_perk_ids(secondary),
        item_0=participant.item0,
        item_1=participant.item1,
        item_2=participant.item2,
        item_3=participant.item3,
        item_4=participant.item4,
        item_5=participant.item5,
        item_6=participant.item6,
        player_key=pseudonymize_puuid(participant.puuid),
    )


def _normalize_team(match_id: str, team: MatchTeam) -> NormalizedTeam:
    objectives = team.objectives
    return NormalizedTeam(
        match_id=match_id,
        team_id=team.teamId,
        win=team.win,
        champion_kills=_objective_kills(objectives.champion),
        baron_kills=_objective_kills(objectives.baron),
        baron_first=_objective_first(objectives.baron),
        dragon_kills=_objective_kills(objectives.dragon),
        dragon_first=_objective_first(objectives.dragon),
        herald_kills=_objective_kills(objectives.riftHerald),
        herald_first=_objective_first(objectives.riftHerald),
        tower_kills=_objective_kills(objectives.tower),
        tower_first=_objective_first(objectives.tower),
        inhibitor_kills=_objective_kills(objectives.inhibitor),
        inhibitor_first=_objective_first(objectives.inhibitor),
        bans=[ban.championId for ban in team.bans if ban.championId is not None],
    )


def _team_kill_totals(
    teams: list[MatchTeam],
    participants: list[MatchParticipant],
) -> dict[int, int]:
    totals: dict[int, int] = {}
    for team in teams:
        if team.teamId is None:
            continue
        champion_kills = _objective_kills(team.objectives.champion)
        if champion_kills is not None:
            totals[team.teamId] = champion_kills

    participant_teams = {participant.teamId for participant in participants}
    for team_id in participant_teams:
        if team_id is None or team_id in totals:
            continue
        members = [item for item in participants if item.teamId == team_id]
        if members and all(item.kills is not None for item in members):
            totals[team_id] = sum(item.kills or 0 for item in members)
    return totals


def _perk_ids(style: object) -> list[int]:
    selections = getattr(style, "selections", []) if style else []
    return [selection.perk for selection in selections if selection.perk is not None]


def _objective_kills(objective: TeamObjective | None) -> int | None:
    return objective.kills if objective else None


def _objective_first(objective: TeamObjective | None) -> bool | None:
    return objective.first if objective else None


def _from_milliseconds(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _optional_timestamp(value: int | None) -> datetime | None:
    return _from_milliseconds(value) if value is not None else None
