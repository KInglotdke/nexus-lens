"""Focused tests for the outcome-blind Stage 3.5A rune addendum."""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nexus_lens.stage34b_operations import validate_public_payload
from nexus_lens.stage35a_runes import (
    RuneAddendumConfig,
    RuneFeatureRow,
    RuneObservation,
    _build_quality,
    _feature_dataset_sha256,
    build_support_audit,
    load_rune_mapping,
    map_focal_perks,
    resolve_data_dragon_version,
)


def test_patch_correct_data_dragon_resolution_uses_mapping_rule() -> None:
    versions = ["16.16.1", "16.15.1", "16.15.2", "15.15.1"]
    assert resolve_data_dragon_version("26.15", versions) == "16.15.2"


def test_keystone_identification_uses_primary_tree_membership_not_position(
    tmp_path: Path,
) -> None:
    config = _mapping_config(tmp_path)
    mapping = load_rune_mapping(config)
    perks = {
        "styles": [
            {
                "description": "primaryStyle",
                "style": 8000,
                "selections": [{"perk": 9111}, {"perk": 8005}],
            },
            {
                "description": "subStyle",
                "style": 8100,
                "selections": [{"perk": 8139}],
            },
        ]
    }

    result = map_focal_perks(perks, mapping)

    assert result == {
        "keystone_id": 8005,
        "keystone_name": "Press the Attack",
        "primary_rune_tree_id": 8000,
        "secondary_rune_tree_id": 8100,
        "mapping_status": "mapped",
    }


def test_unknown_primary_rune_id_is_explicitly_unmapped(tmp_path: Path) -> None:
    mapping = load_rune_mapping(_mapping_config(tmp_path))
    result = map_focal_perks(
        {
            "styles": [
                {
                    "description": "primaryStyle",
                    "style": 8000,
                    "selections": [{"perk": 999999}, {"perk": 9111}],
                },
                {"description": "subStyle", "style": 8100, "selections": []},
            ]
        },
        mapping,
    )

    assert result["mapping_status"] == "unknown_keystone_id"
    assert result["keystone_id"] is None
    assert result["keystone_name"] is None


def test_mapped_keystone_is_retained_when_secondary_tree_is_unavailable(
    tmp_path: Path,
) -> None:
    mapping = load_rune_mapping(_mapping_config(tmp_path))
    result = map_focal_perks(
        {
            "styles": [
                {
                    "description": "primaryStyle",
                    "style": 8000,
                    "selections": [{"perk": 8005}],
                }
            ]
        },
        mapping,
    )

    assert result["keystone_id"] == 8005
    assert result["secondary_rune_tree_id"] is None
    assert result["mapping_status"] == "mapped_secondary_tree_unavailable"


def test_mapping_fingerprint_is_deterministic(tmp_path: Path) -> None:
    config = _mapping_config(tmp_path)
    first = load_rune_mapping(config)
    second = load_rune_mapping(config)
    assert first.mapping_sha256 == second.mapping_sha256
    assert len(first.mapping_sha256) == 64


def test_perspective_reversal_uses_each_new_focal_participants_own_keystone(
    tmp_path: Path,
) -> None:
    mapping = load_rune_mapping(_mapping_config(tmp_path))
    first = map_focal_perks(_perks(8005, 8000, 8100), mapping)
    second = map_focal_perks(_perks(8112, 8100, 8000), mapping)

    assert first["keystone_name"] == "Press the Attack"
    assert second["keystone_name"] == "Electrocute"
    assert not any("opponent" in key for key in first)
    assert not any("opponent" in key for key in second)


def test_exact_20000_alignment_grouping_weights_and_outcome_blind_audit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    observations = _observations()
    features = tuple(row.feature for row in observations)

    quality = _build_quality(features, config)
    audit = build_support_audit(observations, quality, config)
    rendered = json.dumps(audit, sort_keys=True).lower()

    assert quality["exact_20000_row_alignment"] is True
    assert quality["exact_10000_match_groups"] is True
    assert quality["rows_per_match_exactly_two"] is True
    assert quality["scientific_weight_per_match_equals_one"] is True
    assert quality["outcome_conditioned_statistics_calculated"] == 0
    assert quality["predictive_models_fitted"] == 0
    assert audit["scope"]["outcome_conditioned_statistics"] == 0
    assert audit["scope"]["predictive_model_fits"] == 0
    for forbidden in (
        "gold_difference",
        "xp_difference",
        "game_win",
        "win_rate_by_keystone",
    ):
        assert forbidden not in rendered
    assert audit["privacy"]["opponent_rune_fields"] is False
    assert not any("opponent" in name for name in RuneFeatureRow.model_fields)
    validate_public_payload(audit)


def test_derived_rows_link_parent_hash_without_copying_targets(tmp_path: Path) -> None:
    features = tuple(row.feature for row in _observations()[:2])
    first = _feature_dataset_sha256(features)
    second = _feature_dataset_sha256(features)
    rendered = json.dumps(
        [row.model_dump(mode="json") for row in features], sort_keys=True
    ).lower()

    assert first == second
    assert all(row.parent_row_sha256 == "b" * 64 for row in features)
    assert "trajectory" not in rendered
    assert "game_win" not in rendered
    assert "opponent" not in rendered


def test_addendum_module_contains_no_predictive_model_dependency() -> None:
    import nexus_lens.stage35a_runes as module

    source = inspect.getsource(module).lower()
    for forbidden in ("import sklearn", "from sklearn", "import catboost", ".fit("):
        assert forbidden not in source


def test_checked_rune_schema_and_template_validate() -> None:
    schema_path = Path("config/stage3.5a/rune_addendum.schema.json")
    example_path = Path("config/stage3.5a/rune_addendum.example.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    example = json.loads(example_path.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(example)
    assert schema["properties"]["public_patch"]["const"] == "26.15"
    config = RuneAddendumConfig.model_validate(example)
    assert config.template_only is True


def _mapping_config(tmp_path: Path) -> RuneAddendumConfig:
    config = _config(tmp_path)
    versions = ["16.16.1", "16.15.1", "16.14.1"]
    runes = [
        {
            "id": 8000,
            "name": "Precision",
            "slots": [
                {
                    "runes": [
                        {"id": 8005, "name": "Press the Attack"},
                        {"id": 8010, "name": "Conqueror"},
                    ]
                },
                {"runes": [{"id": 9111, "name": "Triumph"}]},
            ],
        },
        {
            "id": 8100,
            "name": "Domination",
            "slots": [
                {"runes": [{"id": 8112, "name": "Electrocute"}]},
                {"runes": [{"id": 8139, "name": "Taste of Blood"}]},
            ],
        },
    ]
    config.data_dragon_versions_path.write_text(json.dumps(versions), encoding="utf-8")
    config.data_dragon_runes_path.write_text(json.dumps(runes), encoding="utf-8")
    return config.model_copy(
        update={
            "data_dragon_versions_sha256": _file_sha(config.data_dragon_versions_path),
            "data_dragon_runes_sha256": _file_sha(config.data_dragon_runes_path),
        }
    )


def _config(tmp_path: Path) -> RuneAddendumConfig:
    return RuneAddendumConfig(
        schema_version="stage3.5a-rune-addendum-config-v1",
        public_patch="26.15",
        parent_dataset_path=tmp_path / "parent.jsonl",
        parent_dataset_sha256="a" * 64,
        stage35a_config_path=tmp_path / "stage35a.json",
        data_dragon_version="16.15.1",
        data_dragon_versions_path=tmp_path / "versions.json",
        data_dragon_versions_sha256="b" * 64,
        data_dragon_runes_path=tmp_path / "runes.json",
        data_dragon_runes_sha256="c" * 64,
        meaningful_support_threshold=20,
        private_output_directory=tmp_path / "private",
        aggregate_output_directory=tmp_path / "public",
    )


def _observations() -> tuple[RuneObservation, ...]:
    output: list[RuneObservation] = []
    base = datetime(2026, 7, 30, tzinfo=UTC)
    for group_index in range(10_000):
        group = "match_group_" + hashlib.sha256(
            f"group-{group_index}".encode()
        ).hexdigest()[:32]
        for perspective in range(2):
            row_index = group_index * 2 + perspective
            keystone_id = 8005 if (group_index + perspective) % 3 else 8010
            keystone_name = (
                "Press the Attack" if keystone_id == 8005 else "Conqueror"
            )
            feature = RuneFeatureRow(
                parent_row_index=row_index,
                focal_row_key=hashlib.sha256(f"row-{row_index}".encode()).hexdigest(),
                parent_row_sha256="b" * 64,
                match_group_id=group,
                scientific_weight=0.5,
                keystone_id=keystone_id,
                keystone_name=keystone_name,
                primary_rune_tree_id=8000,
                secondary_rune_tree_id=8100,
                mapping_status="mapped",
            )
            output.append(
                RuneObservation(
                    feature=feature,
                    platform="eun1" if group_index % 2 == 0 else "euw1",
                    game_creation=base + timedelta(minutes=group_index),
                    focal_champion_id=114 if perspective == 0 else 516,
                    focal_champion_name="Fiora" if perspective == 0 else "Ornn",
                    enemy_champion_id=516 if perspective == 0 else 114,
                    enemy_champion_name="Ornn" if perspective == 0 else "Fiora",
                )
            )
    return tuple(output)


def _perks(keystone: int, primary_tree: int, secondary_tree: int) -> dict:
    return {
        "styles": [
            {
                "description": "primaryStyle",
                "style": primary_tree,
                "selections": [{"perk": keystone}],
            },
            {
                "description": "subStyle",
                "style": secondary_tree,
                "selections": [],
            },
        ]
    }


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
