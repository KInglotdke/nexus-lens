"""Controlled, resumable Ranked Solo/Duo population sampling."""

import asyncio
import json
import random
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nexus_lens.catalog import ProcessingCatalog
from nexus_lens.normalization import NormalizationError, normalize_match
from nexus_lens.patches import accepted_public_patch_window
from nexus_lens.population_state import PopulationState, atomic_write_json
from nexus_lens.privacy import pseudonymize_puuid
from nexus_lens.riot_client import (
    RiotApiError,
    RiotClient,
    RiotRequestBudgetExceeded,
    RiotRetryExhausted,
)
from nexus_lens.schemas import LeagueEntry, RiotMatch
from nexus_lens.storage import write_normalized_batch

DEFAULT_TIERS = ("GOLD", "PLATINUM", "EMERALD", "DIAMOND")
DEFAULT_DIVISIONS = ("I", "II", "III", "IV")
SAMPLING_STRATEGIES = ("balanced", "fast")
COLLECTION_LINEAGE_POLICY_VERSION = "collection-lineage-v1"
PLATFORM_ROUTES = {
    "eun1": ("europe", "eune"),
    "euw1": ("europe", "euw"),
}
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]")
_TERMINAL_PLAYER_STATUSES = {
    "coverage_only",
    "history_examined",
    "history_max_reached",
    "history_old_patch_cutoff",
}
EFFICIENCY_METRIC_DEFINITIONS = {
    "payloads_downloaded": (
        "unique Match-V5 payloads newly downloaded and parsed this invocation"
    ),
    "accepted_matches_credited_this_run": (
        "unique accepted-window matches newly credited after invocation start"
    ),
    "total_accepted_matches_credited": (
        "unique accepted-window matches currently credited toward the target"
    ),
    "known_terminal_matches_reused_without_download": (
        "unique terminal catalog or raw-cache matches reused without a download"
    ),
    "known_wrong_patch_matches_reused_without_download": (
        "wrong-patch subset of known terminal matches reused without a download"
    ),
    "new_download_acceptance_rate": (
        "newly downloaded accepted matches / payloads downloaded"
    ),
    "overall_examined_match_acceptance_rate": (
        "target matches credited this run / unique terminal matches examined this run"
    ),
    "new_payloads_per_newly_downloaded_accepted_match": (
        "payloads downloaded / newly downloaded accepted matches"
    ),
    "new_payloads_per_accepted_match_credited_this_run": (
        "payloads downloaded / accepted-window matches credited this run"
    ),
}


@dataclass(frozen=True)
class PopulationConfig:
    platform: str
    target_public_patch: str
    patch_window_size: int = 2
    tiers: tuple[str, ...] = DEFAULT_TIERS
    divisions: tuple[str, ...] = DEFAULT_DIVISIONS
    target_matches: int = 100
    max_players: int = 100
    initial_history_batch_size: int = 5
    max_history_per_player: int = 20
    older_patch_stop_threshold: int = 2
    sampling_strategy: str = "balanced"
    minimum_players_per_tier: int = 0
    seed: int = 42
    concurrency: int = 1
    pages_per_stratum: int = 1
    max_start_page: int = 5
    max_match_ids: int = 1_000
    max_requests: int = 1_000
    stop_on_newer_patch: bool = False
    histories_per_player: int | None = None

    def __post_init__(self) -> None:
        if self.histories_per_player is not None:
            if (
                self.max_history_per_player != 20
                and self.max_history_per_player != self.histories_per_player
            ):
                raise ValueError(
                    "histories_per_player conflicts with max_history_per_player"
                )
            object.__setattr__(
                self, "max_history_per_player", self.histories_per_player
            )
            object.__setattr__(
                self,
                "initial_history_batch_size",
                min(self.initial_history_batch_size, self.histories_per_player),
            )
        if self.platform.lower() not in PLATFORM_ROUTES:
            raise ValueError(f"unsupported platform: {self.platform}")
        accepted_public_patch_window(
            self.target_public_patch, self.patch_window_size
        )
        if not self.tiers or not self.divisions:
            raise ValueError("at least one tier and division are required")
        if self.target_matches < 1 or self.max_players < 1:
            raise ValueError("target matches and maximum players must be positive")
        if not 1 <= self.initial_history_batch_size <= 100:
            raise ValueError("initial history batch size must be between 1 and 100")
        if not 1 <= self.max_history_per_player <= 100:
            raise ValueError("maximum history per player must be between 1 and 100")
        if self.initial_history_batch_size > self.max_history_per_player:
            raise ValueError("initial history batch cannot exceed maximum history")
        if self.older_patch_stop_threshold < 1:
            raise ValueError("older-patch stopping threshold must be positive")
        if self.sampling_strategy not in SAMPLING_STRATEGIES:
            raise ValueError(f"sampling strategy must be one of {SAMPLING_STRATEGIES}")
        if self.minimum_players_per_tier < 0:
            raise ValueError("minimum tier coverage cannot be negative")
        if not 1 <= self.concurrency <= 4:
            raise ValueError("request concurrency must be between 1 and 4")
        if self.pages_per_stratum < 1 or self.max_start_page < 1:
            raise ValueError("discovery page bounds must be positive")
        if self.max_match_ids < self.target_matches:
            raise ValueError("maximum match IDs cannot be below target matches")
        if self.max_requests < 1:
            raise ValueError("maximum requests must be positive")

    @property
    def regional_routing(self) -> str:
        return PLATFORM_ROUTES[self.platform.lower()][0]

    @property
    def analysis_region(self) -> str:
        return PLATFORM_ROUTES[self.platform.lower()][1]

    @property
    def accepted_public_patches(self) -> tuple[str, ...]:
        return accepted_public_patch_window(
            self.target_public_patch, self.patch_window_size
        )

    @property
    def lower_public_patch(self) -> str:
        return self.accepted_public_patches[-1]

    def non_sensitive_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform.lower(),
            "regional_routing": self.regional_routing,
            "analysis_region": self.analysis_region,
            "target_public_patch": self.target_public_patch,
            "patch_window_size": self.patch_window_size,
            "accepted_public_patches": list(self.accepted_public_patches),
            "queue_id": 420,
            "tiers": list(self.tiers),
            "divisions": list(self.divisions),
            "target_matches": self.target_matches,
            "max_players": self.max_players,
            "initial_history_batch_size": self.initial_history_batch_size,
            "max_history_per_player": self.max_history_per_player,
            "older_patch_stop_threshold": self.older_patch_stop_threshold,
            "sampling_strategy": self.sampling_strategy,
            "minimum_players_per_tier": self.minimum_players_per_tier,
            "seed": self.seed,
            "concurrency": self.concurrency,
            "pages_per_stratum": self.pages_per_stratum,
            "max_start_page": self.max_start_page,
            "max_match_ids": self.max_match_ids,
            "max_requests": self.max_requests,
            "stop_on_newer_patch": self.stop_on_newer_patch,
        }

    @classmethod
    def from_saved(cls, saved: dict[str, Any]) -> "PopulationConfig":
        """Load both current and Stage 2 configuration dictionaries."""

        legacy_history = int(saved.get("histories_per_player", 20))
        return cls(
            platform=str(saved["platform"]),
            target_public_patch=str(saved["target_public_patch"]),
            patch_window_size=int(saved.get("patch_window_size", 1)),
            tiers=tuple(saved.get("tiers", DEFAULT_TIERS)),
            divisions=tuple(saved.get("divisions", DEFAULT_DIVISIONS)),
            target_matches=int(saved.get("target_matches", 100)),
            max_players=int(saved.get("max_players", 100)),
            initial_history_batch_size=int(
                saved.get("initial_history_batch_size", min(5, legacy_history))
            ),
            max_history_per_player=int(
                saved.get("max_history_per_player", legacy_history)
            ),
            older_patch_stop_threshold=int(
                saved.get("older_patch_stop_threshold", 2)
            ),
            sampling_strategy=str(saved.get("sampling_strategy", "fast")),
            minimum_players_per_tier=int(
                saved.get("minimum_players_per_tier", 0)
            ),
            seed=int(saved.get("seed", 42)),
            concurrency=int(saved.get("concurrency", 1)),
            pages_per_stratum=int(saved.get("pages_per_stratum", 1)),
            max_start_page=int(saved.get("max_start_page", 5)),
            max_match_ids=int(saved.get("max_match_ids", 1_000)),
            max_requests=int(saved.get("max_requests", 1_000)),
            stop_on_newer_patch=bool(saved.get("stop_on_newer_patch", False)),
        )


class CheckpointCompatibilityError(ValueError):
    """A sanitized explanation of an unsafe checkpoint extension."""


def validate_checkpoint_extension(
    saved: dict[str, Any], requested: PopulationConfig
) -> PopulationConfig:
    """Validate a monotonic extension while preserving the saved schedule."""

    if int(saved.get("queue_id", 420)) != 420:
        raise CheckpointCompatibilityError(
            "checkpoint incompatible: queue differs from Ranked Solo/Duo 420"
        )
    saved_config = PopulationConfig.from_saved(saved)
    immutable_fields = (
        "platform",
        "target_public_patch",
        "tiers",
        "divisions",
        "seed",
        "sampling_strategy",
        "initial_history_batch_size",
        "max_history_per_player",
        "older_patch_stop_threshold",
        "concurrency",
        "pages_per_stratum",
        "max_start_page",
        "stop_on_newer_patch",
    )
    for field in immutable_fields:
        if getattr(saved_config, field) != getattr(requested, field):
            raise CheckpointCompatibilityError(
                f"checkpoint incompatible: {field} differs"
            )
    saved_patches = set(saved_config.accepted_public_patches)
    requested_patches = set(requested.accepted_public_patches)
    if not saved_patches.issubset(requested_patches):
        raise CheckpointCompatibilityError(
            "checkpoint incompatible: requested patch window excludes saved results"
        )
    monotonic_fields = (
        "target_matches",
        "max_players",
        "max_match_ids",
        "max_requests",
        "minimum_players_per_tier",
    )
    for field in monotonic_fields:
        if getattr(requested, field) < getattr(saved_config, field):
            raise CheckpointCompatibilityError(
                f"checkpoint incompatible: {field} cannot decrease"
            )
    return saved_config


@dataclass
class PopulationSummary:
    values: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return self.values

    def __getattr__(self, name: str) -> object:
        try:
            return self.values[name]
        except KeyError as error:
            raise AttributeError(name) from error


async def discover_league_entries(
    client: RiotClient,
    *,
    tier: str,
    division: str,
    start_page: int,
    max_pages: int,
) -> list[LeagueEntry]:
    """Read bounded consecutive League-V4 pages, stopping at the first empty one."""

    discovered: list[LeagueEntry] = []
    for page in range(start_page, start_page + max_pages):
        entries = await client.get_league_entries(tier, division, page=page)
        if not entries:
            break
        discovered.extend(entries)
    return discovered


class PopulationCollector:
    """Collect a bounded public-patch sample with persistent local checkpoints."""

    def __init__(
        self,
        *,
        config: PopulationConfig,
        client: RiotClient,
        catalog: ProcessingCatalog,
        state: PopulationState,
        raw_snapshot_dir: Path,
        processed_root: Path,
        external_deduplication_match_ids: set[str] | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.catalog = catalog
        self.state = state
        self.raw_snapshot_dir = raw_snapshot_dir
        self.processed_root = processed_root
        self.external_deduplication_match_ids = (
            external_deduplication_match_ids or set()
        )
        self._stop_reason = "bounds_exhausted"
        if self.config.stop_on_newer_patch and self.state.payload.get(
            "patch_transition"
        ):
            self._stop_reason = "newer_patch_transition_detected"
        self._initial_target_match_ids = {
            match_id
            for match_id, record in self.state.matches.items()
            if self._is_target_record(record)
        }
        self._downloaded_match_ids: set[str] = set()
        self._raw_cache_match_ids: set[str] = set()
        self._catalog_accepted_match_ids: set[str] = set()
        self._catalog_wrong_patch_match_ids: set[str] = set()
        self._known_terminal_match_ids: set[str] = set()
        self._known_wrong_patch_match_ids: set[str] = set()
        self._initial_downloaded = self._downloaded_count()
        self._request_metrics_at_start = dict(
            self.state.payload.get("request_metrics", {})
        )
        self.state.payload["active_request_invocation"] = {
            "schema_version": "population-request-invocation-v1",
            "baseline_attempted_requests": int(
                self._request_metrics_at_start.get("attempted_requests", 0)
            ),
        }
        self.state.before_save = self._checkpoint_request_metrics
        self._ensure_state_shape()

    async def collect(self) -> PopulationSummary:
        started = time.perf_counter()
        for record in self.state.matches.values():
            if record.get("status") == "request_failed":
                record["status"] = "pending"
        try:
            self._migrate_window_eligible_records()
            await self._download_pending()
            await self._resume_incomplete_players()
            if self.config.sampling_strategy == "balanced":
                await self._collect_balanced()
            else:
                await self._collect_fast()
        finally:
            self.state.payload.pop("active_request_invocation", None)
            self._update_checkpoint_metadata()
            self.state.save()

        target_reached = self._target_reached()
        coverage_met = self._coverage_met()
        if target_reached and coverage_met:
            self._stop_reason = "target_reached"
        elif target_reached:
            self._stop_reason = "target_reached_coverage_incomplete"
        elif len(self.state.players) >= self.config.max_players:
            self._stop_reason = "maximum_players_reached"
        elif len(self.state.matches) >= self.config.max_match_ids:
            self._stop_reason = "maximum_match_ids_reached"

        elapsed = time.perf_counter() - started
        summary = self._summary(elapsed, target_reached, coverage_met)
        self.state.save()
        self._write_manifest(summary)
        return summary

    def _checkpoint_request_metrics(self) -> None:
        self.state.payload["request_metrics"] = _merge_request_metrics(
            self._request_metrics_at_start,
            self.client.metrics.as_dict(),
        )
        self._update_checkpoint_metadata()

    def _ensure_state_shape(self) -> None:
        self.state.payload["version"] = max(
            4, int(self.state.payload.get("version", 1))
        )
        self.state.payload.setdefault("overlap_events", 0)
        self.state.payload.setdefault("request_metrics", {})
        sampling = self.state.payload.get("sampling")
        if sampling is None:
            schedule = build_sampling_schedule(self.config)
            sampling = {
                "strategy": self.config.sampling_strategy,
                "schedule": [
                    {"tier": tier, "division": division, "start_page": page}
                    for tier, division, page in schedule
                ],
                "cursor": 0,
                "candidates": {},
                "candidate_observed_at": {},
                "candidate_offsets": {},
                "exhausted": [],
            }
            self.state.payload["sampling"] = sampling
        elif sampling.get("strategy") != self.config.sampling_strategy:
            raise ValueError("resume sampling strategy differs from checkpoint")
        sampling.setdefault("candidate_observed_at", {})
        _repair_legacy_player_lineage(self.state.players, sampling)
        self.state.payload["lineage_policy_version"] = COLLECTION_LINEAGE_POLICY_VERSION
        self.state.payload["lineage_preservation_enabled"] = True
        self._update_checkpoint_metadata()
        self.state.save()

    def _update_checkpoint_metadata(self) -> None:
        self.state.payload["patch_window_size"] = self.config.patch_window_size
        self.state.payload["accepted_public_patches"] = list(
            self.config.accepted_public_patches
        )
        self.state.payload["accepted_match_counts_by_public_patch"] = (
            self._accepted_counts_by_patch()
        )

    def _migrate_window_eligible_records(self) -> None:
        migratable = {
            "wrong_patch",
            "wrong_patch_cached",
            "outside_patch_window",
            "outside_patch_window_cached",
            "newer_patch_transition",
            "newer_patch_transition_cached",
        }
        for match_id, record in self.state.matches.items():
            if record.get("status") not in migratable:
                continue
            if record.get("public_patch") not in self.config.accepted_public_patches:
                continue
            cached_path = self._find_cached_raw_path(match_id, record)
            if cached_path is None:
                record["status"] = "pending"
                continue
            try:
                raw_match = RiotMatch.model_validate_json(
                    cached_path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError):
                record["status"] = "pending"
                continue
            self._raw_cache_match_ids.add(match_id)
            self._known_terminal_match_ids.add(match_id)
            self._process_download(match_id, raw_match, write_raw=False)

    def _find_cached_raw_path(
        self, match_id: str, record: dict[str, Any]
    ) -> Path | None:
        raw_path = record.get("raw_path")
        if raw_path:
            current = self.raw_snapshot_dir / str(raw_path)
            if current.is_file():
                return current
        observation = self.catalog.match_observation(match_id)
        source = observation.get("source_snapshot") if observation else None
        if not isinstance(source, str) or Path(source).name != source:
            return None
        candidate = (
            self.raw_snapshot_dir.parent
            / source
            / "downloads"
            / f"{_safe(match_id)}.json"
        )
        return candidate if candidate.is_file() else None

    async def _collect_balanced(self) -> None:
        sampling = self.state.payload["sampling"]
        schedule = sampling["schedule"]
        if not schedule:
            return
        while not self._should_stop():
            if len(sampling["exhausted"]) >= len(schedule):
                return
            index = int(sampling["cursor"]) % len(schedule)
            sampling["cursor"] = int(sampling["cursor"]) + 1
            item = schedule[index]
            entry = await self._next_candidate(item)
            self.state.save()
            if entry is None:
                continue
            await self._examine_player(
                entry,
                item["tier"],
                item["division"],
                rank_observed_at=self._candidate_observed_at(item),
            )

    async def _collect_fast(self) -> None:
        sampling = self.state.payload["sampling"]
        schedule = sampling["schedule"]
        while int(sampling["cursor"]) < len(schedule) and not self._should_stop():
            item = schedule[int(sampling["cursor"])]
            entry = await self._next_candidate(item)
            if entry is None:
                sampling["cursor"] = int(sampling["cursor"]) + 1
                self.state.save()
                continue
            await self._examine_player(
                entry,
                item["tier"],
                item["division"],
                rank_observed_at=self._candidate_observed_at(item),
            )

    async def _next_candidate(self, item: dict[str, Any]) -> LeagueEntry | None:
        sampling = self.state.payload["sampling"]
        key = _stratum_key(item["tier"], item["division"], item["start_page"])
        if key not in sampling["candidates"]:
            try:
                entries = await discover_league_entries(
                    self.client,
                    tier=item["tier"],
                    division=item["division"],
                    start_page=int(item["start_page"]),
                    max_pages=self.config.pages_per_stratum,
                )
            except RiotRequestBudgetExceeded:
                self._stop_reason = "request_budget_exhausted"
                return None
            except RiotApiError as error:
                if error.status_code in (401, 403):
                    raise
                entries = []
            except RiotRetryExhausted:
                entries = []
            random.Random(
                f"{self.config.seed}:{item['tier']}:{item['division']}:"
                f"{item['start_page']}"
            ).shuffle(entries)
            sampling["candidates"][key] = [
                entry.model_dump(mode="json", by_alias=True) for entry in entries
            ]
            sampling["candidate_observed_at"][key] = datetime.now(UTC).isoformat()
            sampling["candidate_offsets"][key] = 0
        offset = int(sampling["candidate_offsets"].get(key, 0))
        candidates = sampling["candidates"][key]
        if offset >= len(candidates):
            if key not in sampling["exhausted"]:
                sampling["exhausted"].append(key)
            return None
        sampling["candidate_offsets"][key] = offset + 1
        return LeagueEntry.model_validate(candidates[offset])

    def _candidate_observed_at(self, item: dict[str, Any]) -> str | None:
        key = _stratum_key(item["tier"], item["division"], item["start_page"])
        value = self.state.payload["sampling"]["candidate_observed_at"].get(key)
        return str(value) if isinstance(value, str) and value else None

    async def _resume_incomplete_players(self) -> None:
        for puuid, player in list(self.state.players.items()):
            if self._should_stop():
                return
            if player.get("status") not in _TERMINAL_PLAYER_STATUSES:
                await self._continue_player_history(puuid, player)

    async def _examine_player(
        self,
        entry: LeagueEntry,
        tier: str,
        division: str,
        *,
        rank_observed_at: str | None = None,
    ) -> bool:
        try:
            puuid = await self._resolve_puuid(entry)
        except RiotRequestBudgetExceeded:
            self._stop_reason = "request_budget_exhausted"
            return False
        except RiotApiError as error:
            if error.status_code in (401, 403):
                raise
            return False
        except (RiotRetryExhausted, ValidationError):
            return False
        if puuid is None:
            return False
        existing = self.state.players.get(puuid)
        if existing is not None:
            _merge_player_lineage(
                existing,
                entry=entry,
                tier=tier,
                division=division,
                rank_observed_at=rank_observed_at,
            )
            if existing.get("status") not in _TERMINAL_PLAYER_STATUSES:
                await self._continue_player_history(puuid, existing)
            return False
        if len(self.state.players) >= self.config.max_players:
            return False
        player = {
            "tier": tier,
            "division": division,
            "status": "discovered",
            "history_offset": 0,
            "consecutive_older_patch": 0,
            "history_pending_ids": [],
            "seed_player_key": pseudonymize_puuid(puuid),
            "collection_contexts": [_collection_context(tier=tier, division=division)],
            "rank_observations": [
                _rank_observation(entry, rank_observed_at=rank_observed_at)
            ],
        }
        self.state.players[puuid] = player
        self.state.save()
        if self._target_reached():
            player["status"] = "coverage_only"
            self.state.save()
            return True
        await self._continue_player_history(puuid, player)
        return True

    async def _continue_player_history(
        self, puuid: str, player: dict[str, Any]
    ) -> None:
        while not self._download_should_stop():
            pending_ids = list(player.get("history_pending_ids", []))
            if not pending_ids:
                offset = int(player.get("history_offset", 0))
                if offset >= self.config.max_history_per_player:
                    player["status"] = "history_max_reached"
                    self.state.save()
                    return
                count = min(
                    self.config.initial_history_batch_size,
                    self.config.max_history_per_player - offset,
                )
                try:
                    pending_ids = await self.client.get_match_ids_by_puuid(
                        puuid, start=offset, count=count
                    )
                except RiotRequestBudgetExceeded:
                    player["status"] = "request_budget_exhausted"
                    self._stop_reason = "request_budget_exhausted"
                    self.state.save()
                    return
                except RiotApiError as error:
                    if error.status_code in (401, 403):
                        raise
                    player["status"] = "history_failed"
                    self.state.save()
                    return
                except RiotRetryExhausted:
                    player["status"] = "history_failed"
                    self.state.save()
                    return
                player["history_pending_ids"] = pending_ids
                player["last_history_request_count"] = count
                self.state.save()
                if not pending_ids:
                    player["status"] = "history_examined"
                    self.state.save()
                    return
            stopped_early = await self._process_history_ids(
                puuid, player, pending_ids
            )
            requested = int(player.get("last_history_request_count", len(pending_ids)))
            player["history_offset"] = int(player.get("history_offset", 0)) + len(
                pending_ids
            )
            player["history_pending_ids"] = []
            if stopped_early:
                player["status"] = "history_old_patch_cutoff"
                self.state.save()
                return
            if len(pending_ids) < requested:
                player["status"] = "history_examined"
                self.state.save()
                return
            self.state.save()

    async def _process_history_ids(
        self, puuid: str, player: dict[str, Any], match_ids: list[str]
    ) -> bool:
        tier = str(player["tier"])
        division = str(player["division"])
        for start in range(0, len(match_ids), self.config.concurrency):
            if self._download_should_stop():
                return False
            chunk = match_ids[start : start + self.config.concurrency]
            relations = await self._process_match_chunk(
                chunk, puuid=puuid, tier=tier, division=division
            )
            for relation in relations:
                if relation == "older":
                    player["consecutive_older_patch"] = int(
                        player.get("consecutive_older_patch", 0)
                    ) + 1
                else:
                    player["consecutive_older_patch"] = 0
                if (
                    int(player["consecutive_older_patch"])
                    >= self.config.older_patch_stop_threshold
                ):
                    return True
            self.state.save()
        return False

    async def _process_match_chunk(
        self, match_ids: list[str], *, puuid: str, tier: str, division: str
    ) -> list[str | None]:
        records = [
            self._register_match(match_id, puuid, tier, division)
            for match_id in match_ids
        ]
        pending = [
            match_id
            for match_id, record in zip(match_ids, records, strict=True)
            if record.get("status") == "pending"
        ]
        remaining = max(0, self.config.target_matches - self._accepted_count())
        pending = pending[:remaining]
        results = await asyncio.gather(
            *(self._get_or_load_match(match_id) for match_id in pending),
            return_exceptions=True,
        )
        for match_id, result in zip(pending, results, strict=True):
            if isinstance(result, RiotRequestBudgetExceeded):
                self._stop_reason = "request_budget_exhausted"
                self.state.matches[match_id]["status"] = "request_failed"
            elif isinstance(result, RiotApiError) and result.status_code in (401, 403):
                raise result
            elif isinstance(result, ValidationError):
                self.state.matches[match_id]["status"] = "rejected_schema"
            elif isinstance(result, Exception):
                self.state.matches[match_id]["status"] = "request_failed"
            else:
                raw_match, already_raw = result
                self._process_download(match_id, raw_match, write_raw=not already_raw)
        self.state.save()
        return [self._patch_relation(record) for record in records]

    def _register_match(
        self, match_id: str, puuid: str, tier: str, division: str
    ) -> dict[str, Any]:
        player = self.state.players.get(puuid, {})
        source = _match_discovery_source(
            puuid=puuid,
            player=player,
            tier=tier,
            division=division,
            platform=self.config.platform.lower(),
            regional_routing=self.config.regional_routing,
            analysis_region=self.config.analysis_region,
        )
        existing = self.state.matches.get(match_id)
        if existing is not None:
            sources = existing.setdefault("sources", [])
            if not any(
                _source_identity(item) == _source_identity(source) for item in sources
            ):
                sources.append(source)
                sources.sort(key=_source_sort_key)
                self.state.payload["overlap_events"] += 1
            if existing.get("status") not in ("pending", "request_failed"):
                self._record_cache_hit(match_id, existing)
            return existing
        if len(self.state.matches) >= self.config.max_match_ids:
            self._stop_reason = "maximum_match_ids_reached"
            return {"status": "not_registered", "sources": [source]}

        observation = self.catalog.match_observation(match_id)
        record: dict[str, Any] = {"status": "pending", "sources": [source]}
        if match_id in self.external_deduplication_match_ids:
            record["status"] = "external_duplicate"
            self._known_terminal_match_ids.add(match_id)
        elif observation and observation["status"] == "processed":
            record.update(
                status=(
                    "already_cataloged_accepted"
                    if observation["public_patch"]
                    in self.config.accepted_public_patches
                    else "already_cataloged_other"
                ),
                api_patch=observation["api_patch"],
                public_patch=observation["public_patch"],
                patch_resolution_status=observation["patch_resolution_status"],
            )
            if record["status"] == "already_cataloged_accepted":
                self._catalog_accepted_match_ids.add(match_id)
            self._record_cache_hit(match_id, record)
        elif observation and observation["status"] == "rejected":
            failure = observation.get("failure_code")
            public_patch = observation["public_patch"]
            if (
                failure in ("wrong_patch", "outside_patch_window")
                and public_patch in self.config.accepted_public_patches
            ):
                cached_status = "pending"
            elif failure in ("wrong_patch", "outside_patch_window"):
                cached_status = "outside_patch_window_cached"
            elif failure == "newer_patch_transition":
                cached_status = "newer_patch_transition_cached"
                if self.config.stop_on_newer_patch:
                    self._record_patch_transition(public_patch)
            else:
                cached_status = "cached_rejected"
            record.update(
                status=cached_status,
                api_patch=observation["api_patch"],
                public_patch=observation["public_patch"],
                patch_resolution_status=observation["patch_resolution_status"],
            )
            if record["status"] == "outside_patch_window_cached":
                self._catalog_wrong_patch_match_ids.add(match_id)
            if record["status"] != "pending":
                self._record_cache_hit(match_id, record)
        self.state.matches[match_id] = record
        return record

    def _record_cache_hit(self, match_id: str, record: dict[str, Any]) -> None:
        self._known_terminal_match_ids.add(match_id)
        if record.get("status") in (
            "wrong_patch",
            "wrong_patch_cached",
            "outside_patch_window",
            "outside_patch_window_cached",
            "newer_patch_transition",
            "newer_patch_transition_cached",
        ):
            self._known_wrong_patch_match_ids.add(match_id)

    async def _get_or_load_match(self, match_id: str) -> tuple[RiotMatch, bool]:
        record = self.state.matches[match_id]
        raw_path = record.get("raw_path")
        if raw_path:
            path = self.raw_snapshot_dir / str(raw_path)
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                self._raw_cache_match_ids.add(match_id)
                self._record_cache_hit(match_id, record)
                return RiotMatch.model_validate(payload), True
        cached_path = self._find_cached_raw_path(match_id, record)
        if cached_path is not None:
            payload = json.loads(cached_path.read_text(encoding="utf-8"))
            self._raw_cache_match_ids.add(match_id)
            self._record_cache_hit(match_id, record)
            return RiotMatch.model_validate(payload), True
        match = await self.client.get_match(match_id)
        self._downloaded_match_ids.add(match_id)
        return match, False

    async def _download_pending(self) -> None:
        """Finish pending matches from Stage 2 checkpoints before new discovery."""

        while not self._download_should_stop():
            pending = [
                match_id
                for match_id, record in self.state.matches.items()
                if record.get("status") == "pending"
            ]
            if not pending:
                return
            remaining = self.config.target_matches - self._accepted_count()
            if remaining <= 0:
                return
            selected = pending[: min(self.config.concurrency, remaining)]
            results = await asyncio.gather(
                *(self._get_or_load_match(match_id) for match_id in selected),
                return_exceptions=True,
            )
            for match_id, result in zip(selected, results, strict=True):
                if isinstance(result, RiotRequestBudgetExceeded):
                    self._stop_reason = "request_budget_exhausted"
                    return
                if (
                    isinstance(result, RiotApiError)
                    and result.status_code in (401, 403)
                ):
                    raise result
                if isinstance(result, ValidationError):
                    self.state.matches[match_id]["status"] = "rejected_schema"
                elif isinstance(result, Exception):
                    self.state.matches[match_id]["status"] = "request_failed"
                else:
                    raw_match, already_raw = result
                    self._process_download(
                        match_id, raw_match, write_raw=not already_raw
                    )
            self.state.save()

    def _process_download(
        self, match_id: str, raw_match: RiotMatch, *, write_raw: bool = True
    ) -> None:
        raw_relative = Path("downloads") / f"{_safe(match_id)}.json"
        if write_raw:
            atomic_write_json(
                self.raw_snapshot_dir / raw_relative,
                raw_match.model_dump(mode="json", by_alias=True),
            )
        record = self.state.matches[match_id]
        if write_raw:
            record["raw_path"] = raw_relative.as_posix()
        try:
            batch = normalize_match(raw_match)
        except NormalizationError as error:
            record["status"] = f"rejected_{error.code}"
            self.catalog.record_rejected(
                match_id=match_id,
                routing_region=self.config.analysis_region,
                source_snapshot=self.raw_snapshot_dir.name,
                queue_id=raw_match.info.queueId,
                failure_code=error.code,
                failure_reason="payload failed normalization validation",
                api_game_version=raw_match.info.gameVersion,
            )
            return
        record["api_patch"] = batch.match.api_patch
        record["public_patch"] = batch.match.public_patch
        record["patch_resolution_status"] = batch.match.patch_resolution_status
        if batch.match.patch_resolution_status != "resolved":
            record["status"] = "unresolved_patch"
            self._record_rejected_batch(match_id, batch, "unresolved_patch")
            return
        if batch.match.public_patch not in self.config.accepted_public_patches:
            if self.config.stop_on_newer_patch and _is_newer_patch(
                batch.match.public_patch, self.config.target_public_patch
            ):
                record["status"] = "newer_patch_transition"
                self._record_rejected_batch(
                    match_id, batch, "newer_patch_transition"
                )
                self._record_patch_transition(batch.match.public_patch)
            else:
                record["status"] = "outside_patch_window"
                self._record_rejected_batch(
                    match_id, batch, "outside_patch_window"
                )
            return

        write_normalized_batch(
            self.processed_root, self.config.analysis_region, batch
        )
        self.catalog.record_processed(
            match_id=match_id,
            routing_region=self.config.analysis_region,
            api_game_version=batch.match.api_game_version,
            api_patch=batch.match.api_patch,
            public_patch=batch.match.public_patch,
            patch_resolution_method=batch.match.patch_resolution_method,
            patch_resolution_status=batch.match.patch_resolution_status,
            queue_id=batch.match.queue_id,
            source_snapshot=self.raw_snapshot_dir.name,
        )
        record["status"] = "accepted"

    def _record_rejected_batch(self, match_id: str, batch: Any, code: str) -> None:
        self.catalog.record_rejected(
            match_id=match_id,
            routing_region=self.config.analysis_region,
            source_snapshot=self.raw_snapshot_dir.name,
            queue_id=batch.match.queue_id,
            failure_code=code,
            failure_reason=(
                "newer public patch transition detected"
                if code == "newer_patch_transition"
                else "payload is outside the accepted public patch window"
            ),
            api_game_version=batch.match.api_game_version,
            api_patch=batch.match.api_patch,
            public_patch=batch.match.public_patch,
            patch_resolution_method=batch.match.patch_resolution_method,
            patch_resolution_status=batch.match.patch_resolution_status,
        )

    def _record_patch_transition(self, public_patch: str | None) -> None:
        transition = self.state.payload.setdefault(
            "patch_transition",
            {
                "detected": True,
                "public_patch": public_patch,
                "rejected_match_count": 0,
                "detected_at": datetime.now(UTC).isoformat(),
            },
        )
        transition["rejected_match_count"] = int(
            transition.get("rejected_match_count", 0)
        ) + 1
        self._stop_reason = "newer_patch_transition_detected"

    async def _resolve_puuid(self, entry: LeagueEntry) -> str | None:
        if entry.puuid:
            return entry.puuid
        if entry.summonerId:
            return (await self.client.get_summoner_by_id(entry.summonerId)).puuid
        return None

    def _patch_relation(self, record: dict[str, Any]) -> str | None:
        public_patch = record.get("public_patch")
        if public_patch in self.config.accepted_public_patches:
            return "accepted"
        if isinstance(public_patch, str) and _is_older_patch(
            public_patch, self.config.lower_public_patch
        ):
            return "older"
        return None

    def _target_reached(self) -> bool:
        return self._accepted_count() >= self.config.target_matches

    def _coverage_met(self) -> bool:
        minimum = self.config.minimum_players_per_tier
        if minimum == 0:
            return True
        counts = Counter(str(player["tier"]) for player in self.state.players.values())
        return all(counts[tier] >= minimum for tier in self.config.tiers)

    def _goal_reached(self) -> bool:
        return self._target_reached() and self._coverage_met()

    def _should_stop(self) -> bool:
        return bool(
            self._goal_reached()
            or len(self.state.players) >= self.config.max_players
            or len(self.state.matches) >= self.config.max_match_ids
            or self.client.metrics.attempted_requests >= self.config.max_requests
            or self._stop_reason
            in ("request_budget_exhausted", "newer_patch_transition_detected")
        )

    def _download_should_stop(self) -> bool:
        return bool(
            self._target_reached()
            or len(self.state.matches) >= self.config.max_match_ids
            or self.client.metrics.attempted_requests >= self.config.max_requests
            or self._stop_reason
            in ("request_budget_exhausted", "newer_patch_transition_detected")
        )

    def _accepted_count(self) -> int:
        return sum(
            record.get("status")
            in (
                "accepted",
                "already_cataloged_target",
                "already_cataloged_accepted",
            )
            for record in self.state.matches.values()
        )

    @staticmethod
    def _is_target_record(record: dict[str, Any]) -> bool:
        return record.get("status") in (
            "accepted",
            "already_cataloged_target",
            "already_cataloged_accepted",
        )

    def _wrong_patch_count(self) -> int:
        return sum(
            record.get("status")
            in (
                "wrong_patch",
                "wrong_patch_cached",
                "outside_patch_window",
                "outside_patch_window_cached",
                "newer_patch_transition",
                "newer_patch_transition_cached",
            )
            for record in self.state.matches.values()
        )

    def _accepted_counts_by_patch(self) -> dict[str, int]:
        counts = Counter(
            str(record.get("public_patch"))
            for record in self.state.matches.values()
            if self._is_target_record(record) and record.get("public_patch")
        )
        return dict(sorted(counts.items()))

    def _downloaded_count(self) -> int:
        return sum("raw_path" in record for record in self.state.matches.values())

    def _summary(
        self, elapsed: float, target_reached: bool, coverage_met: bool
    ) -> PopulationSummary:
        statuses = Counter(
            record.get("status") for record in self.state.matches.values()
        )
        strata = Counter(
            f"{player['tier']} {player['division']}"
            for player in self.state.players.values()
        )
        tiers = Counter(str(player["tier"]) for player in self.state.players.values())
        divisions = Counter(
            str(player["division"]) for player in self.state.players.values()
        )
        contributing: Counter[str] = Counter()
        unattributed_target_matches = 0
        for record in self.state.matches.values():
            if not self._is_target_record(record):
                continue
            sources = record.get("sources", [])
            if sources:
                source = sources[0]
                contributing[f"{source['tier']} {source['division']}"] += 1
            else:
                unattributed_target_matches += 1
        rejected = sum(
            count
            for status, count in statuses.items()
            if str(status).startswith("rejected_")
            or status
            in (
                "unresolved_patch",
                "request_failed",
                "cached_rejected",
                "newer_patch_transition",
                "newer_patch_transition_cached",
            )
        )
        malformed = sum(
            statuses[status]
            for status in (
                "rejected_schema",
                "rejected_participant_count",
                "rejected_team_count",
            )
        )
        final_target_ids = {
            match_id
            for match_id, record in self.state.matches.items()
            if self._is_target_record(record)
        }
        credited_target_ids = final_target_ids - self._initial_target_match_ids
        downloaded_accepted_ids = (
            credited_target_ids & self._downloaded_match_ids
        )
        catalog_reused_accepted_ids = (
            credited_target_ids & self._catalog_accepted_match_ids
        )
        raw_reused_accepted_ids = credited_target_ids & self._raw_cache_match_ids
        reused_accepted_ids = credited_target_ids - downloaded_accepted_ids
        checkpoint_reused_accepted_ids = (
            reused_accepted_ids
            - catalog_reused_accepted_ids
            - raw_reused_accepted_ids
        )
        downloaded_wrong_patch_ids = {
            match_id
            for match_id in self._downloaded_match_ids
            if self.state.matches[match_id].get("status")
            in ("wrong_patch", "outside_patch_window")
            or self.state.matches[match_id].get("status")
            == "newer_patch_transition"
        }
        examined_terminal_ids = (
            self._downloaded_match_ids | self._known_terminal_match_ids
        )
        reused_without_download_ids = (
            self._known_terminal_match_ids - self._downloaded_match_ids
        )
        wrong_reused_without_download_ids = (
            self._known_wrong_patch_match_ids - self._downloaded_match_ids
        )
        efficiency = calculate_efficiency_metrics(
            payloads_downloaded=len(self._downloaded_match_ids),
            newly_downloaded_accepted=len(downloaded_accepted_ids),
            accepted_reused=len(reused_accepted_ids),
            newly_downloaded_wrong_patch=len(downloaded_wrong_patch_ids),
            known_terminal_matches_reused_without_download=len(
                reused_without_download_ids
            ),
            known_wrong_patch_matches_reused_without_download=len(
                wrong_reused_without_download_ids
            ),
            examined_terminal_matches=len(examined_terminal_ids),
        )
        accepted_counts_by_patch = self._accepted_counts_by_patch()
        target_patch_matches = accepted_counts_by_patch.get(
            self.config.target_public_patch, 0
        )
        previous_patch_matches = sum(
            count
            for patch, count in accepted_counts_by_patch.items()
            if patch != self.config.target_public_patch
        )
        target_patch_credited_ids = {
            match_id
            for match_id in credited_target_ids
            if self.state.matches[match_id].get("public_patch")
            == self.config.target_public_patch
        }
        values: dict[str, object] = {
            "target_public_patch": self.config.target_public_patch,
            "patch_window_size": self.config.patch_window_size,
            "accepted_public_patches": list(self.config.accepted_public_patches),
            "accepted_matches_by_public_patch": accepted_counts_by_patch,
            "platform": self.config.platform.lower(),
            "regional_routing": self.config.regional_routing,
            "sampling_strategy": self.config.sampling_strategy,
            "rank_representative": False,
            "sampled_distribution": dict(sorted(strata.items())),
            "planned_player_distribution": planned_player_distribution(self.config),
            "players_examined_by_tier": dict(sorted(tiers.items())),
            "players_examined_by_division": dict(sorted(divisions.items())),
            "strata_planned": len(build_sampling_schedule(self.config)),
            "strata_visited": dict(sorted(strata.items())),
            "target_matches_by_contributing_stratum": dict(
                sorted(contributing.items())
            ),
            "accepted_matches_by_contributing_stratum": dict(
                sorted(contributing.items())
            ),
            "unattributed_target_matches": unattributed_target_matches,
            "unattributed_accepted_matches": unattributed_target_matches,
            "minimum_players_per_tier": self.config.minimum_players_per_tier,
            "coverage_goal_met": coverage_met,
            "players_examined": len(self.state.players),
            "match_ids_discovered": len(self.state.matches),
            "duplicate_match_ids": int(self.state.payload["overlap_events"]),
            "cross_location_duplicate_match_ids": statuses[
                "external_duplicate"
            ],
            "already_cataloged_matches": statuses["already_cataloged_target"]
            + statuses["already_cataloged_accepted"]
            + statuses["already_cataloged_other"],
            "already_downloaded_matches": self._initial_downloaded,
            "payloads_downloaded": len(self._downloaded_match_ids),
            "accepted_matches": self._accepted_count(),
            "total_accepted_matches_credited": self._accepted_count(),
            "accepted_matches_credited_this_run": len(credited_target_ids),
            "accepted_target_patch_matches": target_patch_matches,
            "total_target_patch_matches_credited": target_patch_matches,
            "accepted_previous_patch_matches": previous_patch_matches,
            "target_patch_matches_credited_this_run": len(
                target_patch_credited_ids
            ),
            "newly_downloaded_accepted_matches": len(downloaded_accepted_ids),
            "accepted_matches_reused_this_run": len(reused_accepted_ids),
            "accepted_matches_reused_from_catalog_this_run": len(
                catalog_reused_accepted_ids
            ),
            "accepted_matches_reused_from_raw_cache_this_run": len(
                raw_reused_accepted_ids
            ),
            "accepted_matches_reused_from_checkpoint_state_this_run": len(
                checkpoint_reused_accepted_ids
            ),
            "wrong_patch_matches": self._wrong_patch_count(),
            "total_wrong_patch_matches_observed": self._wrong_patch_count(),
            "outside_patch_window_matches": self._wrong_patch_count(),
            "newer_patch_transition_matches": statuses[
                "newer_patch_transition"
            ]
            + statuses["newer_patch_transition_cached"],
            "newly_downloaded_wrong_patch_matches": len(
                downloaded_wrong_patch_ids
            ),
            "unresolved_patch_matches": statuses["unresolved_patch"],
            "malformed_matches": malformed,
            "rejected_matches": rejected,
            "accepted_matches_per_player_examined": _ratio(
                self._accepted_count(), len(self.state.players)
            ),
            "request_metrics": self.state.payload["request_metrics"],
            "elapsed_seconds": round(elapsed, 6),
            "target_reached": target_reached,
            "completion_status": self._stop_reason,
            "patch_transition": self.state.payload.get("patch_transition"),
            "efficiency_metric_definitions": EFFICIENCY_METRIC_DEFINITIONS,
        }
        values.update(efficiency)
        return PopulationSummary(values)

    def _write_manifest(self, summary: PopulationSummary) -> None:
        accepted_files = [
            record["raw_path"]
            for record in self.state.matches.values()
            if record.get("status") == "accepted" and "raw_path" in record
        ]
        atomic_write_json(
            self.raw_snapshot_dir / "manifest.json",
            {
                "schema_version": 5,
                "run_id": self.state.payload["run_id"],
                "created_at": datetime.now(UTC).isoformat(),
                "platform": self.config.platform.lower(),
                "regional_routing": self.config.regional_routing,
                "routing_region": self.config.analysis_region,
                "target_public_patch": self.config.target_public_patch,
                "patch_window_size": self.config.patch_window_size,
                "accepted_public_patches": list(
                    self.config.accepted_public_patches
                ),
                "accepted_matches_by_public_patch": (
                    self._accepted_counts_by_patch()
                ),
                "configuration": self.config.non_sensitive_dict(),
                "match_files": accepted_files,
                "summary": summary.as_dict(),
                "lineage_policy_version": COLLECTION_LINEAGE_POLICY_VERSION,
                "lineage_preservation_enabled": True,
            },
        )


def build_plan(config: PopulationConfig) -> dict[str, object]:
    """Return a non-sensitive dry-run plan without constructing an API client."""

    return {
        "mode": "dry-run",
        "network_requests": 0,
        "configuration": config.non_sensitive_dict(),
        "strata": len(config.tiers) * len(config.divisions),
        "planned_player_distribution": planned_player_distribution(config),
        "rank_representative": False,
        "queue_id": 420,
        "professional_esports_data": False,
    }


def build_sampling_schedule(
    config: PopulationConfig,
) -> list[tuple[str, str, int]]:
    """Build a deterministic fast or tier-interleaved discovery schedule."""

    random_source = random.Random(config.seed)
    tiers = [tier.upper() for tier in config.tiers]
    divisions = [division.upper() for division in config.divisions]
    if config.sampling_strategy == "fast":
        strata = [(tier, division) for tier in tiers for division in divisions]
        random_source.shuffle(strata)
    else:
        random_source.shuffle(tiers)
        per_tier: dict[str, list[str]] = {}
        for tier in tiers:
            tier_divisions = list(divisions)
            random_source.shuffle(tier_divisions)
            per_tier[tier] = tier_divisions
        strata = [
            (tier, per_tier[tier][index])
            for index in range(len(divisions))
            for tier in tiers
        ]
    return [
        (tier, division, random_source.randint(1, config.max_start_page))
        for tier, division in strata
    ]


def planned_player_distribution(config: PopulationConfig) -> dict[str, int]:
    """Allocate the player bound over the deterministic schedule for reporting."""

    schedule = build_sampling_schedule(config)
    if not schedule:
        return {}
    counts: Counter[str] = Counter()
    if config.sampling_strategy == "fast":
        counts[f"{schedule[0][0]} {schedule[0][1]}"] = config.max_players
    else:
        for index in range(config.max_players):
            tier, division, _ = schedule[index % len(schedule)]
            counts[f"{tier} {division}"] += 1
    return dict(sorted(counts.items()))


def new_run_id(now: datetime | None = None) -> str:
    timestamp = now or datetime.now(UTC)
    return timestamp.strftime("%Y%m%dT%H%M%S%fZ-population")


def _merge_request_metrics(
    previous: dict[str, object], current: dict[str, object]
) -> dict[str, object]:
    integer_fields = (
        "attempted_requests",
        "successful_requests",
        "retries",
        "responses_429",
        "responses_5xx",
        "authentication_failures",
    )
    duration_fields = (
        "total_backoff_seconds",
        "proactive_backoff_seconds",
        "reactive_retry_after_seconds",
        "transient_backoff_seconds",
    )
    merged: dict[str, object] = {
        field: int(previous.get(field, 0)) + int(current.get(field, 0))
        for field in integer_fields
    }
    merged.update(
        {
            field: round(
                float(previous.get(field, 0)) + float(current.get(field, 0)), 6
            )
            for field in duration_fields
        }
    )
    previous_classified = sum(
        float(previous.get(field, 0)) for field in duration_fields[1:]
    )
    legacy_unclassified = float(
        previous.get(
            "legacy_unclassified_backoff_seconds",
            max(
                0.0,
                float(previous.get("total_backoff_seconds", 0))
                - previous_classified,
            ),
        )
    )
    merged["legacy_unclassified_backoff_seconds"] = round(
        legacy_unclassified
        + float(current.get("legacy_unclassified_backoff_seconds", 0)),
        6,
    )
    for field in (
        "requests_by_endpoint",
        "proactive_backoff_by_endpoint",
        "reactive_retry_after_by_endpoint",
    ):
        combined = Counter(previous.get(field, {})) + Counter(current.get(field, {}))
        merged[field] = dict(sorted(combined.items()))
    merged["observed_rate_limit_headers"] = sorted(
        set(previous.get("observed_rate_limit_headers", []))
        | set(current.get("observed_rate_limit_headers", []))
    )
    merged["total_backoff_definition"] = (
        "proactive bucket waits + reactive Retry-After + transient retry backoff; "
        "legacy unclassified waits are reported separately"
    )
    return merged


def _is_older_patch(candidate: str, target: str) -> bool:
    try:
        return tuple(map(int, candidate.split("."))) < tuple(
            map(int, target.split("."))
        )
    except ValueError:
        return False


def _is_newer_patch(candidate: str, target: str) -> bool:
    try:
        return tuple(map(int, candidate.split("."))) > tuple(
            map(int, target.split("."))
        )
    except ValueError:
        return False


def calculate_efficiency_metrics(
    *,
    payloads_downloaded: int,
    newly_downloaded_accepted: int,
    accepted_reused: int,
    newly_downloaded_wrong_patch: int,
    known_terminal_matches_reused_without_download: int,
    known_wrong_patch_matches_reused_without_download: int,
    examined_terminal_matches: int,
) -> dict[str, object]:
    """Build non-overlapping, explicitly denominated collection metrics."""

    target_matches_credited_this_run = newly_downloaded_accepted + accepted_reused
    other_downloaded_outcomes = (
        payloads_downloaded
        - newly_downloaded_accepted
        - newly_downloaded_wrong_patch
    )
    if other_downloaded_outcomes < 0:
        raise ValueError("download outcome counts exceed downloaded payloads")
    if (
        known_wrong_patch_matches_reused_without_download
        > known_terminal_matches_reused_without_download
    ):
        raise ValueError("wrong-patch reuse must be a subset of terminal reuse")
    return {
        "known_terminal_matches_reused_without_download": (
            known_terminal_matches_reused_without_download
        ),
        "known_wrong_patch_matches_reused_without_download": (
            known_wrong_patch_matches_reused_without_download
        ),
        "downloaded_payloads_with_other_outcome": other_downloaded_outcomes,
        "examined_terminal_matches_this_run": examined_terminal_matches,
        "new_download_acceptance_rate": _ratio(
            newly_downloaded_accepted, payloads_downloaded
        ),
        "new_download_wrong_patch_rate": _ratio(
            newly_downloaded_wrong_patch, payloads_downloaded
        ),
        "overall_examined_match_acceptance_rate": _ratio(
            target_matches_credited_this_run, examined_terminal_matches
        ),
        "new_payloads_per_newly_downloaded_accepted_match": _ratio(
            payloads_downloaded, newly_downloaded_accepted
        ),
        "new_payloads_per_accepted_match_credited_this_run": _ratio(
            payloads_downloaded, target_matches_credited_this_run
        ),
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _stratum_key(tier: str, division: str, start_page: int) -> str:
    return f"{tier}:{division}:{start_page}"


def _collection_context(*, tier: str, division: str) -> dict[str, object]:
    normalized_tier = tier.upper()
    normalized_division = division.upper()
    return {
        "collection_tier": normalized_tier,
        "collection_division": normalized_division,
        "collection_stratum": f"{normalized_tier} {normalized_division}",
        "collection_context_status": "collection_context",
        "source": "league_v4_ladder_sampling_schedule",
    }


def _rank_observation(
    entry: LeagueEntry, *, rank_observed_at: str | None
) -> dict[str, object]:
    observed = bool(entry.tier and entry.rank and entry.queueType == "RANKED_SOLO_5x5")
    return {
        "rank_tier": entry.tier.upper() if observed else None,
        "rank_division": entry.rank.upper() if observed else None,
        "queue_id": 420,
        "rank_status": "observed" if observed else "not_collected",
        "rank_source": "league_v4_ranked_solo_ladder_entry" if observed else None,
        "rank_observed_at": rank_observed_at if observed else None,
        "rank_observed_at_status": (
            "observed"
            if observed and rank_observed_at is not None
            else "not_collected"
        ),
    }


def _merge_player_lineage(
    player: dict[str, Any],
    *,
    entry: LeagueEntry,
    tier: str,
    division: str,
    rank_observed_at: str | None,
) -> None:
    contexts = player.setdefault("collection_contexts", [])
    context = _collection_context(tier=tier, division=division)
    if context not in contexts:
        contexts.append(context)
        contexts.sort(
            key=lambda item: (
                str(item.get("collection_tier")),
                str(item.get("collection_division")),
            )
        )
    observations = player.setdefault("rank_observations", [])
    observation = _rank_observation(entry, rank_observed_at=rank_observed_at)
    identity = (
        observation["rank_tier"],
        observation["rank_division"],
        observation["rank_source"],
    )
    if not any(
        (
            item.get("rank_tier"),
            item.get("rank_division"),
            item.get("rank_source"),
        )
        == identity
        for item in observations
    ):
        observations.append(observation)
        observations.sort(
            key=lambda item: (
                str(item.get("rank_tier")),
                str(item.get("rank_division")),
                str(item.get("rank_source")),
            )
        )


def _repair_legacy_player_lineage(
    players: dict[str, dict[str, Any]], sampling: dict[str, Any]
) -> None:
    candidates_by_puuid: dict[str, list[tuple[LeagueEntry, str | None]]] = defaultdict(
        list
    )
    observed_at = sampling.get("candidate_observed_at", {})
    for stratum, candidates in sampling.get("candidates", {}).items():
        timestamp = observed_at.get(stratum)
        for candidate in candidates:
            try:
                entry = LeagueEntry.model_validate(candidate)
            except ValidationError:
                continue
            if entry.puuid:
                candidates_by_puuid[entry.puuid].append(
                    (entry, str(timestamp) if timestamp else None)
                )
    for puuid, player in players.items():
        player.setdefault("seed_player_key", pseudonymize_puuid(puuid))
        tier = str(player.get("tier") or "")
        division = str(player.get("division") or "")
        if tier and division:
            contexts = player.setdefault("collection_contexts", [])
            context = _collection_context(tier=tier, division=division)
            if context not in contexts:
                contexts.append(context)
        player.setdefault("rank_observations", [])
        for entry, timestamp in candidates_by_puuid.get(puuid, []):
            _merge_player_lineage(
                player,
                entry=entry,
                tier=tier or str(entry.tier or ""),
                division=division or str(entry.rank or ""),
                rank_observed_at=timestamp,
            )


def _match_discovery_source(
    *,
    puuid: str,
    player: dict[str, Any],
    tier: str,
    division: str,
    platform: str,
    regional_routing: str,
    analysis_region: str,
) -> dict[str, Any]:
    rank_observations = sorted(
        player.get("rank_observations", []),
        key=lambda item: (
            str(item.get("rank_tier")),
            str(item.get("rank_division")),
            str(item.get("rank_source")),
        ),
    )
    observed_ranks = {
        (item.get("rank_tier"), item.get("rank_division"))
        for item in rank_observations
        if item.get("rank_status") == "observed"
    }
    if len(observed_ranks) == 1:
        rank_tier, rank_division = next(iter(observed_ranks))
        rank_status = "observed"
    elif observed_ranks:
        rank_tier = rank_division = None
        rank_status = "ambiguous"
    else:
        rank_tier = rank_division = None
        rank_status = "not_collected"
    context = _collection_context(tier=tier, division=division)
    return {
        "puuid": puuid,
        "seed_player_key": player.get("seed_player_key") or pseudonymize_puuid(puuid),
        "platform_id": platform,
        "platform_status": "observed",
        "regional_routing": regional_routing,
        "regional_routing_status": "collection_context",
        "analysis_region": analysis_region,
        "analysis_region_status": "derived",
        "tier": tier,
        "division": division,
        **context,
        "seed_rank_tier": rank_tier,
        "seed_rank_division": rank_division,
        "seed_rank_status": rank_status,
        "seed_rank_observations": rank_observations,
        "discovery_timestamp": datetime.now(UTC).isoformat(),
        "discovery_timestamp_status": "observed",
        "discovery_source": "match_v5_history_by_seed_puuid",
        "discovery_source_status": "observed",
        "lineage_policy_version": COLLECTION_LINEAGE_POLICY_VERSION,
    }


def _source_identity(source: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(source.get("seed_player_key") or source.get("puuid") or ""),
        str(source.get("collection_tier") or source.get("tier") or ""),
        str(source.get("collection_division") or source.get("division") or ""),
        str(source.get("platform_id") or ""),
    )


def _source_sort_key(source: dict[str, Any]) -> tuple[str, ...]:
    return (*_source_identity(source), str(source.get("discovery_timestamp") or ""))


def _safe(value: str) -> str:
    return _SAFE_FILENAME.sub("_", value)
