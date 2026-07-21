"""Small schemas for the Riot responses used in Stage 0."""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class RiotAccount(BaseModel):
    """An account resolved from a Riot ID."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    puuid: str
    game_name: str = Field(alias="gameName")
    tag_line: str = Field(alias="tagLine")


class MatchMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    matchId: str
    participants: list[str]


class MatchInfo(BaseModel):
    """The stable fields needed to sanity-check a collected match."""

    model_config = ConfigDict(extra="allow")

    gameCreation: int
    gameDuration: int
    queueId: int
    participants: list[dict[str, object]]


class RiotMatch(BaseModel):
    """A Match-V5 payload while retaining fields not yet modeled."""

    model_config = ConfigDict(extra="allow")

    metadata: MatchMetadata
    info: MatchInfo


class CollectionManifest(BaseModel):
    """Provenance for one local feasibility snapshot."""

    collected_at: datetime
    routing_region: str
    account: RiotAccount
    requested_match_count: int
    match_ids: list[str]
    match_files: list[Path]
