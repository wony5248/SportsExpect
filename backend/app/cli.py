from __future__ import annotations

import argparse
import json
from datetime import date, datetime

from backend.app.config import KST
from backend.app.services.refresh import refresh_all, refresh_kbo, refresh_mlb
from backend.app.database import SessionLocal, init_db, session_scope
from backend.app.services.backtest import walk_forward_backtest
from backend.app.services.archived_starters import backfill_archived_starters
from backend.app.services.bullpen import seed_league
from backend.app.services.model_lifecycle import run_model_lifecycle
from backend.app.services.historical_replay import run_historical_replay
from backend.app.services.operations import backup_database


def main() -> None:
    parser = argparse.ArgumentParser(description="Dugout Lab data pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh", help="collect KBO data and generate predictions")
    refresh.add_argument("--date", type=date.fromisoformat, default=datetime.now(KST).date())
    refresh.add_argument("--force", action="store_true")
    refresh.add_argument("--league", choices=("ALL", "KBO", "MLB"), default="ALL")
    backtest = sub.add_parser("backtest", help="evaluate stored pre-game predictions without future leakage")
    backtest.add_argument("--league", choices=("ALL", "KBO", "MLB"), default="ALL")
    backtest.add_argument("--stage", choices=("T_MINUS_24H", "T_MINUS_3H", "T_MINUS_60M", "T_MINUS_40M", "T_MINUS_15M"))
    lifecycle = sub.add_parser("model-lifecycle", help="train, promote, or roll back a league model")
    lifecycle.add_argument("--league", choices=("KBO", "MLB"), required=True)
    replay = sub.add_parser("historical-replay", help="replay final games from strictly pregame historical features")
    replay.add_argument("--league", choices=("KBO", "MLB"), required=True)
    replay.add_argument("--start-date", type=date.fromisoformat)
    replay.add_argument("--end-date", type=date.fromisoformat)
    replay.add_argument("--limit", type=int, default=20)
    starters = sub.add_parser("backfill-starters", help="record archived game starters with strictly-prior totals")
    starters.add_argument("--season", type=int, default=datetime.now(KST).year)
    starters.add_argument("--limit", type=int, default=400)
    starters.add_argument("--league", choices=("KBO", "MLB"), default="MLB")
    bullpen = sub.add_parser("seed-bullpen", help="create or refresh derived bullpen leverage profiles")
    bullpen.add_argument("--league", choices=("ALL", "KBO", "MLB"), default="ALL")
    sub.add_parser("backup", help="create a consistent SQLite backup")
    args = parser.parse_args()
    if args.command == "refresh":
        operation = refresh_all if args.league == "ALL" else (refresh_kbo if args.league == "KBO" else refresh_mlb)
        print(json.dumps(operation(args.date, args.force), ensure_ascii=False, indent=2))
    elif args.command == "backtest":
        init_db()
        with SessionLocal() as session:
            print(json.dumps(walk_forward_backtest(session, args.league, args.stage), ensure_ascii=False, indent=2))
    elif args.command == "model-lifecycle":
        init_db()
        with session_scope() as session:
            print(json.dumps(run_model_lifecycle(session, args.league), ensure_ascii=False, indent=2))
    elif args.command == "historical-replay":
        init_db()
        with session_scope() as session:
            print(json.dumps(run_historical_replay(
                session, args.league, args.start_date, args.end_date, args.limit,
            ), ensure_ascii=False, indent=2))
    elif args.command == "backfill-starters":
        init_db()
        with session_scope() as session:
            print(json.dumps(backfill_archived_starters(session, args.season, args.limit, args.league),
                             ensure_ascii=False, indent=2))
    elif args.command == "seed-bullpen":
        init_db()
        leagues = ("KBO", "MLB") if args.league == "ALL" else (args.league,)
        with session_scope() as session:
            report = [seed_league(session, league) for league in leagues]
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "backup":
        print(json.dumps(backup_database(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
