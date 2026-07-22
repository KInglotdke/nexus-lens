from typing import Any

import pytest

from nexus_lens.normalization import (
    NormalizationError,
    normalize_match,
    normalize_position,
)
from nexus_lens.schemas import RiotMatch
from tests.factories import make_match_payload


def test_queue_420_is_accepted_and_non_420_is_rejected() -> None:
    accepted = RiotMatch.model_validate(make_match_payload(queue_id=420))
    assert normalize_match(accepted).match.queue_id == 420

    rejected = RiotMatch.model_validate(make_match_payload(queue_id=440))
    with pytest.raises(NormalizationError) as exc_info:
        normalize_match(rejected)
    assert exc_info.value.code == "wrong_queue"


@pytest.mark.parametrize(
    ("participant_count", "team_count", "code"),
    [(9, 2, "participant_count"), (10, 1, "team_count")],
)
def test_exactly_ten_participants_and_two_teams_are_required(
    participant_count: int,
    team_count: int,
    code: str,
) -> None:
    raw = RiotMatch.model_validate(
        make_match_payload(
            participant_count=participant_count,
            team_count=team_count,
        )
    )
    with pytest.raises(NormalizationError) as exc_info:
        normalize_match(raw)
    assert exc_info.value.code == code


def test_missing_optional_fields_do_not_crash() -> None:
    payload = make_match_payload()
    info: dict[str, Any] = payload["info"]
    info.pop("gameStartTimestamp")
    info.pop("gameEndTimestamp")
    participant = info["participants"][0]
    participant.pop("individualPosition")
    participant.pop("totalHeal")
    participant.pop("perks")

    batch = normalize_match(RiotMatch.model_validate(payload))

    assert batch.match.game_start is None
    assert batch.match.game_end is None
    assert batch.participants[0].individual_position is None
    assert batch.participants[0].healing_done is None
    assert batch.participants[0].primary_perk_ids == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mid", "MIDDLE"),
        ("support", "UTILITY"),
        ("TOP", "TOP"),
        ("roamer", "ROAMER"),
        ("", None),
        (None, None),
    ],
)
def test_position_normalization_preserves_unknown_values(
    value: str | None, expected: str | None
) -> None:
    assert normalize_position(value) == expected


def test_participant_derivations_and_team_normalization() -> None:
    batch = normalize_match(RiotMatch.model_validate(make_match_payload()))
    participant = batch.participants[0]
    team = batch.teams[0]

    assert batch.match.api_game_version == "16.12.788.4269"
    assert batch.match.api_patch == "16.12"
    assert batch.match.public_patch == "26.12"
    assert batch.match.patch_resolution_status == "resolved"
    assert len(batch.participants) == 10
    assert len(batch.teams) == 2
    assert participant.kda == 3.0
    assert participant.kill_participation == 0.6
    assert participant.cs == 110
    assert participant.primary_style_id == 8000
    assert participant.secondary_style_id == 8300
    assert team.champion_kills == 5
    assert team.dragon_kills == 3
    assert len(team.bans) == 5


def test_zero_team_kills_has_safe_kill_participation() -> None:
    payload = make_match_payload()
    for participant in payload["info"]["participants"][:5]:
        participant["kills"] = 0
        participant["assists"] = 0
    payload["info"]["teams"][0]["objectives"]["champion"]["kills"] = 0

    participant = normalize_match(RiotMatch.model_validate(payload)).participants[0]

    assert participant.kill_participation == 0.0


def test_normalization_is_deterministic() -> None:
    raw = RiotMatch.model_validate(make_match_payload())
    first = normalize_match(raw).model_dump(mode="json")
    second = normalize_match(raw).model_dump(mode="json")
    assert first == second
