"""Raw Riot and normalized Stage 1 data contracts."""

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

RANKED_SOLO_QUEUE_ID = 420


class RiotModel(BaseModel):
    """Base for forward-compatible Riot payload fragments."""

    model_config = ConfigDict(extra="allow")


class RiotAccount(RiotModel):
    """An account resolved from a Riot ID."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    puuid: str
    game_name: str = Field(alias="gameName")
    tag_line: str = Field(alias="tagLine")


class MatchMetadata(RiotModel):
    matchId: str
    dataVersion: str | None = None
    participants: list[str] = Field(default_factory=list)


class PerkSelection(RiotModel):
    perk: int | None = None


class PerkStyle(RiotModel):
    description: str | None = None
    style: int | None = None
    selections: list[PerkSelection] = Field(default_factory=list)


class ParticipantPerks(RiotModel):
    styles: list[PerkStyle] = Field(default_factory=list)
    statPerks: dict[str, int] = Field(default_factory=dict)


class MatchParticipant(RiotModel):
    """Participant fields used by the first structural analyses."""

    participantId: int | None = None
    puuid: str | None = None
    teamId: int | None = None
    championId: int | None = None
    championName: str | None = None
    teamPosition: str | None = None
    individualPosition: str | None = None
    role: str | None = None
    lane: str | None = None
    win: bool | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    goldEarned: int | None = None
    totalMinionsKilled: int | None = None
    neutralMinionsKilled: int | None = None
    visionScore: int | None = None
    wardsPlaced: int | None = None
    wardsKilled: int | None = None
    totalDamageDealtToChampions: int | None = None
    totalDamageTaken: int | None = None
    damageSelfMitigated: int | None = None
    totalHeal: int | None = None
    timeCCingOthers: int | None = None
    summoner1Id: int | None = None
    summoner2Id: int | None = None
    perks: ParticipantPerks | None = None
    item0: int | None = None
    item1: int | None = None
    item2: int | None = None
    item3: int | None = None
    item4: int | None = None
    item5: int | None = None
    item6: int | None = None


class TeamObjective(RiotModel):
    first: bool | None = None
    kills: int | None = None


class TeamObjectives(RiotModel):
    baron: TeamObjective | None = None
    champion: TeamObjective | None = None
    dragon: TeamObjective | None = None
    inhibitor: TeamObjective | None = None
    riftHerald: TeamObjective | None = None
    tower: TeamObjective | None = None


class TeamBan(RiotModel):
    championId: int | None = None
    pickTurn: int | None = None


class MatchTeam(RiotModel):
    teamId: int | None = None
    win: bool | None = None
    bans: list[TeamBan] = Field(default_factory=list)
    objectives: TeamObjectives = Field(default_factory=TeamObjectives)


class MatchInfo(RiotModel):
    """Match fields needed for normalization and quality checks."""

    gameCreation: int
    gameDuration: int
    queueId: int
    gameStartTimestamp: int | None = None
    gameEndTimestamp: int | None = None
    gameVersion: str | None = None
    gameMode: str | None = None
    gameType: str | None = None
    mapId: int | None = None
    platformId: str | None = None
    participants: list[MatchParticipant] = Field(default_factory=list)
    teams: list[MatchTeam] = Field(default_factory=list)


class RiotMatch(RiotModel):
    """The Match-V5 payload retained in raw snapshots."""

    metadata: MatchMetadata
    info: MatchInfo


class SkippedMatch(BaseModel):
    """A downloaded match excluded by defensive queue validation."""

    match_id: str
    queue_id: int
    reason: str


class CollectionManifest(BaseModel):
    """Provenance for one local feasibility snapshot."""

    collected_at: datetime
    routing_region: str
    account: RiotAccount
    queue_id: int
    requested_match_count: int
    match_ids: list[str]
    accepted_match_ids: list[str]
    match_files: list[Path]
    skipped_matches: list[SkippedMatch]


class NormalizedMatch(BaseModel):
    match_id: str
    data_version: str | None
    game_creation: datetime
    game_start: datetime | None
    game_end: datetime | None
    game_duration_seconds: int
    api_game_version: str | None
    api_patch: str | None
    public_patch: str | None
    patch_resolution_method: str
    patch_resolution_status: str
    queue_id: int
    game_mode: str | None
    game_type: str | None
    map_id: int | None
    platform_id: str | None
    participant_count: int
    team_count: int


class NormalizedParticipant(BaseModel):
    match_id: str
    participant_id: int | None
    team_id: int | None
    champion_id: int | None
    champion_name: str | None
    team_position: str | None
    individual_position: str | None
    legacy_role: str | None
    legacy_lane: str | None
    win: bool | None
    kills: int
    deaths: int
    assists: int
    kda: float
    kill_participation: float | None
    gold_earned: int | None
    total_minions_killed: int
    neutral_minions_killed: int
    cs: int
    vision_score: int | None
    wards_placed: int | None
    wards_killed: int | None
    damage_to_champions: int | None
    damage_taken: int | None
    damage_mitigated: int | None
    healing_done: int | None
    time_ccing_others: int | None
    summoner_spell_1_id: int | None
    summoner_spell_2_id: int | None
    primary_style_id: int | None
    secondary_style_id: int | None
    primary_perk_ids: list[int]
    secondary_perk_ids: list[int]
    item_0: int | None
    item_1: int | None
    item_2: int | None
    item_3: int | None
    item_4: int | None
    item_5: int | None
    item_6: int | None
    player_key: str | None


class NormalizedTeam(BaseModel):
    match_id: str
    team_id: int | None
    win: bool | None
    champion_kills: int | None
    baron_kills: int | None
    baron_first: bool | None
    dragon_kills: int | None
    dragon_first: bool | None
    herald_kills: int | None
    herald_first: bool | None
    tower_kills: int | None
    tower_first: bool | None
    inhibitor_kills: int | None
    inhibitor_first: bool | None
    bans: list[int]


class NormalizedBatch(BaseModel):
    match: NormalizedMatch
    participants: list[NormalizedParticipant]
    teams: list[NormalizedTeam]


class LeagueEntry(RiotModel):
    """Minimal League-V4 entry used for privacy-conscious discovery."""

    puuid: str | None = None
    summonerId: str | None = None
    tier: str | None = None
    rank: str | None = None
    queueType: str | None = None


class SummonerRecord(RiotModel):
    """Minimal Summoner-V4 response used only to resolve PUUID internally."""

    puuid: str


JsonObject = dict[str, Any]
