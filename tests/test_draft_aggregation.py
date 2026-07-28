from __future__ import annotations

import hashlib
import inspect
import json
import math
from pathlib import Path

import pytest
from scipy.stats import beta as beta_distribution

import nexus_lens.draft_aggregation as aggregation
from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.draft_aggregation import (
    AGGREGATION_SCHEMA_VERSION,
    PROVISIONAL_MINIMUM_PRACTICAL_ADVANTAGE,
    Stage33AInput,
    beta_binomial_posterior,
    beta_survival_probability,
    build_aggregation_dataset,
    evidence_tier,
    run_stage3_3b,
    write_aggregation_dataset,
)
from nexus_lens.draft_observations import (
    DRAFT_OBSERVATION_SCHEMA_VERSION,
    DRAFT_POLICY_VERSION,
    DRAFT_QUALITY_SCHEMA_VERSION,
    MatchDraftContext,
    ParticipantDraftObservation,
    TeamDraftObservation,
)

POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
RUN_ID = "20260722T125547567196Z-synthetic"


def test_directional_matchups_and_reciprocal_outcomes(tmp_path: Path) -> None:
    stage33a = _stage33a(
        tmp_path,
        [_match("MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15))],
    )
    dataset = _build(tmp_path, stage33a)
    forward = _matchup(dataset, 1, "TOP", 11, "TOP")
    reverse = _matchup(dataset, 11, "TOP", 1, "TOP")

    assert forward.statistics.observed_games == reverse.statistics.observed_games == 1
    assert forward.statistics.wins == reverse.statistics.losses == 1
    assert forward.statistics.losses == reverse.statistics.wins == 0
    assert len(dataset.matchup_aggregates) == 10


def test_duplicate_directional_matchup_per_match_is_rejected(tmp_path: Path) -> None:
    participants, teams, match = _match(
        "MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15)
    )
    participants[1] = participants[1].model_copy(
        update={
            "champion_id": 1,
            "champion_name": "Champion 1",
            "analysis_position": "TOP",
            "lane_opponent_participant_id": 6,
            "lane_opponent_champion_id": 11,
            "lane_opponent_champion_name": "Champion 11",
        }
    )
    with pytest.raises(Stage3ValidationError) as caught:
        _build(tmp_path, _stage33a(tmp_path, [(participants, teams, match)]))
    assert caught.value.category == "duplicate_directional_matchup"


def test_directional_synergy_and_pair_deduplication(tmp_path: Path) -> None:
    stage33a = _stage33a(
        tmp_path,
        [_match("MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15))],
    )
    dataset = _build(tmp_path, stage33a)
    forward = _synergy(dataset, 1, "TOP", 2, "JUNGLE")
    reverse = _synergy(dataset, 2, "JUNGLE", 1, "TOP")

    assert forward.statistics.observed_games == reverse.statistics.observed_games == 1
    assert len(dataset.synergy_aggregates) == 40
    assert sum(row.source_observation_count for row in dataset.synergy_aggregates) == 40


def test_duplicate_directional_synergy_per_team_match_is_rejected(
    tmp_path: Path,
) -> None:
    participants, teams, match = _match(
        "MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15)
    )
    participants[2] = participants[2].model_copy(
        update={
            "champion_id": 2,
            "champion_name": "Champion 2",
            "analysis_position": "JUNGLE",
        }
    )
    with pytest.raises(Stage3ValidationError) as caught:
        _build(tmp_path, _stage33a(tmp_path, [(participants, teams, match)]))
    assert caught.value.category == "duplicate_directional_synergy"


def test_roles_remain_separate(tmp_path: Path) -> None:
    first = _match("MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15))
    second = _match("MATCH-2", "26.14", (21, 22, 1, 24, 25), (31, 32, 33, 34, 35))
    dataset = _build(tmp_path, _stage33a(tmp_path, [first, second]))

    champion_rows = [
        row for row in dataset.champion_role_statistics if row.champion_id == 1
    ]
    assert {row.analysis_position for row in champion_rows} == {"TOP", "MIDDLE"}


def test_patch_specific_cumulative_windows_missing_patch_and_decay(
    tmp_path: Path,
) -> None:
    newest = _match(
        "MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15), winner=100
    )
    older = _match(
        "MATCH-2", "26.12", (1, 22, 23, 24, 25), (11, 32, 33, 34, 35), winner=200
    )
    dataset = _build(tmp_path, _stage33a(tmp_path, [newest, older]))
    row = _matchup(dataset, 1, "TOP", 11, "TOP")

    assert [(item.public_patch, item.patch_weight) for item in row.patch_specific] == [
        ("26.14", 1.0),
        ("26.12", 0.8**2),
    ]
    assert len(row.cumulative_patch_windows) == 3
    middle = row.cumulative_patch_windows[1]
    assert middle.input_missing_patch_ages == [1]
    assert middle.input_missing_patches == ["26.13"]
    assert middle.statistics.observed_games == 1
    final = row.cumulative_patch_windows[-1].statistics
    assert final.weighted_wins == pytest.approx(1.0)
    assert final.weighted_losses == pytest.approx(0.64)
    assert final.sum_weights == pytest.approx(1.64)
    assert final.sum_squared_weights == pytest.approx(1 + 0.64**2)
    assert final.effective_sample_size == pytest.approx(1.64**2 / (1 + 0.64**2))


def test_leave_matchup_out_baseline_components(tmp_path: Path) -> None:
    first = _match("MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15))
    second = _match("MATCH-2", "26.14", (1, 22, 23, 24, 25), (21, 32, 33, 34, 35))
    third = _match("MATCH-3", "26.14", (31, 42, 43, 44, 45), (11, 52, 53, 54, 55))
    dataset = _build(tmp_path, _stage33a(tmp_path, [first, second, third]))
    row = _matchup(dataset, 1, "TOP", 11, "TOP")

    assert row.focal_leave_opponent_out.statistics.observed_games == 1
    assert row.opponent_leave_focal_out.statistics.observed_games == 1
    assert row.focal_leave_opponent_out.availability_status == "available"


def test_leave_ally_out_baseline_components(tmp_path: Path) -> None:
    first = _match("MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15))
    second = _match("MATCH-2", "26.14", (1, 22, 23, 24, 25), (31, 32, 33, 34, 35))
    third = _match("MATCH-3", "26.14", (41, 2, 43, 44, 45), (51, 52, 53, 54, 55))
    dataset = _build(tmp_path, _stage33a(tmp_path, [first, second, third]))
    row = _synergy(dataset, 1, "TOP", 2, "JUNGLE")

    assert row.focal_without_ally.statistics.observed_games == 1
    assert row.ally_without_focal.statistics.observed_games == 1


def test_missing_baseline_components_are_explicit(tmp_path: Path) -> None:
    dataset = _build(
        tmp_path,
        _stage33a(
            tmp_path,
            [_match("MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15))],
        ),
    )
    matchup = _matchup(dataset, 1, "TOP", 11, "TOP")
    synergy = _synergy(dataset, 1, "TOP", 2, "JUNGLE")
    expected = "unavailable_no_leave_pair_out_observations"

    assert matchup.focal_leave_opponent_out.availability_status == expected
    assert matchup.opponent_leave_focal_out.availability_status == expected
    assert synergy.focal_without_ally.availability_status == expected
    assert synergy.ally_without_focal.availability_status == expected


def test_zero_eligible_observations_produce_empty_aggregates(tmp_path: Path) -> None:
    participants, teams, match = _match(
        "MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15)
    )
    participants = [
        row.model_copy(
            update={
                "matchup_eligibility": False,
                "synergy_eligibility": False,
                "role_analysis_eligibility": False,
            }
        )
        for row in participants
    ]
    dataset = _build(tmp_path, _stage33a(tmp_path, [(participants, teams, match)]))

    assert dataset.matchup_aggregates == []
    assert dataset.synergy_aggregates == []
    assert all(
        row.role_eligible.statistics.observed_games == 0
        for row in dataset.champion_role_statistics
    )
    assert dataset.quality_report["ready_for_calibration"] is True


def test_missing_champion_name_preserves_id(tmp_path: Path) -> None:
    participants, teams, match = _match(
        "MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15)
    )
    participants[0] = participants[0].model_copy(update={"champion_name": None})
    dataset = _build(tmp_path, _stage33a(tmp_path, [(participants, teams, match)]))
    row = _matchup(dataset, 1, "TOP", 11, "TOP")

    assert row.focal_champion_id == 1
    assert row.focal_champion_name is None


def test_beta_survival_known_results_and_boundaries() -> None:
    assert beta_survival_probability(1, 1, 0.25) == pytest.approx(0.75)
    assert beta_survival_probability(2, 2, 0.5) == pytest.approx(0.5)
    assert beta_survival_probability(2, 3, -0.1) == 1.0
    assert beta_survival_probability(2, 3, 0.0) == 1.0
    assert beta_survival_probability(2, 3, 1.0) == 0.0
    assert beta_survival_probability(2, 3, 1.1) == 0.0


def test_beta_prior_fractional_counts_and_posterior_mean() -> None:
    result = beta_binomial_posterior(
        baseline_probability=0.5,
        prior_equivalent_games=4.0,
        observed_wins=0.5,
        observed_losses=1.5,
        minimum_practical_advantage=0.01,
    )

    assert result.prior_expected_wins == 2
    assert result.prior_expected_losses == 2
    assert result.posterior_alpha == 2.5
    assert result.posterior_beta == 3.5
    assert result.posterior_mean == pytest.approx(2.5 / 6)
    assert result.posterior_probability_practical_advantage == pytest.approx(
        beta_distribution.sf(0.51, 2.5, 3.5)
    )


def test_evidence_tier_boundaries_are_exact() -> None:
    assert evidence_tier(0.899999) == "insufficient_evidence"
    assert evidence_tier(0.90) == "moderate_evidence"
    assert evidence_tier(0.949999) == "moderate_evidence"
    assert evidence_tier(0.95) == "strong_evidence"
    assert evidence_tier(1.0) == "strong_evidence"


def test_prior_strength_is_required_and_zero_behavior_is_defined() -> None:
    signature = inspect.signature(beta_binomial_posterior)
    assert (
        signature.parameters["prior_equivalent_games"].default
        is inspect.Parameter.empty
    )
    with pytest.raises(TypeError):
        beta_binomial_posterior(  # type: ignore[call-arg]
            baseline_probability=0.5,
            observed_wins=1,
            observed_losses=1,
        )
    proper = beta_binomial_posterior(
        baseline_probability=0.5,
        prior_equivalent_games=0,
        observed_wins=1,
        observed_losses=1,
        minimum_practical_advantage=0,
    )
    assert proper.posterior_alpha == proper.posterior_beta == 1
    with pytest.raises(ValueError, match="proper beta posterior"):
        beta_binomial_posterior(
            baseline_probability=0.5,
            prior_equivalent_games=0,
            observed_wins=1,
            observed_losses=0,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("baseline_probability", 0.0),
        ("baseline_probability", 1.0),
        ("baseline_probability", -0.1),
        ("baseline_probability", 1.1),
        ("prior_equivalent_games", -1.0),
        ("observed_wins", -1.0),
        ("observed_losses", -1.0),
        ("minimum_practical_advantage", -0.1),
        ("observed_wins", math.nan),
        ("observed_losses", math.inf),
    ],
)
def test_invalid_beta_binomial_inputs(field: str, value: float) -> None:
    kwargs = {
        "baseline_probability": 0.5,
        "prior_equivalent_games": 2.0,
        "observed_wins": 1.0,
        "observed_losses": 1.0,
        "minimum_practical_advantage": 0.01,
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        beta_binomial_posterior(**kwargs)


def test_beta_survival_rejects_invalid_distribution_parameters() -> None:
    for args in ((0, 1, 0.5), (1, 0, 0.5), (math.nan, 1, 0.5), (1, 1, math.inf)):
        with pytest.raises(ValueError):
            beta_survival_probability(*args)


def test_population_rows_leave_posterior_unevaluated(tmp_path: Path) -> None:
    dataset = _build(
        tmp_path,
        _stage33a(
            tmp_path,
            [_match("MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15))],
        ),
    )
    for row in [*dataset.matchup_aggregates, *dataset.synergy_aggregates]:
        assert row.statistical_status == "not_evaluated_policy_unresolved"
        assert row.recommendation_eligibility is None
        assert row.posterior.baseline_probability is None
        assert row.posterior.prior_equivalent_games is None
        assert row.posterior.posterior_mean is None
        assert (
            row.posterior.minimum_practical_advantage
            == PROVISIONAL_MINIMUM_PRACTICAL_ADVANTAGE
        )


def test_deterministic_ordering_rerun_and_failure_safe_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _build(
        tmp_path,
        _stage33a(
            tmp_path,
            [_match("MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15))],
        ),
    )
    output = write_aggregation_dataset(dataset)
    first = _file_hashes(output)
    write_aggregation_dataset(dataset)
    assert _file_hashes(output) == first
    assert len({row.logical_key for row in dataset.matchup_aggregates}) == len(
        dataset.matchup_aggregates
    )

    def fail_write(*_: object, **__: object) -> None:
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(aggregation, "_write_staged_dataset", fail_write)
    with pytest.raises(OSError, match="synthetic publication failure"):
        write_aggregation_dataset(dataset)
    assert _file_hashes(output) == first


def test_validation_only_incompatible_schema_and_prior_preservation(
    tmp_path: Path,
) -> None:
    input_directory, stage31, stage32 = _write_stage33a_run(tmp_path)
    before31 = _file_hashes(stage31)
    before32 = _file_hashes(stage32)
    before33a = _file_hashes(input_directory)
    output_root = tmp_path / "output"

    run_stage3_3b(
        input_directory=input_directory,
        output_root=output_root,
        validate_only=True,
        expected_participant_count=10,
        expected_team_count=2,
        expected_match_count=1,
    )
    assert not output_root.exists()
    assert _file_hashes(stage31) == before31
    assert _file_hashes(stage32) == before32
    assert _file_hashes(input_directory) == before33a

    metadata_path = input_directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["processing_schema_version"] = "incompatible"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(Stage3ValidationError) as caught:
        run_stage3_3b(
            input_directory=input_directory,
            output_root=output_root,
            validate_only=True,
            expected_participant_count=10,
            expected_team_count=2,
            expected_match_count=1,
        )
    assert caught.value.category == "incompatible_stage3_3a_schema"
    assert not output_root.exists()


def test_privacy_metadata_and_scipy_contract(tmp_path: Path) -> None:
    stage33a = _stage33a(
        tmp_path,
        [_match("MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15))],
    )
    dataset = _build(tmp_path, stage33a)
    output_rows = {
        "matchups": [row.model_dump() for row in dataset.matchup_aggregates],
        "synergies": [row.model_dump() for row in dataset.synergy_aggregates],
        "roles": [row.model_dump() for row in dataset.champion_role_statistics],
    }
    serialized = json.dumps(output_rows).lower()
    for forbidden in (
        "player_key",
        "puuid",
        "summonerid",
        "accountid",
        "riotid",
        "gamename",
        "tagline",
    ):
        assert forbidden not in serialized
    aggregate_reports = json.dumps([dataset.quality_report, dataset.metadata]).lower()
    assert not any(
        row.player_key.lower() in aggregate_reports for row in stage33a.participants
    )
    numerical = dataset.metadata["numerical_implementation"]
    assert numerical["beta_survival_probability"] == "scipy.stats.beta.sf"
    assert numerical["custom_approximation"] is False
    assert dataset.metadata["processing_schema_version"] == AGGREGATION_SCHEMA_VERSION


def _build(tmp_path: Path, stage33a: Stage33AInput):
    return build_aggregation_dataset(
        stage33a=stage33a,
        output_directory=tmp_path / "stage3b" / f"run={RUN_ID}",
    )


def _matchup(
    dataset, focal: int, focal_position: str, opponent: int, opponent_position: str
):
    return next(
        row
        for row in dataset.matchup_aggregates
        if row.focal_champion_id == focal
        and row.focal_position == focal_position
        and row.opponent_champion_id == opponent
        and row.opponent_position == opponent_position
    )


def _synergy(dataset, focal: int, focal_position: str, ally: int, ally_position: str):
    return next(
        row
        for row in dataset.synergy_aggregates
        if row.focal_champion_id == focal
        and row.focal_position == focal_position
        and row.ally_champion_id == ally
        and row.ally_position == ally_position
    )


def _stage33a(
    tmp_path: Path,
    matches: list[
        tuple[
            list[ParticipantDraftObservation],
            list[TeamDraftObservation],
            MatchDraftContext,
        ]
    ],
) -> Stage33AInput:
    participants = [row for match in matches for row in match[0]]
    teams = [row for match in matches for row in match[1]]
    contexts = [match[2] for match in matches]
    return Stage33AInput(
        run_id=RUN_ID,
        input_directory=tmp_path / "stage3a",
        stage3_1_directory=tmp_path / "stage31",
        stage3_2_directory=tmp_path / "stage32",
        participants=participants,
        teams=teams,
        matches=contexts,
        lineage_hashes={
            "participant_draft_observations.jsonl": "a" * 64,
            "team_draft_observations.jsonl": "b" * 64,
            "match_draft_context.jsonl": "c" * 64,
            "draft_observation_quality_report.json": "d" * 64,
            "metadata.json": "e" * 64,
        },
        stage3_1_hashes={"synthetic-stage31": "f" * 64},
        stage3_2_hashes={"synthetic-stage32": "1" * 64},
    )


def _match(
    match_id: str,
    patch: str,
    first_champions: tuple[int, int, int, int, int],
    second_champions: tuple[int, int, int, int, int],
    *,
    winner: int = 100,
) -> tuple[
    list[ParticipantDraftObservation],
    list[TeamDraftObservation],
    MatchDraftContext,
]:
    participants: list[ParticipantDraftObservation] = []
    champions = (*first_champions, *second_champions)
    for index, champion_id in enumerate(champions):
        participant_id = index + 1
        team_id = 100 if index < 5 else 200
        position_index = index if index < 5 else index - 5
        opponent_id = participant_id + 5 if team_id == 100 else participant_id - 5
        opponent_champion_id = (
            second_champions[position_index]
            if team_id == 100
            else first_champions[position_index]
        )
        allies = first_champions if team_id == 100 else second_champions
        enemies = second_champions if team_id == 100 else first_champions
        won = team_id == winner
        participants.append(
            ParticipantDraftObservation(
                match_id=match_id,
                participant_id=participant_id,
                player_key=hashlib.sha256(
                    f"{match_id}:{participant_id}".encode()
                ).hexdigest()[:32],
                champion_id=champion_id,
                champion_name=f"Champion {champion_id}",
                team_id=team_id,
                win=won,
                team_win=won,
                enemy_team_win=not won,
                public_patch=patch,
                queue_id=420,
                platform="eun1",
                region=None,
                region_lineage_status="unavailable_in_approved_inputs",
                rank_bracket=None,
                collection_stratum=None,
                rank_lineage_status="unavailable_in_approved_inputs",
                game_duration_seconds=1_800,
                short_game=False,
                team_position=POSITIONS[position_index],
                individual_position=POSITIONS[position_index],
                analysis_position=POSITIONS[position_index],
                analysis_position_source="team_position",
                position_disagreement=False,
                position_pairing_eligibility=True,
                allied_champion_ids=[value for value in allies if value != champion_id],
                enemy_champion_ids=list(enemies),
                lane_opponent_participant_id=opponent_id,
                lane_opponent_champion_id=opponent_champion_id,
                lane_opponent_champion_name=f"Champion {opponent_champion_id}",
                lane_opponent_resolution_status="resolved_unique",
                lane_opponent_resolution_reason="unique_reciprocal_same_position",
                general_analysis_eligibility=True,
                role_analysis_eligibility=True,
                matchup_eligibility=True,
                matchup_exclusion_reasons=[],
                synergy_eligibility=True,
                synergy_exclusion_reasons=[],
                team_structure_valid=True,
                opponent_team_structure_valid=True,
                source_run_id=RUN_ID,
                source_stage3_1_schema_version="stage3.1-v1",
                source_stage3_2_schema_version="stage3.2-v1",
                processing_schema_version=DRAFT_OBSERVATION_SCHEMA_VERSION,
            )
        )
    team_rows = [
        _team_row(
            match_id,
            patch,
            team_id=100,
            opponent_team_id=200,
            champions=first_champions,
            opponents=second_champions,
            won=winner == 100,
        ),
        _team_row(
            match_id,
            patch,
            team_id=200,
            opponent_team_id=100,
            champions=second_champions,
            opponents=first_champions,
            won=winner == 200,
        ),
    ]
    context = MatchDraftContext(
        match_id=match_id,
        public_patch=patch,
        queue_id=420,
        platform="eun1",
        region=None,
        region_lineage_status="unavailable_in_approved_inputs",
        rank_bracket=None,
        collection_stratum=None,
        rank_lineage_status="unavailable_in_approved_inputs",
        game_duration_seconds=1_800,
        short_game=False,
        participant_count=10,
        team_count=2,
        participants_complete=True,
        teams_complete=True,
        positions_complete=True,
        position_disagreement_count=0,
        position_fallback_count=0,
        unresolved_position_count=0,
        team_compositions=[
            {
                "team_id": 100,
                "champion_ids": list(first_champions),
                "win": winner == 100,
            },
            {
                "team_id": 200,
                "champion_ids": list(second_champions),
                "win": winner == 200,
            },
        ],
        team_bans=[
            {"team_id": 100, "bans": []},
            {"team_id": 200, "bans": []},
        ],
        resolved_lane_opponent_pairs=5,
        all_five_lane_opponent_pairs_resolved=True,
        general_analysis_eligibility=True,
        general_exclusion_reasons=[],
        matchup_eligibility=True,
        matchup_exclusion_reasons=[],
        synergy_eligibility=True,
        synergy_exclusion_reasons=[],
        source_run_id=RUN_ID,
        source_stage3_1_schema_version="stage3.1-v1",
        source_stage3_2_schema_version="stage3.2-v1",
        processing_schema_version=DRAFT_OBSERVATION_SCHEMA_VERSION,
    )
    return participants, team_rows, context


def _team_row(
    match_id: str,
    patch: str,
    *,
    team_id: int,
    opponent_team_id: int,
    champions: tuple[int, ...],
    opponents: tuple[int, ...],
    won: bool,
) -> TeamDraftObservation:
    return TeamDraftObservation(
        match_id=match_id,
        team_id=team_id,
        win=won,
        opponent_team_id=opponent_team_id,
        opponent_win=not won,
        public_patch=patch,
        queue_id=420,
        platform="eun1",
        region=None,
        region_lineage_status="unavailable_in_approved_inputs",
        rank_bracket=None,
        collection_stratum=None,
        rank_lineage_status="unavailable_in_approved_inputs",
        game_duration_seconds=1_800,
        short_game=False,
        champion_ids=list(champions),
        opponent_champion_ids=list(opponents),
        bans=[],
        opponent_bans=[],
        team_structure_valid=True,
        opponent_team_structure_valid=True,
        positions_complete=True,
        all_lane_opponents_resolved=True,
        general_analysis_eligibility=True,
        matchup_eligibility=True,
        matchup_exclusion_reasons=[],
        synergy_eligibility=True,
        synergy_exclusion_reasons=[],
        source_run_id=RUN_ID,
        source_stage3_1_schema_version="stage3.1-v1",
        source_stage3_2_schema_version="stage3.2-v1",
        processing_schema_version=DRAFT_OBSERVATION_SCHEMA_VERSION,
    )


def _write_stage33a_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    participants, teams, match = _match(
        "MATCH-1", "26.14", (1, 2, 3, 4, 5), (11, 12, 13, 14, 15)
    )
    stage31 = tmp_path / "stage31"
    stage32 = tmp_path / "stage32"
    input_directory = tmp_path / "stage33a"
    stage31.mkdir()
    stage32.mkdir()
    input_directory.mkdir()
    (stage31 / "artifact.json").write_text('{"stage":"3.1"}\n', encoding="utf-8")
    (stage32 / "artifact.json").write_text('{"stage":"3.2"}\n', encoding="utf-8")
    stage31_hashes = _file_hashes(stage31)
    stage32_hashes = _file_hashes(stage32)
    _write_jsonl(input_directory / "participant_draft_observations.jsonl", participants)
    _write_jsonl(input_directory / "team_draft_observations.jsonl", teams)
    _write_jsonl(input_directory / "match_draft_context.jsonl", [match])
    quality = {
        "quality_report_schema_version": DRAFT_QUALITY_SCHEMA_VERSION,
        "ready_for_matchup_synergy_aggregation": True,
        "invariant_failures": {},
        "reconciliation_failures": {},
    }
    _write_json(input_directory / "draft_observation_quality_report.json", quality)
    output_hashes = _file_hashes(input_directory)
    metadata = {
        "processing_schema_version": DRAFT_OBSERVATION_SCHEMA_VERSION,
        "draft_policy_version": DRAFT_POLICY_VERSION,
        "run_id": RUN_ID,
        "inputs": {
            "stage3_1": {
                "directory": stage31.as_posix(),
                "sha256": stage31_hashes,
            },
            "stage3_2": {
                "directory": stage32.as_posix(),
                "sha256": stage32_hashes,
            },
        },
        "output": {"sha256": output_hashes},
    }
    _write_json(input_directory / "metadata.json", metadata)
    return input_directory, stage31, stage32


def _write_jsonl(path: Path, rows: list) -> None:
    path.write_text(
        "".join(
            json.dumps(
                row.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _file_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }
