from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.app.config import KST, ROOT_DIR, settings
from backend.app.database import IS_SQLITE, database_now, engine
from backend.app.models import CrawlLog, Game, Prediction, PredictionSnapshot


LOCK_DIR = ROOT_DIR / "data" / "locks"
BACKUP_DIR = ROOT_DIR / "data" / "backups"


class LockUnavailable(RuntimeError):
    pass


@contextmanager
def process_lock(name: str, *, blocking: bool = False) -> Iterator[None]:
    """Small-host singleton lock; sufficient for the intended one-machine deployment."""
    import fcntl

    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCK_DIR / f"{name}.lock"
    handle = path.open("a+", encoding="utf-8")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise LockUnavailable(f"{name} 작업이 이미 실행 중입니다.") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": __import__("os").getpid(), "acquired_at": datetime.now(KST).isoformat()}))
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def job_lock(name: str, *, blocking: bool = False) -> Iterator[None]:
    """Serialize a refresh across Vercel instances with a PostgreSQL advisory lock."""
    if IS_SQLITE:
        with process_lock(name, blocking=blocking):
            yield
        return
    connection = engine.connect()
    # Transaction-scoped locks are safe with Supabase's transaction pooler (PgBouncer).
    if blocking:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": name})
    else:
        acquired = bool(connection.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:name))"), {"name": name},
        ))
        if not acquired:
            connection.close()
            raise LockUnavailable(f"{name} 작업이 이미 실행 중입니다.")
    try:
        yield
    finally:
        connection.rollback()
        connection.close()


def backup_database() -> dict[str, Any]:
    if not settings.database_url.startswith("sqlite:///"):
        return {"status": "skipped", "reason": "SQLite가 아닌 데이터베이스는 제공자 백업을 사용하세요."}
    source = Path(settings.database_url.removeprefix("sqlite:///"))
    if not source.exists():
        return {"status": "skipped", "reason": "데이터베이스 파일이 없습니다."}
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(KST)
    destination = BACKUP_DIR / f"baseball-{now:%Y%m%d-%H%M%S}.db"
    with process_lock("database-backup", blocking=False):
        with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
        cutoff = now - timedelta(days=settings.backup_retention_days)
        removed = 0
        for candidate in BACKUP_DIR.glob("baseball-*.db"):
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=KST)
            if modified < cutoff:
                candidate.unlink()
                removed += 1
    return {"status": "ok", "path": str(destination), "bytes": destination.stat().st_size, "expired_removed": removed}


def operational_status(session: Session) -> dict[str, Any]:
    now = database_now()
    last_log = session.scalar(select(CrawlLog).order_by(CrawlLog.finished_at.desc()).limit(1))
    last_success = session.scalar(select(CrawlLog).where(CrawlLog.status == "SUCCESS").order_by(CrawlLog.finished_at.desc()).limit(1))
    recent_cutoff = now - timedelta(hours=24)
    failures = session.scalar(select(func.count(CrawlLog.id)).where(
        CrawlLog.status == "FAILED", CrawlLog.finished_at >= recent_cutoff,
    )) or 0
    attempts = session.scalar(select(func.count(CrawlLog.id)).where(CrawlLog.finished_at >= recent_cutoff)) or 0
    scheduled = session.scalar(select(func.count(Game.id)).where(Game.status == "SCHEDULED")) or 0
    predictions = session.scalar(select(func.count(Prediction.id))) or 0
    recent_snapshots = session.scalars(select(PredictionSnapshot).where(
        PredictionSnapshot.captured_at >= recent_cutoff,
    )).all()
    change_alerts = sum(any(change.get("type") not in {"INITIAL", "STABLE"} for change in (snapshot.changes or []))
                        for snapshot in recent_snapshots)
    success_at = last_success.finished_at if last_success else None
    stale = success_at is None or success_at < now - timedelta(minutes=settings.stale_after_minutes)
    failure_rate = failures / attempts if attempts else 0.0
    degraded = stale or (last_log is not None and last_log.status == "FAILED") or failure_rate >= .2
    return {
        "status": "degraded" if degraded else "ok",
        "database": "connected",
        "collection_data_stale": stale,
        "stale_after_minutes": settings.stale_after_minutes,
        "last_collection": _log_payload(last_log),
        "last_success": _log_payload(last_success),
        "failures_24h": failures,
        "attempts_24h": attempts,
        "failure_rate_24h": round(failure_rate, 4),
        "scheduled_games": scheduled,
        "stored_predictions": predictions,
        "change_alerts_24h": change_alerts,
        "time": datetime.now(KST).isoformat(),
    }


def _log_payload(log: CrawlLog | None) -> dict[str, Any] | None:
    if not log:
        return None
    return {
        "collector": log.collector,
        "status": log.status,
        "finished_at": _aware_iso(log.finished_at),
        "error": log.error,
    }


def _aware_iso(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=KST)).isoformat()
