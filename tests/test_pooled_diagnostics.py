from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

import nexus_lens.composition_modeling as modeling
import nexus_lens.pooled_diagnostics as diagnostics
from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.composition_modeling import (
    POSITIONS,
    build_feature_vocabulary,
    build_match_draft_corpus,
    swap_teams,
    vectorize_draft,
)
from nexus_lens.pooled_development import PooledInput
from nexus_lens.pooled_diagnostics import (
    PublishedArtifacts,
    build_post_publication_diagnostic,
    diagnostic_sha256,
    write_post_publication_diagnostic,
)
from tests.test_composition_modeling import _stage33a


def test_blue_red_target_sign_role_pairing_and_swap_are_exact(tmp_path: Path) -> None:
    corpus = build_match_draft_corpus(_stage33a(tmp_path))
    blue_win = next(row for row in corpus.observations if row.outcome == 1)
    red_win = next(row for row in corpus.observations if row.outcome == 0)

    assert (blue_win.allied_team_id, blue_win.opposing_team_id) == (100, 200)
    assert (red_win.allied_team_id, red_win.opposing_team_id) == (100, 200)
    vocabulary = build_feature_vocabulary(
        corpus.observations, include_lane_matchups=True
    )
    vector = vectorize_draft(blue_win, vocabulary)
    allied = {row.role: row.champion_id for row in blue_win.allied}
    opposing = {row.role: row.champion_id for row in blue_win.opposing}
    for row in blue_win.allied:
        index = vocabulary.index[("composition", row.role, row.champion_id, 0)]
        assert vector[index] == 1
    for row in blue_win.opposing:
        index = vocabulary.index[("composition", row.role, row.champion_id, 0)]
        assert vector[index] == -1
    for role in POSITIONS:
        low, high = sorted((allied[role], opposing[role]))
        value = vector[vocabulary.index[("lane_matchup", role, low, high)]]
        assert value == (1 if allied[role] == low else -1)

    swapped = vectorize_draft(swap_teams(blue_win), vocabulary)
    assert vector == {index: -value for index, value in swapped.items()}


def test_vocabulary_is_complete_deterministic_fold_local_and_outcome_free(
    tmp_path: Path,
) -> None:
    drafts = build_match_draft_corpus(_stage33a(tmp_path)).observations
    first = build_feature_vocabulary(drafts, include_lane_matchups=True)
    second = build_feature_vocabulary(reversed(drafts), include_lane_matchups=True)
    flipped = tuple(replace(row, outcome=1 - row.outcome) for row in drafts)
    third = build_feature_vocabulary(flipped, include_lane_matchups=True)

    assert first == second == third
    assert first.keys == tuple(sorted(set(first.keys)))
    assert all(
        vectorize_draft(left, first) == vectorize_draft(right, first)
        for left, right in zip(drafts, flipped, strict=True)
    )

    held_out = drafts[-1]
    training = drafts[:-1]
    fold_vocabulary = build_feature_vocabulary(
        training, include_lane_matchups=True
    )
    assert any(
        key not in fold_vocabulary.index
        for key in build_feature_vocabulary(
            (held_out,), include_lane_matchups=True
        ).keys
    )


def test_aggregate_diagnostic_is_zero_fit_deterministic_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pooled, protocol, published = _fixture(tmp_path)

    def forbid_fit(*args, **kwargs):
        raise AssertionError("diagnostics must not fit models")

    monkeypatch.setattr(modeling, "fit_composition_model", forbid_fit)
    first = build_post_publication_diagnostic(
        pooled=pooled,
        protocol=protocol,
        published=published,
        exclusion_reasons={"eune": {}, "euw": {}, "overall": {}},
    )
    second = build_post_publication_diagnostic(
        pooled=pooled,
        protocol=protocol,
        published=published,
        exclusion_reasons={"eune": {}, "euw": {}, "overall": {}},
    )

    assert first == second
    assert diagnostic_sha256(first) == diagnostic_sha256(second)
    assert first["audit_conclusion"]["model_fit_count"] == 0
    assert first["invariance_audit"]["target_is_blue_team_100_win"] is True
    assert first["invariance_audit"]["team_swap_negates_all_features"] is True
    assert (
        first["published_uncertainty"]["paired_model_difference"]
        ["log_loss_95_percent_interval"]
        is None
    )
    assert (
        first["prediction_dispersion"]["status"]
        == "in_sample_structural_diagnostic_not_performance"
    )
    rendered = json.dumps(first)
    for forbidden in (
        "SYNTHETIC-",
        "player_key",
        "puuid",
        "source_path",
    ):
        assert forbidden not in rendered


def test_aggregate_diagnostic_writer_is_immutable(tmp_path: Path) -> None:
    pooled, protocol, published = _fixture(tmp_path)
    first = build_post_publication_diagnostic(
        pooled=pooled,
        protocol=protocol,
        published=published,
        exclusion_reasons={"eune": {}, "euw": {}, "overall": {}},
    )
    output = tmp_path / "diagnostics"
    target = write_post_publication_diagnostic(first, output)
    before = target.read_bytes()

    assert write_post_publication_diagnostic(first, output) == target
    assert target.read_bytes() == before
    changed = {**first, "status": "changed"}
    with pytest.raises(Stage3ValidationError) as caught:
        write_post_publication_diagnostic(changed, output)
    assert caught.value.category == "diagnostic_immutable_output_conflict"
    assert target.read_bytes() == before


def _fixture(
    tmp_path: Path,
) -> tuple[PooledInput, dict, PublishedArtifacts]:
    base = build_match_draft_corpus(_stage33a(tmp_path)).observations
    drafts = tuple(
        replace(row, platform="eun1", match_id=f"EUNE-{index:03d}")
        for index, row in enumerate(base)
    ) + tuple(
        replace(row, platform="euw1", match_id=f"EUW-{index:03d}")
        for index, row in enumerate(base)
    )
    counts = {"eune": len(base), "euw": len(base), "overall": len(drafts)}
    pooled = PooledInput(
        observations=drafts,
        combined_input_sha256="a" * 64,
        source_audit=(),
        accepted_counts=counts,
        eligible_counts=counts,
        exclusion_counts={"eune": 0, "euw": 0, "overall": 0},
    )
    model_rows = [
        _model_row(drafts, "composition_only"),
        _model_row(drafts, "composition_plus_lane_matchups"),
    ]
    class_balance = {}
    for scope, rows in (
        ("overall", drafts),
        ("eun1", drafts[: len(base)]),
        ("euw1", drafts[len(base) :]),
    ):
        wins = sum(row.outcome for row in rows)
        class_balance[scope] = {
            "matches": len(rows),
            "lower_team_wins": wins,
            "lower_team_losses": len(rows) - wins,
            "lower_team_win_rate": wins / len(rows),
        }
    metrics = {
        "accepted_counts": counts,
        "eligible_counts": counts,
        "excluded_counts": {"eune": 0, "euw": 0, "overall": 0},
        "class_balance": class_balance,
        "outer_fold_fingerprint_sha256": "b" * 64,
        "final_selection_fold_fingerprint_sha256": "c" * 64,
        "unseen_feature_coverage": {
            "composition_plus_lane_matchups": {
                "lane_matchup_slots": 10,
                "unseen_lane_matchup_slots": 2,
                "unseen_lane_matchup_slot_rate": 0.2,
                "matches_with_unseen_lane_matchup": 2,
                "matches_with_unseen_lane_matchup_rate": 0.1,
            }
        },
        "out_of_fold_metrics": {
            variant: {
                "overall": {"log_loss": 0.69, "brier_score": 0.25},
                "confidence_intervals": {"overall": {}},
            }
            for variant in modeling.MODEL_VARIANTS
        },
    }
    protocol = {
        "protocol_id": "synthetic-protocol",
        "features": {
            "intercept": False,
            "evaluation_unseen_feature_fallback": "zero_coefficient_contribution",
        },
    }
    published = PublishedArtifacts(
        metrics=metrics,
        final_models={"models": model_rows},
        quality={
            "invariant_failures": [],
            "leakage_controls": {
                "fold_local_vocabulary": True,
                "match_grouped_outer_inner_and_final_selection": True,
            },
        },
        manifest={
            "combined_input_sha256": pooled.combined_input_sha256,
            "selected_l2": {
                "composition_only": 0.1,
                "composition_plus_lane_matchups": 0.1,
            },
        },
        execution={"deterministic_bundle_sha256": "d" * 64},
    )
    return pooled, protocol, published


def _model_row(drafts, variant: str) -> dict:
    vocabulary = build_feature_vocabulary(
        drafts, include_lane_matchups=variant == "composition_plus_lane_matchups"
    )
    supports: Counter[int] = Counter()
    for draft in drafts:
        for index in vectorize_draft(draft, vocabulary):
            supports[index] += 1
    coefficients = tuple(
        (index - len(vocabulary.keys) / 2) / 100_000
        for index in range(len(vocabulary.keys))
    )
    parameters = []
    for index, (key, coefficient) in enumerate(
        zip(vocabulary.keys, coefficients, strict=True)
    ):
        family, role, first, second = key
        feature = (
            {"family": family, "role": role, "champion_id": first}
            if family == "composition"
            else {
                "family": family,
                "role": role,
                "lower_champion_id": first,
                "higher_champion_id": second,
            }
        )
        parameters.append(
            {
                "feature": feature,
                "coefficient": coefficient,
                "training_match_count": supports[index],
            }
        )
    return {
        "variant": variant,
        "l2_strength": 0.1,
        "optimizer_iterations": 1,
        "optimizer_status": "converged",
        "training_match_set_sha256": "e" * 64,
        "vocabulary_sha256": diagnostics._sha256_json(
            [list(key) for key in vocabulary.keys]
        ),
        "coefficient_sha256": diagnostics._sha256_json(list(coefficients)),
        "coefficient_count": len(coefficients),
        "parameters": parameters,
    }
