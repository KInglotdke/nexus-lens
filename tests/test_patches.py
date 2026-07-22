from datetime import UTC, datetime

import pytest

from nexus_lens.patches import (
    accepted_public_patch_window,
    resolve_legacy_match_record,
    resolve_patch,
)


@pytest.mark.parametrize(
    ("version", "public_patch"),
    [
        ("16.12.788.4269", "26.12"),
        ("16.13.790.1000", "26.13"),
        ("16.14.792.1234", "26.14"),
    ],
)
def test_known_2026_api_patches_resolve(
    version: str,
    public_patch: str,
) -> None:
    resolution = resolve_patch(version, datetime(2026, 7, 1, tzinfo=UTC))
    assert resolution.api_game_version == version
    assert resolution.api_patch == version.rsplit(".", 2)[0]
    assert resolution.public_patch == public_patch
    assert resolution.status == "resolved"


@pytest.mark.parametrize("version", [None, "", "v16.14", "16", "16.x.1"])
def test_malformed_versions_remain_unresolved(version: str | None) -> None:
    resolution = resolve_patch(version, datetime(2026, 7, 1, tzinfo=UTC))
    assert resolution.public_patch is None
    assert resolution.status == "malformed_version"


def test_unsupported_year_remains_unresolved() -> None:
    resolution = resolve_patch(
        "16.14.792.1234",
        datetime(2025, 7, 1, tzinfo=UTC),
    )
    assert resolution.api_patch == "16.14"
    assert resolution.public_patch is None
    assert resolution.status == "unsupported_year"


def test_public_version_is_not_double_converted() -> None:
    resolution = resolve_patch(
        "26.14.1.1",
        datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert resolution.api_patch == "26.14"
    assert resolution.public_patch is None
    assert resolution.status == "unsupported_api_patch"


def test_legacy_record_gets_non_mutating_compatibility_view() -> None:
    legacy = {
        "match_id": "SYNTHETIC",
        "game_creation": "2026-07-01T00:00:00Z",
        "game_version": "16.14.792.1234",
        "patch": "16.14",
    }
    upgraded = resolve_legacy_match_record(legacy)

    assert legacy["patch"] == "16.14"
    assert upgraded["api_game_version"] == "16.14.792.1234"
    assert upgraded["api_patch"] == "16.14"
    assert upgraded["public_patch"] == "26.14"
    assert upgraded["patch_resolution_status"] == "resolved"


@pytest.mark.parametrize(
    ("target", "size", "accepted"),
    [
        ("26.14", 2, ("26.14", "26.13")),
        ("26.2", 2, ("26.2", "26.1")),
        ("26.1", 2, ("26.1",)),
        ("27.1", 2, ("27.1",)),
        ("26.14", 1, ("26.14",)),
    ],
)
def test_public_patch_window_stays_within_season(
    target: str,
    size: int,
    accepted: tuple[str, ...],
) -> None:
    assert accepted_public_patch_window(target, size) == accepted


@pytest.mark.parametrize("target", ["", "26", "26.0", "v26.14", "26.x"])
def test_public_patch_window_rejects_malformed_target(target: str) -> None:
    with pytest.raises(ValueError, match="target public patch"):
        accepted_public_patch_window(target)
