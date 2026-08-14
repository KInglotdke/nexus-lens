"""Collect checksummed Stage 3.5A Match-V5 timelines."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from nexus_lens.timeline_collection import (
    TimelinePayloadReader,
    collect_platform_timelines,
    load_stage35_config,
    verify_stage31_sources,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect bounded, resumable, checksummed Match-V5 timelines for the "
            "private Stage 3.5A top-lane feasibility dataset."
        )
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--platform", choices=("eun1", "euw1"), action="append")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify sources and print a path-free plan without loading .env.",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify every retained timeline checksum without network access.",
    )
    return parser.parse_args()


async def _collect(args: argparse.Namespace) -> list[dict[str, object]]:
    config = load_stage35_config(args.config)
    matches = verify_stage31_sources(config)
    selected = set(args.platform or [row.platform for row in config.platforms])
    summaries = []
    for platform in sorted(config.platforms, key=lambda row: row.platform):
        if platform.platform not in selected:
            continue
        summary = await collect_platform_timelines(
            config=config,
            platform_config=platform,
            matches=matches,
        )
        summaries.append(summary.as_dict())
    return summaries


def main() -> int:
    args = parse_args()
    try:
        config = load_stage35_config(args.config)
        matches = verify_stage31_sources(config)
        selected = set(args.platform or [row.platform for row in config.platforms])
        if args.dry_run:
            plan = {
                "schema_version": "stage3.5a-timeline-plan-v1",
                "queue_id": config.queue_id,
                "public_patch": config.public_patch,
                "source_matches": len(matches),
                "concurrency": config.concurrency,
                "platforms": [
                    {
                        "platform": row.platform,
                        "routing_region": row.routing_region,
                        "source_matches": sum(
                            match.platform == row.platform for match in matches
                        ),
                        "maximum_requests": row.maximum_requests,
                    }
                    for row in sorted(config.platforms, key=lambda item: item.platform)
                    if row.platform in selected
                ],
                "credentials_loaded": False,
                "network_requests": 0,
            }
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        if args.verify_only:
            result = {
                row.platform: TimelinePayloadReader(row).verify_all()
                for row in config.platforms
                if row.platform in selected
            }
            print(
                json.dumps(
                    {
                        "schema_version": "stage3.5a-timeline-verification-v1",
                        "verified_timeline_counts": dict(sorted(result.items())),
                        "network_requests": 0,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        summaries = asyncio.run(_collect(args))
        print(json.dumps(summaries, indent=2, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print("Stage 3.5A timeline collection interrupted safely; resume is retained.")
        return 130
    except Exception as error:
        category = _safe_category(error)
        print(
            f"Stage 3.5A timeline collection stopped: category={category}; "
            "the private checkpoint remains resumable."
        )
        return 1


def _safe_category(error: Exception) -> str:
    name = type(error).__name__.lower()
    if "budget" in name:
        return "request_budget"
    if "api" in name:
        return "riot_api"
    if "validation" in name or isinstance(error, (TypeError, ValueError)):
        return "validation"
    if isinstance(error, OSError):
        return "local_storage"
    return "unexpected"


if __name__ == "__main__":
    raise SystemExit(main())
