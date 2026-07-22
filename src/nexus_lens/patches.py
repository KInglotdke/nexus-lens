"""Versioned resolution from Match-V5 client versions to public patches."""

import re
from dataclasses import dataclass
from datetime import datetime

_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)(?:\.\d+)*$")
_PUBLIC_PATCH_PATTERN = re.compile(r"^(\d+)\.([1-9]\d*)$")


@dataclass(frozen=True)
class PatchMappingRule:
    name: str
    game_year: int
    api_major: int
    public_major: int
    minimum_minor: int = 1
    maximum_minor: int = 99


@dataclass(frozen=True)
class PatchResolution:
    api_game_version: str | None
    api_patch: str | None
    public_patch: str | None
    method: str
    status: str


PATCH_MAPPING_RULES: tuple[PatchMappingRule, ...] = (
    PatchMappingRule(
        name="riot_public_patch_2026_api16_v1",
        game_year=2026,
        api_major=16,
        public_major=26,
        minimum_minor=1,
        maximum_minor=99,
    ),
)


def accepted_public_patch_window(
    target_public_patch: str,
    window_size: int = 2,
) -> tuple[str, ...]:
    """Return newest-first public patches without crossing a season major."""

    match = _PUBLIC_PATCH_PATTERN.fullmatch(target_public_patch.strip())
    if match is None:
        raise ValueError("target public patch must look like 26.14")
    if not 1 <= window_size <= 100:
        raise ValueError("patch window size must be between 1 and 100")
    major, target_minor = map(int, match.groups())
    lower_minor = max(1, target_minor - window_size + 1)
    return tuple(
        f"{major}.{minor}"
        for minor in range(target_minor, lower_minor - 1, -1)
    )


def resolve_patch(
    api_game_version: str | None,
    game_creation: datetime,
    *,
    rules: tuple[PatchMappingRule, ...] = PATCH_MAPPING_RULES,
) -> PatchResolution:
    """Resolve one version using explicit year-scoped rules without guessing."""

    parsed = _parse_version(api_game_version)
    if parsed is None:
        return PatchResolution(
            api_game_version=api_game_version,
            api_patch=None,
            public_patch=None,
            method="none",
            status="malformed_version",
        )
    api_major, minor = parsed
    api_patch = f"{api_major}.{minor}"
    for rule in rules:
        if (
            game_creation.year == rule.game_year
            and api_major == rule.api_major
            and rule.minimum_minor <= minor <= rule.maximum_minor
        ):
            return PatchResolution(
                api_game_version=api_game_version,
                api_patch=api_patch,
                public_patch=f"{rule.public_major}.{minor}",
                method=rule.name,
                status="resolved",
            )

    status = (
        "unsupported_year"
        if not any(rule.game_year == game_creation.year for rule in rules)
        else "unsupported_api_patch"
    )
    return PatchResolution(
        api_game_version=api_game_version,
        api_patch=api_patch,
        public_patch=None,
        method="none",
        status=status,
    )


def resolve_legacy_match_record(record: dict[str, object]) -> dict[str, object]:
    """Return a non-mutating Stage 2 view of a Stage 1 match record."""

    if "api_game_version" in record:
        return dict(record)
    upgraded = dict(record)
    game_version = record.get("game_version")
    creation_value = record.get("game_creation")
    try:
        creation = datetime.fromisoformat(str(creation_value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        creation = datetime.min
    resolution = resolve_patch(
        str(game_version) if game_version is not None else None,
        creation,
    )
    upgraded.update(
        {
            "api_game_version": resolution.api_game_version,
            "api_patch": resolution.api_patch or record.get("patch"),
            "public_patch": resolution.public_patch,
            "patch_resolution_method": resolution.method,
            "patch_resolution_status": (
                resolution.status
                if resolution.status == "resolved"
                else "legacy_unresolved"
            ),
        }
    )
    upgraded.pop("game_version", None)
    upgraded.pop("patch", None)
    return upgraded


def _parse_version(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    match = _VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))
