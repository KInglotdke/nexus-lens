"""Centralized Stage 3.2 analytical formula contracts."""

from __future__ import annotations

from typing import Any

FORMULA_CONTRACT_VERSION = "stage3.2-formulas-v1"
RATIO_UNIT = "fraction"

FORMULA_CONTRACT: dict[str, dict[str, Any]] = {
    "total_cs": {
        "formula": "total_minions_killed + neutral_minions_killed",
        "null_behavior": "null when either source component is null",
        "unit": "minions",
    },
    "duration_minutes": {
        "formula": "game_duration_seconds / 60",
        "invalid_denominator": "null when duration is null or <= 0",
        "unit": "minutes",
    },
    "kda": {
        "formula": "(kills + assists) / max(1, deaths)",
        "null_behavior": "null when kills, deaths, or assists is null",
        "zero_death_convention": (
            "zero deaths uses denominator 1, so KDA equals kills + assists"
        ),
        "unit": "ratio",
    },
    "kill_participation": {
        "formula": "(kills + assists) / team_kills",
        "invalid_denominator": "null when team_kills is null or <= 0",
        "unit": RATIO_UNIT,
    },
    "team_gold_share": {
        "formula": "gold_earned / team_gold",
        "invalid_denominator": "null when team_gold is null or <= 0",
        "unit": RATIO_UNIT,
    },
    "team_damage_share": {
        "formula": ("total_damage_dealt_to_champions / team_champion_damage"),
        "invalid_denominator": ("null when team_champion_damage is null or <= 0"),
        "unit": RATIO_UNIT,
    },
    "per_minute": {
        "formula": "source_total / duration_minutes",
        "invalid_denominator": "null when duration_minutes is null or <= 0",
        "unit": "source units per minute",
    },
}


def total_cs(
    total_minions_killed: int | None,
    neutral_minions_killed: int | None,
) -> int | None:
    """Return total CS only when both canonical components are present."""

    if total_minions_killed is None or neutral_minions_killed is None:
        return None
    return total_minions_killed + neutral_minions_killed


def duration_minutes(game_duration_seconds: int | None) -> float | None:
    """Convert a positive duration to minutes without rounding."""

    if game_duration_seconds is None or game_duration_seconds <= 0:
        return None
    return game_duration_seconds / 60


def kda(
    kills: int | None,
    deaths: int | None,
    assists: int | None,
) -> float | None:
    """Calculate KDA with denominator one for zero-death games."""

    if kills is None or deaths is None or assists is None:
        return None
    return (kills + assists) / max(1, deaths)


def ratio(
    numerator: int | float | None, denominator: int | float | None
) -> float | None:
    """Return a fractional ratio, or null for missing/non-positive denominators."""

    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def per_minute(
    value: int | float | None,
    minutes: float | None,
) -> float | None:
    """Return an unrounded rate, or null for missing/non-positive duration."""

    return ratio(value, minutes)


def complete_sum(values: list[int | None]) -> int | None:
    """Aggregate only complete source vectors; never silently impute nulls."""

    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)
