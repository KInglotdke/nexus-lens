"""Safely inspect recent queue-420 metadata for the configured account."""

import argparse
import asyncio

from nexus_lens.config import Settings
from nexus_lens.inspection import inspect_recent_ranked_matches
from nexus_lens.riot_client import RiotClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Display non-sensitive metadata for at most five recent Ranked "
            "Solo/Duo matches."
        )
    )
    parser.add_argument(
        "--count",
        type=int,
        choices=range(1, 6),
        default=5,
        metavar="1-5",
        help="Number of queue-420 matches to inspect (default: 5).",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print the zero-request inspection plan without loading settings.",
    )
    return parser.parse_args()


async def run(count: int) -> None:
    settings = Settings()  # type: ignore[call-arg]
    async with RiotClient(settings) as client:
        rows = await inspect_recent_ranked_matches(
            client,
            game_name=settings.game_name,
            tag_line=settings.tag_line,
            count=count,
        )
    print(
        "seq | UTC date | public patch | API patch | API game version | "
        "queue | champion | 26.13/26.14"
    )
    for row in rows:
        print(
            f"{row['sequence']} | {row['game_date_utc']} | "
            f"{row['public_patch'] or 'unresolved'} | "
            f"{row['api_patch'] or 'unresolved'} | "
            f"{row['api_game_version'] or 'unavailable'} | "
            f"{row['queue_id']} | {row['champion'] or 'unavailable'} | "
            f"{'yes' if row['is_public_26_13_or_26_14'] else 'no'}"
        )


def main() -> int:
    args = parse_args()
    if args.plan:
        print("Inspection plan: queue=420, matches<=5, network requests=0")
        return 0
    try:
        asyncio.run(run(args.count))
    except KeyboardInterrupt:
        print("Inspection interrupted; no participant identifiers were displayed.")
        return 130
    except Exception:
        print("Inspection failed safely; no participant identifiers were displayed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
