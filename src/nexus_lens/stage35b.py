"""Frozen, leakage-safe Stage 3.5B rolling-origin development evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import statistics
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import scipy
import sklearn
from catboost import CatBoostClassifier, CatBoostRegressor
from catboost import __version__ as catboost_version
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sklearn.linear_model import ElasticNet, LogisticRegression, Ridge
from sklearn.metrics import log_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from nexus_lens.data_seal import sha256_file
from nexus_lens.stage34b_operations import validate_public_payload
from nexus_lens.stage35a_runes import RuneFeatureRow

PROTOCOL_SCHEMA_VERSION = "stage3.5b-protocol-v1"
CONFIG_SCHEMA_VERSION = "stage3.5b-execution-config-v1"
RESULT_SCHEMA_VERSION = "stage3.5b-development-results-v1"
EXPECTED_PUBLIC_FILES = frozenset(
    {
        "bundle_manifest.json",
        "development_report.md",
        "development_results.json",
        "gate_decisions.json",
        "input_manifest.json",
        "operation_ledger.json",
        "paired_comparisons.json",
        "quality_report.json",
    }
)
FORBIDDEN_FEATURE_TOKENS = frozenset(
    {
        "player",
        "match_id",
        "group_id",
        "platform",
        "familiarity",
        "history",
        "rank",
        "opponent_keystone",
        "minor_rune",
        "intervention",
        "duration",
        "gold",
        "xp",
        "farm",
        "level",
        "tower",
        "win",
    }
)
SUPPORT_TIERS = ((0, 4, "0-4"), (5, 19, "5-19"), (20, 49, "20-49"), (50, 10**9, "50+"))


class Stage35BModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stage35BExecutionConfig(Stage35BModel):
    schema_version: str = Field(pattern=r"^stage3\.5b-execution-config-v1$")
    template_only: bool = False
    parent_dataset_path: Path
    rune_dataset_path: Path
    rune_manifest_path: Path
    stage34b_bundle_manifest_path: Path
    private_output_directory: Path
    aggregate_output_directory: Path
    maximum_publication_bytes: int = Field(default=5_000_000, ge=1)

    @model_validator(mode="after")
    def validate_paths(self) -> Stage35BExecutionConfig:
        if self.private_output_directory == self.aggregate_output_directory:
            raise ValueError("private and aggregate output directories must differ")
        return self


@dataclass(frozen=True)
class DevelopmentRow:
    row_index: int
    match_group_id: str
    platform: str
    game_creation: datetime
    side: str
    focal_champion: str
    enemy_champion: str
    focal_keystone: str
    weight: float
    continuous: dict[str, float]
    tower_outcome: str
    game_win: int


@dataclass(frozen=True)
class GroupMetadata:
    match_group_id: str
    platform: str
    game_creation: datetime


@dataclass(frozen=True)
class OuterFold:
    fold_id: str
    train_indexes: tuple[int, ...]
    validation_indexes: tuple[int, ...]
    inner_train_indexes: tuple[int, ...]
    inner_validation_indexes: tuple[int, ...]
    start: datetime
    end: datetime


@dataclass(frozen=True)
class LoadedData:
    rows: tuple[DevelopmentRow, ...]
    all_groups: tuple[GroupMetadata, ...]
    holdout_groups: tuple[GroupMetadata, ...]
    parent_file_sha256: str
    rune_derived_sha256: str
    alignment_sha256: str


@dataclass(frozen=True)
class Preflight:
    summary: dict[str, Any]
    data: LoadedData
    folds: tuple[OuterFold, ...]


class OperationLedger:
    def __init__(
        self, callback: Callable[[dict[str, Any]], None] | None = None
    ) -> None:
        self.callback = callback
        self.records: list[dict[str, Any]] = []
        self.optimizer_fits = 0
        self.analytic_operations = 0
        self.failures = 0

    def analytic(self, **fields: Any) -> None:
        self.analytic_operations += 1
        self._record("analytic_training_operation_completed", **fields)

    def fit(self, fit: Callable[[], Any], **fields: Any) -> Any:
        self.optimizer_fits += 1
        number = self.optimizer_fits
        started = time.perf_counter()
        self._record("optimizer_fit_started", optimizer_fit_number=number, **fields)
        try:
            model = fit()
        except Exception:
            self.failures += 1
            self._record(
                "optimizer_fit_failed",
                optimizer_fit_number=number,
                duration_seconds=time.perf_counter() - started,
                **fields,
            )
            raise
        iterations = _model_iterations(model)
        self._record(
            "optimizer_fit_completed",
            optimizer_fit_number=number,
            duration_seconds=time.perf_counter() - started,
            optimizer_iterations=iterations,
            optimizer_status="completed",
            **fields,
        )
        return model

    def _record(self, event: str, **fields: Any) -> None:
        record = {"event": event, **fields}
        self.records.append(record)
        if self.callback is not None:
            self.callback(record)


def load_execution_config(
    path: Path, *, allow_template: bool = False
) -> Stage35BExecutionConfig:
    try:
        config = Stage35BExecutionConfig.model_validate_json(path.read_text("utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("Stage 3.5B execution configuration is invalid") from error
    if config.template_only and not allow_template:
        raise ValueError("template Stage 3.5B configuration is not executable")
    return config


def load_protocol(path: Path, schema_path: Path) -> dict[str, Any]:
    try:
        protocol = json.loads(path.read_text("utf-8"))
        schema = json.loads(schema_path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Stage 3.5B protocol or schema is unreadable") from error
    required = set(schema.get("required", ()))
    if (
        not isinstance(protocol, dict)
        or set(protocol) != required
        or protocol.get("schema_version") != PROTOCOL_SCHEMA_VERSION
        or protocol.get("protocol_id") != "stage3.5b-patch26.15-rolling-development-v1"
        or protocol.get("status") != "prospectively_frozen_before_real_model_fitting"
    ):
        raise ValueError("Stage 3.5B frozen protocol differs")
    _validate_protocol_contract(protocol)
    return protocol


def build_preflight(
    config: Stage35BExecutionConfig,
    protocol: dict[str, Any],
    *,
    protocol_path: Path,
    schema_path: Path,
    executable_files: tuple[Path, ...],
) -> Preflight:
    data = load_development_data(config, protocol)
    folds = construct_folds(data.rows, data.all_groups, protocol)
    feature_schema = _feature_schema(protocol)
    scope = protocol["data_scope"]
    fold_hash = _fold_fingerprint(folds, data.rows)
    protocol_hash = sha256_file(protocol_path)
    executable_hash = _bundle_hash(
        tuple((path.name, sha256_file(path)) for path in executable_files)
        + ((schema_path.name, sha256_file(schema_path)),)
    )
    budget = _expected_operation_budget(protocol)
    if budget != protocol["operation_budget"]:
        raise ValueError("Stage 3.5B operation budget does not reconcile")
    _validate_software_versions(protocol)
    platform_counts = Counter(group.platform for group in data.all_groups)
    holdout_platform_counts = Counter(group.platform for group in data.holdout_groups)
    summary = {
        "schema_version": "stage3.5b-preflight-v1",
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": protocol_hash,
        "executable_bundle_sha256": executable_hash,
        "parent_dataset_sha256": data.parent_file_sha256,
        "rune_dataset_sha256": data.rune_derived_sha256,
        "alignment_sha256": data.alignment_sha256,
        "fold_sha256": fold_hash,
        "feature_schema_sha256": _sha256_json(feature_schema),
        "scientific_preflight_sha256": "",
        "focal_rows": scope["expected_focal_rows"],
        "match_groups": scope["expected_match_groups"],
        "development_rows": len(data.rows),
        "development_evaluation_groups": sum(
            len(fold.validation_indexes) // 2 for fold in folds
        ),
        "final_holdout_groups": len(data.holdout_groups),
        "platform_group_counts": dict(sorted(platform_counts.items())),
        "holdout_platform_group_counts": dict(sorted(holdout_platform_counts.items())),
        "outer_folds": [_fold_summary(fold, data.rows) for fold in folds],
        "operation_budget": budget,
        "quality": {
            "exact_parent_and_rune_alignment": True,
            "exact_two_perspectives_per_group": True,
            "weight_per_group_equals_one": True,
            "training_strictly_precedes_validation": True,
            "no_group_overlap": True,
            "both_platforms_in_every_train_and_validation_fold": True,
            "paired_models_share_identical_membership_and_order": True,
            "target_feature_separation": True,
            "allowed_feature_schema_only": True,
            "forbidden_features_absent": True,
            "training_only_support_and_preprocessing": True,
            "final_holdout_targets_accessed": False,
            "predictive_model_fits": 0,
            "optimizer_fits": 0,
            "bootstrap_model_fits": 0,
            "expected_public_files": sorted(EXPECTED_PUBLIC_FILES),
            "public_output_aggregate_only": True,
        },
    }
    fingerprint_payload = {
        key: value
        for key, value in summary.items()
        if key != "scientific_preflight_sha256"
    }
    summary["scientific_preflight_sha256"] = _sha256_json(fingerprint_payload)
    validate_public_payload(summary)
    _require_finite(summary)
    return Preflight(summary=summary, data=data, folds=folds)


def load_development_data(
    config: Stage35BExecutionConfig, protocol: dict[str, Any]
) -> LoadedData:
    scope = protocol["data_scope"]
    parent_hash = sha256_file(config.parent_dataset_path)
    if parent_hash != scope["parent_dataset_sha256"]:
        raise ValueError("Stage 3.5A parent dataset fingerprint differs")
    rune_manifest = json.loads(config.rune_manifest_path.read_text("utf-8"))
    if (
        rune_manifest.get("derived_dataset_sha256") != scope["rune_dataset_sha256"]
        or rune_manifest.get("rune_mapping_sha256") != scope["rune_mapping_sha256"]
        or rune_manifest.get("executable_bundle_sha256")
        != scope["rune_executable_bundle_sha256"]
    ):
        raise ValueError("Stage 3.5A rune lineage differs")
    stage34b = json.loads(config.stage34b_bundle_manifest_path.read_text("utf-8"))
    if (
        stage34b.get("scientific_deterministic_bundle_sha256")
        != scope["stage34b_scientific_bundle_sha256"]
    ):
        raise ValueError("Stage 3.4B scientific bundle differs")
    holdout_start = _parse_time(protocol["folds"]["final_holdout_start_utc"])
    rows: list[DevelopmentRow] = []
    all_groups: dict[str, GroupMetadata] = {}
    holdout_groups: dict[str, GroupMetadata] = {}
    alignment = hashlib.sha256()
    rune_digest = hashlib.sha256()
    parent_count = 0
    with (
        config.parent_dataset_path.open("rb") as parent_handle,
        config.rune_dataset_path.open("rb") as rune_handle,
    ):
        for row_index, (parent_line, rune_line) in enumerate(
            zip(parent_handle, rune_handle, strict=True)
        ):
            parent_count += 1
            parent_hash_row = hashlib.sha256(parent_line).hexdigest()
            parent = json.loads(parent_line)
            rune = RuneFeatureRow.model_validate_json(rune_line)
            rune_digest.update(_json_bytes(rune.model_dump(mode="json")))
            if (
                rune.parent_row_index != row_index
                or rune.parent_row_sha256 != parent_hash_row
                or rune.match_group_id != parent.get("match_group_id")
                or rune.scientific_weight != parent.get("scientific_weight")
                or rune.keystone_id is None
            ):
                raise ValueError("Stage 3.5A parent/rune row alignment differs")
            group = str(parent["match_group_id"])
            platform = str(parent["platform"])
            timestamp = _parse_time(str(parent["game_creation"]))
            metadata = GroupMetadata(group, platform, timestamp)
            previous = all_groups.setdefault(group, metadata)
            if previous != metadata:
                raise ValueError("paired group metadata differs")
            alignment.update(
                f"{row_index}\0{parent_hash_row}\0{rune.focal_row_key}\n".encode()
            )
            if timestamp >= holdout_start:
                holdout_groups.setdefault(group, metadata)
                continue
            rows.append(_development_row(row_index, parent, rune, timestamp))
    if (
        parent_count != scope["expected_focal_rows"]
        or len(all_groups) != scope["expected_match_groups"]
    ):
        raise ValueError("Stage 3.5A parent population differs")
    if rune_digest.hexdigest() != scope["rune_dataset_sha256"]:
        raise ValueError("Stage 3.5A rune derived fingerprint differs")
    if len(holdout_groups) != protocol["folds"]["expected_final_holdout_groups"]:
        raise ValueError("Stage 3.5B final holdout membership differs")
    _validate_group_rows(tuple(rows), all_groups, holdout_groups)
    return LoadedData(
        rows=tuple(rows),
        all_groups=tuple(
            sorted(
                all_groups.values(),
                key=lambda row: (row.game_creation, row.match_group_id),
            )
        ),
        holdout_groups=tuple(
            sorted(
                holdout_groups.values(),
                key=lambda row: (row.game_creation, row.match_group_id),
            )
        ),
        parent_file_sha256=parent_hash,
        rune_derived_sha256=rune_digest.hexdigest(),
        alignment_sha256=alignment.hexdigest(),
    )


def construct_folds(
    rows: tuple[DevelopmentRow, ...],
    all_groups: tuple[GroupMetadata, ...],
    protocol: dict[str, Any],
) -> tuple[OuterFold, ...]:
    folds: list[OuterFold] = []
    group_time = {group.match_group_id: group.game_creation for group in all_groups}
    for raw in protocol["folds"]["outer_blocks"]:
        start = _parse_time(raw["start_utc"])
        end = _parse_time(raw["end_utc"])
        inner_start = start - timedelta(
            hours=protocol["folds"]["inner_validation_hours"]
        )
        train = tuple(i for i, row in enumerate(rows) if row.game_creation < start)
        valid = tuple(
            i for i, row in enumerate(rows) if start <= row.game_creation < end
        )
        inner_train = tuple(i for i in train if rows[i].game_creation < inner_start)
        inner_valid = tuple(
            i for i in train if inner_start <= rows[i].game_creation < start
        )
        fold = OuterFold(raw["id"], train, valid, inner_train, inner_valid, start, end)
        if len(valid) // 2 != raw["expected_groups"]:
            raise ValueError("Stage 3.5B outer fold count differs")
        _validate_fold(fold, rows, group_time)
        folds.append(fold)
    if (
        sum(len(fold.validation_indexes) // 2 for fold in folds)
        != protocol["folds"]["expected_development_evaluation_groups"]
    ):
        raise ValueError("Stage 3.5B evaluation population differs")
    initial = sum(
        group.game_creation
        < _parse_time(protocol["folds"]["initial_training_end_exclusive_utc"])
        for group in all_groups
    )
    if initial != protocol["folds"]["expected_initial_training_groups"]:
        raise ValueError("Stage 3.5B initial training population differs")
    return tuple(folds)


def _development_row(
    row_index: int, parent: dict[str, Any], rune: RuneFeatureRow, timestamp: datetime
) -> DevelopmentRow:
    if parent.get("public_patch") != "26.15" or parent.get("queue_id") != 420:
        raise ValueError("Stage 3.5B parent patch or queue differs")
    trajectory = parent.get("trajectory")
    if not isinstance(trajectory, dict):
        raise ValueError("Stage 3.5B trajectory is missing")
    continuous: dict[str, float] = {}
    for minute in (5, 10, 15):
        frame = trajectory.get(str(minute), trajectory.get(minute))
        if not isinstance(frame, dict):
            raise ValueError("Stage 3.5B primary trajectory frame is missing")
        for source, prefix in (
            ("gold_difference", "gold_difference"),
            ("xp_difference", "xp_difference"),
            ("lane_minion_cs_difference", "lane_minion_cs_difference"),
            ("total_farm_difference", "total_farm_difference"),
            ("level_difference", "level_difference"),
        ):
            value = frame.get(source)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise ValueError("Stage 3.5B continuous target is invalid")
            continuous[f"{prefix}_{minute}"] = float(value)
    tower = parent.get("tower")
    tower_label = tower.get("primary_label_at_15") if isinstance(tower, dict) else None
    if tower_label not in {
        "enemy_top_outer_first",
        "neither_by_15",
        "allied_top_outer_first",
    }:
        raise ValueError("Stage 3.5B tower target is invalid")
    side = str(parent.get("focal_side"))
    if side not in {"BLUE", "RED"}:
        raise ValueError("Stage 3.5B side is invalid")
    win = parent.get("game_win")
    if not isinstance(win, bool):
        raise ValueError("Stage 3.5B game-win target is invalid")
    return DevelopmentRow(
        row_index=row_index,
        match_group_id=str(parent["match_group_id"]),
        platform=str(parent["platform"]),
        game_creation=timestamp,
        side=side,
        focal_champion=str(parent["focal_champion_id"]),
        enemy_champion=str(parent["enemy_top_champion_id"]),
        focal_keystone=str(rune.keystone_id),
        weight=float(parent["scientific_weight"]),
        continuous=continuous,
        tower_outcome=str(tower_label),
        game_win=int(win),
    )


def _validate_group_rows(
    rows: tuple[DevelopmentRow, ...],
    all_groups: dict[str, GroupMetadata],
    holdout_groups: dict[str, GroupMetadata],
) -> None:
    grouped: dict[str, list[DevelopmentRow]] = defaultdict(list)
    for row in rows:
        grouped[row.match_group_id].append(row)
    development_groups = set(all_groups) - set(holdout_groups)
    if set(grouped) != development_groups:
        raise ValueError("Stage 3.5B development/holdout partition differs")
    for values in grouped.values():
        if len(values) != 2 or not math.isclose(sum(row.weight for row in values), 1.0):
            raise ValueError("Stage 3.5B paired perspective invariant differs")


def _validate_fold(
    fold: OuterFold,
    rows: tuple[DevelopmentRow, ...],
    group_time: dict[str, datetime],
) -> None:
    for label, indexes in (
        ("train", fold.train_indexes),
        ("validation", fold.validation_indexes),
        ("inner_train", fold.inner_train_indexes),
        ("inner_validation", fold.inner_validation_indexes),
    ):
        if not indexes:
            raise ValueError(f"Stage 3.5B {label} fold is empty")
        counts = Counter(rows[index].platform for index in indexes)
        if set(counts) != {"eun1", "euw1"}:
            raise ValueError("Stage 3.5B fold lacks a required platform")
    train_groups = {rows[i].match_group_id for i in fold.train_indexes}
    valid_groups = {rows[i].match_group_id for i in fold.validation_indexes}
    if train_groups & valid_groups:
        raise ValueError("Stage 3.5B fold group leakage")
    if max(group_time[group] for group in train_groups) >= min(
        group_time[group] for group in valid_groups
    ):
        raise ValueError("Stage 3.5B training does not strictly precede validation")


def _validate_protocol_contract(protocol: dict[str, Any]) -> None:
    candidates = protocol["candidates"]
    allowed = set(protocol["features"]["allowed"])
    if candidates.get("three_way_matchup_keystone_primary_candidate") is not False:
        raise ValueError("three-way matchup-keystone candidate is forbidden")
    for candidate in candidates["linear"] + candidates["nonlinear"]:
        fields = candidate["features"]
        if len(fields) != len(set(fields)) or not set(fields) <= allowed:
            raise ValueError("Stage 3.5B candidate feature schema differs")
        lowered = {str(field).lower() for field in fields}
        if lowered & FORBIDDEN_FEATURE_TOKENS:
            raise ValueError("Stage 3.5B forbidden feature is configured")
        if "focal_champion_by_focal_keystone" in fields and not {
            "focal_champion",
            "focal_keystone",
        } <= set(fields):
            raise ValueError("Stage 3.5B interaction hierarchy differs")
    budget = protocol["operation_budget"]
    if (
        budget["bootstrap_model_fits"] != 0
        or protocol["metrics"]["bootstrap_replicates"] != 2000
    ):
        raise ValueError("Stage 3.5B bootstrap contract differs")
    if protocol["features"]["platform_predictive_feature"]:
        raise ValueError("platform cannot be a Stage 3.5B predictor")


def _validate_software_versions(protocol: dict[str, Any]) -> None:
    expected = protocol["hyperparameters"]["software_versions"]
    observed = {
        "python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}",
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "scikit_learn": sklearn.__version__,
        "catboost": catboost_version,
    }
    if observed != expected:
        raise ValueError("Stage 3.5B frozen software versions differ")


def _expected_operation_budget(protocol: dict[str, Any]) -> dict[str, int]:
    folds = len(protocol["folds"]["outer_blocks"])
    outcomes = len(protocol["outcomes"]["co_primary"]) + len(
        protocol["outcomes"]["continuous_secondary"]
    )
    analytic = len(protocol["candidates"]["analytic"]) * outcomes * folds
    analytic += len(protocol["outcomes"]["categorical_secondary"]) * folds
    optimizer_candidates = len(protocol["candidates"]["linear"]) + len(
        protocol["candidates"]["nonlinear"]
    )
    selection = (
        optimizer_candidates * 2 * len(protocol["outcomes"]["co_primary"]) * folds
    )
    outer = optimizer_candidates * outcomes * folds
    categorical = 2 * len(protocol["outcomes"]["categorical_secondary"]) * folds
    optimizer = selection + outer + categorical
    comparisons = (len(protocol["comparisons"]) + 1) * outcomes + 2 * len(
        protocol["outcomes"]["categorical_secondary"]
    )
    return {
        "analytic_training_operations": analytic,
        "optimizer_fits": optimizer,
        "total_training_operations": analytic + optimizer,
        "bootstrap_model_fits": 0,
        "bootstrap_comparison_evaluations": comparisons,
    }


def _feature_schema(protocol: dict[str, Any]) -> dict[str, Any]:
    return {
        "allowed": protocol["features"]["allowed"],
        "forbidden": protocol["features"]["forbidden"],
        "candidates": protocol["candidates"],
        "rare_keystone_policy": {
            "minimum_training_rows": protocol["features"][
                "rare_keystone_minimum_training_rows"
            ],
            "fallback": "__RARE_KEYSTONE__",
            "learned_from": "training_fold_only",
        },
        "one_hot_unknown": "ignore_zero_contribution",
        "catboost_category_handling": "ordered_native_training_fold_only",
    }


def _fold_summary(fold: OuterFold, rows: tuple[DevelopmentRow, ...]) -> dict[str, Any]:
    return {
        "id": fold.fold_id,
        "training_groups": len(fold.train_indexes) // 2,
        "validation_groups": len(fold.validation_indexes) // 2,
        "inner_training_groups": len(fold.inner_train_indexes) // 2,
        "inner_validation_groups": len(fold.inner_validation_indexes) // 2,
        "validation_platform_groups": {
            platform: len(
                {
                    rows[i].match_group_id
                    for i in fold.validation_indexes
                    if rows[i].platform == platform
                }
            )
            for platform in ("eun1", "euw1")
        },
        "start_utc": fold.start.isoformat().replace("+00:00", "Z"),
        "end_utc": fold.end.isoformat().replace("+00:00", "Z"),
    }


def _fold_fingerprint(
    folds: tuple[OuterFold, ...], rows: tuple[DevelopmentRow, ...]
) -> str:
    def groups(indexes: tuple[int, ...]) -> list[str]:
        return sorted(
            {_private_group_digest(rows[index].match_group_id) for index in indexes}
        )

    payload = []
    for fold in folds:
        payload.append(
            {
                "id": fold.fold_id,
                "train": groups(fold.train_indexes),
                "validation": groups(fold.validation_indexes),
                "inner_train": groups(fold.inner_train_indexes),
                "inner_validation": groups(fold.inner_validation_indexes),
            }
        )
    return _sha256_json(payload)


def _private_group_digest(group: str) -> str:
    return hashlib.sha256(f"stage35b-fold\0{group}".encode()).hexdigest()


def _parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _model_iterations(model: Any) -> int | None:
    if hasattr(model, "tree_count_"):
        value = model.tree_count_
        return int(value) if value is not None else None
    if hasattr(model, "n_iter_"):
        value = np.asarray(model.n_iter_).reshape(-1)
        return int(value.max()) if value.size else None
    if isinstance(model, Pipeline) and hasattr(model[-1], "n_iter_"):
        value = np.asarray(model[-1].n_iter_).reshape(-1)
        return int(value.max()) if value.size else None
    return None


def _bundle_hash(items: tuple[tuple[str, str], ...]) -> str:
    return _sha256_json(dict(sorted(items)))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _require_finite(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _require_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _require_finite(item)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Stage 3.5B payload contains NaN or infinity")


def evaluate_development(
    preflight: Preflight,
    protocol: dict[str, Any],
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], tuple[dict[str, Any], ...]]:
    """Execute the one frozen real-data experiment and return aggregate results."""

    rows = preflight.data.rows
    folds = preflight.folds
    continuous_outcomes = tuple(
        protocol["outcomes"]["co_primary"]
        + protocol["outcomes"]["continuous_secondary"]
    )
    analytic_candidates = tuple(protocol["candidates"]["analytic"])
    learned_candidates = tuple(
        protocol["candidates"]["linear"] + protocol["candidates"]["nonlinear"]
    )
    all_candidates = analytic_candidates + tuple(
        candidate["id"] for candidate in learned_candidates
    )
    ledger = OperationLedger(progress_callback)
    predictions = {
        outcome: {
            candidate: np.full(len(rows), np.nan, dtype=float)
            for candidate in all_candidates
        }
        for outcome in continuous_outcomes
    }
    support_tiers: list[str | None] = [None] * len(rows)
    outer_block: list[str | None] = [None] * len(rows)
    selected_configs: dict[str, dict[str, str]] = defaultdict(dict)
    for fold in folds:
        if progress_callback:
            progress_callback({"event": "outer_fold_started", "fold": fold.fold_id})
        train_rows = _take(rows, fold.train_indexes)
        valid_rows = _take(rows, fold.validation_indexes)
        support = Counter(row.focal_champion for row in train_rows)
        for index in fold.validation_indexes:
            support_tiers[index] = _support_tier(support[rows[index].focal_champion])
            outer_block[index] = fold.fold_id
        for outcome in continuous_outcomes:
            global_value = _weighted_mean(
                np.asarray([row.continuous[outcome] for row in train_rows]),
                np.asarray([row.weight for row in train_rows]),
            )
            champion_values = _champion_means(
                train_rows,
                outcome,
                protocol["features"]["champion_mean_minimum_training_rows"],
                global_value,
            )
            predictions[outcome]["global_mean"][list(fold.validation_indexes)] = (
                global_value
            )
            predictions[outcome]["focal_champion_mean"][
                list(fold.validation_indexes)
            ] = [
                champion_values.get(row.focal_champion, global_value)
                for row in valid_rows
            ]
            ledger.analytic(
                phase="outer_evaluation",
                fold=fold.fold_id,
                model="global_mean",
                outcome=outcome,
            )
            ledger.analytic(
                phase="outer_evaluation",
                fold=fold.fold_id,
                model="focal_champion_mean",
                outcome=outcome,
            )
        for candidate in learned_candidates:
            selected = _select_configuration(
                rows,
                fold,
                candidate,
                protocol,
                ledger,
            )
            selected_configs[fold.fold_id][candidate["id"]] = selected["id"]
            for outcome in continuous_outcomes:
                train_prepared, valid_prepared = _prepare_features(
                    train_rows,
                    valid_rows,
                    candidate["features"],
                    protocol["features"]["rare_keystone_minimum_training_rows"],
                )
                y_train = np.asarray(
                    [row.continuous[outcome] for row in train_rows], dtype=float
                )
                weights = np.asarray([row.weight for row in train_rows], dtype=float)
                model = ledger.fit(
                    partial(
                        _fit_regressor,
                        candidate,
                        selected,
                        train_prepared,
                        y_train,
                        weights,
                        protocol,
                    ),
                    phase="outer_evaluation",
                    fold=fold.fold_id,
                    model=candidate["id"],
                    configuration=selected["id"],
                    outcome=outcome,
                )
                values = _predict_regressor(candidate, model, valid_prepared)
                _require_finite(values.tolist())
                predictions[outcome][candidate["id"]][list(fold.validation_indexes)] = (
                    values
                )
        if progress_callback:
            progress_callback({"event": "outer_fold_completed", "fold": fold.fold_id})

    evaluation_indexes = tuple(
        index for fold in folds for index in fold.validation_indexes
    )
    continuous_results = _continuous_results(
        rows,
        evaluation_indexes,
        predictions,
        support_tiers,
        outer_block,
    )
    comparisons, best_linear = _continuous_comparisons(
        rows,
        evaluation_indexes,
        predictions,
        continuous_results,
        outer_block,
        protocol,
    )
    categorical_results, categorical_predictions, categorical_comparisons = (
        _evaluate_categorical(
            rows,
            folds,
            support_tiers,
            outer_block,
            protocol,
            ledger,
        )
    )
    comparisons.extend(categorical_comparisons)
    gates = _gate_decisions(
        comparisons,
        continuous_results,
        best_linear,
        protocol,
    )
    expected = protocol["operation_budget"]
    if (
        ledger.optimizer_fits != expected["optimizer_fits"]
        or ledger.analytic_operations != expected["analytic_training_operations"]
        or ledger.failures != 0
    ):
        raise ValueError("Stage 3.5B observed operation count differs")
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "interpretation": (
            "rolling_origin_patch_26.15_development_estimates_not_final_test"
        ),
        "co_primary_outcomes": protocol["outcomes"]["co_primary"],
        "continuous_results": continuous_results,
        "categorical_secondary_results": categorical_results,
        "selected_configurations_by_outer_fold": dict(sorted(selected_configs.items())),
        "best_regularized_linear_comparator": best_linear,
        "gate_decisions": gates,
        "final_holdout_evaluated": False,
        "product_recommendation_authorized": False,
        "rune_interpretation": "rune-conditioned expected lane performance",
    }
    private_rows = _private_prediction_rows(
        rows,
        evaluation_indexes,
        predictions,
        categorical_predictions,
        support_tiers,
        outer_block,
    )
    ledger_payload = {
        "schema_version": "stage3.5b-operation-ledger-v1",
        "expected": expected,
        "actual": {
            "analytic_training_operations": ledger.analytic_operations,
            "optimizer_fits": ledger.optimizer_fits,
            "total_training_operations": ledger.analytic_operations
            + ledger.optimizer_fits,
            "bootstrap_model_fits": 0,
            "bootstrap_comparison_evaluations": len(comparisons),
            "failures": ledger.failures,
            "retries": 0,
        },
        "all_counts_reconciled": len(comparisons)
        == expected["bootstrap_comparison_evaluations"],
    }
    if not ledger_payload["all_counts_reconciled"]:
        raise ValueError("Stage 3.5B bootstrap comparison count differs")
    result["_paired_comparisons"] = comparisons
    for payload in (result, comparisons, gates, ledger_payload):
        _require_finite(payload)
    return result, ledger_payload, tuple(private_rows)


def _select_configuration(
    rows: tuple[DevelopmentRow, ...],
    fold: OuterFold,
    candidate: dict[str, Any],
    protocol: dict[str, Any],
    ledger: OperationLedger,
) -> dict[str, Any]:
    grid = _candidate_grid(candidate, protocol)
    inner_train = _take(rows, fold.inner_train_indexes)
    inner_valid = _take(rows, fold.inner_validation_indexes)
    train_prepared, valid_prepared = _prepare_features(
        inner_train,
        inner_valid,
        candidate["features"],
        protocol["features"]["rare_keystone_minimum_training_rows"],
    )
    scores: list[tuple[float, str, dict[str, Any]]] = []
    for configuration in grid:
        normalized: list[float] = []
        for outcome in protocol["outcomes"]["co_primary"]:
            y_train = np.asarray(
                [row.continuous[outcome] for row in inner_train], dtype=float
            )
            y_valid = np.asarray(
                [row.continuous[outcome] for row in inner_valid], dtype=float
            )
            weights = np.asarray([row.weight for row in inner_train], dtype=float)
            model = ledger.fit(
                partial(
                    _fit_regressor,
                    candidate,
                    configuration,
                    train_prepared,
                    y_train,
                    weights,
                    protocol,
                ),
                phase="inner_selection",
                fold=fold.fold_id,
                model=candidate["id"],
                configuration=configuration["id"],
                outcome=outcome,
            )
            prediction = _predict_regressor(candidate, model, valid_prepared)
            scale = float(np.std(y_train))
            if not math.isfinite(scale) or scale <= 0:
                raise ValueError("Stage 3.5B inner target scale is invalid")
            normalized.append(float(np.mean(np.abs(prediction - y_valid))) / scale)
        scores.append(
            (statistics.fmean(normalized), configuration["id"], configuration)
        )
    scores.sort(key=lambda value: (round(value[0], 12), value[1]))
    return scores[0][2]


def _candidate_grid(
    candidate: dict[str, Any], protocol: dict[str, Any]
) -> list[dict[str, Any]]:
    estimator = candidate.get("estimator")
    if estimator == "ridge":
        return protocol["hyperparameters"]["ridge_grid"]
    if estimator == "elastic_net":
        return protocol["hyperparameters"]["elastic_net_grid"]
    return protocol["hyperparameters"]["catboost_grid"]


def _prepare_features(
    training_rows: tuple[DevelopmentRow, ...],
    scoring_rows: tuple[DevelopmentRow, ...],
    fields: list[str],
    rare_keystone_minimum: int,
) -> tuple[list[list[str]], list[list[str]]]:
    support = Counter(row.focal_keystone for row in training_rows)
    supported_keystones = {
        key for key, count in support.items() if count >= rare_keystone_minimum
    }

    def encode(row: DevelopmentRow) -> list[str]:
        keystone = (
            row.focal_keystone
            if row.focal_keystone in supported_keystones
            else "__RARE_KEYSTONE__"
        )
        values = {
            "focal_champion": row.focal_champion,
            "enemy_top_champion": row.enemy_champion,
            "directional_matchup_pair": f"{row.focal_champion}>{row.enemy_champion}",
            "focal_keystone": keystone,
            "focal_champion_by_focal_keystone": f"{row.focal_champion}>{keystone}",
            "side": row.side,
        }
        return [values[field] for field in fields]

    return [encode(row) for row in training_rows], [encode(row) for row in scoring_rows]


def _fit_regressor(
    candidate: dict[str, Any],
    configuration: dict[str, Any],
    features: list[list[str]],
    target: np.ndarray,
    weights: np.ndarray,
    protocol: dict[str, Any],
) -> Any:
    estimator = candidate.get("estimator")
    if estimator == "ridge":
        model: Any = Pipeline(
            [
                ("categories", OneHotEncoder(handle_unknown="ignore")),
                (
                    "model",
                    Ridge(
                        alpha=configuration["alpha"],
                        solver=protocol["hyperparameters"]["ridge_solver"],
                    ),
                ),
            ]
        )
        model.fit(features, target, model__sample_weight=weights)
        return model
    if estimator == "elastic_net":
        model = Pipeline(
            [
                ("categories", OneHotEncoder(handle_unknown="ignore")),
                (
                    "model",
                    ElasticNet(
                        alpha=configuration["alpha"],
                        l1_ratio=configuration["l1_ratio"],
                        max_iter=protocol["hyperparameters"][
                            "elastic_net_max_iterations"
                        ],
                        tol=protocol["hyperparameters"]["elastic_net_tolerance"],
                        selection=protocol["hyperparameters"]["elastic_net_selection"],
                    ),
                ),
            ]
        )
        model.fit(features, target, model__sample_weight=weights)
        if (
            int(np.asarray(model[-1].n_iter_).max())
            >= protocol["hyperparameters"]["elastic_net_max_iterations"]
        ):
            raise ValueError("Stage 3.5B Elastic Net did not converge")
        return model
    model = CatBoostRegressor(
        loss_function="RMSE",
        random_seed=protocol["hyperparameters"]["seed"],
        iterations=configuration["iterations"],
        depth=configuration["depth"],
        learning_rate=configuration["learning_rate"],
        l2_leaf_reg=configuration["l2_leaf_reg"],
        boosting_type=protocol["hyperparameters"]["catboost_boosting_type"],
        bootstrap_type=protocol["hyperparameters"]["catboost_bootstrap_type"],
        thread_count=protocol["hyperparameters"]["catboost_thread_count"],
        allow_writing_files=False,
        verbose=False,
    )
    model.fit(
        features,
        target,
        cat_features=list(range(len(candidate["features"]))),
        sample_weight=weights,
    )
    return model


def _predict_regressor(
    candidate: dict[str, Any], model: Any, features: list[list[str]]
) -> np.ndarray:
    return np.asarray(model.predict(features), dtype=float)


def _champion_means(
    rows: tuple[DevelopmentRow, ...], outcome: str, minimum: int, fallback: float
) -> dict[str, float]:
    values: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        values[row.focal_champion].append((row.continuous[outcome], row.weight))
    return {
        champion: _weighted_mean(
            np.asarray([value for value, _ in observed]),
            np.asarray([weight for _, weight in observed]),
        )
        if len(observed) >= minimum
        else fallback
        for champion, observed in values.items()
    }


def _weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    denominator = float(weights.sum())
    if denominator <= 0:
        raise ValueError("Stage 3.5B weighted denominator is invalid")
    return float(np.dot(values, weights) / denominator)


def _support_tier(count: int) -> str:
    for lower, upper, label in SUPPORT_TIERS:
        if lower <= count <= upper:
            return label
    raise ValueError("Stage 3.5B training support tier is invalid")


def _take(
    rows: tuple[DevelopmentRow, ...], indexes: tuple[int, ...]
) -> tuple[DevelopmentRow, ...]:
    return tuple(rows[index] for index in indexes)


def _continuous_results(
    rows: tuple[DevelopmentRow, ...],
    indexes: tuple[int, ...],
    predictions: dict[str, dict[str, np.ndarray]],
    support_tiers: list[str | None],
    outer_blocks: list[str | None],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for outcome, candidates in predictions.items():
        target = np.asarray([rows[i].continuous[outcome] for i in indexes])
        weights = np.asarray([rows[i].weight for i in indexes])
        model_results: dict[str, Any] = {}
        for candidate, all_prediction in candidates.items():
            prediction = all_prediction[list(indexes)]
            overall = _continuous_metrics(target, prediction, weights)
            slices: dict[str, dict[str, Any]] = {}
            slice_values = {
                "platform": [rows[i].platform for i in indexes],
                "outer_block": [outer_blocks[i] for i in indexes],
                "side": [rows[i].side for i in indexes],
                "training_support_tier": [support_tiers[i] for i in indexes],
            }
            for slice_name, values in slice_values.items():
                slices[slice_name] = {}
                for value in sorted(set(values), key=str):
                    selected = np.asarray([item == value for item in values])
                    slices[slice_name][str(value)] = _continuous_metrics(
                        target[selected], prediction[selected], weights[selected]
                    )
            model_results[candidate] = {"overall": overall, "slices": slices}
        output[outcome] = model_results
    return output


def _continuous_metrics(
    target: np.ndarray, prediction: np.ndarray, weights: np.ndarray
) -> dict[str, Any]:
    finite = np.isfinite(prediction)
    coverage = float(np.dot(finite.astype(float), weights) / weights.sum())
    if not finite.all():
        raise ValueError("Stage 3.5B prediction coverage is incomplete")
    error = prediction - target
    mean_target = _weighted_mean(target, weights)
    denominator = float(np.dot(weights, (target - mean_target) ** 2))
    r2 = (
        None
        if denominator == 0
        else 1.0 - float(np.dot(weights, error**2)) / denominator
    )
    return {
        "rows": len(target),
        "match_weight": float(weights.sum()),
        "mae": _weighted_mean(np.abs(error), weights),
        "rmse": math.sqrt(_weighted_mean(error**2, weights)),
        "r2": r2,
        "sign_accuracy": _weighted_mean(
            (np.sign(prediction) == np.sign(target)).astype(float), weights
        ),
        "coverage": coverage,
    }


def _continuous_comparisons(
    rows: tuple[DevelopmentRow, ...],
    indexes: tuple[int, ...],
    predictions: dict[str, dict[str, np.ndarray]],
    results: dict[str, Any],
    outer_blocks: list[str | None],
    protocol: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    primary = protocol["outcomes"]["co_primary"]
    linear_ids = [candidate["id"] for candidate in protocol["candidates"]["linear"]]
    normalized_scores: list[tuple[float, int, str]] = []
    for priority, candidate in enumerate(linear_ids):
        scores = []
        for outcome in primary:
            scale = float(np.std([rows[i].continuous[outcome] for i in indexes]))
            scores.append(results[outcome][candidate]["overall"]["mae"] / scale)
        normalized_scores.append((statistics.fmean(scores), priority, candidate))
    normalized_scores.sort(key=lambda item: (round(item[0], 12), item[1], item[2]))
    best_linear = normalized_scores[0][2]
    pairs = [tuple(pair) for pair in protocol["comparisons"]]
    pairs.append(("catboost", best_linear))
    comparisons: list[dict[str, Any]] = []
    for outcome in predictions:
        for complex_model, simpler_model in pairs:
            comparisons.append(
                _paired_continuous_comparison(
                    rows,
                    indexes,
                    outcome,
                    complex_model,
                    simpler_model,
                    predictions[outcome][complex_model],
                    predictions[outcome][simpler_model],
                    outer_blocks,
                    protocol,
                )
            )
    return comparisons, best_linear


def _paired_continuous_comparison(
    rows: tuple[DevelopmentRow, ...],
    indexes: tuple[int, ...],
    outcome: str,
    complex_model: str,
    simpler_model: str,
    complex_prediction: np.ndarray,
    simpler_prediction: np.ndarray,
    outer_blocks: list[str | None],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    group_values: dict[str, list[tuple[float, float, str, str]]] = defaultdict(list)
    for index in indexes:
        target = rows[index].continuous[outcome]
        difference = abs(complex_prediction[index] - target) - abs(
            simpler_prediction[index] - target
        )
        group_values[rows[index].match_group_id].append(
            (
                difference,
                rows[index].weight,
                rows[index].platform,
                str(outer_blocks[index]),
            )
        )
    group_records: list[tuple[float, str, str]] = []
    for values in group_values.values():
        if len(values) != 2 or not math.isclose(sum(value[1] for value in values), 1.0):
            raise ValueError("Stage 3.5B paired bootstrap group differs")
        group_records.append(
            (
                sum(value[0] * value[1] for value in values),
                values[0][2],
                values[0][3],
            )
        )
    point, lower, upper = _stratified_bootstrap(
        group_records,
        protocol["metrics"]["bootstrap_replicates"],
        protocol["metrics"]["bootstrap_seed"],
        f"continuous|{outcome}|{complex_model}|{simpler_model}",
    )
    simpler_mae = _weighted_mean(
        np.asarray(
            [abs(simpler_prediction[i] - rows[i].continuous[outcome]) for i in indexes]
        ),
        np.asarray([rows[i].weight for i in indexes]),
    )
    platform = _group_delta_slices(group_records, position=1)
    chronology = _group_delta_slices(group_records, position=2)
    return {
        "endpoint_type": "continuous",
        "outcome": outcome,
        "complex_model": complex_model,
        "simpler_model": simpler_model,
        "metric": "mae",
        "delta_complex_minus_simpler": point,
        "confidence_interval_95": [lower, upper],
        "absolute_effect": point,
        "relative_effect": None if simpler_mae == 0 else point / simpler_mae,
        "platform_delta": platform,
        "chronological_block_delta": chronology,
        "bootstrap_replicates": protocol["metrics"]["bootstrap_replicates"],
        "bootstrap_model_fits": 0,
    }


def _stratified_bootstrap(
    records: list[tuple[float, str, str]],
    replicates: int,
    seed: int,
    label: str,
) -> tuple[float, float, float]:
    strata: dict[tuple[str, str], np.ndarray] = {}
    for platform, block in sorted({(row[1], row[2]) for row in records}):
        strata[(platform, block)] = np.asarray(
            [row[0] for row in records if row[1] == platform and row[2] == block],
            dtype=float,
        )
    derived_seed = seed + int(hashlib.sha256(label.encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(derived_seed)
    samples = np.empty(replicates, dtype=float)
    total = len(records)
    for replicate in range(replicates):
        numerator = 0.0
        for values in strata.values():
            numerator += float(rng.choice(values, size=len(values), replace=True).sum())
        samples[replicate] = numerator / total
    point = statistics.fmean(record[0] for record in records)
    return point, float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975))


def _group_delta_slices(
    records: list[tuple[float, str, str]], position: int
) -> dict[str, float]:
    output: dict[str, float] = {}
    for value in sorted({record[position] for record in records}):
        selected = [record[0] for record in records if record[position] == value]
        output[value] = statistics.fmean(selected)
    return output


def _evaluate_categorical(
    rows: tuple[DevelopmentRow, ...],
    folds: tuple[OuterFold, ...],
    support_tiers: list[str | None],
    outer_blocks: list[str | None],
    protocol: dict[str, Any],
    ledger: OperationLedger,
) -> tuple[dict[str, Any], dict[str, dict[str, np.ndarray]], list[dict[str, Any]]]:
    classes_by_endpoint = {
        "tower_outcome_15": (
            "allied_top_outer_first",
            "enemy_top_outer_first",
            "neither_by_15",
        ),
        "game_win": (0, 1),
    }
    predictions: dict[str, dict[str, np.ndarray]] = {}
    for endpoint, classes in classes_by_endpoint.items():
        predictions[endpoint] = {
            "global_probability": np.full((len(rows), len(classes)), np.nan),
            "regularized_logistic": np.full((len(rows), len(classes)), np.nan),
            "catboost": np.full((len(rows), len(classes)), np.nan),
        }
        for fold in folds:
            train_rows = _take(rows, fold.train_indexes)
            valid_rows = _take(rows, fold.validation_indexes)
            y_train_raw = [
                row.tower_outcome if endpoint == "tower_outcome_15" else row.game_win
                for row in train_rows
            ]
            class_index = {value: index for index, value in enumerate(classes)}
            y_train = np.asarray(
                [class_index[value] for value in y_train_raw], dtype=int
            )
            weights = np.asarray([row.weight for row in train_rows], dtype=float)
            counts = np.bincount(y_train, weights=weights, minlength=len(classes))
            probabilities = counts / counts.sum()
            predictions[endpoint]["global_probability"][
                list(fold.validation_indexes), :
            ] = probabilities
            ledger.analytic(
                phase="outer_secondary_classification",
                fold=fold.fold_id,
                model="global_probability",
                outcome=endpoint,
            )
            fields = [
                "focal_champion",
                "enemy_top_champion",
                "side",
                "directional_matchup_pair",
                "focal_keystone",
                "focal_champion_by_focal_keystone",
            ]
            train_x, valid_x = _prepare_features(
                train_rows,
                valid_rows,
                fields,
                protocol["features"]["rare_keystone_minimum_training_rows"],
            )
            logistic = ledger.fit(
                partial(_fit_logistic, train_x, y_train, weights, protocol),
                phase="outer_secondary_classification",
                fold=fold.fold_id,
                model="regularized_logistic",
                configuration="logistic-c-1",
                outcome=endpoint,
            )
            logistic_values = logistic.predict_proba(valid_x)
            predictions[endpoint]["regularized_logistic"][
                list(fold.validation_indexes), :
            ] = logistic_values
            cat = ledger.fit(
                partial(
                    _fit_catboost_classifier,
                    train_x,
                    y_train,
                    weights,
                    len(classes),
                    protocol,
                ),
                phase="outer_secondary_classification",
                fold=fold.fold_id,
                model="catboost",
                configuration="cat-depth-4",
                outcome=endpoint,
            )
            cat_values = cat.predict_proba(valid_x)
            predictions[endpoint]["catboost"][list(fold.validation_indexes), :] = (
                cat_values
            )
    indexes = tuple(index for fold in folds for index in fold.validation_indexes)
    results: dict[str, Any] = {}
    comparisons: list[dict[str, Any]] = []
    for endpoint, classes in classes_by_endpoint.items():
        target = np.asarray(
            [
                classes.index(
                    rows[index].tower_outcome
                    if endpoint == "tower_outcome_15"
                    else rows[index].game_win
                )
                for index in indexes
            ],
            dtype=int,
        )
        weights = np.asarray([rows[index].weight for index in indexes], dtype=float)
        results[endpoint] = {}
        for model, all_values in predictions[endpoint].items():
            values = all_values[list(indexes), :]
            results[endpoint][model] = {
                "overall": _classification_metrics(target, values, weights),
                "platform": {
                    platform: _classification_metrics(
                        target[[rows[i].platform == platform for i in indexes]],
                        values[[rows[i].platform == platform for i in indexes]],
                        weights[[rows[i].platform == platform for i in indexes]],
                    )
                    for platform in ("eun1", "euw1")
                },
                "chronological_block": {
                    fold.fold_id: _classification_metrics(
                        target[[outer_blocks[i] == fold.fold_id for i in indexes]],
                        values[[outer_blocks[i] == fold.fold_id for i in indexes]],
                        weights[[outer_blocks[i] == fold.fold_id for i in indexes]],
                    )
                    for fold in folds
                },
            }
        for complex_model, simpler_model in (
            ("regularized_logistic", "global_probability"),
            ("catboost", "regularized_logistic"),
        ):
            comparisons.append(
                _paired_classification_comparison(
                    rows,
                    indexes,
                    endpoint,
                    target,
                    predictions[endpoint][complex_model],
                    predictions[endpoint][simpler_model],
                    outer_blocks,
                    complex_model,
                    simpler_model,
                    protocol,
                )
            )
    return results, predictions, comparisons


def _fit_logistic(
    features: list[list[str]],
    target: np.ndarray,
    weights: np.ndarray,
    protocol: dict[str, Any],
) -> Pipeline:
    model = Pipeline(
        [
            ("categories", OneHotEncoder(handle_unknown="ignore")),
            (
                "model",
                LogisticRegression(
                    C=protocol["hyperparameters"]["secondary_logistic_c"],
                    max_iter=protocol["hyperparameters"][
                        "secondary_logistic_max_iterations"
                    ],
                    tol=protocol["hyperparameters"]["secondary_logistic_tolerance"],
                    random_state=protocol["hyperparameters"]["seed"],
                ),
            ),
        ]
    )
    model.fit(features, target, model__sample_weight=weights)
    if (
        int(np.asarray(model[-1].n_iter_).max())
        >= protocol["hyperparameters"]["secondary_logistic_max_iterations"]
    ):
        raise ValueError("Stage 3.5B logistic model did not converge")
    return model


def _fit_catboost_classifier(
    features: list[list[str]],
    target: np.ndarray,
    weights: np.ndarray,
    class_count: int,
    protocol: dict[str, Any],
) -> CatBoostClassifier:
    configuration = protocol["hyperparameters"]["catboost_grid"][0]
    model = CatBoostClassifier(
        loss_function="MultiClass" if class_count > 2 else "Logloss",
        random_seed=protocol["hyperparameters"]["seed"],
        iterations=configuration["iterations"],
        depth=configuration["depth"],
        learning_rate=configuration["learning_rate"],
        l2_leaf_reg=configuration["l2_leaf_reg"],
        boosting_type=protocol["hyperparameters"]["catboost_boosting_type"],
        bootstrap_type=protocol["hyperparameters"]["catboost_bootstrap_type"],
        thread_count=protocol["hyperparameters"]["catboost_thread_count"],
        allow_writing_files=False,
        verbose=False,
    )
    model.fit(
        features,
        target,
        cat_features=list(range(len(features[0]))),
        sample_weight=weights,
    )
    return model


def _classification_metrics(
    target: np.ndarray, probabilities: np.ndarray, weights: np.ndarray
) -> dict[str, Any]:
    if not np.isfinite(probabilities).all() or not np.allclose(
        probabilities.sum(axis=1), 1.0, atol=1e-8
    ):
        raise ValueError("Stage 3.5B classification probabilities are invalid")
    one_hot = np.eye(probabilities.shape[1])[target]
    brier = _weighted_mean(np.sum((probabilities - one_hot) ** 2, axis=1), weights)
    confidence = probabilities.max(axis=1)
    correct = (probabilities.argmax(axis=1) == target).astype(float)
    ece = 0.0
    total = float(weights.sum())
    for lower in np.linspace(0.0, 0.9, 10):
        selected = (confidence >= lower) & (confidence < lower + 0.1 + 1e-12)
        if selected.any():
            bin_weight = float(weights[selected].sum())
            ece += (
                bin_weight
                / total
                * abs(
                    _weighted_mean(correct[selected], weights[selected])
                    - _weighted_mean(confidence[selected], weights[selected])
                )
            )
    return {
        "rows": len(target),
        "match_weight": total,
        "log_loss": float(
            log_loss(
                target,
                probabilities,
                sample_weight=weights,
                labels=list(range(probabilities.shape[1])),
            )
        ),
        "brier": brier,
        "expected_calibration_error": ece,
        "class_coverage": {
            str(index): int(np.sum(target == index))
            for index in range(probabilities.shape[1])
        },
        "prediction_coverage": 1.0,
    }


def _paired_classification_comparison(
    rows: tuple[DevelopmentRow, ...],
    indexes: tuple[int, ...],
    endpoint: str,
    target: np.ndarray,
    complex_all: np.ndarray,
    simpler_all: np.ndarray,
    outer_blocks: list[str | None],
    complex_model: str,
    simpler_model: str,
    protocol: dict[str, Any],
) -> dict[str, Any]:
    complex_values = complex_all[list(indexes)]
    simpler_values = simpler_all[list(indexes)]
    group_values: dict[str, list[tuple[float, float, str, str]]] = defaultdict(list)
    for offset, index in enumerate(indexes):
        difference = -math.log(
            max(complex_values[offset, target[offset]], 1e-15)
        ) + math.log(max(simpler_values[offset, target[offset]], 1e-15))
        group_values[rows[index].match_group_id].append(
            (
                difference,
                rows[index].weight,
                rows[index].platform,
                str(outer_blocks[index]),
            )
        )
    records = [
        (
            sum(value[0] * value[1] for value in values),
            values[0][2],
            values[0][3],
        )
        for values in group_values.values()
    ]
    point, lower, upper = _stratified_bootstrap(
        records,
        protocol["metrics"]["bootstrap_replicates"],
        protocol["metrics"]["bootstrap_seed"],
        f"categorical|{endpoint}|{complex_model}|{simpler_model}",
    )
    return {
        "endpoint_type": "categorical_secondary",
        "outcome": endpoint,
        "complex_model": complex_model,
        "simpler_model": simpler_model,
        "metric": "log_loss",
        "delta_complex_minus_simpler": point,
        "confidence_interval_95": [lower, upper],
        "absolute_effect": point,
        "relative_effect": None,
        "platform_delta": _group_delta_slices(records, 1),
        "chronological_block_delta": _group_delta_slices(records, 2),
        "bootstrap_replicates": protocol["metrics"]["bootstrap_replicates"],
        "bootstrap_model_fits": 0,
    }


def _gate_decisions(
    comparisons: list[dict[str, Any]],
    continuous_results: dict[str, Any],
    best_linear: str,
    protocol: dict[str, Any],
) -> dict[str, Any]:
    primary = protocol["outcomes"]["co_primary"]

    def comparison(
        outcome: str, complex_model: str, simpler_model: str
    ) -> dict[str, Any]:
        matches = [
            row
            for row in comparisons
            if row["endpoint_type"] == "continuous"
            and row["outcome"] == outcome
            and row["complex_model"] == complex_model
            and row["simpler_model"] == simpler_model
        ]
        if len(matches) != 1:
            raise ValueError("Stage 3.5B gate comparison is missing")
        return matches[0]

    def decide(name: str, complex_model: str, simpler_model: str) -> dict[str, Any]:
        evidence = [
            comparison(outcome, complex_model, simpler_model) for outcome in primary
        ]
        checks = {
            "both_point_estimates_favour_complex": all(
                row["delta_complex_minus_simpler"] < 0 for row in evidence
            ),
            "both_interval_upper_bounds_below_zero": all(
                row["confidence_interval_95"][1] < 0 for row in evidence
            ),
            "both_platforms_favour_complex_for_both_outcomes": all(
                all(value < 0 for value in row["platform_delta"].values())
                for row in evidence
            ),
            "at_least_three_blocks_favour_complex_for_both_outcomes": all(
                sum(value < 0 for value in row["chronological_block_delta"].values())
                >= protocol["gates"]["minimum_favourable_chronological_blocks"]
                for row in evidence
            ),
            "complete_prediction_coverage": all(
                continuous_results[outcome][complex_model]["overall"]["coverage"] == 1.0
                for outcome in primary
            ),
        }
        return {
            "gate": name,
            "complex_model": complex_model,
            "simpler_model": simpler_model,
            "checks": checks,
            "passed": all(checks.values()),
            "development_only": True,
        }

    matchup = decide("matchup", "matchup_ridge", "additive_ridge")
    keystone = decide("keystone", "matchup_keystone_ridge", "matchup_ridge")
    champion_keystone = decide(
        "champion_keystone", "champion_keystone_ridge", "matchup_keystone_ridge"
    )
    catboost = decide("catboost", "catboost", best_linear)
    eligible = "additive_ridge"
    if matchup["passed"]:
        eligible = "matchup_ridge"
    if matchup["passed"] and keystone["passed"]:
        eligible = "matchup_keystone_ridge"
    if matchup["passed"] and keystone["passed"] and champion_keystone["passed"]:
        eligible = "champion_keystone_ridge"
    if catboost["passed"]:
        eligible = "catboost"
    return {
        "matchup": matchup,
        "keystone": keystone,
        "champion_keystone": champion_keystone,
        "catboost": catboost,
        "eligible_structure_for_separately_authorized_final_holdout": eligible,
        "generic_matchup_recommendation_authorized": False,
        "keystone_recommendation_authorized": False,
        "product_recommendation_authorized": False,
        "secondary_outcomes_used_to_rescue_primary_gate": False,
    }


def _private_prediction_rows(
    rows: tuple[DevelopmentRow, ...],
    indexes: tuple[int, ...],
    continuous: dict[str, dict[str, np.ndarray]],
    categorical: dict[str, dict[str, np.ndarray]],
    support_tiers: list[str | None],
    outer_blocks: list[str | None],
) -> list[dict[str, Any]]:
    output = []
    for index in indexes:
        row = rows[index]
        output.append(
            {
                "schema_version": "stage3.5b-private-oof-row-v1",
                "parent_row_index": row.row_index,
                "match_group_id": row.match_group_id,
                "platform": row.platform,
                "outer_block": outer_blocks[index],
                "side": row.side,
                "training_support_tier": support_tiers[index],
                "continuous_targets": row.continuous,
                "continuous_predictions": {
                    outcome: {
                        model: float(values[index]) for model, values in models.items()
                    }
                    for outcome, models in continuous.items()
                },
                "categorical_targets": {
                    "tower_outcome_15": row.tower_outcome,
                    "game_win": row.game_win,
                },
                "categorical_predictions": {
                    outcome: {
                        model: values[index].tolist()
                        for model, values in models.items()
                    }
                    for outcome, models in categorical.items()
                },
            }
        )
    return output


def build_publication_payloads(
    *,
    preflight: Preflight,
    protocol: dict[str, Any],
    protocol_path: Path,
    schema_path: Path,
    repository_commit: str,
    result: dict[str, Any],
    operation_ledger: dict[str, Any],
    private_rows: tuple[dict[str, Any], ...],
) -> tuple[dict[str, bytes], dict[str, bytes], str]:
    comparisons = _extract_comparisons(result)
    gates = result["gate_decisions"]
    input_manifest = {
        "schema_version": "stage3.5b-input-manifest-v1",
        "repository_commit": repository_commit,
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": sha256_file(protocol_path),
        "protocol_schema_sha256": sha256_file(schema_path),
        "preflight_sha256": preflight.summary["scientific_preflight_sha256"],
        "parent_dataset_sha256": preflight.summary["parent_dataset_sha256"],
        "rune_dataset_sha256": preflight.summary["rune_dataset_sha256"],
        "alignment_sha256": preflight.summary["alignment_sha256"],
        "fold_sha256": preflight.summary["fold_sha256"],
        "feature_schema_sha256": preflight.summary["feature_schema_sha256"],
        "executable_bundle_sha256": preflight.summary["executable_bundle_sha256"],
        "development_evaluation_groups": preflight.summary[
            "development_evaluation_groups"
        ],
        "final_holdout_groups": preflight.summary["final_holdout_groups"],
        "final_holdout_evaluated": False,
    }
    quality = {
        "schema_version": "stage3.5b-quality-v1",
        "all_sealed_fingerprints_verified": True,
        "fold_and_feature_fingerprints_verified": True,
        "operation_counts_reconciled": operation_ledger["all_counts_reconciled"],
        "all_predictions_finite": True,
        "all_predictions_complete": True,
        "paired_group_weight_preserved": True,
        "bootstrap_model_fits": 0,
        "row_level_predictions_published": False,
        "identifiers_published": False,
        "external_paths_published": False,
        "coefficients_published": False,
        "platform_used_as_predictor": False,
        "history_or_opponent_account_used": False,
        "final_holdout_targets_accessed": False,
        "product_recommendation_authorized": False,
    }
    public_objects = {
        "development_results.json": result,
        "paired_comparisons.json": {
            "schema_version": "stage3.5b-paired-comparisons-v1",
            "comparisons": comparisons,
        },
        "gate_decisions.json": gates,
        "operation_ledger.json": operation_ledger,
        "input_manifest.json": input_manifest,
        "quality_report.json": quality,
    }
    report = _render_development_report(result, operation_ledger).encode("utf-8")
    payloads = {name: _json_bytes(value) for name, value in public_objects.items()}
    payloads["development_report.md"] = report
    scientific_hash = _bundle_hash(
        tuple(
            (name, hashlib.sha256(value).hexdigest())
            for name, value in payloads.items()
        )
    )
    bundle_manifest = {
        "schema_version": "stage3.5b-bundle-manifest-v1",
        "protocol_id": protocol["protocol_id"],
        "repository_commit": repository_commit,
        "scientific_result_bundle_sha256": scientific_hash,
        "published_file_sha256": {
            name: hashlib.sha256(value).hexdigest()
            for name, value in sorted(payloads.items())
        },
        "private_oof_rows": len(private_rows),
        "private_oof_rows_committed": False,
        "final_holdout_evaluated": False,
    }
    payloads["bundle_manifest.json"] = _json_bytes(bundle_manifest)
    if set(payloads) != EXPECTED_PUBLIC_FILES:
        raise ValueError("Stage 3.5B publication file set differs")
    private_payloads = {
        "oof_predictions.jsonl": b"".join(_json_bytes(row) for row in private_rows),
        "private_manifest.json": _json_bytes(
            {
                "schema_version": "stage3.5b-private-manifest-v1",
                "parent_dataset_sha256": preflight.summary["parent_dataset_sha256"],
                "rune_dataset_sha256": preflight.summary["rune_dataset_sha256"],
                "fold_sha256": preflight.summary["fold_sha256"],
                "oof_rows": len(private_rows),
                "final_holdout_rows": 0,
            }
        ),
    }
    for _name, value in public_objects.items():
        validate_public_payload(value)
        _require_finite(value)
    validate_public_payload(bundle_manifest)
    return payloads, private_payloads, scientific_hash


def _extract_comparisons(result: dict[str, Any]) -> list[dict[str, Any]]:
    comparisons = result.pop("_paired_comparisons", None)
    if not isinstance(comparisons, list):
        raise ValueError("Stage 3.5B paired comparisons are missing")
    return comparisons


def attach_comparisons(
    result: dict[str, Any], comparisons: list[dict[str, Any]]
) -> None:
    result["_paired_comparisons"] = comparisons


def write_publication(
    *,
    public_payloads: dict[str, bytes],
    private_payloads: dict[str, bytes],
    config: Stage35BExecutionConfig,
) -> None:
    if sum(map(len, public_payloads.values())) > config.maximum_publication_bytes:
        raise ValueError("Stage 3.5B public bundle exceeds its size ceiling")
    _atomic_publish_directory(config.private_output_directory, private_payloads)
    try:
        _atomic_publish_directory(config.aggregate_output_directory, public_payloads)
    except Exception:
        shutil.rmtree(config.private_output_directory, ignore_errors=True)
        raise


def reconstruct_bundle_hash(path: Path) -> str:
    manifest = json.loads((path / "bundle_manifest.json").read_text("utf-8"))
    hashes = {
        name: sha256_file(path / name)
        for name in EXPECTED_PUBLIC_FILES
        if name != "bundle_manifest.json"
    }
    if hashes != manifest.get("published_file_sha256"):
        raise ValueError("Stage 3.5B published file hashes differ")
    return _bundle_hash(tuple(hashes.items()))


def _render_development_report(
    result: dict[str, Any], operation_ledger: dict[str, Any]
) -> str:
    gold = result["continuous_results"]["gold_difference_10"]
    xp = result["continuous_results"]["xp_difference_10"]
    gates = result["gate_decisions"]
    lines = [
        "# Nexus Lens Stage 3.5B rolling-origin development results",
        "",
        "These are patch-26.15 development estimates, not final-holdout performance.",
        "No product, matchup, or rune recommendation is authorized.",
        "",
        "## Co-primary MAE",
        "",
        "| Model | Gold 10 | XP 10 |",
        "|---|---:|---:|",
    ]
    for model in gold:
        lines.append(
            f"| {model} | {gold[model]['overall']['mae']:.6f} | "
            f"{xp[model]['overall']['mae']:.6f} |"
        )
    lines.extend(
        [
            "",
            "## Frozen gates",
            "",
            f"- Matchup gate: `{gates['matchup']['passed']}`",
            f"- Keystone gate: `{gates['keystone']['passed']}`",
            f"- Champion-keystone gate: `{gates['champion_keystone']['passed']}`",
            f"- CatBoost gate: `{gates['catboost']['passed']}`",
            "- Product recommendation authorized: `False`",
            "",
            "## Operation reconciliation",
            "",
            "- Analytic operations: "
            f"`{operation_ledger['actual']['analytic_training_operations']}`",
            f"- Optimizer fits: `{operation_ledger['actual']['optimizer_fits']}`",
            "- Bootstrap model fits: `0`",
            "- Retries: `0`",
            "",
            "Rune results, if any, are observational rune-conditioned expected "
            "lane performance and are not causal switching effects.",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_publish_directory(path: Path, payloads: dict[str, bytes]) -> None:
    if path.exists():
        raise ValueError("Stage 3.5B output directory already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
    try:
        for name, payload in payloads.items():
            (temporary / name).write_bytes(payload)
        os.replace(temporary, path)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
