"""Run the Stage 0 collection experiment."""

import argparse
import asyncio

from nexus_lens.collector import FeasibilityCollector
from nexus_lens.config import Settings
from nexus_lens.riot_client import RiotClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect a small raw Riot match snapshot for feasibility review."
    )
    parser.add_argument(
        "--count",
        type=int,
        choices=range(1, 101),
        metavar="1-100",
        help="Override NEXUS_LENS_MATCH_COUNT.",
    )
    return parser.parse_args()


async def run(count: int | None) -> None:
    settings = Settings()  # type: ignore[call-arg]
    async with RiotClient(settings) as client:
        output_dir = await FeasibilityCollector(settings, client).collect(count=count)
    print(f"Snapshot written to {output_dir}")


if __name__ == "__main__":
    arguments = parse_args()
    asyncio.run(run(arguments.count))
