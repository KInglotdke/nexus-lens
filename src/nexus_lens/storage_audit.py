"""Read-only storage readiness inventory for retained Nexus Lens artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

STORAGE_AUDIT_SCHEMA_VERSION = "stage3.3c-storage-audit-v1"


def audit_storage(data_root: Path) -> dict[str, Any]:
    """Return an aggregate inventory without writing or mutating the data tree."""

    resolved_root = data_root.resolve()
    files = [path for path in resolved_root.rglob("*") if path.is_file()]
    component_bytes: Counter[str] = Counter()
    component_files: Counter[str] = Counter()
    scope_bytes: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    scope_files: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    file_hashes: dict[tuple[str, str], dict[str, Counter[tuple[str, int]]]] = (
        defaultdict(lambda: defaultdict(Counter))
    )
    for path in files:
        relative = path.relative_to(resolved_root)
        size = path.stat().st_size
        component = classify_component(relative)
        scenario, platform = _scope(relative)
        component_bytes[component] += size
        component_files[component] += 1
        scope_bytes[(scenario, platform)][component] += size
        scope_files[(scenario, platform)][component] += 1
        if scenario in {"canary", "pilot"}:
            file_hashes[(scenario, platform)][component][
                (_sha256_file(path), size)
            ] += 1
    accepted = _accepted_counts(resolved_root)
    shared_sources = _pilot_shared_source_components(resolved_root)
    scopes = []
    for key in sorted(set(scope_bytes) | set(accepted)):
        scenario, platform = key
        physical_bytes = sum(scope_bytes[key].values())
        shared_bytes = sum(shared_sources.get(key, {}).values())
        analysis_bytes = physical_bytes + shared_bytes
        accepted_matches = accepted.get(key)
        scopes.append(
            {
                "scenario": scenario,
                "platform": platform,
                "accepted_matches": accepted_matches,
                "physical_total_bytes": physical_bytes,
                "shared_operational_source_bytes": shared_bytes,
                "analysis_footprint_bytes": analysis_bytes,
                "bytes_per_accepted_match": (
                    analysis_bytes / accepted_matches if accepted_matches else None
                ),
                "component_bytes": dict(sorted(scope_bytes[key].items())),
                "shared_operational_source_component_bytes": dict(
                    sorted(shared_sources.get(key, {}).items())
                ),
                "component_files": dict(sorted(scope_files[key].items())),
            }
        )
    duplicate_rows = []
    for platform in sorted(
        {platform for scenario, platform in file_hashes if scenario == "pilot"}
    ):
        by_component = {}
        duplicate_files = duplicate_bytes = 0
        for component in sorted(
            set(file_hashes.get(("canary", platform), {}))
            | set(file_hashes.get(("pilot", platform), {}))
        ):
            canary = file_hashes[("canary", platform)][component]
            pilot = file_hashes[("pilot", platform)][component]
            duplicate_component_files = duplicate_component_bytes = 0
            for key in canary.keys() & pilot.keys():
                copies = min(canary[key], pilot[key])
                duplicate_component_files += copies
                duplicate_component_bytes += copies * key[1]
            by_component[component] = {
                "duplicate_files": duplicate_component_files,
                "duplicate_bytes": duplicate_component_bytes,
            }
            duplicate_files += duplicate_component_files
            duplicate_bytes += duplicate_component_bytes
        raw = by_component.get(
            "raw_payloads", {"duplicate_files": 0, "duplicate_bytes": 0}
        )
        duplicate_rows.append(
            {
                "platform": platform,
                "exact_duplicate_files": duplicate_files,
                "exact_duplicate_bytes": duplicate_bytes,
                "exact_raw_payload_duplicate_files": raw["duplicate_files"],
                "exact_raw_payload_duplicate_bytes": raw["duplicate_bytes"],
                "by_component": by_component,
                "method": "sha256_and_size_intersection",
            }
        )
    projections = _pilot_projections(scope_bytes, shared_sources, accepted)
    free_bytes = shutil.disk_usage(resolved_root).free
    combined_headroom = sum(
        row["conservative_additional_headroom_bytes"] for row in projections
    )
    return {
        "schema_version": STORAGE_AUDIT_SCHEMA_VERSION,
        "mode": "read_only",
        "data_root": resolved_root.as_posix(),
        "current": {
            "total_bytes": sum(component_bytes.values()),
            "total_files": sum(component_files.values()),
            "component_bytes": dict(sorted(component_bytes.items())),
            "component_files": dict(sorted(component_files.items())),
            "scopes": scopes,
        },
        "canary_pilot_exact_raw_duplication": duplicate_rows,
        "ten_thousand_match_per_platform_projection": projections,
        "free_space": {
            "observed_free_bytes": free_bytes,
            "combined_conservative_additional_headroom_bytes": combined_headroom,
            "headroom_sufficient_for_combined_projection": free_bytes
            >= combined_headroom,
            "warning": (
                "Filesystem free space can change concurrently; this is a read-only "
                "point estimate, not a reservation."
            ),
        },
        "retention_guidance": {
            "must_retain_for_strongest_reproducibility": [
                "raw payloads with content hashes",
                "collection manifests, catalogs, and resumable checkpoints",
                "configuration, schema, code version, and lineage manifests",
            ],
            "reversible_options_not_executed": [
                "losslessly compress immutable raw JSON with post-migration hash "
                "manifests",
                "move raw and processed roots only through a verified copy-and-hash "
                "migration",
                "regenerate derived normalized and Stage 3 outputs from retained raw "
                "inputs",
            ],
            "future_layout_options_not_executed": [
                "separate raw and processed storage roots",
                "keep temporary publication staging on the processed volume",
                "apply an explicit derived-artifact retention policy after "
                "reproducibility tests",
            ],
        },
    }


def classify_component(relative: Path) -> str:
    parts = tuple(part.lower() for part in relative.parts)
    name = relative.name.lower()
    if "raw" in parts:
        return "raw_payloads"
    if "checkpoint" in name:
        return "checkpoints"
    if relative.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return "catalogs"
    if "normalized" in parts or (
        "processed" in parts
        and any(part.startswith("region=") for part in parts)
        and any(part.startswith("patch=") for part in parts)
    ):
        return "normalized"
    if "processed" in parts and "stage3" in parts:
        return "stage_outputs"
    if "processed" in parts and "lineage" in parts:
        return "lineage"
    return "manifests_reports_other"


def _scope(relative: Path) -> tuple[str, str]:
    parts = relative.parts
    scenario = parts[0].lower() if parts else "root"
    platform = "unscoped"
    if scenario in {"canary", "pilot"} and len(parts) >= 3:
        platform = parts[2].lower()
    return scenario, platform


def _accepted_counts(root: Path) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for scenario in ("canary", "pilot"):
        scenario_root = root / scenario
        if not scenario_root.exists():
            continue
        for path in scenario_root.glob(
            "*/[Ee][Uu]*/processed/stage3/schema=stage3.1-v1/run=*/matches.jsonl"
        ):
            relative = path.relative_to(root)
            key = (scenario, relative.parts[2].lower())
            line_count = sum(1 for line in path.open(encoding="utf-8") if line.strip())
            counts[key] = max(counts.get(key, 0), line_count)
    return counts


def _pilot_shared_source_components(
    root: Path,
) -> dict[tuple[str, str], Counter[str]]:
    """Find operational sources referenced by pilot Stage 3.1 manifests."""

    shared: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    repository_root = root.parent
    for metadata_path in root.glob(
        "pilot/*/*/processed/stage3/schema=stage3.1-v1/run=*/metadata.json"
    ):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            manifest_value = metadata.get("input_manifest")
            if not isinstance(manifest_value, str):
                continue
            manifest_path = (repository_root / manifest_value).resolve()
            manifest_path.relative_to(root)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if len(manifest_path.parents) < 3:
            continue
        platform_root = manifest_path.parents[2]
        relative_metadata = metadata_path.relative_to(root)
        key = ("pilot", relative_metadata.parts[2].lower())
        for path in platform_root.rglob("*"):
            if not path.is_file():
                continue
            component = classify_component(path.relative_to(root))
            if component in {"stage_outputs", "lineage"}:
                continue
            shared[key][component] += path.stat().st_size
    return shared


def _pilot_projections(
    scope_bytes: dict[tuple[str, str], Counter[str]],
    shared_sources: dict[tuple[str, str], Counter[str]],
    accepted: dict[tuple[str, str], int],
) -> list[dict[str, Any]]:
    rows = []
    for (scenario, platform), components in sorted(scope_bytes.items()):
        if scenario != "pilot" or not accepted.get((scenario, platform)):
            continue
        current_matches = accepted[(scenario, platform)]
        effective_components = components + shared_sources.get(
            (scenario, platform), Counter()
        )
        factor = 10_000 / current_matches
        current_total = sum(effective_components.values())
        projected_components = {
            name: round(value * factor)
            for name, value in sorted(effective_components.items())
        }
        projected_total = sum(projected_components.values())
        projected_derived = sum(
            projected_components.get(name, 0)
            for name in ("normalized", "stage_outputs", "lineage")
        )
        incremental_permanent = max(projected_total - current_total, 0)
        temporary_publication_peak = projected_derived
        conservative = round(
            (incremental_permanent + temporary_publication_peak) * 1.10
        )
        rows.append(
            {
                "platform": platform,
                "basis_accepted_matches": current_matches,
                "target_accepted_matches": 10_000,
                "linear_scale_factor": factor,
                "projected_component_bytes": projected_components,
                "projected_permanent_bytes": projected_total,
                "incremental_permanent_bytes": incremental_permanent,
                "temporary_publication_peak_bytes": temporary_publication_peak,
                "conservative_additional_headroom_bytes": conservative,
                "formula": (
                    "1.10 * (incremental projected permanent bytes + one projected "
                    "normalized/stage/lineage publication copy)"
                ),
            }
        )
    return rows


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_json(data_root: Path) -> str:
    return json.dumps(audit_storage(data_root), indent=2, sort_keys=True) + "\n"
