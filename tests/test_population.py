import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from nexus_lens.catalog import ProcessingCatalog
from nexus_lens.population import (
    CheckpointCompatibilityError,
    PopulationCollector,
    PopulationConfig,
    build_plan,
    build_sampling_schedule,
    calculate_efficiency_metrics,
    discover_league_entries,
    validate_checkpoint_extension,
)
from nexus_lens.population_state import PopulationState, atomic_write_json
from nexus_lens.riot_client import RequestMetrics, RiotRetryExhausted
from nexus_lens.schemas import LeagueEntry, RiotMatch, SummonerRecord
from tests.factories import make_match_payload


class StubPopulationClient:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str, int], list[LeagueEntry]] = {}
        self.histories: dict[str, list[str]] = {}
        self.matches: dict[str, RiotMatch | Exception] = {}
        self.summoners: dict[str, str] = {}
        self.metrics = RequestMetrics()
        self.league_pages: list[int] = []
        self.summoner_calls = 0
        self.match_calls: Counter[str] = Counter()
        self.history_calls: list[tuple[str, int, int]] = []
        self.match_order: list[str] = []
        self.active_match_requests = 0
        self.maximum_active_match_requests = 0
        self.match_delay = 0.0

    async def get_league_entries(
        self,
        tier: str,
        division: str,
        *,
        page: int = 1,
    ) -> list[LeagueEntry]:
        self.league_pages.append(page)
        return list(self.entries.get((tier, division, page), []))

    async def get_summoner_by_id(self, encrypted_id: str) -> SummonerRecord:
        self.summoner_calls += 1
        return SummonerRecord(puuid=self.summoners[encrypted_id])

    async def get_match_ids_by_puuid(
        self,
        puuid: str,
        *,
        start: int = 0,
        count: int = 5,
    ) -> list[str]:
        self.history_calls.append((puuid, start, count))
        return self.histories.get(puuid, [])[start : start + count]

    async def get_match(self, match_id: str) -> RiotMatch:
        self.match_calls[match_id] += 1
        self.match_order.append(match_id)
        self.active_match_requests += 1
        self.maximum_active_match_requests = max(
            self.maximum_active_match_requests,
            self.active_match_requests,
        )
        if self.match_delay:
            await asyncio.sleep(self.match_delay)
        self.active_match_requests -= 1
        result = self.matches[match_id]
        if isinstance(result, Exception):
            raise result
        return result


def make_config(**overrides: object) -> PopulationConfig:
    values: dict[str, object] = {
        "platform": "eun1",
        "target_public_patch": "26.14",
        "patch_window_size": 1,
        "tiers": ("GOLD",),
        "divisions": ("I",),
        "target_matches": 2,
        "max_players": 5,
        "histories_per_player": 10,
        "seed": 42,
        "concurrency": 1,
        "pages_per_stratum": 1,
        "max_start_page": 1,
        "max_match_ids": 20,
        "max_requests": 100,
    }
    values.update(overrides)
    return PopulationConfig(**values)  # type: ignore[arg-type]


def make_state(tmp_path: Path, config: PopulationConfig) -> PopulationState:
    return PopulationState.create(
        tmp_path / "state" / "checkpoint.json",
        run_id="SYNTHETIC_RUN",
        config=config.non_sensitive_dict(),
    )


def make_collector(
    tmp_path: Path,
    config: PopulationConfig,
    client: StubPopulationClient,
    catalog: ProcessingCatalog,
    state: PopulationState,
) -> PopulationCollector:
    return PopulationCollector(
        config=config,
        client=client,  # type: ignore[arg-type]
        catalog=catalog,
        state=state,
        raw_snapshot_dir=tmp_path / "raw" / "SYNTHETIC_RUN",
        processed_root=tmp_path / "processed",
    )


@pytest.mark.asyncio
async def test_league_discovery_paginates_until_empty() -> None:
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 2)] = [LeagueEntry(puuid="player-a")]
    client.entries[("GOLD", "I", 3)] = [LeagueEntry(puuid="player-b")]

    entries = await discover_league_entries(
        client,  # type: ignore[arg-type]
        tier="GOLD",
        division="I",
        start_page=2,
        max_pages=3,
    )

    assert [entry.puuid for entry in entries] == ["player-a", "player-b"]
    assert client.league_pages == [2, 3, 4]


@pytest.mark.asyncio
async def test_target_patch_boundaries_and_exact_stop(tmp_path: Path) -> None:
    config = make_config(target_matches=2)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["OLD", "UNRESOLVED", "TARGET_A", "TARGET_B"]
    client.matches = {
        "OLD": RiotMatch.model_validate(
            make_match_payload(match_id="OLD", game_version="16.13.1.1")
        ),
        "UNRESOLVED": RiotMatch.model_validate(
            make_match_payload(match_id="UNRESOLVED", game_version="26.14.1.1")
        ),
        "TARGET_A": RiotMatch.model_validate(
            make_match_payload(match_id="TARGET_A", game_version="16.14.1.1")
        ),
        "TARGET_B": RiotMatch.model_validate(
            make_match_payload(match_id="TARGET_B", game_version="16.14.2.2")
        ),
    }
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.accepted_target_patch_matches == 2
    assert summary.wrong_patch_matches == 1
    assert summary.rejected_matches == 1
    assert summary.target_reached is True
    assert sum(client.match_calls.values()) == 4
    assert sum(summary.target_matches_by_contributing_stratum.values()) == 2
    target_files = list(
        (tmp_path / "processed").glob("region=eune/patch=26.14/**/TARGET*")
    )
    assert len(target_files) == 6
    manifest = (tmp_path / "raw" / "SYNTHETIC_RUN" / "manifest.json").read_text(
        encoding="utf-8"
    )
    assert "player-a" not in manifest


@pytest.mark.asyncio
async def test_overlapping_histories_download_match_once(tmp_path: Path) -> None:
    config = make_config(target_matches=2)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [
        LeagueEntry(puuid="player-a"),
        LeagueEntry(puuid="player-b"),
    ]
    client.histories = {"player-a": ["SHARED"], "player-b": ["SHARED"]}
    client.matches["SHARED"] = RiotMatch.model_validate(
        make_match_payload(match_id="SHARED", game_version="16.14.1.1")
    )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.accepted_target_patch_matches == 1
    assert summary.duplicate_match_ids == 1
    assert client.match_calls["SHARED"] == 1
    assert len(state.matches["SHARED"]["sources"]) == 2
    assert sum(summary.target_matches_by_contributing_stratum.values()) == 1
    rendered = json.dumps(summary.as_dict())
    assert "player-a" not in rendered
    assert "player-b" not in rendered


@pytest.mark.asyncio
async def test_future_checkpoint_preserves_observed_seed_lineage(
    tmp_path: Path,
) -> None:
    config = make_config(target_matches=1)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [
        LeagueEntry(
            puuid="player-a",
            tier="GOLD",
            rank="I",
            queueType="RANKED_SOLO_5x5",
        )
    ]
    client.histories["player-a"] = ["TARGET"]
    client.matches["TARGET"] = RiotMatch.model_validate(
        make_match_payload(match_id="TARGET", game_version="16.14.1.1")
    )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        await make_collector(tmp_path, config, client, catalog, state).collect()

    player = state.players["player-a"]
    source = state.matches["TARGET"]["sources"][0]
    assert state.payload["version"] == 4
    assert state.payload["lineage_preservation_enabled"] is True
    assert (
        player["collection_contexts"][0]["collection_context_status"]
        == "collection_context"
    )
    assert player["rank_observations"][0]["rank_status"] == "observed"
    assert player["rank_observations"][0]["rank_observed_at_status"] == "observed"
    assert source["platform_id"] == "eun1"
    assert source["regional_routing"] == "europe"
    assert source["analysis_region"] == "eune"
    assert source["collection_stratum"] == "GOLD I"
    assert source["seed_rank_status"] == "observed"
    assert source["discovery_timestamp_status"] == "observed"
    assert source["seed_player_key"] != "player-a"


@pytest.mark.asyncio
async def test_existing_catalog_target_match_needs_no_download(tmp_path: Path) -> None:
    config = make_config(target_matches=1)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["CATALOGED"]
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        catalog.record_processed(
            match_id="CATALOGED",
            routing_region="eune",
            api_game_version="16.14.1.1",
            api_patch="16.14",
            public_patch="26.14",
            patch_resolution_method="test",
            patch_resolution_status="resolved",
            queue_id=420,
            source_snapshot="older",
        )
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.accepted_target_patch_matches == 1
    assert summary.already_cataloged_matches == 1
    assert summary.payloads_downloaded == 0
    assert summary.target_patch_matches_credited_this_run == 1
    assert summary.newly_downloaded_accepted_matches == 0
    assert summary.accepted_matches_reused_from_catalog_this_run == 1
    assert summary.new_download_acceptance_rate is None
    assert summary.new_payloads_per_newly_downloaded_accepted_match is None
    assert summary.new_payloads_per_accepted_match_credited_this_run == 0.0
    assert client.match_calls == Counter()


@pytest.mark.asyncio
async def test_summoner_fallback_resolves_puuid(tmp_path: Path) -> None:
    config = make_config(target_matches=1)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(summonerId="encrypted")]
    client.summoners["encrypted"] = "resolved-player"
    client.histories["resolved-player"] = ["TARGET"]
    client.matches["TARGET"] = RiotMatch.model_validate(
        make_match_payload(match_id="TARGET", game_version="16.14.1.1")
    )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert client.summoner_calls == 1
    assert summary.target_reached is True


@pytest.mark.asyncio
async def test_failed_payload_is_retried_on_resume(tmp_path: Path) -> None:
    config = make_config(target_matches=1)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["RETRY"]
    client.matches["RETRY"] = RiotRetryExhausted("synthetic transient failure")
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        first = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()
        assert first.target_reached is False
        assert state.matches["RETRY"]["status"] == "request_failed"

        resumed_client = StubPopulationClient()
        resumed_client.matches["RETRY"] = RiotMatch.model_validate(
            make_match_payload(match_id="RETRY", game_version="16.14.1.1")
        )
        second = await make_collector(
            tmp_path, config, resumed_client, catalog, state
        ).collect()

    assert second.target_reached is True
    assert resumed_client.match_calls["RETRY"] == 1


@pytest.mark.asyncio
async def test_concurrency_is_bounded_and_configurable(tmp_path: Path) -> None:
    config = make_config(target_matches=3, concurrency=2)
    client = StubPopulationClient()
    client.match_delay = 0.01
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["A", "B", "C"]
    for match_id in ("A", "B", "C"):
        client.matches[match_id] = RiotMatch.model_validate(
            make_match_payload(match_id=match_id, game_version="16.14.1.1")
        )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.accepted_target_patch_matches == 3
    assert client.maximum_active_match_requests == 2


@pytest.mark.asyncio
async def test_no_target_games_returns_bounded_not_reached(tmp_path: Path) -> None:
    config = make_config(target_matches=1, max_players=1)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = []
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.target_reached is False
    assert summary.completion_status == "maximum_players_reached"
    assert summary.match_ids_discovered == 0


def test_sampling_is_reproducible_and_dry_run_has_no_requests() -> None:
    config = make_config(
        seed=73,
        tiers=("GOLD", "PLATINUM"),
        divisions=("I", "II"),
        max_start_page=5,
    )
    assert build_sampling_schedule(config) == build_sampling_schedule(config)
    assert build_sampling_schedule(config) != build_sampling_schedule(
        make_config(
            seed=74,
            tiers=("GOLD", "PLATINUM"),
            divisions=("I", "II"),
            max_start_page=5,
        )
    )
    plan = build_plan(config)
    assert plan["network_requests"] == 0
    assert plan["queue_id"] == 420


def test_balanced_schedule_interleaves_tiers() -> None:
    config = make_config(
        tiers=("GOLD", "PLATINUM", "EMERALD"),
        divisions=("I", "II"),
        sampling_strategy="balanced",
    )

    schedule = build_sampling_schedule(config)

    assert len({tier for tier, _, _ in schedule[:3]}) == 3
    assert [tier for tier, _, _ in schedule[:3]] == [
        tier for tier, _, _ in build_sampling_schedule(config)[:3]
    ]


@pytest.mark.asyncio
async def test_small_target_reports_structural_not_representative(
    tmp_path: Path,
) -> None:
    config = make_config(target_matches=1)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["TARGET"]
    client.matches["TARGET"] = RiotMatch.model_validate(
        make_match_payload(match_id="TARGET", game_version="16.14.1.1")
    )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.rank_representative is False
    assert summary.coverage_goal_met is True
    assert summary.strata_planned == 1


@pytest.mark.asyncio
async def test_minimum_tier_coverage_is_honored(tmp_path: Path) -> None:
    config = make_config(
        tiers=("GOLD", "PLATINUM"),
        target_matches=2,
        minimum_players_per_tier=1,
        sampling_strategy="balanced",
    )
    client = StubPopulationClient()
    schedule = build_sampling_schedule(config)
    for index, (tier, division, page) in enumerate(schedule):
        puuid = f"player-{index}"
        match_id = f"TARGET_{index}"
        client.entries[(tier, division, page)] = [LeagueEntry(puuid=puuid)]
        client.histories[puuid] = [match_id]
        client.matches[match_id] = RiotMatch.model_validate(
            make_match_payload(match_id=match_id, game_version="16.14.1.1")
        )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.coverage_goal_met is True
    assert summary.players_examined_by_tier == {"GOLD": 1, "PLATINUM": 1}


@pytest.mark.asyncio
async def test_newest_first_processing_stops_after_older_patch_cutoff(
    tmp_path: Path,
) -> None:
    config = make_config(
        target_matches=2,
        max_players=1,
        initial_history_batch_size=4,
        max_history_per_player=4,
        older_patch_stop_threshold=2,
        histories_per_player=None,
    )
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["TARGET", "OLD_1", "OLD_2", "NEVER"]
    for match_id, version in (
        ("TARGET", "16.14.1.1"),
        ("OLD_1", "16.13.1.1"),
        ("OLD_2", "16.13.2.2"),
        ("NEVER", "16.12.1.1"),
    ):
        client.matches[match_id] = RiotMatch.model_validate(
            make_match_payload(match_id=match_id, game_version=version)
        )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        await make_collector(tmp_path, config, client, catalog, state).collect()

    assert client.match_order == ["TARGET", "OLD_1", "OLD_2"]
    assert client.match_calls["NEVER"] == 0
    assert state.players["player-a"]["status"] == "history_old_patch_cutoff"


@pytest.mark.asyncio
async def test_player_with_no_target_patch_matches_is_cut_off(tmp_path: Path) -> None:
    config = make_config(
        target_matches=1,
        max_players=1,
        initial_history_batch_size=5,
        max_history_per_player=20,
        older_patch_stop_threshold=2,
        histories_per_player=None,
    )
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = [f"OLD_{index}" for index in range(20)]
    for match_id in client.histories["player-a"]:
        client.matches[match_id] = RiotMatch.model_validate(
            make_match_payload(match_id=match_id, game_version="16.13.1.1")
        )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.accepted_target_patch_matches == 0
    assert sum(client.match_calls.values()) == 2


@pytest.mark.asyncio
async def test_history_fetches_another_batch_only_when_needed(tmp_path: Path) -> None:
    config = make_config(
        target_matches=3,
        initial_history_batch_size=2,
        max_history_per_player=4,
        histories_per_player=None,
    )
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["A", "B", "C", "D"]
    for match_id in client.histories["player-a"]:
        client.matches[match_id] = RiotMatch.model_validate(
            make_match_payload(match_id=match_id, game_version="16.14.1.1")
        )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.accepted_target_patch_matches == 3
    assert client.history_calls == [("player-a", 0, 2), ("player-a", 2, 2)]
    assert client.match_order == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_old_patch_cutoff_moves_to_next_stratum_player(tmp_path: Path) -> None:
    config = make_config(
        tiers=("GOLD", "PLATINUM"),
        target_matches=1,
        sampling_strategy="balanced",
        older_patch_stop_threshold=2,
    )
    client = StubPopulationClient()
    first, second = build_sampling_schedule(config)[:2]
    old_player = "old-player"
    target_player = "target-player"
    client.entries[first] = [LeagueEntry(puuid=old_player)]
    client.entries[second] = [LeagueEntry(puuid=target_player)]
    client.histories[old_player] = ["OLD_1", "OLD_2"]
    client.histories[target_player] = ["TARGET"]
    for match_id in ("OLD_1", "OLD_2"):
        client.matches[match_id] = RiotMatch.model_validate(
            make_match_payload(match_id=match_id, game_version="16.13.1.1")
        )
    client.matches["TARGET"] = RiotMatch.model_validate(
        make_match_payload(match_id="TARGET", game_version="16.14.1.1")
    )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.target_reached is True
    assert summary.players_examined == 2
    assert state.players[old_player]["status"] == "history_old_patch_cutoff"


@pytest.mark.asyncio
async def test_cached_wrong_patch_is_not_redownloaded(tmp_path: Path) -> None:
    config = make_config(target_matches=1)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["KNOWN_OLD", "TARGET"]
    client.matches["TARGET"] = RiotMatch.model_validate(
        make_match_payload(match_id="TARGET", game_version="16.14.1.1")
    )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        catalog.record_rejected(
            match_id="KNOWN_OLD",
            routing_region="eune",
            source_snapshot="older-run",
            queue_id=420,
            failure_code="wrong_patch",
            failure_reason="synthetic",
            api_game_version="16.13.1.1",
            api_patch="16.13",
            public_patch="26.13",
            patch_resolution_method="test",
            patch_resolution_status="resolved",
        )
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert client.match_calls["KNOWN_OLD"] == 0
    assert summary.known_wrong_patch_matches_reused_without_download == 1
    assert summary.newly_downloaded_wrong_patch_matches == 0
    assert summary.payloads_downloaded == 1


def test_resumed_state_preserves_seeded_schedule_and_cursor(tmp_path: Path) -> None:
    config = make_config(tiers=("GOLD", "PLATINUM"), divisions=("I", "II"))
    state = make_state(tmp_path, config)
    client = StubPopulationClient()
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        make_collector(tmp_path, config, client, catalog, state)
    state.payload["sampling"]["cursor"] = 3
    state.save()

    resumed = PopulationState.load(state.path)
    original_schedule = resumed.payload["sampling"]["schedule"]
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        make_collector(tmp_path, config, client, catalog, resumed)

    assert resumed.payload["sampling"]["cursor"] == 3
    assert resumed.payload["sampling"]["schedule"] == original_schedule


@pytest.mark.asyncio
async def test_impossible_coverage_terminates_bounded(tmp_path: Path) -> None:
    config = make_config(
        tiers=("GOLD", "PLATINUM"),
        target_matches=1,
        minimum_players_per_tier=1,
        sampling_strategy="balanced",
    )
    client = StubPopulationClient()
    first = build_sampling_schedule(config)[0]
    client.entries[first] = [LeagueEntry(puuid="only-player")]
    client.histories["only-player"] = ["TARGET"]
    client.matches["TARGET"] = RiotMatch.model_validate(
        make_match_payload(match_id="TARGET", game_version="16.14.1.1")
    )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.target_reached is True
    assert summary.coverage_goal_met is False
    assert summary.completion_status == "target_reached_coverage_incomplete"


@pytest.mark.asyncio
async def test_efficiency_metrics_use_current_run_payloads(tmp_path: Path) -> None:
    config = make_config(target_matches=1)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["OLD", "TARGET"]
    client.matches["OLD"] = RiotMatch.model_validate(
        make_match_payload(match_id="OLD", game_version="16.13.1.1")
    )
    client.matches["TARGET"] = RiotMatch.model_validate(
        make_match_payload(match_id="TARGET", game_version="16.14.1.1")
    )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.new_download_acceptance_rate == 0.5
    assert summary.new_download_wrong_patch_rate == 0.5
    assert summary.accepted_matches_per_player_examined == 1.0
    assert summary.new_payloads_per_newly_downloaded_accepted_match == 2.0
    assert summary.new_payloads_per_accepted_match_credited_this_run == 2.0


def test_live_shape_efficiency_reconciliation_is_explicit() -> None:
    metrics = calculate_efficiency_metrics(
        payloads_downloaded=32,
        newly_downloaded_accepted=4,
        accepted_reused=6,
        newly_downloaded_wrong_patch=28,
        known_terminal_matches_reused_without_download=6,
        known_wrong_patch_matches_reused_without_download=0,
        examined_terminal_matches=38,
    )

    assert metrics["new_download_acceptance_rate"] == 0.125
    assert metrics["new_download_wrong_patch_rate"] == 0.875
    assert metrics["overall_examined_match_acceptance_rate"] == 0.263158
    assert metrics["new_payloads_per_newly_downloaded_accepted_match"] == 8.0
    assert metrics["new_payloads_per_accepted_match_credited_this_run"] == 3.2
    assert metrics["downloaded_payloads_with_other_outcome"] == 0


def test_retained_live_summary_reconciles_disjoint_outcomes() -> None:
    metrics = calculate_efficiency_metrics(
        payloads_downloaded=32,
        newly_downloaded_accepted=6,
        accepted_reused=4,
        newly_downloaded_wrong_patch=26,
        known_terminal_matches_reused_without_download=6,
        known_wrong_patch_matches_reused_without_download=2,
        examined_terminal_matches=38,
    )

    assert metrics["new_download_acceptance_rate"] == 0.1875
    assert metrics["new_download_wrong_patch_rate"] == 0.8125
    assert metrics["overall_examined_match_acceptance_rate"] == 0.263158
    assert metrics["new_payloads_per_newly_downloaded_accepted_match"] == 5.333333
    assert metrics["new_payloads_per_accepted_match_credited_this_run"] == 3.2


def test_entirely_fresh_efficiency_metrics() -> None:
    metrics = calculate_efficiency_metrics(
        payloads_downloaded=5,
        newly_downloaded_accepted=5,
        accepted_reused=0,
        newly_downloaded_wrong_patch=0,
        known_terminal_matches_reused_without_download=0,
        known_wrong_patch_matches_reused_without_download=0,
        examined_terminal_matches=5,
    )

    assert metrics["new_download_acceptance_rate"] == 1.0
    assert metrics["overall_examined_match_acceptance_rate"] == 1.0
    assert metrics["new_payloads_per_newly_downloaded_accepted_match"] == 1.0


def test_entirely_cached_and_zero_acceptance_metrics() -> None:
    cached = calculate_efficiency_metrics(
        payloads_downloaded=0,
        newly_downloaded_accepted=0,
        accepted_reused=5,
        newly_downloaded_wrong_patch=0,
        known_terminal_matches_reused_without_download=5,
        known_wrong_patch_matches_reused_without_download=0,
        examined_terminal_matches=5,
    )
    rejected = calculate_efficiency_metrics(
        payloads_downloaded=2,
        newly_downloaded_accepted=0,
        accepted_reused=0,
        newly_downloaded_wrong_patch=2,
        known_terminal_matches_reused_without_download=0,
        known_wrong_patch_matches_reused_without_download=0,
        examined_terminal_matches=2,
    )

    assert cached["new_download_acceptance_rate"] is None
    assert cached["new_payloads_per_newly_downloaded_accepted_match"] is None
    assert cached["new_payloads_per_accepted_match_credited_this_run"] == 0.0
    assert cached["overall_examined_match_acceptance_rate"] == 1.0
    assert rejected["new_download_acceptance_rate"] == 0.0
    assert rejected["new_payloads_per_newly_downloaded_accepted_match"] is None
    assert rejected["new_payloads_per_accepted_match_credited_this_run"] is None


@pytest.mark.asyncio
async def test_raw_cache_takes_precedence_for_pending_checkpoint_match(
    tmp_path: Path,
) -> None:
    config = make_config(target_matches=1)
    client = StubPopulationClient()
    state = make_state(tmp_path, config)
    raw_relative = Path("downloads/OVERLAP.json")
    state.matches["OVERLAP"] = {
        "status": "pending",
        "sources": [
            {"puuid": "synthetic", "tier": "GOLD", "division": "I"}
        ],
        "raw_path": raw_relative.as_posix(),
    }
    atomic_write_json(
        tmp_path / "raw" / "SYNTHETIC_RUN" / raw_relative,
        make_match_payload(match_id="OVERLAP", game_version="16.14.1.1"),
    )
    state.save()
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        catalog.record_processed(
            match_id="OVERLAP",
            routing_region="eune",
            api_game_version="16.14.1.1",
            api_patch="16.14",
            public_patch="26.14",
            patch_resolution_method="test",
            patch_resolution_status="resolved",
            queue_id=420,
            source_snapshot="older",
        )
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.payloads_downloaded == 0
    assert summary.newly_downloaded_accepted_matches == 0
    assert summary.accepted_matches_reused_from_raw_cache_this_run == 1
    assert summary.accepted_matches_reused_from_catalog_this_run == 0
    assert summary.accepted_matches_reused_from_checkpoint_state_this_run == 0
    assert summary.known_terminal_matches_reused_without_download == 1
    assert summary.total_target_patch_matches_credited == 1
    assert client.match_calls == Counter()


@pytest.mark.asyncio
async def test_completed_resume_does_not_credit_target_twice(tmp_path: Path) -> None:
    config = make_config(target_matches=1)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["TARGET"]
    client.matches["TARGET"] = RiotMatch.model_validate(
        make_match_payload(match_id="TARGET", game_version="16.14.1.1")
    )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        first = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()
        resumed_client = StubPopulationClient()
        resumed = await make_collector(
            tmp_path, config, resumed_client, catalog, state
        ).collect()

    assert first.target_patch_matches_credited_this_run == 1
    assert first.total_target_patch_matches_credited == 1
    assert resumed.target_patch_matches_credited_this_run == 0
    assert resumed.total_target_patch_matches_credited == 1
    assert resumed.payloads_downloaded == 0
    assert resumed.new_download_acceptance_rate is None
    assert resumed_client.match_calls == Counter()


@pytest.mark.asyncio
async def test_two_patch_window_accepts_previous_and_rejects_older(
    tmp_path: Path,
) -> None:
    config = make_config(
        patch_window_size=2,
        target_matches=3,
        max_players=1,
        histories_per_player=None,
        initial_history_batch_size=5,
        max_history_per_player=5,
        older_patch_stop_threshold=2,
    )
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["P14", "P13", "P12_A", "P12_B", "NEVER"]
    for match_id, version in (
        ("P14", "16.14.1.1"),
        ("P13", "16.13.1.1"),
        ("P12_A", "16.12.1.1"),
        ("P12_B", "16.12.2.2"),
        ("NEVER", "16.11.1.1"),
    ):
        client.matches[match_id] = RiotMatch.model_validate(
            make_match_payload(match_id=match_id, game_version=version)
        )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.accepted_public_patches == ["26.14", "26.13"]
    assert summary.accepted_matches_by_public_patch == {"26.13": 1, "26.14": 1}
    assert summary.accepted_target_patch_matches == 1
    assert summary.accepted_previous_patch_matches == 1
    assert summary.outside_patch_window_matches == 2
    assert client.match_calls["NEVER"] == 0
    assert state.players["player-a"]["status"] == "history_old_patch_cutoff"


@pytest.mark.asyncio
async def test_two_patch_window_stops_exactly_across_both_patches(
    tmp_path: Path,
) -> None:
    config = make_config(patch_window_size=2, target_matches=2)
    client = StubPopulationClient()
    client.entries[("GOLD", "I", 1)] = [LeagueEntry(puuid="player-a")]
    client.histories["player-a"] = ["P14", "P13", "UNUSED"]
    for match_id, version in (
        ("P14", "16.14.1.1"),
        ("P13", "16.13.1.1"),
        ("UNUSED", "16.12.1.1"),
    ):
        client.matches[match_id] = RiotMatch.model_validate(
            make_match_payload(match_id=match_id, game_version=version)
        )
    state = make_state(tmp_path, config)
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, config, client, catalog, state
        ).collect()

    assert summary.accepted_matches == 2
    assert summary.target_reached is True
    assert sum(client.match_calls.values()) == 2
    assert client.match_calls["UNUSED"] == 0
    assert list((tmp_path / "processed").glob("region=eune/patch=26.13/**/P13*"))
    assert list((tmp_path / "processed").glob("region=eune/patch=26.14/**/P14*"))


@pytest.mark.asyncio
async def test_exact_checkpoint_can_seed_broader_window(tmp_path: Path) -> None:
    exact = make_config(target_matches=2, patch_window_size=1)
    state = make_state(tmp_path, exact)
    state.matches.update(
        {
            "P14": {
                "status": "accepted",
                "public_patch": "26.14",
                "sources": [
                    {"puuid": "synthetic", "tier": "GOLD", "division": "I"}
                ],
                "raw_path": "downloads/P14.json",
            },
            "P13": {
                "status": "wrong_patch",
                "public_patch": "26.13",
                "sources": [
                    {"puuid": "synthetic", "tier": "GOLD", "division": "I"}
                ],
                "raw_path": "downloads/P13.json",
            },
        }
    )
    for match_id, version in (("P14", "16.14.1.1"), ("P13", "16.13.1.1")):
        atomic_write_json(
            tmp_path / "raw" / "SYNTHETIC_RUN" / "downloads" / f"{match_id}.json",
            make_match_payload(match_id=match_id, game_version=version),
        )
    state.save()
    broader = make_config(target_matches=2, patch_window_size=2)
    client = StubPopulationClient()
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        summary = await make_collector(
            tmp_path, broader, client, catalog, state
        ).collect()

    assert summary.accepted_matches == 2
    assert summary.accepted_matches_by_public_patch == {"26.13": 1, "26.14": 1}
    assert summary.accepted_matches_reused_from_raw_cache_this_run == 1
    assert summary.payloads_downloaded == 0
    assert state.payload["accepted_public_patches"] == ["26.14", "26.13"]
    assert state.payload["accepted_match_counts_by_public_patch"] == {
        "26.13": 1,
        "26.14": 1,
    }


@pytest.mark.asyncio
async def test_broader_window_reuses_rejected_catalog_raw_payload(
    tmp_path: Path,
) -> None:
    exact = make_config(target_matches=1, patch_window_size=1)
    state = make_state(tmp_path, exact)
    state.matches["P13"] = {
        "status": "wrong_patch_cached",
        "public_patch": "26.13",
        "sources": [
            {"puuid": "synthetic", "tier": "GOLD", "division": "I"}
        ],
    }
    state.save()
    atomic_write_json(
        tmp_path / "raw" / "older" / "downloads" / "P13.json",
        make_match_payload(match_id="P13", game_version="16.13.1.1"),
    )
    broader = make_config(target_matches=1, patch_window_size=2)
    client = StubPopulationClient()
    with ProcessingCatalog(tmp_path / "processed" / "catalog.sqlite3") as catalog:
        catalog.record_rejected(
            match_id="P13",
            routing_region="eune",
            source_snapshot="older",
            queue_id=420,
            failure_code="wrong_patch",
            failure_reason="synthetic exact-patch rejection",
            api_game_version="16.13.1.1",
            api_patch="16.13",
            public_patch="26.13",
            patch_resolution_method="test",
            patch_resolution_status="resolved",
        )
        summary = await make_collector(
            tmp_path, broader, client, catalog, state
        ).collect()

    assert summary.accepted_matches == 1
    assert summary.accepted_previous_patch_matches == 1
    assert summary.accepted_matches_reused_from_raw_cache_this_run == 1
    assert summary.payloads_downloaded == 0
    assert client.match_calls == Counter()
    assert list((tmp_path / "processed").glob("region=eune/patch=26.13/**/P13*"))


def test_checkpoint_allows_monotonic_collection_extension() -> None:
    saved = make_config(
        target_matches=10,
        max_players=25,
        max_match_ids=250,
        max_requests=300,
        minimum_players_per_tier=0,
        patch_window_size=1,
    )
    requested = make_config(
        target_matches=100,
        max_players=100,
        max_match_ids=1_000,
        max_requests=1_000,
        minimum_players_per_tier=3,
        patch_window_size=2,
    )

    restored = validate_checkpoint_extension(
        saved.non_sensitive_dict(), requested
    )

    assert restored.target_matches == 10
    assert restored.accepted_public_patches == ("26.14",)
    assert requested.accepted_public_patches == ("26.14", "26.13")


@pytest.mark.parametrize(
    "override",
    [
        {"platform": "euw1"},
        {"target_public_patch": "26.13"},
        {"seed": 73},
        {"tiers": ("GOLD", "PLATINUM")},
        {"target_matches": 5},
        {"max_requests": 200},
    ],
)
def test_checkpoint_rejects_incompatible_change(
    override: dict[str, object],
) -> None:
    saved = make_config(
        target_matches=10,
        max_requests=300,
        patch_window_size=1,
    )
    requested_values: dict[str, object] = {
        "target_matches": 10,
        "max_requests": 300,
        "patch_window_size": 1,
    }
    requested_values.update(override)
    requested = make_config(**requested_values)

    with pytest.raises(CheckpointCompatibilityError, match="checkpoint incompatible"):
        validate_checkpoint_extension(saved.non_sensitive_dict(), requested)
