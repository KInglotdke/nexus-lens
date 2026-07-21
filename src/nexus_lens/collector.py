"""Orchestration and raw snapshot persistence for Stage 0."""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from nexus_lens.config import Settings
from nexus_lens.riot_client import RiotClient
from nexus_lens.schemas import (
    RANKED_SOLO_QUEUE_ID,
    CollectionManifest,
    SkippedMatch,
)

logger = logging.getLogger(__name__)


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

        accepted_match_ids: list[str] = []
        match_files: list[Path] = []
        skipped_matches: list[SkippedMatch] = []
        for match_id in match_ids:
            match = await self._client.get_match(match_id)
            if match.info.queueId != RANKED_SOLO_QUEUE_ID:
                reason = (
                    f"expected queueId {RANKED_SOLO_QUEUE_ID}, "
                    f"got {match.info.queueId}"
                )
                logger.warning("Skipping match %s: %s", match_id, reason)
                skipped_matches.append(
                    SkippedMatch(
                        match_id=match_id,
                        queue_id=match.info.queueId,
                        reason=reason,
                    )
                )
                continue

            filename = f"{_safe_filename(match_id)}.json"
            relative_path = Path("matches") / filename
            _write_json(
                snapshot_dir / relative_path,
                match.model_dump(mode="json", by_alias=True),
            )
            accepted_match_ids.append(match_id)
            match_files.append(relative_path)

        manifest = CollectionManifest(
            collected_at=collected_at,
            routing_region=self._settings.routing_region,
            account=account,
            queue_id=RANKED_SOLO_QUEUE_ID,
            requested_match_count=requested_count,
            match_ids=match_ids,
            accepted_match_ids=accepted_match_ids,
            match_files=match_files,
            skipped_matches=skipped_matches,
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
