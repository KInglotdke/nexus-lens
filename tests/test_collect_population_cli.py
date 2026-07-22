from pathlib import Path

import pytest

from nexus_lens.population import CheckpointCompatibilityError, PopulationConfig
from nexus_lens.population_state import PopulationState
from nexus_lens.riot_client import (
    RiotApiError,
    RiotRequestBudgetExceeded,
    RiotRetryExhausted,
)
from scripts.collect_population import (
    recover_missing_request_budget,
    sanitized_collection_error,
)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            CheckpointCompatibilityError(
                "checkpoint incompatible: target_public_patch differs"
            ),
            "checkpoint incompatible: target_public_patch differs",
        ),
        (
            RiotRequestBudgetExceeded("sensitive internal detail"),
            "request budget exhausted",
        ),
        (
            RiotApiError(401, "account"),
            "Riot authentication failed; verify the local development key",
        ),
        (
            RiotRetryExhausted("sensitive internal detail"),
            "Riot request retries exhausted",
        ),
    ],
)
def test_actionable_collection_errors_are_sanitized(
    error: Exception,
    message: str,
) -> None:
    assert sanitized_collection_error(error) == message


def test_unknown_error_does_not_echo_exception_text() -> None:
    error = RuntimeError("do-not-echo-this player-identifier")

    rendered = sanitized_collection_error(error)

    assert rendered == "unexpected local collection error"
    assert "sensitive" not in rendered
    assert "player" not in rendered


def test_interrupted_checkpoint_gets_conservative_request_charge(
    tmp_path: Path,
) -> None:
    config = PopulationConfig(
        platform="eun1",
        target_public_patch="26.14",
        initial_history_batch_size=5,
        max_history_per_player=20,
    )
    state = PopulationState.create(
        tmp_path / "checkpoint.json",
        run_id="SYNTHETIC",
        config=config.non_sensitive_dict(),
    )
    state.players.update({"one": {}, "two": {}})
    state.matches.update({"A": {}, "B": {}, "C": {}})
    state.payload["sampling"] = {
        "candidates": {"one": [], "two": []},
        "candidate_offsets": {"one": 1, "two": 1},
    }

    charged = recover_missing_request_budget(state, config)

    assert charged == 60
    assert state.payload["request_metrics"]["attempted_requests"] == 60
    assert state.payload["request_budget_recovery"] == {
        "method": "conservative_upper_bound",
        "charged_attempts": 60,
    }
