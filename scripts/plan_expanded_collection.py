"""Print a deterministic EUNE/EUW population plan without network access."""

import argparse
import json
from pathlib import Path

from nexus_lens.collection_planning import (
    ExpandedCollectionPlanConfig,
    build_expanded_collection_plan,
    load_expanded_collection_config,
)
from nexus_lens.population import DEFAULT_DIVISIONS, DEFAULT_TIERS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan later EUNE/EUW queue-420 collection without loading .env, "
            "constructing a Riot client, or making network requests."
        )
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--platforms",
        nargs="+",
        choices=("eun1", "euw1"),
        default=["eun1", "euw1"],
    )
    parser.add_argument("--target-public-patch")
    parser.add_argument("--patch-window-size", type=int, default=2)
    parser.add_argument("--tiers", nargs="+", default=list(DEFAULT_TIERS))
    parser.add_argument("--divisions", nargs="+", default=list(DEFAULT_DIVISIONS))
    parser.add_argument("--target-matches-per-platform", type=int, default=1_000)
    parser.add_argument("--max-players-per-platform", type=int, default=2_000)
    parser.add_argument("--initial-history-batch-size", type=int, default=5)
    parser.add_argument("--max-history-per-player", type=int, default=20)
    parser.add_argument("--older-patch-stop-threshold", type=int, default=2)
    parser.add_argument("--pages-per-stratum", type=int, default=1)
    parser.add_argument("--max-start-page", type=int, default=5)
    parser.add_argument("--max-match-ids-per-platform", type=int, default=20_000)
    parser.add_argument("--max-requests-per-platform", type=int, default=20_000)
    parser.add_argument("--concurrency", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=Path("data/snapshots/population"),
    )
    return parser.parse_args()


def make_config(args: argparse.Namespace) -> ExpandedCollectionPlanConfig:
    if args.config:
        return load_expanded_collection_config(args.config)
    if not args.target_public_patch:
        raise ValueError("--target-public-patch is required without --config")
    return ExpandedCollectionPlanConfig(
        platforms=tuple(args.platforms),
        target_public_patch=args.target_public_patch,
        patch_window_size=args.patch_window_size,
        tiers=tuple(args.tiers),
        divisions=tuple(args.divisions),
        target_matches_per_platform=args.target_matches_per_platform,
        max_players_per_platform=args.max_players_per_platform,
        initial_history_batch_size=args.initial_history_batch_size,
        max_history_per_player=args.max_history_per_player,
        older_patch_stop_threshold=args.older_patch_stop_threshold,
        pages_per_stratum=args.pages_per_stratum,
        max_start_page=args.max_start_page,
        max_match_ids_per_platform=args.max_match_ids_per_platform,
        max_requests_per_platform=args.max_requests_per_platform,
        concurrency=args.concurrency,
        seed=args.seed,
        raw_root=args.raw_root,
        processed_root=args.processed_root,
        snapshot_root=args.snapshot_root,
    )


def main() -> int:
    args = parse_args()
    try:
        config = make_config(args)
        plan = build_expanded_collection_plan(config)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
