"""Non-sensitive recent-match inspection for a configured consenting account."""

from datetime import UTC, datetime

from nexus_lens.patches import resolve_patch
from nexus_lens.riot_client import RiotClient
from nexus_lens.schemas import RANKED_SOLO_QUEUE_ID


async def inspect_recent_ranked_matches(
    client: RiotClient,
    *,
    game_name: str,
    tag_line: str,
    count: int = 5,
) -> list[dict[str, object]]:
    """Fetch at most five matches and return only approved display fields."""

    bounded_count = min(max(count, 1), 5)
    account = await client.get_account_by_riot_id(game_name, tag_line)
    match_ids = await client.get_match_ids_by_puuid(
        account.puuid,
        count=bounded_count,
    )
    rows: list[dict[str, object]] = []
    for sequence, match_id in enumerate(match_ids, start=1):
        match = await client.get_match(match_id)
        if match.info.queueId != RANKED_SOLO_QUEUE_ID:
            continue
        creation = datetime.fromtimestamp(match.info.gameCreation / 1000, tz=UTC)
        patch = resolve_patch(match.info.gameVersion, creation)
        participant = next(
            (
                item
                for item in match.info.participants
                if item.puuid == account.puuid
            ),
            None,
        )
        rows.append(
            {
                "sequence": sequence,
                "game_date_utc": creation.date().isoformat(),
                "public_patch": patch.public_patch,
                "api_patch": patch.api_patch,
                "api_game_version": patch.api_game_version,
                "patch_resolution_status": patch.status,
                "queue_id": match.info.queueId,
                "champion": participant.championName if participant else None,
                "is_public_26_13_or_26_14": patch.public_patch in ("26.13", "26.14"),
            }
        )
    return rows
