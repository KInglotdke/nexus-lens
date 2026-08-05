import sys

from scripts.build_analytical_features import parse_args as parse_stage32_args
from scripts.build_draft_aggregates import _parser as stage33b_parser
from scripts.build_draft_observations import parse_args as parse_stage33a_args
from scripts.repair_collection_lineage import parse_args as parse_lineage_args


def test_stage32_cli_accepts_expanded_match_count(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_analytical_features.py", "--expected-match-count", "1000"],
    )

    assert parse_stage32_args().expected_match_count == 1000


def test_stage33a_cli_accepts_expanded_match_count(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_draft_observations.py", "--expected-match-count", "1000"],
    )

    assert parse_stage33a_args().expected_match_count == 1000


def test_stage33b_cli_accepts_expanded_match_count() -> None:
    args = stage33b_parser().parse_args(["--expected-match-count", "1000"])

    assert args.expected_match_count == 1000


def test_lineage_cli_accepts_expanded_match_count(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["repair_collection_lineage.py", "--expected-match-count", "1000"],
    )

    assert parse_lineage_args().expected_match_count == 1000
