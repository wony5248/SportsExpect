from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.database.base import Base
from backend.app.models import BatterSplit
from backend.app.repositories.repository import fresh_batter_split_ids


def _split(player_id: str, collected_at: datetime) -> BatterSplit:
    return BatterSplit(
        league="KBO", season=2026, player_id=player_id, player_name=player_id,
        state="BASES_EMPTY", source="test", source_url="test",
        collected_at=collected_at,
    )


def test_batter_splits_are_reused_for_24_hours_and_refreshed_afterward():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 23, 12, 0)

    with Session(engine) as session:
        session.add_all([
            _split("fresh", now - timedelta(hours=23, minutes=59)),
            _split("stale", now - timedelta(hours=24, minutes=1)),
        ])
        session.commit()

        result = fresh_batter_split_ids(
            session, "KBO", 2026, ["fresh", "stale", "missing"],
            max_age=timedelta(hours=24), now=now,
        )

    assert result == {"fresh"}
