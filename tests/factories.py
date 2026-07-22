"""Synthetic, non-sensitive Match-V5 fixtures."""

import json
from pathlib import Path
from typing import Any


def make_match_payload(
    *,
    match_id: str = "TEST_1",
    queue_id: int = 420,
    participant_count: int = 10,
    team_count: int = 2,
    game_version: str = "16.12.788.4269",
) -> dict[str, Any]:
    positions = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
    participants = []
    for index in range(participant_count):
        team_id = 100 if index < 5 else 200
        participants.append(
            {
                "participantId": index + 1,
                "puuid": f"synthetic-player-{index + 1}",
                "teamId": team_id,
                "championId": 1000 + index,
                "championName": f"SyntheticChampion{index + 1}",
                "teamPosition": positions[index % 5],
                "individualPosition": positions[index % 5],
                "role": "SOLO",
                "lane": positions[index % 5],
                "win": team_id == 100,
                "kills": 1,
                "deaths": 0 if index == 0 else 2,
                "assists": 2,
                "goldEarned": 10_000 + index,
                "totalMinionsKilled": 100 + index,
                "neutralMinionsKilled": 10 + index,
                "visionScore": 20 + index,
                "wardsPlaced": 8,
                "wardsKilled": 2,
                "totalDamageDealtToChampions": 15_000 + index,
                "totalDamageTaken": 12_000 + index,
                "damageSelfMitigated": 5_000 + index,
                "totalHeal": 1_000 + index,
                "timeCCingOthers": 15,
                "summoner1Id": 4,
                "summoner2Id": 14,
                "perks": {
                    "styles": [
                        {
                            "description": "primaryStyle",
                            "style": 8000,
                            "selections": [{"perk": 8005}, {"perk": 9111}],
                        },
                        {
                            "description": "subStyle",
                            "style": 8300,
                            "selections": [{"perk": 8345}],
                        },
                    ],
                    "statPerks": {},
                },
                **{f"item{slot}": 3000 + slot for slot in range(7)},
            }
        )

    teams = [
        _make_team(100, win=True, champion_kills=5),
        _make_team(200, win=False, champion_kills=5),
    ][:team_count]
    return {
        "metadata": {
            "dataVersion": "2",
            "matchId": match_id,
            "participants": [item["puuid"] for item in participants],
        },
        "info": {
            "gameCreation": 1_781_000_000_000,
            "gameStartTimestamp": 1_781_000_010_000,
            "gameEndTimestamp": 1_781_001_810_000,
            "gameDuration": 1_800,
            "gameVersion": game_version,
            "queueId": queue_id,
            "gameMode": "CLASSIC",
            "gameType": "MATCHED_GAME",
            "mapId": 11,
            "platformId": "TEST1",
            "participants": participants,
            "teams": teams,
        },
    }


def write_snapshot(
    raw_root: Path,
    name: str,
    payloads: list[dict[str, Any]],
) -> Path:
    snapshot = raw_root / name
    matches_dir = snapshot / "matches"
    matches_dir.mkdir(parents=True, exist_ok=True)
    match_files = []
    for index, payload in enumerate(payloads):
        relative = Path("matches") / f"synthetic-{index}.json"
        (snapshot / relative).write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        match_files.append(relative.as_posix())
    (snapshot / "manifest.json").write_text(
        json.dumps(
            {
                "routing_region": "test-region",
                "match_files": match_files,
            }
        ),
        encoding="utf-8",
    )
    return snapshot


def _make_team(team_id: int, *, win: bool, champion_kills: int) -> dict[str, Any]:
    return {
        "teamId": team_id,
        "win": win,
        "bans": [
            {"championId": 10 + index, "pickTurn": index + 1}
            for index in range(5)
        ],
        "objectives": {
            "champion": {"first": win, "kills": champion_kills},
            "baron": {"first": win, "kills": 1 if win else 0},
            "dragon": {"first": win, "kills": 3 if win else 1},
            "riftHerald": {"first": win, "kills": 1 if win else 0},
            "tower": {"first": win, "kills": 8 if win else 2},
            "inhibitor": {"first": win, "kills": 2 if win else 0},
        },
    }
