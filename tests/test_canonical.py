"""Synthetic coverage for offline Stage 3.1 canonicalization."""

import json
from pathlib import Path

import pytest

import nexus_lens.canonical as canonical
from nexus_lens.canonical import (
    ApprovedPayload,
    Stage3ValidationError,
    build_canonical_dataset,
    build_retained_population_dataset,
    pseudonymize_puuid,
    write_canonical_dataset,
)
from nexus_lens.catalog import ProcessingCatalog
from tests.factories import make_match_payload


def test_normal_match_produces_canonical_rows_without_derived_metrics(
    tmp_path: Path,
) -> None:
    payload = _rich_payload()
    dataset = _build(tmp_path, [payload], {"26.14": 1})

    assert dataset.quality_report["ready_for_stage_3_2"] is True
    assert (len(dataset.matches), len(dataset.participants), len(dataset.teams)) == (
        1,
        10,
        2,
    )
    assert len(dataset.bans) == 10
    participant = dataset.participants[0].model_dump()
    assert participant["physical_damage_dealt_to_champions"] == 8_000
    assert participant["challenge_skillshots_hit"] == 4
    assert participant["control_wards_placed"] == 1
    assert not {"kda", "cs_per_minute", "kill_participation"} & participant.keys()


def test_both_accepted_public_patches_are_preserved(tmp_path: Path) -> None:
    payloads = [
        _rich_payload(match_id="TEST_13", game_version="16.13.1.1"),
        _rich_payload(match_id="TEST_14", game_version="16.14.1.1"),
    ]
    dataset = _build(tmp_path, payloads, {"26.13": 1, "26.14": 1})

    assert [row.public_patch for row in dataset.matches] == ["26.13", "26.14"]


def test_payload_patch_conflict_fails_closed(tmp_path: Path) -> None:
    payload = _rich_payload(game_version="16.13.1.1")
    approved = [_approved(tmp_path, payload, "26.14")]
    dataset = _build_from_approved(tmp_path, approved, {"26.14": 1})

    assert dataset.quality_report["ready_for_stage_3_2"] is False
    assert dataset.quality_report["invariant_failures"] == {
        "payload_patch_conflict": 1,
        "processed_match_count": 1,
        "processed_patch_counts": 1,
    }
    with pytest.raises(Stage3ValidationError, match="not written"):
        write_canonical_dataset(dataset)


def test_manifest_payload_outside_approval_state_is_rejected(tmp_path: Path) -> None:
    inputs = _write_retained_inputs(tmp_path, [_rich_payload()])
    extra = _rich_payload(match_id="UNAPPROVED")
    extra_path = inputs["manifest"].parent / "downloads" / "UNAPPROVED.json"
    extra_path.write_text(json.dumps(extra), encoding="utf-8")
    manifest = json.loads(inputs["manifest"].read_text(encoding="utf-8"))
    manifest["match_files"].append("downloads/UNAPPROVED.json")
    inputs["manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(Stage3ValidationError) as caught:
        _build_from_retained(inputs)
    assert caught.value.category == "unapproved_payload"


def test_duplicate_manifest_payload_is_rejected(tmp_path: Path) -> None:
    inputs = _write_retained_inputs(tmp_path, [_rich_payload()])
    manifest = json.loads(inputs["manifest"].read_text(encoding="utf-8"))
    manifest["match_files"] *= 2
    inputs["manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(Stage3ValidationError) as caught:
        _build_from_retained(inputs)
    assert caught.value.category == "duplicate_match"


def test_duplicate_participant_id_is_rejected(tmp_path: Path) -> None:
    payload = _rich_payload()
    payload["info"]["participants"][1]["participantId"] = 1
    dataset = _build(tmp_path, [payload], {"26.14": 1})

    report = dataset.quality_report
    assert report["ready_for_stage_3_2"] is False
    assert report["shape"]["duplicate_participant_ids_within_matches"] == 1
    assert (
        report["payload_quality"]["malformed_or_skipped_payloads_by_category"][
            "duplicate_participant_id"
        ]
        == 1
    )


def test_missing_challenges_is_nullable_and_reported(tmp_path: Path) -> None:
    payload = _rich_payload()
    for participant in payload["info"]["participants"]:
        participant.pop("challenges")
    dataset = _build(tmp_path, [payload], {"26.14": 1})

    assert (
        dataset.quality_report["payload_quality"]["participants_missing_challenges"]
        == 10
    )
    assert dataset.participants[0].challenge_solo_kills is None


def test_missing_and_ambiguous_positions_are_reported(tmp_path: Path) -> None:
    payload = _rich_payload()
    payload["info"]["participants"][0]["teamPosition"] = ""
    payload["info"]["participants"][1]["individualPosition"] = "TOP"
    dataset = _build(tmp_path, [payload], {"26.14": 1})

    positions = dataset.quality_report["positions"]
    assert positions["missing_team_positions"] == 1
    assert positions["ambiguous_team_vs_individual_positions"] == 1
    assert positions["team_position_distribution"]["<missing>"] == 1


def test_absent_and_incomplete_bans_are_reported(tmp_path: Path) -> None:
    payload = _rich_payload()
    payload["info"]["teams"][0]["bans"] = []
    payload["info"]["teams"][1]["bans"] = payload["info"]["teams"][1]["bans"][:3]
    dataset = _build(tmp_path, [payload], {"26.14": 1})

    assert len(dataset.bans) == 3
    assert dataset.quality_report["bans"] == {
        "rows": 3,
        "expected_slots_per_match": 10,
        "matches_with_incomplete_bans": 1,
        "matches_with_incomplete_bans_rate": 1.0,
        "missing_slots": 7,
        "missing_slot_rate": 0.7,
        "explicit_no_ban_rows": 0,
    }


def test_missing_optional_timestamps_remain_nullable(tmp_path: Path) -> None:
    payload = _rich_payload()
    payload["info"].pop("gameStartTimestamp")
    payload["info"].pop("gameEndTimestamp")
    dataset = _build(tmp_path, [payload], {"26.14": 1})

    row = dataset.matches[0]
    assert row.game_start_timestamp is None and row.game_end_timestamp is None
    assert dataset.quality_report["timestamps_and_duration"][
        "missing_optional_timestamps"
    ] == {"game_end": 1, "game_start": 1}


def test_short_match_rule_is_strictly_below_five_minutes(tmp_path: Path) -> None:
    short = _rich_payload(match_id="SHORT")
    short["info"]["gameDuration"] = 299
    boundary = _rich_payload(match_id="BOUNDARY")
    boundary["info"]["gameDuration"] = 300
    dataset = _build(tmp_path, [short, boundary], {"26.14": 2})

    flags = {row.match_id: row.is_remake_or_short_game for row in dataset.matches}
    assert flags == {"BOUNDARY": False, "SHORT": True}
    assert (
        dataset.quality_report["timestamps_and_duration"]["remake_or_short_game_count"]
        == 1
    )


def test_malformed_critical_duration_is_sanitized(tmp_path: Path) -> None:
    payload = _rich_payload()
    payload["info"]["gameDuration"] = None
    payload["info"]["participants"][0]["summonerName"] = "PRIVATE NAME"
    dataset = _build(tmp_path, [payload], {"26.14": 1})

    rendered = json.dumps(dataset.quality_report)
    assert "PRIVATE NAME" not in rendered
    assert "synthetic-player" not in rendered
    assert (
        dataset.quality_report["payload_quality"][
            "malformed_or_skipped_payloads_by_category"
        ]["malformed_duration"]
        == 1
    )


def test_pseudonym_is_deterministic_and_raw_identifier_is_not_stored(
    tmp_path: Path,
) -> None:
    payload = _rich_payload()
    raw_identifier = payload["info"]["participants"][0]["puuid"]
    dataset = _build(tmp_path, [payload], {"26.14": 1})

    assert pseudonymize_puuid(raw_identifier) == pseudonymize_puuid(raw_identifier)
    row_json = json.dumps(dataset.participants[0].model_dump(mode="json"))
    assert raw_identifier not in row_json
    assert "puuid" not in type(dataset.participants[0]).model_fields
    assert dataset.quality_report["privacy"]["raw_puuid_stored"] is False


def test_deterministic_rerun_publishes_equivalent_files(tmp_path: Path) -> None:
    dataset = _build(tmp_path, [_rich_payload()], {"26.14": 1})
    output = write_canonical_dataset(dataset)
    first = {path.name: path.read_bytes() for path in output.iterdir()}

    write_canonical_dataset(dataset)
    second = {path.name: path.read_bytes() for path in output.iterdir()}

    assert second == first
    assert len(json.loads((output / "matches.jsonl").read_text())) == len(
        dataset.matches[0].model_dump()
    )


def test_staging_failure_preserves_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _build(tmp_path, [_rich_payload()], {"26.14": 1})
    target = dataset.output_directory
    target.mkdir(parents=True)
    sentinel = target / "sentinel.txt"
    sentinel.write_text("retained", encoding="utf-8")

    def fail_write(*_: object, **__: object) -> None:
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(canonical, "_write_staged_dataset", fail_write)
    with pytest.raises(OSError, match="synthetic storage failure"):
        write_canonical_dataset(dataset)
    assert sentinel.read_text(encoding="utf-8") == "retained"


def _rich_payload(
    *, match_id: str = "TEST_1", game_version: str = "16.14.1.1"
) -> dict[str, object]:
    payload = make_match_payload(match_id=match_id, game_version=game_version)
    for participant in payload["info"]["participants"]:
        participant.update(
            {
                "champLevel": 18,
                "detectorWardsPlaced": 1,
                "magicDamageDealtToChampions": 6_000,
                "physicalDamageDealtToChampions": 8_000,
                "timePlayed": 1_800,
                "totalDamageShieldedOnTeammates": 100,
                "totalHealsOnTeammates": 200,
                "trueDamageDealtToChampions": 1_000,
                "visionWardsBoughtInGame": 2,
                "challenges": {
                    "objectivesStolen": 0,
                    "saveAllyFromDeath": 1,
                    "skillshotsDodged": 3,
                    "skillshotsHit": 4,
                    "soloKills": 1,
                    "turretPlatesTaken": 2,
                },
            }
        )
    return payload


def _approved(
    tmp_path: Path, payload: dict[str, object], public_patch: str
) -> ApprovedPayload:
    match_id = payload["metadata"]["matchId"]
    path = tmp_path / f"{match_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    provenance = {
        "match_id": match_id,
        "api_game_version": payload["info"]["gameVersion"],
        "public_patch": public_patch,
        "queue_id": 420,
    }
    return ApprovedPayload(
        match_id=match_id,
        public_patch=public_patch,
        path=path,
        source_reference=f"synthetic/{match_id}.json",
        catalog={
            **provenance,
            "patch_resolution_status": "resolved",
            "status": "processed",
        },
        partition_record=provenance,
    )


def _build(
    tmp_path: Path,
    payloads: list[dict[str, object]],
    patch_counts: dict[str, int],
):
    approved = [
        _approved(
            tmp_path, payload, f"26.{payload['info']['gameVersion'].split('.')[1]}"
        )
        for payload in payloads
    ]
    return _build_from_approved(tmp_path, approved, patch_counts)


def _build_from_approved(
    tmp_path: Path,
    approved: list[ApprovedPayload],
    patch_counts: dict[str, int],
):
    return build_canonical_dataset(
        run_id="synthetic-run",
        input_manifest="synthetic/manifest.json",
        platform="test1",
        approved_payloads=approved,
        output_directory=tmp_path / "stage3-output",
        expected_match_count=sum(patch_counts.values()),
        expected_patch_counts=patch_counts,
    )


def _write_retained_inputs(
    tmp_path: Path, payloads: list[dict[str, object]]
) -> dict[str, object]:
    run_id = "synthetic-population"
    raw_root = tmp_path / "raw"
    raw_run = raw_root / run_id
    downloads = raw_run / "downloads"
    downloads.mkdir(parents=True)
    processed = tmp_path / "processed"
    checkpoint_root = tmp_path / "snapshots" / "population"
    approvals = {}
    match_files = []
    patch_counts = {}
    catalog_path = processed / "catalog.sqlite3"
    with ProcessingCatalog(catalog_path) as catalog:
        for payload in payloads:
            match_id = payload["metadata"]["matchId"]
            minor = payload["info"]["gameVersion"].split(".")[1]
            public_patch = f"26.{minor}"
            path = downloads / f"{match_id}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            match_files.append(f"downloads/{match_id}.json")
            approvals[match_id] = {
                "status": "accepted",
                "public_patch": public_patch,
            }
            patch_counts[public_patch] = patch_counts.get(public_patch, 0) + 1
            catalog.record_processed(
                match_id=match_id,
                routing_region="test",
                api_game_version=payload["info"]["gameVersion"],
                api_patch=f"16.{minor}",
                public_patch=public_patch,
                patch_resolution_method="synthetic",
                patch_resolution_status="resolved",
                queue_id=420,
                source_snapshot=run_id,
            )
            partition = (
                processed
                / "region=test"
                / f"patch={public_patch}"
                / "queue=420"
                / "matches"
            )
            partition.mkdir(parents=True, exist_ok=True)
            (partition / f"{match_id}.json").write_text(
                json.dumps(
                    {
                        "match_id": match_id,
                        "api_game_version": payload["info"]["gameVersion"],
                        "public_patch": public_patch,
                        "queue_id": 420,
                    }
                ),
                encoding="utf-8",
            )
    count = len(payloads)
    manifest = {
        "run_id": run_id,
        "platform": "test1",
        "routing_region": "test",
        "accepted_public_patches": sorted(patch_counts),
        "configuration": {"platform": "test1", "queue_id": 420},
        "summary": {
            "target_reached": True,
            "completion_status": "target_reached",
            "accepted_matches": count,
            "total_accepted_matches_credited": count,
            "accepted_matches_by_public_patch": patch_counts,
        },
        "match_files": match_files,
    }
    checkpoint = {
        "run_id": run_id,
        "config": {"platform": "test1", "queue_id": 420},
        "accepted_public_patches": sorted(patch_counts),
        "accepted_match_counts_by_public_patch": patch_counts,
        "matches": approvals,
    }
    manifest_path = raw_run / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    checkpoint_path = checkpoint_root / run_id / "checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    return {
        "catalog": catalog_path,
        "checkpoint": checkpoint_path,
        "manifest": manifest_path,
        "patch_counts": patch_counts,
        "processed": processed,
        "raw": raw_root,
    }


def _build_from_retained(inputs: dict[str, object]):
    return build_retained_population_dataset(
        manifest_path=inputs["manifest"],
        checkpoint_path=inputs["checkpoint"],
        catalog_path=inputs["catalog"],
        raw_root=inputs["raw"],
        processed_root=inputs["processed"],
        output_root=inputs["processed"] / "stage3",
        expected_match_count=sum(inputs["patch_counts"].values()),
        expected_patch_counts=inputs["patch_counts"],
    )


def test_bounded_partial_requires_explicit_opt_in(tmp_path: Path) -> None:
    inputs = _write_retained_inputs(
        tmp_path,
        [make_match_payload(match_id="PARTIAL", game_version="16.14.1.1")],
    )
    manifest_path = inputs["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["summary"]["target_reached"] = False
    manifest["summary"]["completion_status"] = "request_budget_exhausted"
    manifest["summary"]["request_metrics"] = {"attempted_requests": 50}
    manifest["configuration"]["max_requests"] = 50
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    checkpoint_path = inputs["checkpoint"]
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["config"]["max_requests"] = 50
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    with pytest.raises(Stage3ValidationError) as error:
        _build_from_retained(inputs)
    assert error.value.category == "incomplete_population"

    dataset = build_retained_population_dataset(
        manifest_path=inputs["manifest"],
        checkpoint_path=inputs["checkpoint"],
        catalog_path=inputs["catalog"],
        raw_root=inputs["raw"],
        processed_root=inputs["processed"],
        output_root=inputs["processed"] / "stage3",
        expected_match_count=1,
        expected_patch_counts=inputs["patch_counts"],
        allow_bounded_partial=True,
    )

    assert dataset.quality_report["ready_for_stage_3_2"] is True
    assert dataset.output_directory.parent.parent.name == "snapshot=accepted-1"


def test_recovered_execution_window_partial_can_be_processed(
    tmp_path: Path,
) -> None:
    inputs = _write_retained_inputs(
        tmp_path,
        [make_match_payload(match_id="INTERRUPTED", game_version="16.14.1.1")],
    )
    manifest_path = inputs["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["summary"]["target_reached"] = False
    manifest["summary"]["completion_status"] = "execution_window_interrupted"
    manifest["summary"]["request_metrics"] = {"attempted_requests": 35}
    manifest["configuration"]["max_requests"] = 50
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    checkpoint_path = inputs["checkpoint"]
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["config"]["max_requests"] = 50
    checkpoint["request_budget_recovery"] = {
        "active_invocation_recovered": True
    }
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    dataset = build_retained_population_dataset(
        manifest_path=inputs["manifest"],
        checkpoint_path=inputs["checkpoint"],
        catalog_path=inputs["catalog"],
        raw_root=inputs["raw"],
        processed_root=inputs["processed"],
        output_root=inputs["processed"] / "stage3",
        expected_match_count=1,
        expected_patch_counts=inputs["patch_counts"],
        allow_bounded_partial=True,
    )

    assert dataset.quality_report["ready_for_stage_3_2"] is True
    assert dataset.output_directory.parent.parent.name == "snapshot=accepted-1"
