"""Privacy-safe feasibility reports derived from normalized records."""

import json
import os
import statistics
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from nexus_lens.catalog import ProcessingCatalog
from nexus_lens.patches import resolve_legacy_match_record
from nexus_lens.processing import ProcessingSummary
from nexus_lens.storage import iter_json_records

CANONICAL_POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
SMALL_SAMPLE_THRESHOLD = 100


def build_feasibility_report(
    *,
    processed_root: Path,
    catalog: ProcessingCatalog,
    processing_summary: ProcessingSummary | None = None,
) -> dict[str, Any]:
    """Aggregate normalized data without returning participant identifiers."""

    processed_match_ids = catalog.processed_match_ids()
    match_candidates = [
        resolve_legacy_match_record(record)
        for record in iter_json_records(processed_root, "matches")
        if str(record.get("match_id")) in processed_match_ids
    ]
    matches_by_id: dict[str, dict[str, Any]] = {}
    for record in match_candidates:
        match_id = str(record.get("match_id"))
        current = matches_by_id.get(match_id)
        if current is None or (
            current.get("patch_resolution_status") != "resolved"
            and record.get("patch_resolution_status") == "resolved"
        ):
            matches_by_id[match_id] = record
    matches = list(matches_by_id.values())

    participant_candidates = [
        record
        for record in iter_json_records(processed_root, "participants")
        if str(record.get("match_id")) in processed_match_ids
    ]
    participants = list(
        {
            (str(record.get("match_id")), record.get("participant_id")): record
            for record in participant_candidates
        }.values()
    )
    team_candidates = [
        record
        for record in iter_json_records(processed_root, "teams")
        if str(record.get("match_id")) in processed_match_ids
    ]
    teams = list(
        {
            (str(record.get("match_id")), record.get("team_id")): record
            for record in team_candidates
        }.values()
    )

    match_ids = [str(match["match_id"]) for match in matches]
    participant_groups: dict[tuple[str, int | None], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for participant in participants:
        key = (str(participant["match_id"]), participant.get("team_id"))
        participant_groups[key].append(participant)

    team_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    team_lookup: dict[tuple[str, int | None], dict[str, Any]] = {}
    for team in teams:
        match_id = str(team["match_id"])
        team_groups[match_id].append(team)
        team_lookup[(match_id, team.get("team_id"))] = team

    champions = Counter(
        participant.get("champion_name") or "<missing>" for participant in participants
    )
    positions = Counter(
        participant.get("team_position") or "<missing>" for participant in participants
    )
    recognized_positions = sum(positions[position] for position in CANONICAL_POSITIONS)
    missing_positions = positions["<missing>"]
    unknown_positions = sum(
        count
        for position, count in positions.items()
        if position not in CANONICAL_POSITIONS and position != "<missing>"
    )

    winner_loser_matches = 0
    for match_id in set(match_ids):
        match_teams = team_groups.get(match_id, [])
        wins = [team.get("win") for team in match_teams]
        if len(match_teams) == 2 and wins.count(True) == 1 and wins.count(False) == 1:
            winner_loser_matches += 1

    participant_win_mismatches = 0
    for participant in participants:
        team = team_lookup.get(
            (str(participant["match_id"]), participant.get("team_id"))
        )
        if (
            team is None
            or participant.get("win") is None
            or team.get("win") is None
            or participant.get("win") != team.get("win")
        ):
            participant_win_mismatches += 1

    five_participant_teams = sum(
        len(team_participants) == 5
        for team_participants in participant_groups.values()
    )
    complete_position_teams = 0
    clean_position_teams = 0
    expected_positions = Counter(CANONICAL_POSITIONS)
    for team_participants in participant_groups.values():
        team_positions = [item.get("team_position") for item in team_participants]
        if len(team_positions) == 5 and all(
            position in CANONICAL_POSITIONS for position in team_positions
        ):
            clean_position_teams += 1
            if Counter(team_positions) == expected_positions:
                complete_position_teams += 1

    dates = sorted(
        str(match["game_creation"])
        for match in matches
        if match.get("game_creation") is not None
    )
    durations = [
        int(match["game_duration_seconds"])
        for match in matches
        if match.get("game_duration_seconds") is not None
    ]
    processing = processing_summary.as_dict() if processing_summary else None
    if processing is not None:
        elapsed = float(processing["elapsed_seconds"])
        processed_count = int(processing["newly_processed_matches"])
        processing["new_matches_per_second"] = (
            round(processed_count / elapsed, 3) if elapsed > 0 else None
        )

    match_count = len(matches)
    warning = (
        f"STRUCTURAL VALIDATION ONLY: {match_count} matches is below the "
        f"{SMALL_SAMPLE_THRESHOLD}-match feasibility threshold. Do not draw "
        "matchup, synergy, balance, or recommendation conclusions from this sample."
        if match_count < SMALL_SAMPLE_THRESHOLD
        else None
    )

    return {
        "sample_warning": warning,
        "counts": {
            "matches": match_count,
            "participants": len(participants),
            "teams": len(teams),
        },
        "queue_ids": sorted({match.get("queue_id") for match in matches}),
        "public_patches": sorted(
            {
                str(match["public_patch"])
                for match in matches
                if match.get("public_patch") is not None
            }
        ),
        "unresolved_patch_matches": sum(
            match.get("patch_resolution_status") != "resolved" for match in matches
        ),
        "patch_resolution_statuses": dict(
            sorted(
                Counter(
                    str(match.get("patch_resolution_status") or "missing")
                    for match in matches
                ).items()
            )
        ),
        "patch_resolution_methods": dict(
            sorted(
                Counter(
                    str(match.get("patch_resolution_method") or "missing")
                    for match in matches
                ).items()
            )
        ),
        "api_patches": sorted(
            {
                str(match["api_patch"])
                for match in matches
                if match.get("api_patch") is not None
            }
        ),
        "api_game_versions": sorted(
            {
                str(match["api_game_version"])
                for match in matches
                if match.get("api_game_version") is not None
            }
        ),
        "date_range_utc": {
            "start": dates[0] if dates else None,
            "end": dates[-1] if dates else None,
        },
        "champion_appearances": dict(sorted(champions.items())),
        "position_distribution": dict(sorted(positions.items())),
        "position_completeness": {
            "recognized": recognized_positions,
            "missing": missing_positions,
            "unknown": unknown_positions,
            "rate": round(recognized_positions / len(participants), 6)
            if participants
            else None,
        },
        "shape_checks": {
            "matches_with_unexpected_participant_count": sum(
                int(match.get("participant_count", -1)) != 10 for match in matches
            ),
            "matches_with_unexpected_team_count": sum(
                int(match.get("team_count", -1)) != 2 for match in matches
            ),
            "duplicate_match_records": len(match_ids) - len(set(match_ids)),
            "teams_with_exactly_five_participants": five_participant_teams,
            "participant_team_groups": len(participant_groups),
            "teams_with_clean_reported_positions": clean_position_teams,
            "teams_with_one_of_each_position": complete_position_teams,
        },
        "win_loss_checks": {
            "matches_with_exactly_one_winner_and_one_loser": winner_loser_matches,
            "matches_checked": len(set(match_ids)),
            "participant_team_win_mismatches": participant_win_mismatches,
        },
        "missingness": {
            "matches": _missingness(
                matches,
                (
                    "game_start",
                    "game_end",
                    "api_game_version",
                    "api_patch",
                    "public_patch",
                    "platform_id",
                ),
            ),
            "participants": _missingness(
                participants,
                (
                    "participant_id",
                    "team_id",
                    "champion_id",
                    "champion_name",
                    "team_position",
                    "individual_position",
                    "legacy_role",
                    "legacy_lane",
                    "win",
                    "kill_participation",
                    "gold_earned",
                    "vision_score",
                    "damage_to_champions",
                ),
            ),
            "teams": _missingness(
                teams,
                ("team_id", "win", "champion_kills"),
            ),
        },
        "duration_seconds": {
            "minimum": min(durations) if durations else None,
            "mean": round(statistics.fmean(durations), 3) if durations else None,
            "median": statistics.median(durations) if durations else None,
            "maximum": max(durations) if durations else None,
        },
        "processing_throughput": processing,
        "catalog": catalog.stats(),
    }


def write_feasibility_reports(
    report: dict[str, Any],
    report_dir: Path,
    formats: set[str],
) -> list[Path]:
    """Atomically write requested machine- and human-readable reports."""

    written: list[Path] = []
    if "json" in formats:
        path = report_dir / "feasibility_report.json"
        _atomic_write(path, json.dumps(report, indent=2, sort_keys=True) + "\n")
        written.append(path)
    if "markdown" in formats:
        path = report_dir / "feasibility_report.md"
        _atomic_write(path, _render_markdown(report))
        written.append(path)
    return written


def _missingness(
    records: list[dict[str, Any]], fields: tuple[str, ...]
) -> dict[str, dict[str, float | int | None]]:
    result: dict[str, dict[str, float | int | None]] = {}
    for field in fields:
        missing = sum(record.get(field) is None for record in records)
        result[field] = {
            "missing": missing,
            "total": len(records),
            "rate": round(missing / len(records), 6) if records else None,
        }
    return result


def _render_markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    shape = report["shape_checks"]
    wins = report["win_loss_checks"]
    positions = report["position_completeness"]
    duration = report["duration_seconds"]
    lines = [
        "# Nexus Lens feasibility report",
        "",
        f"> {report['sample_warning']}" if report["sample_warning"] else "",
        "",
        "## Sample",
        "",
        f"- Matches: {counts['matches']}",
        f"- Participants: {counts['participants']}",
        f"- Teams: {counts['teams']}",
        f"- Queue IDs: {_join(report['queue_ids'])}",
        f"- Public patches (canonical): {_join(report['public_patches'])}",
        f"- API patches (internal): {_join(report['api_patches'])}",
        f"- Complete API game versions: {_join(report['api_game_versions'])}",
        f"- Matches with unresolved public patch: "
        f"{report['unresolved_patch_matches']}",
        f"- Patch resolution statuses: {report['patch_resolution_statuses']}",
        f"- Patch resolution methods: {report['patch_resolution_methods']}",
        f"- UTC date range: {report['date_range_utc']['start']} to "
        f"{report['date_range_utc']['end']}",
        "",
        "## Integrity",
        "",
        "- Matches with unexpected participant counts: "
        f"{shape['matches_with_unexpected_participant_count']}",
        "- Matches with unexpected team counts: "
        f"{shape['matches_with_unexpected_team_count']}",
        f"- Duplicate match records: {shape['duplicate_match_records']}",
        "- Teams with exactly five participants: "
        f"{shape['teams_with_exactly_five_participants']} / "
        f"{shape['participant_team_groups']}",
        "- Teams with one of each canonical position: "
        f"{shape['teams_with_one_of_each_position']} / "
        f"{shape['teams_with_clean_reported_positions']}",
        "- Matches with exactly one winner and one loser: "
        f"{wins['matches_with_exactly_one_winner_and_one_loser']} / "
        f"{wins['matches_checked']}",
        "- Participant/team win mismatches: "
        f"{wins['participant_team_win_mismatches']}",
        f"- Position completeness: {positions['recognized']} recognized, "
        f"{positions['missing']} missing, {positions['unknown']} unknown",
        "",
        "## Duration (seconds)",
        "",
        f"- Minimum: {duration['minimum']}",
        f"- Mean: {duration['mean']}",
        f"- Median: {duration['median']}",
        f"- Maximum: {duration['maximum']}",
        "",
        "## Champion appearances",
        "",
    ]
    lines.extend(
        f"- {champion}: {count}"
        for champion, count in report["champion_appearances"].items()
    )
    lines.extend(["", "## Position distribution", ""])
    lines.extend(
        f"- {position}: {count}"
        for position, count in report["position_distribution"].items()
    )
    lines.extend(["", "## Missingness", ""])
    for record_type, fields in report["missingness"].items():
        lines.append(f"### {record_type.title()}")
        lines.append("")
        for field, values in fields.items():
            lines.append(
                f"- {field}: {values['missing']} / {values['total']} missing"
            )
        lines.append("")
    processing = report["processing_throughput"]
    lines.extend(["## Latest processing run", ""])
    if processing is None:
        lines.append("- No processing summary was supplied.")
    else:
        lines.extend(
            [
                f"- Snapshots examined: {processing['snapshots_examined']}",
                f"- Matches discovered: {processing['matches_discovered']}",
                f"- Newly processed: {processing['newly_processed_matches']}",
                f"- Already processed: {processing['already_processed_matches']}",
                f"- Rejected: {processing['rejected_matches']}",
                f"- Participant rows written: "
                f"{processing['participant_rows_written']}",
                f"- Team rows written: {processing['team_rows_written']}",
                f"- Elapsed seconds: {processing['elapsed_seconds']}",
                f"- New matches per second: "
                f"{processing['new_matches_per_second']}",
            ]
        )
        failures = processing["failure_reasons"]
        failure_summary = ", ".join(
            f"{category}={count}" for category, count in failures.items()
        )
        lines.append(f"- Failure categories: {failure_summary or 'none'}")
    lines.append("")
    lines.extend(["## Catalog", ""])
    lines.append(f"- Total entries: {report['catalog']['total_entries']}")
    for status, count in report["catalog"]["by_status"].items():
        lines.append(f"- {status}: {count}")
    for category, count in report["catalog"]["failures_by_category"].items():
        lines.append(f"- rejected/{category}: {count}")
    lines.append("")
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _join(values: list[Any]) -> str:
    return ", ".join(str(value) for value in values) if values else "none"
