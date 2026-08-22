from __future__ import annotations

import argparse
import json
from datetime import date, datetime

from backend.app.config import KST
from backend.app.services.refresh import refresh_all, refresh_kbo, refresh_mlb
from backend.app.database import SessionLocal, init_db
from backend.app.services.backtest import walk_forward_backtest
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
    backtest.add_argument("--stage", choices=("T_MINUS_24H", "T_MINUS_3H", "T_MINUS_60M", "T_MINUS_15M"))
    sub.add_parser("backup", help="create a consistent SQLite backup")
    args = parser.parse_args()
    if args.command == "refresh":
        operation = refresh_all if args.league == "ALL" else (refresh_kbo if args.league == "KBO" else refresh_mlb)
        print(json.dumps(operation(args.date, args.force), ensure_ascii=False, indent=2))
    elif args.command == "backtest":
        init_db()
        with SessionLocal() as session:
            print(json.dumps(walk_forward_backtest(session, args.league, args.stage), ensure_ascii=False, indent=2))
    elif args.command == "backup":
        print(json.dumps(backup_database(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
