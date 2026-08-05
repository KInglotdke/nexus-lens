import argparse
import json
from pathlib import Path

import pytest

from nexus_lens.population import CheckpointCompatibilityError, PopulationConfig
from nexus_lens.population_state import PopulationState
from nexus_lens.riot_client import (
    RiotApiError,
    RiotRequestBudgetExceeded,
    RiotRetryExhausted,
)
from scripts.build_canonical_tables import _accepted_patch_counts
from scripts.collect_population import (
    make_config,
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


def test_single_platform_plan_config_drives_live_collector_roots(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "canary.json"
    config_path.write_text(
        json.dumps(
            {
                "platforms": ["euw1"],
                "target_public_patch": "26.15",
                "patch_window_size": 2,
                "target_matches_per_platform": 100,
                "max_players_per_platform": 250,
                "max_match_ids_per_platform": 2500,
                "max_requests_per_platform": 2000,
                "raw_root": "isolated/raw",
                "processed_root": "isolated/processed",
                "snapshot_root": "isolated/snapshots",
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        config=config_path,
        raw_dir=Path("wrong/raw"),
        processed_dir=Path("wrong/processed"),
        state_dir=Path("wrong/snapshots"),
    )

    config = make_config(args)

    assert config.platform == "euw1"
    assert config.regional_routing == "europe"
    assert config.analysis_region == "euw"
    assert config.accepted_public_patches == ("26.15", "26.14")
    assert config.target_matches == 100
    assert config.non_sensitive_dict()["queue_id"] == 420
    assert args.raw_dir == Path("isolated/raw")
    assert args.processed_dir == Path("isolated/processed")
    assert args.state_dir == Path("isolated/snapshots")


def test_stage31_patch_expectation_comes_from_completed_manifest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "summary": {
                    "accepted_matches_by_public_patch": {
                        "26.14": 37,
                        "26.15": 63,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert _accepted_patch_counts(path) == {"26.14": 37, "26.15": 63}
