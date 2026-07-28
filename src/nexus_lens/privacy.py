"""Shared privacy primitives for stable project-scoped pseudonyms."""

import hashlib

PLAYER_KEY_METHOD = "sha256-project-scoped-truncated-128-bit"
_PLAYER_KEY_NAMESPACE = "nexus-lens:stage3.1-v1:player"


def pseudonymize_puuid(puuid: str | None) -> str | None:
    """Create the existing stable one-way player key without storing a mapping."""

    if puuid is None or not puuid.strip():
        return None
    material = f"{_PLAYER_KEY_NAMESPACE}\0{puuid}"
    return f"player_{hashlib.sha256(material.encode()).hexdigest()[:32]}"
