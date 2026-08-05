from __future__ import annotations

import json
from pathlib import Path

from nexus_lens.storage_audit import audit_storage, classify_component


def test_component_classification() -> None:
    assert classify_component(Path("pilot/26.15/eune/raw/a.json")) == "raw_payloads"
    assert classify_component(Path("pilot/26.15/eune/checkpoint.json")) == "checkpoints"
    assert classify_component(Path("pilot/26.15/eune/catalog.sqlite3")) == "catalogs"
    assert (
        classify_component(Path("pilot/26.15/eune/normalized/a.json")) == "normalized"
    )
    assert (
        classify_component(
            Path("pilot/26.15/eune/processed/region=eune/patch=26.15/a.json")
        )
        == "normalized"
    )
    assert (
        classify_component(Path("pilot/26.15/eune/processed/stage3/a"))
        == "stage_outputs"
    )
    assert classify_component(Path("pilot/26.15/eune/processed/lineage/a")) == "lineage"


def test_storage_audit_is_read_only_and_detects_exact_duplication(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    canary = data / "canary/26.15/eune/raw"
    pilot = data / "pilot/26.15/eune/raw"
    canary.mkdir(parents=True)
    pilot.mkdir(parents=True)
    (canary / "one.json").write_text("same", encoding="utf-8")
    (pilot / "one.json").write_text("same", encoding="utf-8")
    before = {path: path.read_bytes() for path in data.rglob("*") if path.is_file()}

    report = audit_storage(data)

    after = {path: path.read_bytes() for path in data.rglob("*") if path.is_file()}
    assert before == after
    duplication = report["canary_pilot_exact_raw_duplication"][0]
    assert duplication["exact_raw_payload_duplicate_files"] == 1
    assert duplication["exact_raw_payload_duplicate_bytes"] == 4
    assert duplication["exact_duplicate_files"] == 1
    assert report["mode"] == "read_only"


def test_projection_uses_latest_stage31_match_count(tmp_path: Path) -> None:
    root = tmp_path / "data"
    run = root / "pilot/26.15/eune/processed/stage3/schema=stage3.1-v1/run=x"
    run.mkdir(parents=True)
    (run / "matches.jsonl").write_text("{}\n{}\n", encoding="utf-8")
    (root / "pilot/26.15/eune/raw").mkdir(parents=True)
    (root / "pilot/26.15/eune/raw/a.json").write_bytes(b"x" * 10)

    report = audit_storage(root)
    projection = report["ten_thousand_match_per_platform_projection"][0]

    assert projection["basis_accepted_matches"] == 2
    assert projection["linear_scale_factor"] == 5_000
    assert projection["projected_permanent_bytes"] > 0
    assert projection["conservative_additional_headroom_bytes"] > 0


def test_pilot_projection_includes_shared_canary_operational_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    raw_run = root / "canary/26.15/eune/raw/run-pilot"
    raw_run.mkdir(parents=True)
    (raw_run / "manifest.json").write_text("{}", encoding="utf-8")
    (raw_run / "payload.json").write_bytes(b"x" * 20)
    stage31 = root / "pilot/26.15/eune/processed/stage3/schema=stage3.1-v1/run=pilot"
    stage31.mkdir(parents=True)
    (stage31 / "matches.jsonl").write_text("{}\n", encoding="utf-8")
    (stage31 / "metadata.json").write_text(
        json.dumps(
            {"input_manifest": ("data/canary/26.15/eune/raw/run-pilot/manifest.json")}
        ),
        encoding="utf-8",
    )

    report = audit_storage(root)
    pilot = next(
        scope
        for scope in report["current"]["scopes"]
        if scope["scenario"] == "pilot" and scope["platform"] == "eune"
    )

    assert pilot["shared_operational_source_bytes"] == 22
    assert pilot["analysis_footprint_bytes"] > pilot["physical_total_bytes"]
    projection = report["ten_thousand_match_per_platform_projection"][0]
    assert projection["projected_component_bytes"]["raw_payloads"] == 220_000
