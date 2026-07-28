"""Synthetic tests for deterministic Stage 3.2 analytical features."""

import hashlib
import json
import math
from pathlib import Path

import pytest

import nexus_lens.analytics as analytics
from nexus_lens.analytics import (
    build_analytical_dataset,
    run_stage3_2,
    write_analytical_dataset,
)
from nexus_lens.canonical import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalBan,
    CanonicalMatch,
    CanonicalParticipant,
    CanonicalTeam,
    Stage3ValidationError,
)


def test_ordinary_participant_formulas_and_fractional_shares(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    row = dataset.participant_features[0]

    assert row.kda == 3.0
    assert row.kill_participation == 0.6
    assert row.total_cs == 110
    assert row.cs_per_minute == 110 / 30
    assert row.gold_per_minute == 10_000 / 30
    assert row.team_gold == 50_000
    assert row.team_gold_share == 0.2
    assert row.team_champion_damage == 75_000
    assert row.team_champion_damage_share == 0.2
    assert row.damage_to_champions_per_minute == 500
    assert row.damage_taken_per_minute == 400
    assert row.damage_mitigated_per_minute == 5_000 / 30
    assert row.vision_score_per_minute == 20 / 30
    assert row.total_heal_per_minute == 1_000 / 30
    assert row.teammate_healing_per_minute == 200 / 30
    assert row.teammate_shielding_per_minute == 100 / 30
    assert dataset.quality_report["ratio_unit"] == "fraction"


def test_zero_deaths_uses_denominator_one(tmp_path: Path) -> None:
    participants = _participants()
    participants[0] = participants[0].model_copy(update={"deaths": 0})
    dataset = _dataset(tmp_path, participants=participants)

    assert dataset.participant_features[0].kda == 3.0
    assert dataset.quality_report["formula_conventions"]["kda_zero_death_rows"] == 1


def test_zero_team_kills_makes_kill_participation_null(tmp_path: Path) -> None:
    participants = _participants()
    participants = [
        row.model_copy(update={"kills": 0, "assists": 0}) if row.team_id == 100 else row
        for row in participants
    ]
    teams = _teams(champion_kills_100=0)
    dataset = _dataset(tmp_path, participants=participants, teams=teams)
    first_team = [row for row in dataset.participant_features if row.team_id == 100]

    assert all(row.kill_participation is None for row in first_team)
    assert (
        dataset.quality_report["zero_or_invalid_denominators"][
            "kill_participation_nonpositive_team_kills"
        ]
        == 5
    )


def test_zero_team_damage_makes_damage_share_null(tmp_path: Path) -> None:
    participants = [
        row.model_copy(update={"total_damage_dealt_to_champions": 0})
        if row.team_id == 100
        else row
        for row in _participants()
    ]
    dataset = _dataset(tmp_path, participants=participants)
    first_team = [row for row in dataset.participant_features if row.team_id == 100]

    assert all(row.team_champion_damage_share is None for row in first_team)
    assert (
        dataset.quality_report["zero_or_invalid_denominators"][
            "team_damage_share_nonpositive_team_damage"
        ]
        == 5
    )


def test_invalid_duration_nulls_rates_without_nan_or_infinity(tmp_path: Path) -> None:
    match = _match(duration=0)
    dataset = _dataset(tmp_path, matches=[match])
    row = dataset.participant_features[0]
    context = dataset.match_contexts[0]

    assert row.cs_per_minute is None
    assert row.gold_per_minute is None
    assert row.damage_to_champions_per_minute is None
    assert context.short_game is False
    assert context.analytical_eligibility is False
    assert context.exclusion_reasons == ["invalid_duration"]
    rendered = json.dumps(dataset.quality_report, allow_nan=False)
    assert "NaN" not in rendered and "Infinity" not in rendered


def test_short_game_is_retained_but_analytically_ineligible(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, matches=[_match(duration=299)])

    assert len(dataset.participant_features) == 10
    assert dataset.participant_features[0].cs_per_minute is not None
    assert dataset.match_contexts[0].short_game is True
    assert dataset.match_contexts[0].analytical_eligibility is False
    assert dataset.match_contexts[0].exclusion_reasons == ["short_game"]


def test_denominators_come_from_participants_own_team(tmp_path: Path) -> None:
    participants = [
        row.model_copy(update={"gold_earned": 20_000}) if row.team_id == 200 else row
        for row in _participants()
    ]
    dataset = _dataset(tmp_path, participants=participants)
    first = next(row for row in dataset.participant_features if row.team_id == 100)
    second = next(row for row in dataset.participant_features if row.team_id == 200)

    assert first.team_gold == 50_000 and first.team_gold_share == 0.2
    assert second.team_gold == 100_000 and second.team_gold_share == 0.2


def test_position_agreement_uses_team_position(tmp_path: Path) -> None:
    row = _dataset(tmp_path).participant_features[0]

    assert row.analysis_position == "TOP"
    assert row.analysis_position_source == "team_position"
    assert row.position_disagreement is False
    assert row.role_aggregation_eligibility is True


def test_position_disagreement_keeps_team_position_and_excludes_role_row(
    tmp_path: Path,
) -> None:
    participants = _participants()
    participants[0] = participants[0].model_copy(
        update={"team_position": "TOP", "individual_position": "JUNGLE"}
    )
    dataset = _dataset(tmp_path, participants=participants)
    row = dataset.participant_features[0]

    assert row.analysis_position == "TOP"
    assert row.analysis_position_source == "team_position"
    assert row.position_disagreement is True
    assert row.analytical_eligibility is True
    assert row.role_aggregation_eligibility is False
    assert row.role_exclusion_reasons == ["position_disagreement"]


def test_missing_team_position_uses_explicit_role_ineligible_fallback(
    tmp_path: Path,
) -> None:
    participants = _participants()
    participants[0] = participants[0].model_copy(update={"team_position": None})
    dataset = _dataset(tmp_path, participants=participants)
    row = dataset.participant_features[0]

    assert row.analysis_position == "TOP"
    assert row.analysis_position_source == "individual_position_fallback"
    assert row.role_aggregation_eligibility is False
    assert dataset.match_contexts[0].position_fallback_count == 1


def test_team_aggregation_and_reconciliation(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    team = dataset.team_features[0]

    assert team.team_kills == 5
    assert team.team_deaths == 5
    assert team.team_assists == 10
    assert team.team_gold == 50_000
    assert team.team_champion_damage == 75_000
    assert team.team_cs == 550
    assert dataset.quality_report["reconciliation_failures"] == {}


def test_null_source_propagates_to_team_denominator_and_shares(tmp_path: Path) -> None:
    participants = _participants()
    participants[0] = participants[0].model_copy(update={"gold_earned": None})
    dataset = _dataset(tmp_path, participants=participants)
    first_team = [row for row in dataset.participant_features if row.team_id == 100]

    assert all(row.team_gold is None for row in first_team)
    assert all(row.team_gold_share is None for row in first_team)
    assert (
        dataset.quality_report["derived_metric_null_counts"][
            "participant_match_features"
        ]["team_gold_share"]
        == 5
    )


def test_rows_are_sorted_deterministically(tmp_path: Path) -> None:
    participants = list(reversed(_participants()))
    teams = list(reversed(_teams()))
    dataset = _dataset(tmp_path, participants=participants, teams=teams)

    participant_keys = [
        (row.match_id, row.participant_id) for row in dataset.participant_features
    ]
    team_keys = [(row.match_id, row.team_id) for row in dataset.team_features]
    assert participant_keys == sorted(participant_keys)
    assert team_keys == sorted(team_keys)


def test_deterministic_publication_and_failure_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _dataset(tmp_path)
    output = write_analytical_dataset(dataset)
    first = _file_hashes(output)
    write_analytical_dataset(dataset)
    assert _file_hashes(output) == first

    def fail_write(*_: object, **__: object) -> None:
        raise OSError("synthetic failure")

    monkeypatch.setattr(analytics, "_write_staged_dataset", fail_write)
    with pytest.raises(OSError, match="synthetic failure"):
        write_analytical_dataset(dataset)
    assert _file_hashes(output) == first


def test_validate_only_writes_nothing_and_preserves_stage31(tmp_path: Path) -> None:
    input_run = _write_stage31_input(tmp_path)
    before = _file_hashes(input_run)
    output_root = tmp_path / "output"

    dataset = run_stage3_2(
        input_directory=input_run,
        output_root=output_root,
        validate_only=True,
        expected_match_count=1,
        expected_participant_count=10,
        expected_team_count=2,
        expected_patch_counts={"26.14": 1},
    )

    assert dataset.quality_report["ready_for_stage_3_3_analysis_validation"]
    assert not output_root.exists()
    assert _file_hashes(input_run) == before


def test_incompatible_stage31_schema_fails_closed(tmp_path: Path) -> None:
    input_run = _write_stage31_input(tmp_path)
    metadata_path = input_run / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["processing_schema_version"] = "incompatible"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(Stage3ValidationError) as caught:
        run_stage3_2(
            input_directory=input_run,
            output_root=tmp_path / "output",
            validate_only=True,
            expected_match_count=1,
            expected_participant_count=10,
            expected_team_count=2,
            expected_patch_counts={"26.14": 1},
        )
    assert caught.value.category == "incompatible_stage3_1_schema"


def test_changed_input_fails_existing_output_lineage_gate(tmp_path: Path) -> None:
    input_run = _write_stage31_input(tmp_path)
    output_root = tmp_path / "output"
    run_stage3_2(
        input_directory=input_run,
        output_root=output_root,
        validate_only=False,
        expected_match_count=1,
        expected_participant_count=10,
        expected_team_count=2,
        expected_patch_counts={"26.14": 1},
    )
    participant_path = input_run / "participants.jsonl"
    rows = [
        json.loads(line)
        for line in participant_path.read_text(encoding="utf-8").splitlines()
    ]
    rows[0]["gold_earned"] += 1
    participant_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    with pytest.raises(Stage3ValidationError) as caught:
        run_stage3_2(
            input_directory=input_run,
            output_root=output_root,
            validate_only=True,
            expected_match_count=1,
            expected_participant_count=10,
            expected_team_count=2,
            expected_patch_counts={"26.14": 1},
        )
    assert caught.value.category == "stage3_1_lineage_changed"


def test_privacy_contract_allows_only_player_key(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    columns = type(dataset.participant_features[0]).model_fields
    rendered_report = json.dumps(dataset.quality_report)

    assert "player_key" in columns
    assert (
        not {
            "puuid",
            "summoner_id",
            "summoner_name",
            "riot_id",
            "riot_id_game_name",
            "riot_id_tagline",
        }
        & columns.keys()
    )
    assert "synthetic-player-key" not in rendered_report
    assert dataset.quality_report["privacy"]["additional_identity_fields"] is False


def _dataset(
    tmp_path: Path,
    *,
    matches: list[CanonicalMatch] | None = None,
    participants: list[CanonicalParticipant] | None = None,
    teams: list[CanonicalTeam] | None = None,
):
    source_matches = matches or [_match()]
    source_participants = participants or _participants()
    source_teams = teams or _teams()
    return build_analytical_dataset(
        run_id="synthetic-run",
        input_directory=tmp_path / "stage31",
        output_directory=tmp_path / "stage32",
        matches=source_matches,
        participants=source_participants,
        teams=source_teams,
        lineage_hashes={"matches.jsonl": "synthetic-hash"},
        expected_match_count=1,
        expected_participant_count=10,
        expected_team_count=2,
        expected_patch_counts={"26.14": 1},
    )


def _match(*, duration: int = 1_800) -> CanonicalMatch:
    return CanonicalMatch.model_validate(
        {
            "match_id": "TEST_1",
            "public_patch": "26.14",
            "game_version": "16.14.1.1",
            "platform": "test1",
            "queue_id": 420,
            "game_creation": "2026-06-10T00:00:00Z",
            "game_start_timestamp": "2026-06-10T00:00:10Z",
            "game_end_timestamp": "2026-06-10T00:30:10Z",
            "game_duration_seconds": duration,
            "winning_team_id": 100,
            "is_remake_or_short_game": 0 < duration < 300,
            "source_payload_reference": "synthetic/payload.json",
            "processing_schema_version": CANONICAL_SCHEMA_VERSION,
        }
    )


def _participants() -> list[CanonicalParticipant]:
    positions = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
    rows = []
    for index in range(10):
        team_id = 100 if index < 5 else 200
        rows.append(
            CanonicalParticipant.model_validate(
                {
                    "match_id": "TEST_1",
                    "participant_id": index + 1,
                    "team_id": team_id,
                    "player_key": f"synthetic-player-key-{index + 1}",
                    "champion_id": 1000 + index,
                    "champion_name": f"SyntheticChampion{index + 1}",
                    "summoner_spell_1_id": 4,
                    "summoner_spell_2_id": 14,
                    "team_position": positions[index % 5],
                    "individual_position": positions[index % 5],
                    "lane": positions[index % 5],
                    "role": "SOLO",
                    "win": team_id == 100,
                    "kills": 1,
                    "deaths": 1,
                    "assists": 2,
                    "total_minions_killed": 100,
                    "neutral_minions_killed": 10,
                    "gold_earned": 10_000,
                    "total_damage_dealt_to_champions": 15_000,
                    "physical_damage_dealt_to_champions": 8_000,
                    "magic_damage_dealt_to_champions": 6_000,
                    "true_damage_dealt_to_champions": 1_000,
                    "total_damage_taken": 12_000,
                    "damage_self_mitigated": 5_000,
                    "total_heal": 1_000,
                    "total_heals_on_teammates": 200,
                    "total_damage_shielded_on_teammates": 100,
                    "vision_score": 20,
                    "wards_placed": 8,
                    "wards_killed": 2,
                    "control_wards_placed": 1,
                    "control_wards_purchased": 2,
                    "champion_level": 18,
                    **{f"item_{slot}": 3000 + slot for slot in range(7)},
                    "time_played_seconds": 1_800,
                    "challenge_objectives_stolen": 0,
                    "challenge_save_ally_from_death": 1,
                    "challenge_skillshots_dodged": 3,
                    "challenge_skillshots_hit": 4,
                    "challenge_solo_kills": 1,
                    "challenge_turret_plates_taken": 2,
                    "processing_schema_version": CANONICAL_SCHEMA_VERSION,
                }
            )
        )
    return rows


def _teams(*, champion_kills_100: int = 5) -> list[CanonicalTeam]:
    return [
        CanonicalTeam(
            match_id="TEST_1",
            team_id=team_id,
            win=team_id == 100,
            champion_kills=champion_kills_100 if team_id == 100 else 5,
            champion_first=team_id == 100,
            tower_kills=8 if team_id == 100 else 2,
            tower_first=team_id == 100,
            inhibitor_kills=2 if team_id == 100 else 0,
            inhibitor_first=team_id == 100,
            dragon_kills=3 if team_id == 100 else 1,
            dragon_first=team_id == 100,
            rift_herald_kills=1 if team_id == 100 else 0,
            rift_herald_first=team_id == 100,
            baron_kills=1 if team_id == 100 else 0,
            baron_first=team_id == 100,
            processing_schema_version=CANONICAL_SCHEMA_VERSION,
        )
        for team_id in (100, 200)
    ]


def _write_stage31_input(tmp_path: Path) -> Path:
    directory = tmp_path / "stage31"
    directory.mkdir()
    matches = [_match()]
    participants = _participants()
    teams = _teams()
    bans = [
        CanonicalBan(
            match_id="TEST_1",
            team_id=team_id,
            pick_turn=index + 1,
            champion_id=10 + index,
            processing_schema_version=CANONICAL_SCHEMA_VERSION,
        )
        for team_id in (100, 200)
        for index in range(5)
    ]
    _write_rows(directory / "matches.jsonl", matches)
    _write_rows(directory / "participants.jsonl", participants)
    _write_rows(directory / "teams.jsonl", teams)
    _write_rows(directory / "bans.jsonl", bans)
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "processing_schema_version": CANONICAL_SCHEMA_VERSION,
                "run_id": "synthetic-run",
                "row_counts": {
                    "bans": 10,
                    "matches": 1,
                    "participants": 10,
                    "teams": 2,
                },
                "match_counts_by_public_patch": {"26.14": 1},
            }
        ),
        encoding="utf-8",
    )
    (directory / "quality_report.json").write_text(
        json.dumps(
            {
                "quality_report_schema_version": "stage3.1-quality-v1",
                "ready_for_stage_3_2": True,
                "invariant_failures": {},
            }
        ),
        encoding="utf-8",
    )
    return directory


def _write_rows(path: Path, rows: list[object]) -> None:
    path.write_text(
        "".join(
            json.dumps(row.model_dump(mode="json"), sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _file_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def test_all_serialized_floats_are_finite(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    for row in dataset.participant_features:
        for value in row.model_dump().values():
            if isinstance(value, float):
                assert math.isfinite(value)
