"""Outcome-blind Stage 3.5A focal-keystone feasibility addendum."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_lens.canonical import CanonicalMatch, CanonicalParticipant
from nexus_lens.data_seal import sha256_file
from nexus_lens.patches import PATCH_MAPPING_RULES
from nexus_lens.stage34b_operations import validate_public_payload
from nexus_lens.stage35a import _match_group_id
from nexus_lens.timeline_collection import (
    Stage35Config,
    load_stage35_config,
    verify_stage31_sources,
)

RUNE_CONFIG_SCHEMA_VERSION = "stage3.5a-rune-addendum-config-v1"
RUNE_ROW_SCHEMA_VERSION = "stage3.5a-rune-feature-v1"
RUNE_AUDIT_SCHEMA_VERSION = "stage3.5a-rune-audit-v1"
RUNE_MANIFEST_SCHEMA_VERSION = "stage3.5a-rune-manifest-v1"
RUNE_QUALITY_SCHEMA_VERSION = "stage3.5a-rune-quality-v1"
EXPECTED_PARENT_ROWS = 20_000
EXPECTED_MATCH_GROUPS = 10_000
SUPPORT_THRESHOLDS = (2, 5, 10, 20, 50)
MappingStatus = Literal[
    "mapped",
    "mapped_secondary_tree_unavailable",
    "perks_missing",
    "styles_missing",
    "primary_style_missing_or_ambiguous",
    "secondary_style_missing_or_ambiguous",
    "primary_tree_invalid",
    "secondary_tree_invalid",
    "keystone_missing",
    "unknown_keystone_id",
    "primary_tree_mismatch",
    "keystone_ambiguous",
]


class RuneModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuneAddendumConfig(RuneModel):
    schema_version: str = Field(pattern=r"^stage3\.5a-rune-addendum-config-v1$")
    template_only: bool = False
    public_patch: str = Field(pattern=r"^26\.15$")
    parent_dataset_path: Path
    parent_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage35a_config_path: Path
    data_dragon_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    data_dragon_locale: str = Field(default="en_US", pattern=r"^en_US$")
    data_dragon_versions_path: Path
    data_dragon_versions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_dragon_runes_path: Path
    data_dragon_runes_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    meaningful_support_threshold: int = Field(default=20, ge=1)
    illustrative_combination_limit: int = Field(default=15, ge=1, le=25)
    private_output_directory: Path
    aggregate_output_directory: Path
    maximum_aggregate_publication_bytes: int = Field(default=2_000_000, ge=1)

    @model_validator(mode="after")
    def validate_contract(self) -> RuneAddendumConfig:
        if self.meaningful_support_threshold != 20:
            raise ValueError("meaningful rune support is prospectively fixed at 20")
        return self

    def scientific_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "public_patch": self.public_patch,
            "parent_dataset_sha256": self.parent_dataset_sha256,
            "data_dragon_version": self.data_dragon_version,
            "data_dragon_locale": self.data_dragon_locale,
            "data_dragon_versions_sha256": self.data_dragon_versions_sha256,
            "data_dragon_runes_sha256": self.data_dragon_runes_sha256,
            "meaningful_support_threshold": self.meaningful_support_threshold,
            "illustrative_combination_limit": self.illustrative_combination_limit,
            "maximum_aggregate_publication_bytes": (
                self.maximum_aggregate_publication_bytes
            ),
        }


class RuneFeatureRow(RuneModel):
    processing_schema_version: str = RUNE_ROW_SCHEMA_VERSION
    parent_row_index: int = Field(ge=0)
    focal_row_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_row_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    match_group_id: str = Field(pattern=r"^match_group_[0-9a-f]{32}$")
    scientific_weight: float
    keystone_id: int | None
    keystone_name: str | None
    primary_rune_tree_id: int | None
    secondary_rune_tree_id: int | None
    mapping_status: MappingStatus


@dataclass(frozen=True)
class KeystoneDefinition:
    keystone_id: int
    keystone_name: str
    primary_tree_id: int
    primary_tree_name: str


@dataclass(frozen=True)
class RuneMapping:
    data_dragon_version: str
    definitions: dict[int, KeystoneDefinition]
    tree_ids: frozenset[int]
    mapping_sha256: str


@dataclass(frozen=True)
class SourceRuneMatch:
    match_id: str
    source_payload_reference: str
    focal_participant_ids: dict[str, int]


@dataclass(frozen=True)
class RuneObservation:
    feature: RuneFeatureRow
    platform: str
    game_creation: datetime
    focal_champion_id: int
    focal_champion_name: str
    enemy_champion_id: int
    enemy_champion_name: str


@dataclass(frozen=True)
class RuneAddendumDataset:
    features: tuple[RuneFeatureRow, ...]
    observations: tuple[RuneObservation, ...]
    private_support_detail: dict[str, Any]
    audit: dict[str, Any]
    manifest: dict[str, Any]
    quality_report: dict[str, Any]
    derived_dataset_sha256: str
    parent_alignment_sha256: str


def load_rune_addendum_config(
    path: Path, *, allow_template: bool = False
) -> RuneAddendumConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        config = RuneAddendumConfig.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Stage 3.5A rune addendum configuration is invalid") from error
    if config.template_only and not allow_template:
        raise ValueError("template rune addendum configuration is not executable")
    return config


def resolve_data_dragon_version(
    public_patch: str,
    available_versions: list[str],
    *,
    game_year: int = 2026,
) -> str:
    """Resolve the newest official Data Dragon revision via patch mapping rules."""

    try:
        public_major, public_minor = (int(part) for part in public_patch.split("."))
    except (TypeError, ValueError):
        raise ValueError("public patch is malformed") from None
    rules = [
        rule
        for rule in PATCH_MAPPING_RULES
        if rule.game_year == game_year and rule.public_major == public_major
    ]
    if len(rules) != 1:
        raise ValueError("public patch has no unique Data Dragon mapping rule")
    api_prefix = f"{rules[0].api_major}.{public_minor}."
    candidates: list[tuple[tuple[int, ...], str]] = []
    for version in available_versions:
        if not isinstance(version, str) or not version.startswith(api_prefix):
            continue
        try:
            numeric = tuple(int(part) for part in version.split("."))
        except ValueError:
            continue
        candidates.append((numeric, version))
    if not candidates:
        raise ValueError("official Data Dragon versions lack the target patch")
    return max(candidates)[1]


def load_rune_mapping(config: RuneAddendumConfig) -> RuneMapping:
    if sha256_file(config.data_dragon_versions_path) != (
        config.data_dragon_versions_sha256
    ):
        raise ValueError("Data Dragon version-list checksum differs")
    if sha256_file(config.data_dragon_runes_path) != config.data_dragon_runes_sha256:
        raise ValueError("Data Dragon rune-definition checksum differs")
    versions = json.loads(config.data_dragon_versions_path.read_text(encoding="utf-8"))
    if not isinstance(versions, list) or not all(
        isinstance(item, str) for item in versions
    ):
        raise ValueError("Data Dragon version list is malformed")
    resolved = resolve_data_dragon_version(config.public_patch, versions)
    if resolved != config.data_dragon_version:
        raise ValueError("declared Data Dragon version differs from patch resolution")
    payload = json.loads(config.data_dragon_runes_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Data Dragon rune definitions are malformed")
    definitions: dict[int, KeystoneDefinition] = {}
    tree_ids: set[int] = set()
    for raw_tree in payload:
        if not isinstance(raw_tree, dict):
            raise ValueError("Data Dragon rune tree is malformed")
        tree_id = _required_int(raw_tree.get("id"), "rune tree ID")
        tree_name = _required_text(raw_tree.get("name"), "rune tree name")
        slots = raw_tree.get("slots")
        if tree_id in tree_ids or not isinstance(slots, list) or not slots:
            raise ValueError("Data Dragon rune tree is duplicated or has no slots")
        tree_ids.add(tree_id)
        first_slot = slots[0]
        raw_runes = first_slot.get("runes") if isinstance(first_slot, dict) else None
        if not isinstance(raw_runes, list) or not raw_runes:
            raise ValueError("Data Dragon keystone slot is malformed")
        for raw_rune in raw_runes:
            if not isinstance(raw_rune, dict):
                raise ValueError("Data Dragon keystone is malformed")
            rune_id = _required_int(raw_rune.get("id"), "keystone ID")
            if rune_id in definitions:
                raise ValueError("Data Dragon keystone ID is duplicated")
            definitions[rune_id] = KeystoneDefinition(
                keystone_id=rune_id,
                keystone_name=_required_text(raw_rune.get("name"), "keystone name"),
                primary_tree_id=tree_id,
                primary_tree_name=tree_name,
            )
    mapping_payload = {
        "data_dragon_version": resolved,
        "trees": [
            {
                "tree_id": definition.primary_tree_id,
                "tree_name": definition.primary_tree_name,
                "keystone_id": definition.keystone_id,
                "keystone_name": definition.keystone_name,
            }
            for definition in sorted(
                definitions.values(), key=lambda item: item.keystone_id
            )
        ],
    }
    return RuneMapping(
        data_dragon_version=resolved,
        definitions=definitions,
        tree_ids=frozenset(tree_ids),
        mapping_sha256=_sha256_json(mapping_payload),
    )


def map_focal_perks(perks: Any, mapping: RuneMapping) -> dict[str, Any]:
    """Map one participant's own keystone using tree semantics, never position."""

    result: dict[str, Any] = {
        "keystone_id": None,
        "keystone_name": None,
        "primary_rune_tree_id": None,
        "secondary_rune_tree_id": None,
        "mapping_status": "perks_missing",
    }
    if not isinstance(perks, dict):
        return result
    styles = perks.get("styles")
    if not isinstance(styles, list):
        return {**result, "mapping_status": "styles_missing"}
    primary = [
        style
        for style in styles
        if isinstance(style, dict)
        and str(style.get("description", "")).casefold() == "primarystyle"
    ]
    secondary = [
        style
        for style in styles
        if isinstance(style, dict)
        and str(style.get("description", "")).casefold() == "substyle"
    ]
    if len(primary) != 1:
        return {**result, "mapping_status": "primary_style_missing_or_ambiguous"}
    primary_tree = primary[0].get("style")
    if not isinstance(primary_tree, int) or primary_tree not in mapping.tree_ids:
        return {**result, "mapping_status": "primary_tree_invalid"}
    result["primary_rune_tree_id"] = primary_tree
    secondary_available = len(secondary) == 1
    secondary_tree = secondary[0].get("style") if secondary_available else None
    if not isinstance(secondary_tree, int) or secondary_tree not in mapping.tree_ids:
        secondary_available = False
        secondary_tree = None
    result["secondary_rune_tree_id"] = secondary_tree
    raw_selections = primary[0].get("selections")
    if not isinstance(raw_selections, list) or not raw_selections:
        return {**result, "mapping_status": "keystone_missing"}
    selected_ids = {
        selection.get("perk")
        for selection in raw_selections
        if isinstance(selection, dict) and isinstance(selection.get("perk"), int)
    }
    matching = [
        mapping.definitions[rune_id]
        for rune_id in selected_ids
        if rune_id in mapping.definitions
        and mapping.definitions[rune_id].primary_tree_id == primary_tree
    ]
    if len(matching) == 1:
        definition = matching[0]
        return {
            **result,
            "keystone_id": definition.keystone_id,
            "keystone_name": definition.keystone_name,
            "mapping_status": (
                "mapped" if secondary_available else "mapped_secondary_tree_unavailable"
            ),
        }
    if len(matching) > 1:
        return {**result, "mapping_status": "keystone_ambiguous"}
    if any(rune_id in mapping.definitions for rune_id in selected_ids):
        return {**result, "mapping_status": "primary_tree_mismatch"}
    return {**result, "mapping_status": "unknown_keystone_id"}


def build_rune_addendum_dataset(config: RuneAddendumConfig) -> RuneAddendumDataset:
    if sha256_file(config.parent_dataset_path) != config.parent_dataset_sha256:
        raise ValueError("Stage 3.5A parent dataset checksum differs")
    stage35_config = load_stage35_config(config.stage35a_config_path)
    if stage35_config.public_patch != config.public_patch:
        raise ValueError("Stage 3.5A source patch differs")
    mapping = load_rune_mapping(config)
    source_index = _load_source_index(stage35_config)
    features: list[RuneFeatureRow] = []
    observations: list[RuneObservation] = []
    raw_payload_hashes: dict[str, str] = {}
    current_group: str | None = None
    current_participants: dict[int, dict[str, Any]] = {}
    parent_alignment = hashlib.sha256()
    seen_row_keys: set[str] = set()
    with config.parent_dataset_path.open("rb") as handle:
        for parent_index, line in enumerate(handle):
            parent_row_hash = hashlib.sha256(line).hexdigest()
            parent_alignment.update(f"{parent_index}\0{parent_row_hash}\n".encode())
            try:
                parent = json.loads(line)
            except (UnicodeError, json.JSONDecodeError):
                raise ValueError("Stage 3.5A parent row is malformed") from None
            required = _parent_projection(parent)
            group_id = required["match_group_id"]
            source = source_index.get(group_id)
            if source is None:
                raise ValueError("Stage 3.5A parent/source group join differs")
            participant_id = source.focal_participant_ids.get(
                required["focal_player_key"]
            )
            if participant_id is None:
                raise ValueError("Stage 3.5A focal participant join differs")
            if current_group != group_id:
                payload_path = _resolve_private_reference(
                    source.source_payload_reference
                )
                raw_payload_hashes[group_id] = sha256_file(payload_path)
                current_participants = _load_raw_participants(
                    payload_path, source.match_id, config.public_patch
                )
                current_group = group_id
            raw_participant = current_participants.get(participant_id)
            if raw_participant is None:
                raise ValueError("retained raw focal participant is missing")
            if (
                raw_participant.get("championId") != required["focal_champion_id"]
                or raw_participant.get("teamId") != required["focal_team_id"]
            ):
                raise ValueError("retained raw focal participant conflicts")
            mapped = map_focal_perks(raw_participant.get("perks"), mapping)
            row_key = _sha256_text(
                f"stage3.5a-rune-row\0{config.parent_dataset_sha256}\0"
                f"{parent_index}\0{parent_row_hash}"
            )
            if row_key in seen_row_keys:
                raise ValueError("derived focal row key is duplicated")
            seen_row_keys.add(row_key)
            feature = RuneFeatureRow(
                parent_row_index=parent_index,
                focal_row_key=row_key,
                parent_row_sha256=parent_row_hash,
                match_group_id=group_id,
                scientific_weight=required["scientific_weight"],
                **mapped,
            )
            features.append(feature)
            observations.append(
                RuneObservation(
                    feature=feature,
                    platform=required["platform"],
                    game_creation=required["game_creation"],
                    focal_champion_id=required["focal_champion_id"],
                    focal_champion_name=required["focal_champion_name"],
                    enemy_champion_id=required["enemy_top_champion_id"],
                    enemy_champion_name=required["enemy_top_champion_name"],
                )
            )
    feature_rows = tuple(features)
    observation_rows = tuple(observations)
    derived_hash = _feature_dataset_sha256(feature_rows)
    alignment_hash = parent_alignment.hexdigest()
    private_detail = _build_private_support_detail(observation_rows)
    quality = _build_quality(feature_rows, config)
    audit = build_support_audit(observation_rows, quality, config)
    manifest = _build_manifest(
        config=config,
        mapping=mapping,
        features=feature_rows,
        derived_hash=derived_hash,
        alignment_hash=alignment_hash,
        raw_payload_bundle_sha256=_sha256_json(raw_payload_hashes),
        quality=quality,
        stage35_config=stage35_config,
    )
    for payload in (audit, manifest, quality):
        validate_public_payload(payload)
        if not _is_finite(payload):
            raise ValueError("rune addendum public payload is non-finite")
    return RuneAddendumDataset(
        features=feature_rows,
        observations=observation_rows,
        private_support_detail=private_detail,
        audit=audit,
        manifest=manifest,
        quality_report=quality,
        derived_dataset_sha256=derived_hash,
        parent_alignment_sha256=alignment_hash,
    )


def build_support_audit(
    observations: tuple[RuneObservation, ...],
    quality: dict[str, Any],
    config: RuneAddendumConfig,
) -> dict[str, Any]:
    statuses = Counter(row.feature.mapping_status for row in observations)
    mapped = tuple(row for row in observations if row.feature.keystone_id is not None)
    keystone_groups: dict[tuple[int, str], set[str]] = defaultdict(set)
    keystone_rows: Counter[tuple[int, str]] = Counter()
    platform_rows: dict[str, Counter[tuple[int, str]]] = defaultdict(Counter)
    platform_groups: dict[str, dict[tuple[int, str], set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    champion_keystone_rows: Counter[tuple[int, str, int, str]] = Counter()
    champion_keystone_groups: dict[
        tuple[int, str, int, str], set[str]
    ] = defaultdict(set)
    matchup_keystone_rows: Counter[
        tuple[int, str, int, str, int, str]
    ] = Counter()
    matchup_keystone_groups: dict[
        tuple[int, str, int, str, int, str], set[str]
    ] = defaultdict(set)
    champion_totals: Counter[tuple[int, str]] = Counter()
    champion_keystones: dict[tuple[int, str], Counter[tuple[int, str]]] = defaultdict(
        Counter
    )
    for row in mapped:
        key = _keystone_key(row.feature)
        champion = (row.focal_champion_id, row.focal_champion_name)
        champion_key = (*champion, *key)
        matchup_key = (
            *champion,
            row.enemy_champion_id,
            row.enemy_champion_name,
            *key,
        )
        group = row.feature.match_group_id
        keystone_rows[key] += 1
        keystone_groups[key].add(group)
        platform_rows[row.platform][key] += 1
        platform_groups[row.platform][key].add(group)
        champion_keystone_rows[champion_key] += 1
        champion_keystone_groups[champion_key].add(group)
        matchup_keystone_rows[matchup_key] += 1
        matchup_keystone_groups[matchup_key].add(group)
        champion_totals[champion] += 1
        champion_keystones[champion][key] += 1
    champion_group_counts = [
        len(groups) for groups in champion_keystone_groups.values()
    ]
    matchup_group_counts = [len(groups) for groups in matchup_keystone_groups.values()]
    diversity = Counter()
    meaningful = Counter()
    top_shares: list[float] = []
    for champion, counts in champion_keystones.items():
        distinct = len(counts)
        diversity[
            "one" if distinct == 1 else "two" if distinct == 2 else "three_or_more"
        ] += 1
        supported = sum(
            count >= config.meaningful_support_threshold for count in counts.values()
        )
        meaningful["at_least_two"] += supported >= 2
        meaningful["fewer_than_two"] += supported < 2
        top_shares.append(max(counts.values()) / champion_totals[champion])
    top_combinations = sorted(
        champion_keystone_groups,
        key=lambda key: (-len(champion_keystone_groups[key]), key),
    )[: config.illustrative_combination_limit]
    chronological = _chronological_coverage(observations)
    audit = {
        "schema_version": RUNE_AUDIT_SCHEMA_VERSION,
        "scope": {
            "parent_focal_rows": len(observations),
            "parent_match_groups": len(
                {row.feature.match_group_id for row in observations}
            ),
            "public_patch": config.public_patch,
            "outcome_conditioned_statistics": 0,
            "predictive_model_fits": 0,
        },
        "mapping_coverage": {
            "mapped_focal_rows": len(mapped),
            "unmapped_focal_rows": len(observations) - len(mapped),
            "coverage_rate": _ratio(len(mapped), len(observations)),
            "status_counts": dict(sorted(statuses.items())),
            "observed_keystones": len(keystone_rows),
        },
        "keystone_frequency_overall": _frequency_rows(
            keystone_rows, keystone_groups
        ),
        "keystone_frequency_by_platform": {
            platform: _frequency_rows(counts, platform_groups[platform])
            for platform, counts in sorted(platform_rows.items())
        },
        "champion_keystone_support": {
            "unique_groups": len(champion_group_counts),
            "group_support_distribution": _distribution(champion_group_counts),
            "groups_at_threshold": _threshold_counts(champion_group_counts),
            "champions_by_observed_keystone_count": dict(sorted(diversity.items())),
            "meaningful_support_definition_focal_rows": (
                config.meaningful_support_threshold
            ),
            "champions_with_at_least_two_meaningfully_supported_keystones": (
                meaningful["at_least_two"]
            ),
            "champions_with_fewer_than_two_meaningfully_supported_keystones": (
                meaningful["fewer_than_two"]
            ),
            "top_keystone_share_distribution_by_champion": _distribution(top_shares),
            "illustrative_most_supported_combinations": [
                {
                    "focal_champion_id": key[0],
                    "focal_champion_name": key[1],
                    "keystone_id": key[2],
                    "keystone_name": key[3],
                    "focal_rows": champion_keystone_rows[key],
                    "distinct_match_groups": len(champion_keystone_groups[key]),
                }
                for key in top_combinations
            ],
            "full_frequency_table_private": True,
        },
        "directional_matchup_keystone_support": {
            "unique_groups": len(matchup_group_counts),
            "group_support_distribution": _distribution(matchup_group_counts),
            "groups_at_threshold": _threshold_counts(matchup_group_counts),
            "full_frequency_table_private": True,
        },
        "chronological_coverage": chronological,
        "paired_perspective_integrity": {
            "match_groups": quality["match_groups"],
            "focal_rows": quality["focal_rows"],
            "rows_per_match_exactly_two": quality["rows_per_match_exactly_two"],
            "scientific_weight_per_match_equals_one": quality[
                "scientific_weight_per_match_equals_one"
            ],
            "future_split_and_bootstrap_unit": "match_group_id",
        },
        "support_assessment": {
            "focal_keystone_main_effect": (
                "supportable_regularized_candidate_with_rare-category_fallback"
            ),
            "focal_champion_by_keystone": (
                "partially_supportable_regularized_candidate_with_hierarchical_fallback"
            ),
            "focal_champion_by_enemy_champion_by_keystone": (
                "too_sparse_for_primary_unregularized_feature"
            ),
            "complete_rune_page": "outside_current_scope_not_extracted",
            "nonlinear_model_warning": (
                "CatBoost cannot create absent support and may overfit rare "
                "combinations."
            ),
        },
        "selection_bias": {
            "observational_not_randomized": True,
            "possible_selection_drivers": [
                "enemy champion",
                "player playstyle",
                "champion familiarity",
                "skill",
                "expected lane strategy",
                "external recommendations",
            ],
            "permitted_claim": "rune-conditioned expected lane performance",
            "causal_switching_claim_authorized": False,
        },
        "intervention_diagnostics": {
            "not_used_for_filtering_or_features": True,
            "earlier_any_proxy_rate_at_5_minutes": 0.7781,
            "earlier_herald_linked_top_tower_events": 0,
            "separate_validation_required": True,
        },
        "privacy": {
            "aggregate_only": True,
            "opponent_rune_fields": False,
            "player_identifiers": False,
            "match_identifiers": False,
            "raw_external_paths": False,
        },
    }
    return audit


def write_rune_addendum_dataset(
    dataset: RuneAddendumDataset, config: RuneAddendumConfig
) -> None:
    private_payloads = {
        "focal_rune_features.jsonl": b"".join(
            _json_bytes(row.model_dump(mode="json")) for row in dataset.features
        ),
        "support_detail.json": _json_bytes(dataset.private_support_detail),
        "private_manifest.json": _json_bytes(
            {
                "schema_version": RUNE_MANIFEST_SCHEMA_VERSION,
                "parent_dataset_sha256": config.parent_dataset_sha256,
                "parent_alignment_sha256": dataset.parent_alignment_sha256,
                "derived_dataset_sha256": dataset.derived_dataset_sha256,
                "focal_rows": len(dataset.features),
                "match_groups": len(
                    {row.match_group_id for row in dataset.features}
                ),
            }
        ),
    }
    public_payloads = {
        "audit.json": _json_bytes(dataset.audit),
        "manifest.json": _json_bytes(dataset.manifest),
        "quality_report.json": _json_bytes(dataset.quality_report),
        "feasibility_report.md": _render_report(dataset.audit).encode("utf-8"),
    }
    if sum(map(len, public_payloads.values())) > (
        config.maximum_aggregate_publication_bytes
    ):
        raise ValueError("rune aggregate publication size ceiling exceeded")
    _atomic_publish_directory(config.private_output_directory, private_payloads)
    try:
        _atomic_publish_directory(config.aggregate_output_directory, public_payloads)
    except Exception:
        shutil.rmtree(config.private_output_directory, ignore_errors=True)
        raise


def _load_source_index(config: Stage35Config) -> dict[str, SourceRuneMatch]:
    verify_stage31_sources(config)
    index: dict[str, SourceRuneMatch] = {}
    participant_ids: dict[str, dict[str, int]] = defaultdict(dict)
    source_matches: dict[str, tuple[str, str]] = {}
    for source in sorted(config.sources, key=lambda item: item.label):
        with (source.directory / "matches.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                match = CanonicalMatch.model_validate_json(line)
                if match.public_patch != config.public_patch:
                    continue
                group = _match_group_id(match.platform.lower(), match.match_id)
                if group in source_matches:
                    raise ValueError("rune source match group is duplicated")
                source_matches[group] = (
                    match.match_id,
                    match.source_payload_reference,
                )
        with (source.directory / "participants.jsonl").open(
            encoding="utf-8"
        ) as handle:
            for line in handle:
                participant = CanonicalParticipant.model_validate_json(line)
                group = _match_group_id(source.platform, participant.match_id)
                if group not in source_matches:
                    continue
                if participant.player_key in participant_ids[group]:
                    raise ValueError("rune source focal player key is duplicated")
                participant_ids[group][participant.player_key] = (
                    participant.participant_id
                )
    if set(source_matches) != set(participant_ids):
        raise ValueError("rune source match/participant membership differs")
    for group, (match_id, reference) in source_matches.items():
        index[group] = SourceRuneMatch(
            match_id=match_id,
            source_payload_reference=reference,
            focal_participant_ids=participant_ids[group],
        )
    return index


def _load_raw_participants(
    path: Path, expected_match_id: str, public_patch: str
) -> dict[int, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("retained raw match payload is invalid") from None
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    info = payload.get("info") if isinstance(payload, dict) else None
    participants = info.get("participants") if isinstance(info, dict) else None
    game_version = info.get("gameVersion") if isinstance(info, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("matchId") != expected_match_id
        or not isinstance(participants, list)
        or not isinstance(game_version, str)
        or not game_version.startswith("16.15.")
        or public_patch != "26.15"
        or info.get("queueId") != 420
    ):
        raise ValueError("retained raw match payload lineage differs")
    output: dict[int, dict[str, Any]] = {}
    for participant in participants:
        identifier = (
            participant.get("participantId")
            if isinstance(participant, dict)
            else None
        )
        if not isinstance(identifier, int) or identifier in output:
            raise ValueError("retained raw participant identity is invalid")
        output[identifier] = participant
    if len(output) != 10:
        raise ValueError("retained raw match does not contain ten participants")
    return output


def _parent_projection(parent: Any) -> dict[str, Any]:
    if not isinstance(parent, dict):
        raise ValueError("Stage 3.5A parent row is not an object")
    try:
        timestamp = datetime.fromisoformat(
            str(parent["game_creation"]).replace("Z", "+00:00")
        )
        projection = {
            "match_group_id": str(parent["match_group_id"]),
            "platform": str(parent["platform"]),
            "game_creation": timestamp,
            "focal_player_key": str(parent["focal_player_key"]),
            "focal_team_id": int(parent["focal_team_id"]),
            "focal_champion_id": int(parent["focal_champion_id"]),
            "focal_champion_name": str(parent["focal_champion_name"]),
            "enemy_top_champion_id": int(parent["enemy_top_champion_id"]),
            "enemy_top_champion_name": str(parent["enemy_top_champion_name"]),
            "scientific_weight": float(parent["scientific_weight"]),
        }
    except (KeyError, TypeError, ValueError):
        raise ValueError("Stage 3.5A parent row linkage is malformed") from None
    if (
        len(projection["match_group_id"]) != 44
        or not projection["match_group_id"].startswith("match_group_")
        or projection["platform"] not in {"eun1", "euw1"}
        or projection["scientific_weight"] != 0.5
    ):
        raise ValueError("Stage 3.5A parent row linkage differs")
    return projection


def _build_quality(
    features: tuple[RuneFeatureRow, ...], config: RuneAddendumConfig
) -> dict[str, Any]:
    groups: dict[str, list[RuneFeatureRow]] = defaultdict(list)
    for row in features:
        groups[row.match_group_id].append(row)
    rows_per_group = all(len(rows) == 2 for rows in groups.values())
    weights = all(
        math.isclose(sum(row.scientific_weight for row in rows), 1.0)
        for rows in groups.values()
    )
    statuses = Counter(row.mapping_status for row in features)
    exact_indexes = [row.parent_row_index for row in features] == list(
        range(len(features))
    )
    quality = {
        "schema_version": RUNE_QUALITY_SCHEMA_VERSION,
        "parent_dataset_sha256": config.parent_dataset_sha256,
        "parent_dataset_hash_verified": True,
        "focal_rows": len(features),
        "match_groups": len(groups),
        "exact_20000_row_alignment": len(features) == EXPECTED_PARENT_ROWS,
        "exact_10000_match_groups": len(groups) == EXPECTED_MATCH_GROUPS,
        "parent_row_indexes_contiguous_and_ordered": exact_indexes,
        "parent_row_hash_linkage_present": all(
            len(row.parent_row_sha256) == 64 for row in features
        ),
        "rows_per_match_exactly_two": rows_per_group,
        "scientific_weight_per_match_equals_one": weights,
        "mapping_status_counts": dict(sorted(statuses.items())),
        "all_rune_values_finite_or_explicitly_null": _is_finite(features),
        "opponent_rune_fields_absent": True,
        "target_values_copied_or_modified": False,
        "outcome_conditioned_statistics_calculated": 0,
        "predictive_models_fitted": 0,
        "source_match_or_timeline_files_modified": False,
    }
    if not all(
        (
            quality["exact_20000_row_alignment"],
            quality["exact_10000_match_groups"],
            exact_indexes,
            rows_per_group,
            weights,
            quality["all_rune_values_finite_or_explicitly_null"],
        )
    ):
        raise ValueError("rune addendum quality gate failed")
    return quality


def _build_private_support_detail(
    observations: tuple[RuneObservation, ...],
) -> dict[str, Any]:
    by_champion: dict[str, Counter[str]] = defaultdict(Counter)
    champion_keystone: Counter[str] = Counter()
    matchup_keystone: Counter[str] = Counter()
    for row in observations:
        if row.feature.keystone_id is None:
            continue
        rune = f"{row.feature.keystone_id}|{row.feature.keystone_name}"
        champion = f"{row.focal_champion_id}|{row.focal_champion_name}"
        enemy = f"{row.enemy_champion_id}|{row.enemy_champion_name}"
        by_champion[champion][rune] += 1
        champion_keystone[f"{champion}|{rune}"] += 1
        matchup_keystone[f"{champion}|{enemy}|{rune}"] += 1
    return {
        "schema_version": "stage3.5a-rune-private-support-v1",
        "outcome_conditioned_statistics": 0,
        "keystone_frequency_by_focal_champion": {
            champion: dict(sorted(counts.items()))
            for champion, counts in sorted(by_champion.items())
        },
        "champion_keystone_focal_row_counts": dict(sorted(champion_keystone.items())),
        "directional_matchup_keystone_focal_row_counts": dict(
            sorted(matchup_keystone.items())
        ),
    }


def _build_manifest(
    *,
    config: RuneAddendumConfig,
    mapping: RuneMapping,
    features: tuple[RuneFeatureRow, ...],
    derived_hash: str,
    alignment_hash: str,
    raw_payload_bundle_sha256: str,
    quality: dict[str, Any],
    stage35_config: Stage35Config,
) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    executable_files = (
        Path(__file__),
        root / "scripts/build_stage35a_rune_addendum.py",
    )
    source_files = {
        f"{source.label}:{name}": digest
        for source in stage35_config.sources
        for name, digest in source.file_sha256.items()
    }
    return {
        "schema_version": RUNE_MANIFEST_SCHEMA_VERSION,
        "configuration": config.scientific_payload(),
        "parent_dataset_sha256": config.parent_dataset_sha256,
        "parent_alignment_sha256": alignment_hash,
        "derived_dataset_sha256": derived_hash,
        "rune_mapping_sha256": mapping.mapping_sha256,
        "data_dragon": {
            "version": mapping.data_dragon_version,
            "locale": config.data_dragon_locale,
            "versions_sha256": config.data_dragon_versions_sha256,
            "runes_sha256": config.data_dragon_runes_sha256,
            "keystone_definitions": len(mapping.definitions),
            "primary_tree_definitions": len(mapping.tree_ids),
        },
        "stage31_source_bundle_sha256": _sha256_json(source_files),
        "raw_match_payload_bundle_sha256": raw_payload_bundle_sha256,
        "executable_bundle_sha256": _sha256_json(
            {path.name: sha256_file(path) for path in executable_files}
        ),
        "focal_rows": len(features),
        "match_groups": len({row.match_group_id for row in features}),
        "quality_gates": quality,
        "publication": {
            "aggregate_only_public": True,
            "private_feature_rows_committed": False,
            "outcome_conditioned_statistics": 0,
            "predictive_model_fits": 0,
        },
    }


def _frequency_rows(
    counts: Counter[tuple[int, str]],
    groups: dict[tuple[int, str], set[str]],
) -> list[dict[str, Any]]:
    return [
        {
            "keystone_id": key[0],
            "keystone_name": key[1],
            "focal_rows": count,
            "distinct_match_groups": len(groups[key]),
        }
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _chronological_coverage(
    observations: tuple[RuneObservation, ...],
) -> dict[str, Any]:
    group_times: dict[str, datetime] = {}
    by_group: dict[str, list[RuneObservation]] = defaultdict(list)
    for row in observations:
        group = row.feature.match_group_id
        existing = group_times.setdefault(group, row.game_creation)
        if existing != row.game_creation:
            raise ValueError("paired rune rows have conflicting timestamps")
        by_group[group].append(row)
    ordered = sorted(group_times, key=lambda group: (group_times[group], group))
    blocks: list[dict[str, Any]] = []
    for block_index in range(5):
        start = block_index * len(ordered) // 5
        stop = (block_index + 1) * len(ordered) // 5
        groups = ordered[start:stop]
        rows = [row for group in groups for row in by_group[group]]
        mapped = [row for row in rows if row.feature.keystone_id is not None]
        blocks.append(
            {
                "block": block_index + 1,
                "first_game_creation": min(group_times[group] for group in groups)
                .date()
                .isoformat(),
                "last_game_creation": max(group_times[group] for group in groups)
                .date()
                .isoformat(),
                "match_groups": len(groups),
                "focal_rows": len(rows),
                "mapped_focal_rows": len(mapped),
                "coverage_rate": _ratio(len(mapped), len(rows)),
                "observed_keystones": len(
                    {row.feature.keystone_id for row in mapped}
                ),
            }
        )
    rates = [block["coverage_rate"] for block in blocks]
    return {
        "five_equal_match_group_blocks": blocks,
        "coverage_rate_range": max(rates) - min(rates),
        "availability_changes_materially": (max(rates) - min(rates)) > 0.01,
    }


def _threshold_counts(values: list[int]) -> dict[str, int]:
    return {
        f"at_least_{threshold}": sum(value >= threshold for value in values)
        for threshold in SUPPORT_THRESHOLDS
    }


def _distribution(values: list[float | int]) -> dict[str, Any]:
    numeric = sorted(float(value) for value in values)
    if not numeric:
        return {
            "count": 0,
            "minimum": None,
            "p25": None,
            "median": None,
            "p75": None,
            "maximum": None,
            "mean": None,
        }
    return {
        "count": len(numeric),
        "minimum": numeric[0],
        "p25": _quantile(numeric, 0.25),
        "median": statistics.median(numeric),
        "p75": _quantile(numeric, 0.75),
        "maximum": numeric[-1],
        "mean": statistics.fmean(numeric),
    }


def _quantile(values: list[float], probability: float) -> float:
    position = (len(values) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def _keystone_key(feature: RuneFeatureRow) -> tuple[int, str]:
    if feature.keystone_id is None or feature.keystone_name is None:
        raise ValueError("mapped rune feature lacks a keystone")
    return feature.keystone_id, feature.keystone_name


def _resolve_private_reference(reference: str) -> Path:
    path = Path(reference)
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def _feature_dataset_sha256(features: tuple[RuneFeatureRow, ...]) -> str:
    digest = hashlib.sha256()
    for row in features:
        digest.update(_json_bytes(row.model_dump(mode="json")))
    return digest.hexdigest()


def _render_report(audit: dict[str, Any]) -> str:
    coverage = audit["mapping_coverage"]
    champion = audit["champion_keystone_support"]
    matchup = audit["directional_matchup_keystone_support"]
    return "\n".join(
        (
            "# Nexus Lens Stage 3.5A rune-feasibility addendum",
            "",
            "This is an outcome-blind aggregate support audit. It fitted no model, "
            "estimated no rune-conditioned outcome, and authorizes no recommendation.",
            "",
            "## Coverage",
            "",
            f"- Focal rows: {audit['scope']['parent_focal_rows']}",
            f"- Match groups: {audit['scope']['parent_match_groups']}",
            f"- Mapped rows: {coverage['mapped_focal_rows']}",
            f"- Unmapped rows: {coverage['unmapped_focal_rows']}",
            f"- Observed keystones: {coverage['observed_keystones']}",
            "",
            "## Support",
            "",
            "- Champion-keystone groups with at least 20 match groups: "
            f"{champion['groups_at_threshold']['at_least_20']}",
            "- Directional matchup-keystone groups with at least 20 match groups: "
            f"{matchup['groups_at_threshold']['at_least_20']}",
            "- Full frequency tables remain private.",
            "",
            "## Interpretation",
            "",
            "A focal-keystone main effect is a regularized candidate. "
            "Champion-keystone "
            "interactions require shrinkage and fallback. Full matchup-keystone "
            "interactions are too sparse for a primary unregularized feature. Rune "
            "selection is observational, so future claims must be predictive rather "
            "than causal.",
            "",
        )
    )


def _atomic_publish_directory(path: Path, payloads: dict[str, bytes]) -> None:
    if path.exists():
        raise ValueError("rune addendum output directory already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
    try:
        for name, payload in payloads.items():
            (temporary / name).write_bytes(payload)
        os.replace(temporary, path)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _required_int(value: Any, label: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{label} is invalid")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is invalid")
    return value


def _is_finite(value: Any) -> bool:
    if isinstance(value, BaseModel):
        return _is_finite(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return all(_is_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_is_finite(item) for item in value)
    if isinstance(value, float):
        return math.isfinite(value)
    return True


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(payload: Any) -> str:
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
        + "\n"
    ).encode("utf-8")
