"""Orchestration and raw snapshot persistence for Stage 0."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from nexus_lens.config import Settings
from nexus_lens.riot_client import RiotClient
from nexus_lens.schemas import CollectionManifest


class FeasibilityCollector:
    """Collect a small, inspectable set of matches for one Riot ID."""

    def __init__(self, settings: Settings, client: RiotClient) -> None:
        self._settings = settings
        self._client = client

    async def collect(self, *, count: int | None = None) -> Path:
        requested_count = count or self._settings.match_count
        account = await self._client.get_account_by_riot_id(
            self._settings.game_name,
            self._settings.tag_line,
        )
        match_ids = await self._client.get_match_ids_by_puuid(
            account.puuid,
            count=requested_count,
        )

        collected_at = datetime.now(UTC)
        snapshot_dir = self._settings.raw_data_dir / collected_at.strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        matches_dir = snapshot_dir / "matches"
        matches_dir.mkdir(parents=True, exist_ok=False)

        match_files: list[Path] = []
        for match_id in match_ids:
            match = await self._client.get_match(match_id)
            filename = f"{_safe_filename(match_id)}.json"
            relative_path = Path("matches") / filename
            _write_json(
                snapshot_dir / relative_path,
                match.model_dump(mode="json", by_alias=True),
            )
            match_files.append(relative_path)

        manifest = CollectionManifest(
            collected_at=collected_at,
            routing_region=self._settings.routing_region,
            account=account,
            requested_match_count=requested_count,
            match_ids=match_ids,
            match_files=match_files,
        )
        _write_json(
            snapshot_dir / "manifest.json",
            manifest.model_dump(mode="json", by_alias=True),
        )
        return snapshot_dir


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
