"""Deterministic, no-network planning for future population collection."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus_lens.population import (
    COLLECTION_LINEAGE_POLICY_VERSION,
    DEFAULT_DIVISIONS,
    DEFAULT_TIERS,
    PopulationConfig,
    build_sampling_schedule,
)

PLANNING_SCHEMA_VERSION = "expanded-collection-plan-v1"
SUPPORTED_PLATFORMS = ("eun1", "euw1")
API_KEY_ENVIRONMENT_VARIABLE = "NEXUS_LENS_RIOT_API_KEY"


@dataclass(frozen=True)
class ExpandedCollectionPlanConfig:
    """Configuration shared by one or more independently stored platform runs."""

    platforms: tuple[str, ...]
    target_public_patch: str
    patch_window_size: int = 2
    tiers: tuple[str, ...] = DEFAULT_TIERS
    divisions: tuple[str, ...] = DEFAULT_DIVISIONS
    target_matches_per_platform: int = 1_000
    max_players_per_platform: int = 2_000
    initial_history_batch_size: int = 5
    max_history_per_player: int = 20
    older_patch_stop_threshold: int = 2
    pages_per_stratum: int = 1
    max_start_page: int = 5
    max_match_ids_per_platform: int = 20_000
    max_requests_per_platform: int = 20_000
    concurrency: int = 1
    seed: int = 42
    stop_on_newer_patch: bool = False
    deduplication_indexes: tuple[Path, ...] = ()
    raw_root: Path = Path("data/raw")
    processed_root: Path = Path("data/processed")
    snapshot_root: Path = Path("data/snapshots/population")

    def __post_init__(self) -> None:
        normalized = tuple(platform.lower() for platform in self.platforms)
        if not normalized or len(normalized) != len(set(normalized)):
            raise ValueError("platforms must be non-empty and unique")
        unsupported = set(normalized) - set(SUPPORTED_PLATFORMS)
        if unsupported:
            raise ValueError("only eun1 and euw1 collection planning is supported")
        if self.target_matches_per_platform < 1:
            raise ValueError("target matches per platform must be positive")
        object.__setattr__(self, "platforms", normalized)
        for platform in normalized:
            self.population_config(platform)

    def population_config(self, platform: str) -> PopulationConfig:
        return PopulationConfig(
            platform=platform,
            target_public_patch=self.target_public_patch,
            patch_window_size=self.patch_window_size,
            tiers=tuple(tier.upper() for tier in self.tiers),
            divisions=tuple(division.upper() for division in self.divisions),
            target_matches=self.target_matches_per_platform,
            max_players=self.max_players_per_platform,
            initial_history_batch_size=self.initial_history_batch_size,
            max_history_per_player=self.max_history_per_player,
            older_patch_stop_threshold=self.older_patch_stop_threshold,
            sampling_strategy="balanced",
            seed=self.seed,
            concurrency=self.concurrency,
            pages_per_stratum=self.pages_per_stratum,
            max_start_page=self.max_start_page,
            max_match_ids=self.max_match_ids_per_platform,
            max_requests=self.max_requests_per_platform,
            stop_on_newer_patch=self.stop_on_newer_patch,
        )


def build_expanded_collection_plan(
    config: ExpandedCollectionPlanConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build an aggregate-only plan without constructing any network client."""

    environment_names = os.environ if environment is None else environment
    platform_plans = [
        _platform_plan(config, config.population_config(platform))
        for platform in config.platforms
    ]
    return {
        "planning_schema_version": PLANNING_SCHEMA_VERSION,
        "mode": "dry-run",
        "network_requests_made": 0,
        "requested_platforms": list(config.platforms),
        "required_configuration": {
            "riot_api_key_environment_variable": API_KEY_ENVIRONMENT_VARIABLE,
            "riot_api_key_present": (API_KEY_ENVIRONMENT_VARIABLE in environment_names),
            "secret_value_read": False,
            "dotenv_file_loaded": False,
        },
        "platform_plans": platform_plans,
        "total_target_accepted_matches": sum(
            item["target_accepted_matches"] for item in platform_plans
        ),
        "lineage": {
            "preservation_enabled": True,
            "policy_version": COLLECTION_LINEAGE_POLICY_VERSION,
            "multiple_discovery_contexts_preserved": True,
            "participant_rank_requires_observation": True,
        },
        "scope_exclusions": {
            "professional_esports_data": True,
            "third_party_stats_scraping": True,
            "timeline_collection": True,
            "model_calibration": True,
        },
        "authorization": {
            "collection_started": False,
            "future_collection_requires_user_authorization": True,
            "example_target_is_final_statistical_threshold": False,
        },
    }


def load_expanded_collection_config(
    path: Path,
) -> ExpandedCollectionPlanConfig:
    """Load a strict, non-secret planning example from JSON."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("collection plan configuration is not valid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("collection plan configuration must be an object")
    allowed = {
        "platforms",
        "target_public_patch",
        "patch_window_size",
        "tiers",
        "divisions",
        "target_matches_per_platform",
        "max_players_per_platform",
        "initial_history_batch_size",
        "max_history_per_player",
        "older_patch_stop_threshold",
        "pages_per_stratum",
        "max_start_page",
        "max_match_ids_per_platform",
        "max_requests_per_platform",
        "concurrency",
        "seed",
        "stop_on_newer_patch",
        "deduplication_indexes",
        "raw_root",
        "processed_root",
        "snapshot_root",
    }
    if set(payload) - allowed:
        raise ValueError("collection plan configuration contains unknown fields")
    required = {"platforms", "target_public_patch"}
    if not required <= set(payload):
        raise ValueError("platforms and target_public_patch are required")
    for field in ("platforms", "tiers", "divisions"):
        if field in payload:
            payload[field] = tuple(payload[field])
    for field in ("raw_root", "processed_root", "snapshot_root"):
        if field in payload:
            payload[field] = Path(payload[field])
    if "deduplication_indexes" in payload:
        payload["deduplication_indexes"] = tuple(
            Path(value) for value in payload["deduplication_indexes"]
        )
    try:
        return ExpandedCollectionPlanConfig(**payload)
    except (TypeError, ValueError):
        raise ValueError("collection plan configuration is invalid") from None


def _platform_plan(
    expanded: ExpandedCollectionPlanConfig,
    population: PopulationConfig,
) -> dict[str, Any]:
    strata = [
        f"{tier} {division}"
        for tier in population.tiers
        for division in population.divisions
    ]
    allocation = _allocate_evenly(population.target_matches, strata)
    run_placeholder = f"<{population.platform.upper()}_RUN_ID>"
    schedule = build_sampling_schedule(population)
    return {
        "platform_id": population.platform,
        "regional_routing": population.regional_routing,
        "analysis_region": population.analysis_region,
        "queue_id": 420,
        "queue_name": "Ranked Solo/Duo",
        "target_public_patch": population.target_public_patch,
        "patch_window_size": population.patch_window_size,
        "accepted_public_patches_newest_first": list(
            population.accepted_public_patches
        ),
        "history_traversal": "newest_first",
        "sampling_strategy": "balanced",
        "tiers": list(population.tiers),
        "divisions": list(population.divisions),
        "strata": strata,
        "deterministic_schedule": [
            {
                "tier": tier,
                "division": division,
                "start_page": start_page,
            }
            for tier, division, start_page in schedule
        ],
        "target_accepted_matches": population.target_matches,
        "planning_target_by_stratum": allocation,
        "stratum_target_semantics": (
            "even planning allocation; not an approved statistical threshold "
            "or a hard collector acceptance quota"
        ),
        "request_ceilings": {
            "max_players": population.max_players,
            "max_match_ids": population.max_match_ids,
            "max_requests": population.max_requests,
            "initial_history_batch_size": population.initial_history_batch_size,
            "max_history_per_player": population.max_history_per_player,
            "older_patch_stop_threshold": population.older_patch_stop_threshold,
            "pages_per_stratum": population.pages_per_stratum,
            "max_start_page": population.max_start_page,
            "concurrency": population.concurrency,
        },
        "rate_limit_policy": {
            "safe_handling_enabled": True,
            "proactive_header_buckets": True,
            "retry_after_respected": True,
            "bounded_retries": True,
        },
        "deduplication": {
            "match_id": "deterministic unique key",
            "multiple_discovery_contexts": (
                "preserve all without duplicate analytics rows"
            ),
            "private_cross_location_indexes": [
                path.as_posix() for path in expanded.deduplication_indexes
            ],
        },
        "patch_transition_guard": {
            "stop_on_newer_patch": population.stop_on_newer_patch,
            "accepted_patch_is_exact": population.patch_window_size == 1,
        },
        "resumable_checkpoint": (
            expanded.snapshot_root / run_placeholder / "checkpoint.json"
        ).as_posix(),
        "expected_raw_output": (expanded.raw_root / run_placeholder).as_posix(),
        "expected_processed_root": expanded.processed_root.as_posix(),
        "lineage_preservation_enabled": True,
    }


def _allocate_evenly(target: int, strata: list[str]) -> dict[str, int]:
    quotient, remainder = divmod(target, len(strata))
    return {
        stratum: quotient + int(index < remainder)
        for index, stratum in enumerate(strata)
    }
