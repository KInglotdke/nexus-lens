import json
from pathlib import Path

import pytest

from nexus_lens.collection_planning import (
    ExpandedCollectionPlanConfig,
    build_expanded_collection_plan,
    load_expanded_collection_config,
)


def make_config(**overrides: object) -> ExpandedCollectionPlanConfig:
    values: dict[str, object] = {
        "platforms": ("eun1", "euw1"),
        "target_public_patch": "26.14",
        "patch_window_size": 2,
        "tiers": ("GOLD", "PLATINUM"),
        "divisions": ("I", "II"),
        "target_matches_per_platform": 10,
        "max_players_per_platform": 20,
        "max_match_ids_per_platform": 100,
        "max_requests_per_platform": 200,
    }
    values.update(overrides)
    return ExpandedCollectionPlanConfig(**values)  # type: ignore[arg-type]


def test_eune_and_euw_routes_are_separate_and_queue_420() -> None:
    plan = build_expanded_collection_plan(make_config(), environment={})
    by_platform = {item["platform_id"]: item for item in plan["platform_plans"]}

    assert by_platform["eun1"]["regional_routing"] == "europe"
    assert by_platform["eun1"]["analysis_region"] == "eune"
    assert by_platform["euw1"]["regional_routing"] == "europe"
    assert by_platform["euw1"]["analysis_region"] == "euw"
    assert {item["queue_id"] for item in by_platform.values()} == {420}
    assert (
        by_platform["eun1"]["resumable_checkpoint"]
        != by_platform["euw1"]["resumable_checkpoint"]
    )


def test_plan_is_deterministic_balanced_and_makes_no_network_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(target_matches_per_platform=11)

    def forbidden_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dry-run planner attempted network access")

    monkeypatch.setattr("socket.create_connection", forbidden_network)
    first = build_expanded_collection_plan(config, environment={})
    second = build_expanded_collection_plan(config, environment={})

    assert first == second
    assert first["network_requests_made"] == 0
    for platform in first["platform_plans"]:
        allocation = platform["planning_target_by_stratum"]
        assert sum(allocation.values()) == 11
        assert platform["sampling_strategy"] == "balanced"
        assert platform["history_traversal"] == "newest_first"
        assert platform["accepted_public_patches_newest_first"] == [
            "26.14",
            "26.13",
        ]
        assert platform["lineage_preservation_enabled"] is True


def test_configuration_presence_does_not_read_or_render_secret() -> None:
    secret = "do-not-render-this-secret"

    plan = build_expanded_collection_plan(
        make_config(platforms=("eun1",)),
        environment={"NEXUS_LENS_RIOT_API_KEY": secret},
    )
    rendered = json.dumps(plan)

    assert plan["required_configuration"]["riot_api_key_present"] is True
    assert plan["required_configuration"]["secret_value_read"] is False
    assert secret not in rendered


def test_config_loader_is_strict_and_contains_no_secret(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps(
            {
                "platforms": ["euw1"],
                "target_public_patch": "26.14",
                "target_matches_per_platform": 1_000,
            }
        ),
        encoding="utf-8",
    )

    config = load_expanded_collection_config(path)

    assert config.platforms == ("euw1",)
    assert config.target_matches_per_platform == 1_000
    path.write_text(
        json.dumps(
            {
                "platforms": ["euw1"],
                "target_public_patch": "26.14",
                "NEXUS_LENS_RIOT_API_KEY": "forbidden",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown fields"):
        load_expanded_collection_config(path)


def test_external_roots_deduplication_and_transition_guard_are_planned(
    tmp_path: Path,
) -> None:
    index = tmp_path / "private" / "dedup.sqlite3"
    config = make_config(
        platforms=("eun1",),
        patch_window_size=1,
        stop_on_newer_patch=True,
        deduplication_indexes=(index,),
        raw_root=tmp_path / "raw",
        processed_root=tmp_path / "processed",
        snapshot_root=tmp_path / "snapshots",
    )

    plan = build_expanded_collection_plan(config, environment={})
    platform = plan["platform_plans"][0]

    assert platform["accepted_public_patches_newest_first"] == ["26.14"]
    assert platform["patch_transition_guard"] == {
        "stop_on_newer_patch": True,
        "accepted_patch_is_exact": True,
    }
    assert platform["deduplication"]["private_cross_location_indexes"] == [
        index.as_posix()
    ]


def test_unsupported_platform_and_non_queue_scope_cannot_be_planned() -> None:
    with pytest.raises(ValueError, match="only eun1 and euw1"):
        make_config(platforms=("na1",))
