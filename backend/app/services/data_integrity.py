"""Narrow, auditable maintenance checks for persisted baseball snapshots."""
from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Game, PitcherStat


def pitcher_stats_integrity(session: Session, repair: bool = False) -> dict[str, Any]:
    """Inspect pitcher snapshots; optionally remove only duplicate same-game same-side rows.

    A player appearing in multiple games is expected.  Even byte-identical pregame statistics
    across dates are retained because a pitcher can have no intervening appearance.  The sole
    automatic repair target is an accidental duplicate for the exact game/team side key.
    """
    rows = session.execute(select(PitcherStat, Game).join(Game, Game.id == PitcherStat.game_id)).all()
    before = summarize_pitcher_rows(rows)
    removed_ids: list[int] = []
    if repair:
        for group in before["_same_game_side_rows"]:
            canonical = max(group, key=lambda row: (
                bool(row[0].confirmed), row[0].collected_at, row[0].id,
            ))
            for stat, _ in group:
                if stat.id != canonical[0].id:
                    session.delete(stat)
                    removed_ids.append(stat.id)
        if removed_ids:
            session.flush()
        after_rows = session.execute(select(PitcherStat, Game).join(Game, Game.id == PitcherStat.game_id)).all()
        after = summarize_pitcher_rows(after_rows)
    else:
        after = before
    result = _public_summary(after)
    result["repair"] = {
        "requested": repair,
        "removed_same_game_side_rows": len(removed_ids),
        "removed_ids": removed_ids,
        "rule": "confirmed → newest collected_at → largest id",
    }
    result["before_repair"] = _public_summary(before)
    return result


def summarize_pitcher_rows(rows: list[tuple[Any, Any]]) -> dict[str, Any]:
    by_game_side: dict[tuple[int, str], list[tuple[Any, Any]]] = defaultdict(list)
    by_game_player: dict[tuple[int, str], list[tuple[Any, Any]]] = defaultdict(list)
    by_player_signature: dict[tuple[str, tuple[Any, ...]], list[tuple[Any, Any]]] = defaultdict(list)
    for stat, game in rows:
        by_game_side[(int(stat.game_id), str(stat.side))].append((stat, game))
        if stat.player_id:
            by_game_player[(int(stat.game_id), str(stat.player_id))].append((stat, game))
            by_player_signature[(str(stat.player_id), _snapshot_signature(stat))].append((stat, game))
    same_game_side = [group for group in by_game_side.values() if len(group) > 1]
    same_game_player = [group for group in by_game_player.values() if len(group) > 1]
    repeated_signatures = [group for group in by_player_signature.values()
                           if len({row[1].id for row in group}) > 1]
    return {
        "rows": len(rows),
        "_same_game_side_rows": same_game_side,
        "_same_game_player_rows": same_game_player,
        "_repeated_signature_rows": repeated_signatures,
    }


def _public_summary(summary: dict[str, Any]) -> dict[str, Any]:
    same_game_side = summary["_same_game_side_rows"]
    same_game_player = summary["_same_game_player_rows"]
    repeated = summary["_repeated_signature_rows"]
    return {
        "total_rows": summary["rows"],
        "same_game_side_duplicate_groups": len(same_game_side),
        "same_game_side_duplicate_rows": sum(len(group) - 1 for group in same_game_side),
        "same_game_player_conflict_groups": len(same_game_player),
        "same_game_player_conflict_rows": sum(len(group) - 1 for group in same_game_player),
        "identical_snapshot_groups_across_games": len(repeated),
        "identical_snapshot_groups_same_date": sum(
            len({row[1].game_date for row in group}) == 1 for group in repeated
        ),
        "identical_snapshot_groups_different_dates": sum(
            len({row[1].game_date for row in group}) > 1 for group in repeated
        ),
        "samples": {
            "same_game_side": [_group_sample(group) for group in same_game_side[:20]],
            "same_game_player": [_group_sample(group) for group in same_game_player[:20]],
            "identical_snapshots": [_group_sample(group) for group in repeated[:20]],
        },
        "interpretation": {
            "same_player_multiple_games": "정상: pitcher_stats는 경기별 경기 전 스냅샷입니다.",
            "identical_snapshot_across_games": "삭제하지 않음: 해당 기간 투수 지표가 변하지 않을 수 있습니다.",
            "automatic_repair_scope": "같은 game_id + side 중복만 최신 공식 수집본을 남깁니다.",
        },
    }


def _group_sample(group: list[tuple[Any, Any]]) -> dict[str, Any]:
    first = group[0][0]
    return {
        "player_id": first.player_id,
        "sides": sorted({row[0].side for row in group}),
        "game_ids": sorted({row[1].external_id for row in group}),
        "game_dates": sorted({row[1].game_date.isoformat() for row in group}),
        "row_ids": sorted(row[0].id for row in group),
        "rows": len(group),
    }


def _snapshot_signature(stat: Any) -> tuple[Any, ...]:
    return (
        stat.name, bool(stat.confirmed), stat.era, stat.whip, stat.war, stat.games,
        stat.avg_start_innings, stat.quality_starts, stat.fip, stat.k_bb_rate,
        stat.rest_days, stat.recent_pitches, stat.handedness, stat.opponent_games,
        stat.opponent_innings, stat.opponent_era, stat.opponent_whip,
        json.dumps(stat.recent or {}, sort_keys=True, ensure_ascii=False, default=str),
    )
