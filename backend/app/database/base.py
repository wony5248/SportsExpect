from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.config import KST, settings


class Base(DeclarativeBase):
    pass


IS_SQLITE = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False, "timeout": 30} if IS_SQLITE else {"prepare_threshold": None}
engine_options = {"connect_args": connect_args, "future": True, "pool_pre_ping": True}
if os.getenv("VERCEL"):
    # Supabase's transaction pooler owns connection reuse; serverless instances should not.
    engine_options["poolclass"] = NullPool
engine = create_engine(settings.database_url, **engine_options)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


def init_db() -> None:
    """Create a local SQLite schema; hosted PostgreSQL is managed by Alembic."""
    if not IS_SQLITE and not settings.auto_create_schema:
        return
    from backend.app.models import entities  # noqa: F401

    Base.metadata.create_all(engine)
    # Lightweight additive migration for existing local SQLite databases.
    if engine.url.get_backend_name() == "sqlite":
        additions = {
            "games": {"start_at": "DATETIME", "venue_date": "DATE"},
            "pitcher_stats": {
                "fip": "FLOAT", "k_bb_rate": "FLOAT", "rest_days": "INTEGER",
                "recent_pitches": "INTEGER", "handedness": "VARCHAR(4)", "opponent_games": "INTEGER",
                "opponent_innings": "FLOAT", "opponent_era": "FLOAT", "opponent_whip": "FLOAT",
            },
            "lineups": {
                "opponent_pitcher_id": "VARCHAR(20)", "matchup_plate_appearances": "INTEGER",
                "matchup_at_bats": "INTEGER", "matchup_hits": "INTEGER", "matchup_doubles": "INTEGER",
                "matchup_triples": "INTEGER", "matchup_home_runs": "INTEGER", "matchup_walks": "INTEGER",
                "matchup_hit_by_pitch": "INTEGER", "matchup_strikeouts": "INTEGER",
                "matchup_avg": "FLOAT", "matchup_obp": "FLOAT", "matchup_slg": "FLOAT", "matchup_ops": "FLOAT",
            },
            "runtime_secrets": {"model": "VARCHAR(80)"},
            "game_starters": {
                "prior_hit_batters": "INTEGER DEFAULT 0",
                "prior_batters_faced": "INTEGER DEFAULT 0",
                "metric_schema_version": "INTEGER DEFAULT 1",
            },
        }
        with engine.begin() as connection:
            for table, expected in additions.items():
                columns = {column["name"] for column in inspect(engine).get_columns(table)}
                for column, sql_type in expected.items():
                    if column not in columns:
                        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
            connection.execute(text("""
                UPDATE games
                SET venue_date = CASE WHEN league = 'MLB' THEN date(game_date, '-1 day') ELSE game_date END
                WHERE venue_date IS NULL
            """))


def database_datetime(value: datetime) -> datetime:
    """Keep SQLite's legacy KST-naive values and PostgreSQL's timezone-aware values coherent."""
    if IS_SQLITE and value.tzinfo:
        return value.astimezone(KST).replace(tzinfo=None)
    return value


def database_now() -> datetime:
    return database_datetime(datetime.now(KST))


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
