from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEAL_ROOT = ROOT / "config" / "data_seals" / "patch26.15-10k-v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _load(name: str) -> dict[str, object]:
    return json.loads((SEAL_ROOT / name).read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_two_region_milestone_is_isolated_and_reconciled() -> None:
    milestone = _load("milestone.json")
    regions = milestone["regions"]

    assert milestone["public_patch"] == "26.15"
    assert milestone["queue_id"] == 420
    assert milestone["total_regional_match_observations"] == 10_000
    assert milestone["collection_policy"]["platforms_pooled"] is False
    assert set(regions) == {"eune", "euw"}
    assert sum(row["combined_unique_matches"] for row in regions.values()) == 10_000
    for row in regions.values():
        assert row["external_accepted_matches"] + row["retained_private_matches"] == (
            row["combined_unique_matches"]
        )
        assert row["accepted_retained_overlap"] == 0
        assert row["regional_routing"] == "europe"
    assert milestone["scientific_state_at_seal"] == {
        "model_fitted_or_evaluated": False,
        "patch_26_16_included": False,
        "recommendation_policy_selected": False,
        "seal_type": "post_collection_data_and_provenance_milestone",
    }


def test_seal_hashes_are_well_formed_and_freeze_hashes_match() -> None:
    milestone = _load("milestone.json")
    euw = _load("euw.data-seal.json")
    freeze = milestone["pre_collection_scientific_freeze"]

    assert euw["inventory"]["repeated_results_identical"] is True
    assert euw["inventory"]["exclusions"] == []
    assert euw["dataset"]["combined_unique_matches"] == 5_000
    hashes = [
        euw["inventory"]["sha256"],
        euw["dataset"]["accepted_set_sha256"],
        euw["dataset"]["retained_set_sha256"],
        *(item["sha256"] for item in euw["critical_files"].values()),
        *(item["sha256"] for item in euw["publications"].values()),
        milestone["regions"]["eune"]["accepted_set_sha256"],
        milestone["regions"]["eune"]["retained_set_sha256"],
        *milestone["regions"]["eune"]["critical_file_sha256"].values(),
        *milestone["regions"]["eune"]["publication_sha256"].values(),
    ]
    assert all(SHA256.fullmatch(value) for value in hashes)
    assert freeze["eune_freeze_sha256"] == _sha256(
        ROOT / "config/evaluation/stage3.4a-pre-26.16-v1/eune.freeze.json"
    )
    assert freeze["euw_freeze_sha256"] == _sha256(
        ROOT / "config/evaluation/stage3.4a-pre-26.16-v1/euw.freeze.json"
    )
    assert freeze["prospective_sample_size_sha256"] == _sha256(
        ROOT
        / "config/evaluation/stage3.4a-pre-26.16-v1/prospective_sample_size.json"
    )
    assert freeze["dependency_lock_sha256"] == _sha256(
        ROOT / "config/evaluation/stage3.4a-pre-26.16-v1.dependency-lock.txt"
    )


def test_repository_seals_use_only_logical_paths() -> None:
    rendered = "\n".join(
        (SEAL_ROOT / name).read_text(encoding="utf-8")
        for name in ("euw.data-seal.json", "milestone.json")
    )

    assert not re.search(r"(?i)(?:^|[\"\s])[a-z]:[\\/]", rendered)
    assert "puuid" not in rendered.lower()
    assert "summoner" not in rendered.lower()
