"""Controlled queue-420 population collection with dry-run and resume support."""

import argparse
import asyncio
import json
import math
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from nexus_lens.catalog import ProcessingCatalog
from nexus_lens.collection_planning import load_expanded_collection_config
from nexus_lens.config import Settings
from nexus_lens.population import (
    DEFAULT_DIVISIONS,
    DEFAULT_TIERS,
    CheckpointCompatibilityError,
    PopulationCollector,
    PopulationConfig,
    build_plan,
    new_run_id,
    validate_checkpoint_extension,
)
from nexus_lens.population_state import (
    PopulationRunLockedError,
    PopulationState,
    exclusive_population_run,
)
from nexus_lens.private_dedup import (
    load_private_deduplication_index,
    match_set_sha256,
)
from nexus_lens.riot_client import (
    RiotApiError,
    RiotClient,
    RiotRequestBudgetExceeded,
    RiotRetryExhausted,
)

DEFAULT_TARGET = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect a bounded, resumable Ranked Solo/Duo population sample "
            "from official Riot APIs."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Single-platform expanded-collection JSON configuration. When set, "
            "collection settings and storage roots come from this file."
        ),
    )
    parser.add_argument("--platform", default="eun1", choices=("eun1", "euw1"))
    parser.add_argument("--target-public-patch", metavar="PATCH")
    parser.add_argument(
        "--patch-window-size",
        type=int,
        default=2,
        help=(
            "Accept the target and up to this many same-season patches "
            "newest-first (default: 2)."
        ),
    )
    parser.add_argument("--tiers", nargs="+", default=list(DEFAULT_TIERS))
    parser.add_argument("--divisions", nargs="+", default=list(DEFAULT_DIVISIONS))
    parser.add_argument("--target-matches", type=int, default=DEFAULT_TARGET)
    parser.add_argument("--max-players", type=int, default=100)
    parser.add_argument("--initial-history-batch-size", type=int, default=5)
    parser.add_argument("--max-history-per-player", type=int, default=20)
    parser.add_argument("--older-patch-stop-threshold", type=int, default=2)
    parser.add_argument(
        "--sampling-strategy", choices=("balanced", "fast"), default="balanced"
    )
    parser.add_argument("--minimum-players-per-tier", type=int, default=0)
    parser.add_argument(
        "--histories-per-player",
        type=int,
        default=None,
        help="Deprecated alias for --max-history-per-player.",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--concurrency", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--pages-per-stratum", type=int, default=1)
    parser.add_argument("--max-start-page", type=int, default=5)
    parser.add_argument("--max-match-ids", type=int, default=1_000)
    parser.add_argument("--max-requests", type=int, default=1_000)
    parser.add_argument(
        "--stop-on-newer-patch",
        action="store_true",
        help="Seal the checkpoint after the first resolved patch newer than target",
    )
    parser.add_argument(
        "--deduplication-index",
        action="append",
        type=Path,
        default=[],
        dest="deduplication_indexes",
        help="Private cross-location match-ID index; repeat as needed",
    )
    parser.add_argument(
        "--allow-over-default",
        action="store_true",
        help="Required when target matches exceeds the default safety limit of 100.",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Use a 10-match target and tighter bounds unless explicitly lowered.",
    )
    parser.add_argument(
        "--resume",
        metavar="RUN_ID",
        help="Resume a checkpoint under data/snapshots/population/RUN_ID.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print a non-sensitive zero-request plan without loading .env.",
    )
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--processed-dir", type=Path, default=Path("data/processed")
    )
    parser.add_argument(
        "--state-dir", type=Path, default=Path("data/snapshots/population")
    )
    return parser.parse_args()


def make_config(args: argparse.Namespace) -> PopulationConfig:
    if args.config is not None:
        expanded = load_expanded_collection_config(args.config)
        if len(expanded.platforms) != 1:
            raise ValueError(
                "live collection configuration must contain exactly one platform"
            )
        args.raw_dir = expanded.raw_root
        args.processed_dir = expanded.processed_root
        args.state_dir = expanded.snapshot_root
        args.deduplication_indexes = list(expanded.deduplication_indexes)
        return expanded.population_config(expanded.platforms[0])
    if args.target_public_patch is None:
        raise ValueError("--target-public-patch is required without --config")
    target = args.target_matches
    max_players = args.max_players
    max_match_ids = args.max_match_ids
    max_requests = args.max_requests
    if args.smoke_test and target == DEFAULT_TARGET:
        target = 10
        max_players = min(max_players, 25)
        max_match_ids = min(max_match_ids, 250)
        max_requests = min(max_requests, 300)
    if target > DEFAULT_TARGET and not args.allow_over_default:
        raise ValueError(
            "targets above 100 require the explicit --allow-over-default flag"
        )
    return PopulationConfig(
        platform=args.platform,
        target_public_patch=args.target_public_patch,
        patch_window_size=args.patch_window_size,
        tiers=tuple(tier.upper() for tier in args.tiers),
        divisions=tuple(division.upper() for division in args.divisions),
        target_matches=target,
        max_players=max_players,
        initial_history_batch_size=args.initial_history_batch_size,
        max_history_per_player=(
            args.histories_per_player
            if args.histories_per_player is not None
            else args.max_history_per_player
        ),
        older_patch_stop_threshold=args.older_patch_stop_threshold,
        sampling_strategy=args.sampling_strategy,
        minimum_players_per_tier=args.minimum_players_per_tier,
        seed=args.seed,
        concurrency=args.concurrency,
        pages_per_stratum=args.pages_per_stratum,
        max_start_page=args.max_start_page,
        max_match_ids=max_match_ids,
        max_requests=max_requests,
        stop_on_newer_patch=args.stop_on_newer_patch,
    )


async def run_collection(args: argparse.Namespace, config: PopulationConfig) -> None:
    run_id = args.resume or new_run_id()
    checkpoint = args.state_dir / run_id / "checkpoint.json"
    lock_path = checkpoint.parent / "collector.lock.sqlite3"
    with exclusive_population_run(lock_path):
        await _run_collection_locked(args, config, run_id, checkpoint)


async def _run_collection_locked(
    args: argparse.Namespace,
    config: PopulationConfig,
    run_id: str,
    checkpoint: Path,
) -> None:
    if args.resume:
        if not checkpoint.is_file():
            raise CheckpointCompatibilityError(
                "checkpoint not found: verify the --resume run ID"
            )
        state = PopulationState.load(checkpoint)
        saved_config = state.payload["config"]
        validate_checkpoint_extension(saved_config, config)
        state.payload["config"] = config.non_sensitive_dict()
        state.payload["version"] = max(4, int(state.payload.get("version", 1)))
        state.save()
    else:
        state = PopulationState.create(
            checkpoint,
            run_id=run_id,
            config=config.non_sensitive_dict(),
        )

    external_deduplication_match_ids: set[str] = set()
    for index_path in args.deduplication_indexes:
        external_deduplication_match_ids.update(
            load_private_deduplication_index(
                index_path,
                platform=config.platform,
                analysis_region=config.analysis_region,
                public_patch=config.target_public_patch,
            )
        )
    deduplication_metadata = {
        "schema_version": "private-match-dedup-reference-v1",
        "match_count": len(external_deduplication_match_ids),
        "match_set_sha256": match_set_sha256(external_deduplication_match_ids),
    }
    saved_deduplication = state.payload.get("external_deduplication")
    if saved_deduplication not in (None, deduplication_metadata):
        raise CheckpointCompatibilityError(
            "checkpoint incompatible: external deduplication index differs"
        )
    state.payload["external_deduplication"] = deduplication_metadata
    state.save()

    recover_missing_request_budget(state, config)

    settings = Settings(  # type: ignore[call-arg]
        platform_region=config.platform,
        routing_region=config.regional_routing,
    )
    raw_snapshot = args.raw_dir / run_id
    catalog_path = args.processed_dir / "catalog.sqlite3"
    previous_attempts = int(
        state.payload.get("request_metrics", {}).get("attempted_requests", 0)
    )
    remaining_requests = max(0, config.max_requests - previous_attempts)
    with ProcessingCatalog(catalog_path) as catalog:
        async with RiotClient(
            settings,
            max_requests=remaining_requests,
        ) as client:
            summary = await PopulationCollector(
                config=config,
                client=client,
                catalog=catalog,
                state=state,
                raw_snapshot_dir=raw_snapshot,
                processed_root=args.processed_dir,
                external_deduplication_match_ids=(
                    external_deduplication_match_ids
                ),
            ).collect()
    print_summary(summary.as_dict())


def print_summary(summary: dict[str, object]) -> None:
    print("Nexus Lens Stage 2 population summary")
    for key in (
        "target_public_patch",
        "patch_window_size",
        "accepted_public_patches",
        "accepted_matches_by_public_patch",
        "platform",
        "regional_routing",
        "sampled_distribution",
        "planned_player_distribution",
        "players_examined_by_tier",
        "players_examined_by_division",
        "strata_visited",
        "target_matches_by_contributing_stratum",
        "accepted_matches_by_contributing_stratum",
        "unattributed_target_matches",
        "players_examined",
        "match_ids_discovered",
        "duplicate_match_ids",
        "cross_location_duplicate_match_ids",
        "already_cataloged_matches",
        "already_downloaded_matches",
        "payloads_downloaded",
        "accepted_target_patch_matches",
        "accepted_previous_patch_matches",
        "accepted_matches",
        "total_accepted_matches_credited",
        "accepted_matches_credited_this_run",
        "total_target_patch_matches_credited",
        "target_patch_matches_credited_this_run",
        "newly_downloaded_accepted_matches",
        "accepted_matches_reused_this_run",
        "accepted_matches_reused_from_catalog_this_run",
        "accepted_matches_reused_from_raw_cache_this_run",
        "accepted_matches_reused_from_checkpoint_state_this_run",
        "wrong_patch_matches",
        "outside_patch_window_matches",
        "newer_patch_transition_matches",
        "total_wrong_patch_matches_observed",
        "newly_downloaded_wrong_patch_matches",
        "unresolved_patch_matches",
        "malformed_matches",
        "rejected_matches",
        "known_terminal_matches_reused_without_download",
        "known_wrong_patch_matches_reused_without_download",
        "downloaded_payloads_with_other_outcome",
        "examined_terminal_matches_this_run",
        "new_download_acceptance_rate",
        "new_download_wrong_patch_rate",
        "overall_examined_match_acceptance_rate",
        "accepted_matches_per_player_examined",
        "new_payloads_per_newly_downloaded_accepted_match",
        "new_payloads_per_accepted_match_credited_this_run",
        "efficiency_metric_definitions",
        "request_metrics",
        "elapsed_seconds",
        "target_reached",
        "completion_status",
        "patch_transition",
    ):
        print(f"  {key}: {summary[key]}")


def sanitized_collection_error(error: Exception) -> str:
    """Map internal failures to concise messages containing no request details."""

    if isinstance(error, CheckpointCompatibilityError):
        return str(error)
    if isinstance(error, PopulationRunLockedError):
        return "population checkpoint is already in use by another process"
    if isinstance(error, RiotRequestBudgetExceeded):
        return "request budget exhausted"
    if isinstance(error, RiotApiError):
        if error.status_code in (401, 403):
            return "Riot authentication failed; verify the local development key"
        return f"Riot API request failed with HTTP {error.status_code}"
    if isinstance(error, RiotRetryExhausted):
        return "Riot request retries exhausted"
    if isinstance(error, ValidationError):
        return "invalid configuration or missing required local settings"
    if isinstance(error, (OSError, sqlite3.Error)):
        return "local checkpoint, catalog, or storage operation failed"
    return "unexpected local collection error"


def recover_missing_request_budget(
    state: PopulationState, config: PopulationConfig
) -> int:
    """Charge a conservative attempt upper bound for interrupted old checkpoints."""

    metrics = state.payload.setdefault("request_metrics", {})
    attempted = int(metrics.get("attempted_requests", 0))
    endpoint_attempts = metrics.get("requests_by_endpoint", {})
    recorded_payload_attempts = int(endpoint_attempts.get("match_payload", 0))
    downloaded_payloads = sum(
        1 for record in state.matches.values() if record.get("raw_path")
    )
    previous_recovery = state.payload.get("request_budget_recovery", {})
    if not isinstance(previous_recovery, dict):
        previous_recovery = {}
    covered_payload_gap = int(
        previous_recovery.get("covered_payload_metric_gap", 0)
    )
    current_payload_gap = max(0, downloaded_payloads - recorded_payload_attempts)
    active_invocation = state.payload.pop("active_request_invocation", None)
    structural_gap = current_payload_gap > covered_payload_gap
    if not active_invocation and not structural_gap and "attempted_requests" in metrics:
        return attempted
    if not state.players and not state.matches and not active_invocation:
        return 0
    if active_invocation and not structural_gap:
        charged_attempts = attempted + 4 * max(
            config.pages_per_stratum, config.concurrency
        )
    else:
        sampling = state.payload.get("sampling", {})
        candidates = sampling.get("candidates", {})
        candidate_offsets = sampling.get("candidate_offsets", {})
        league_calls = len(candidates) * config.pages_per_stratum
        summoner_calls = sum(int(value) for value in candidate_offsets.values())
        history_calls = len(state.players) * math.ceil(
            config.max_history_per_player / config.initial_history_batch_size
        )
        payload_calls = len(state.matches)
        maximum_attempts_per_call = 4
        charged_attempts = maximum_attempts_per_call * (
            league_calls + summoner_calls + history_calls + payload_calls
        )
    metrics["attempted_requests"] = max(attempted, charged_attempts)
    state.payload["request_budget_recovery"] = {
        "method": "conservative_upper_bound",
        "charged_attempts": metrics["attempted_requests"],
        "active_invocation_recovered": bool(active_invocation),
        "structural_payload_gap_recovered": structural_gap,
        "covered_payload_metric_gap": max(
            covered_payload_gap, current_payload_gap
        ),
    }
    state.save()
    return int(metrics["attempted_requests"])


def main() -> int:
    args = parse_args()
    try:
        config = make_config(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if args.dry_run:
        print(json.dumps(build_plan(config), indent=2, sort_keys=True))
        return 0
    try:
        asyncio.run(run_collection(args, config))
    except KeyboardInterrupt:
        print("Collection interrupted safely; local checkpoint retained.")
        return 130
    except Exception as error:
        print(
            f"Collection stopped: {sanitized_collection_error(error)}; "
            "local checkpoint retained when available."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
