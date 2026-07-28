import hashlib
import json
from pathlib import Path

import pytest

from nexus_lens.canonical import (
    CanonicalMatch,
    CanonicalParticipant,
    Stage3ValidationError,
)
from nexus_lens.lineage import (
    LINEAGE_SCHEMA_VERSION,
    _build_dataset,
    run_lineage_repair,
    write_lineage_dataset,
)
from nexus_lens.privacy import pseudonymize_puuid

RUN_ID = "20260722T125547567196Z-population"


def make_inputs(
    *,
    sources: list[dict[str, object]] | None = None,
    candidates: list[dict[str, object]] | None = None,
    participant_puuids: tuple[str, ...] = ("seed-a", "other"),
) -> dict[str, object]:
    source_rows = sources or [{"puuid": "seed-a", "tier": "GOLD", "division": "I"}]
    candidate_rows = candidates or [
        {
            "puuid": "seed-a",
            "tier": "GOLD",
            "rank": "I",
            "queueType": "RANKED_SOLO_5x5",
        }
    ]
    participants = [
        CanonicalParticipant.model_construct(
            match_id="MATCH",
            participant_id=index,
            player_key=pseudonymize_puuid(puuid),
        )
        for index, puuid in enumerate(participant_puuids, 1)
    ]
    return {
        "run_id": RUN_ID,
        "checkpoint": {
            "run_id": RUN_ID,
            "config": {
                "platform": "eun1",
                "regional_routing": "europe",
                "analysis_region": "eune",
                "queue_id": 420,
            },
            "matches": {"MATCH": {"status": "accepted", "sources": source_rows}},
            "sampling": {
                "candidates": {"GOLD:I:1": candidate_rows},
                "candidate_observed_at": {},
            },
        },
        "checkpoint_path": Path("checkpoint.json"),
        "checkpoint_sha256": "checkpoint-hash",
        "manifest": {
            "run_id": RUN_ID,
            "platform": "eun1",
            "regional_routing": "europe",
            "routing_region": "eune",
        },
        "manifest_path": Path("manifest.json"),
        "manifest_sha256": "manifest-hash",
        "participants": participants,
        "matches": [
            CanonicalMatch.model_construct(
                match_id="MATCH", platform="eun1", queue_id=420
            )
        ],
        "stage3b_directory": Path("stage3b"),
        "stage3b_hashes": {"metadata.json": "stage3b-hash"},
        "prior": {
            "stage3_1": {
                "directory": Path("stage31"),
                "sha256": {"metadata.json": "stage31-hash"},
            },
            "stage3_2": {
                "directory": Path("stage32"),
                "sha256": {"metadata.json": "stage32-hash"},
            },
            "stage3_3a": {
                "directory": Path("stage33a"),
                "sha256": {"metadata.json": "stage33a-hash"},
            },
        },
    }


def build_dataset(tmp_path: Path, **overrides: object):  # type: ignore[no-untyped-def]
    return _build_dataset(
        make_inputs(**overrides),
        tmp_path / f"schema={LINEAGE_SCHEMA_VERSION}" / f"run={RUN_ID}",
    )


def test_collection_context_and_observed_rank_remain_distinct(
    tmp_path: Path,
) -> None:
    dataset = build_dataset(tmp_path)
    match = dataset.match_lineage[0]
    seed = dataset.participant_rank_lineage[0]
    other = dataset.participant_rank_lineage[1]

    assert match.platform_id == "eun1"
    assert match.regional_routing == "europe"
    assert match.analysis_region == "eune"
    assert match.discovery_contexts[0].collection_context_status == (
        "collection_context"
    )
    assert match.participant_rank_is_match_rank is False
    assert seed.rank_status == "observed"
    assert seed.rank_tier == "GOLD"
    assert other.rank_status == "not_collected"


def test_multiple_discovery_contexts_are_sorted_and_do_not_duplicate_rows(
    tmp_path: Path,
) -> None:
    sources = [
        {"puuid": "seed-b", "tier": "PLATINUM", "division": "II"},
        {"puuid": "seed-a", "tier": "GOLD", "division": "I"},
        {"puuid": "seed-a", "tier": "GOLD", "division": "I"},
    ]
    candidates = [
        {
            "puuid": seed,
            "tier": tier,
            "rank": division,
            "queueType": "RANKED_SOLO_5x5",
        }
        for seed, tier, division in (
            ("seed-a", "GOLD", "I"),
            ("seed-b", "PLATINUM", "II"),
        )
    ]

    dataset = build_dataset(
        tmp_path,
        sources=sources,
        candidates=candidates,
        participant_puuids=("seed-a", "seed-b"),
    )
    match = dataset.match_lineage[0]

    assert len(dataset.match_lineage) == 1
    assert len(dataset.participant_rank_lineage) == 2
    assert match.discovery_context_count == 2
    assert match.multiple_discovery_contexts is True
    assert match.multiple_collection_strata is True
    assert [row.context_key for row in match.discovery_contexts] == sorted(
        row.context_key for row in match.discovery_contexts
    )


def test_conflicting_observed_rank_is_ambiguous(tmp_path: Path) -> None:
    candidates = [
        {
            "puuid": "seed-a",
            "tier": tier,
            "rank": division,
            "queueType": "RANKED_SOLO_5x5",
        }
        for tier, division in (("GOLD", "I"), ("PLATINUM", "II"))
    ]

    dataset = build_dataset(tmp_path, candidates=candidates)
    seed = dataset.participant_rank_lineage[0]
    context = dataset.match_lineage[0].discovery_contexts[0]

    assert seed.rank_status == "ambiguous"
    assert seed.rank_tier is None
    assert len(seed.rank_observations) == 2
    assert context.seed_rank_status == "ambiguous"


def test_only_provable_rank_is_backfilled_and_report_is_aggregate_only(
    tmp_path: Path,
) -> None:
    dataset = build_dataset(tmp_path)
    rendered = json.dumps(dataset.audit_report, sort_keys=True)

    assert dataset.audit_report["recovery"]["participant_rank_statuses"] == {
        "not_collected": 1,
        "observed": 1,
    }
    for participant in dataset.participant_rank_lineage:
        assert participant.player_key not in rendered
    assert "seed-a" not in rendered
    assert "other" not in rendered


def test_deterministic_publication_does_not_modify_prior_inputs(
    tmp_path: Path,
) -> None:
    prior = tmp_path / "prior.json"
    prior.write_text('{"immutable":true}\n', encoding="utf-8")
    before = hashlib.sha256(prior.read_bytes()).hexdigest()
    dataset = build_dataset(tmp_path)

    output = write_lineage_dataset(dataset)
    first = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output.iterdir()
    }
    write_lineage_dataset(dataset)
    second = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output.iterdir()
    }

    assert first == second
    assert hashlib.sha256(prior.read_bytes()).hexdigest() == before


def test_failed_validation_publishes_nothing(tmp_path: Path) -> None:
    dataset = build_dataset(tmp_path)
    dataset.audit_report["ready_for_forward_lineage_use"] = False

    with pytest.raises(Exception, match="not published"):
        write_lineage_dataset(dataset)

    assert not dataset.output_directory.exists()


def test_conflicting_routing_fails_before_publication(tmp_path: Path) -> None:
    inputs = make_inputs()
    inputs["manifest"]["regional_routing"] = "americas"  # type: ignore[index]
    output = tmp_path / f"schema={LINEAGE_SCHEMA_VERSION}" / f"run={RUN_ID}"

    with pytest.raises(Stage3ValidationError, match="values disagree"):
        _build_dataset(inputs, output)

    assert not output.exists()


def test_validation_only_calls_no_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = make_inputs()
    monkeypatch.setattr("nexus_lens.lineage._load_inputs", lambda **_: inputs)

    def fail_if_written(_dataset: object) -> None:
        raise AssertionError("validation-only mode attempted publication")

    monkeypatch.setattr("nexus_lens.lineage.write_lineage_dataset", fail_if_written)

    result = run_lineage_repair(
        stage3_3b_directory=Path("stage3b"),
        checkpoint_path=Path("checkpoint"),
        manifest_path=Path("manifest"),
        output_root=tmp_path,
        validate_only=True,
    )

    assert result.audit_report["ready_for_forward_lineage_use"] is True
    assert not result.output_directory.exists()
