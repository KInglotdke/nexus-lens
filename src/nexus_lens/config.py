"""Environment-backed configuration for the feasibility collector."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from ``.env`` and ``NEXUS_LENS_*`` variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="NEXUS_LENS_",
        extra="ignore",
    )

    riot_api_key: str = Field(min_length=1)
    routing_region: str = "europe"
    game_name: str = Field(min_length=1)
    tag_line: str = Field(min_length=1)
    match_count: int = Field(default=5, ge=1, le=100)
    raw_data_dir: Path = Path("data/raw")
