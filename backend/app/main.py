from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.app.config import KST, settings
from backend.app.database import SessionLocal, init_db
from backend.app.models import Team
from backend.app.repositories.repository import game_cards, game_dates, game_detail, performance_metrics
from backend.app.services.operations import LockUnavailable, backup_database, operational_status
from backend.app.services.backtest import walk_forward_backtest
from backend.app.services.bullpen import TIERS, apply_profile_update, load_profiles
from backend.app.services.jobs import run_cron_refresh, run_full_refresh, run_replay_refresh
from backend.app.services.model_lifecycle import lifecycle_status
from backend.app.services.claude_advisor import clear_claude_cache
from backend.app.services.data_integrity import pitcher_stats_integrity
from backend.app.services.personal_claude import analyze_game_for_user
from backend.app.services.runtime_secrets import (public_user_claude_status, remove_user_claude_key,
                                                  save_user_claude_key, user_claude_configuration,
                                                  verify_claude_key)
from backend.app.services.user_auth import CurrentUser, require_user


class ClaudeKeyAccess(BaseModel):
    api_key: SecretStr | None = Field(default=None, min_length=20, max_length=512)


class ClaudeKeyRegistration(ClaudeKeyAccess):
    model: str = Field(min_length=3, max_length=80, pattern=r"^claude-")
    enabled: bool = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Dugout Lab API",
    version="0.1.0",
    description="KBO/MLB official data + shared statistical forecasts + private per-user Claude analysis.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not configured")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=401, detail="Invalid admin token")


@app.get("/health")
def health(session: Session = Depends(get_session)):
    session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected", "time": datetime.now(KST).isoformat()}


@app.get("/ready")
def readiness(session: Session = Depends(get_session)):
    status = operational_status(session)
    if status["status"] != "ok":
        raise HTTPException(status_code=503, detail=status)
    return status


@app.get("/api/v1/operations/status")
def operations_status(session: Session = Depends(get_session)):
    return operational_status(session)


@app.get("/api/v1/games")
def list_games(
    response: Response,
    target_date: date = Query(alias="date", default_factory=lambda: datetime.now(KST).date()),
    league: str = Query(default="ALL", pattern="^(ALL|KBO|MLB)$"),
    session: Session = Depends(get_session),
):
    cards = game_cards(session, target_date, league)
    # Never serve a stale live state from the CDN. Scheduled/final boards can still share a
    # short cache, while an open live board polls the database directly once per minute.
    response.headers["Cache-Control"] = (
        "no-store" if any(game["status"] == "LIVE" for game in cards)
        else "public, max-age=0, s-maxage=60, stale-while-revalidate=300"
    )
    return {"date": target_date.isoformat(), "league": league, "games": cards}


@app.get("/api/v1/game-dates")
def list_game_dates(
    response: Response,
    year: int = Query(default_factory=lambda: datetime.now(KST).year, ge=2020, le=2100),
    league: str = Query(default="ALL", pattern="^(ALL|KBO|MLB)$"),
    session: Session = Depends(get_session),
):
    response.headers["Cache-Control"] = "public, max-age=0, s-maxage=300, stale-while-revalidate=900"
    return {"year": year, "league": league, "dates": game_dates(session, year, league)}


@app.get("/api/v1/games/{external_id}")
def retrieve_game(external_id: str, session: Session = Depends(get_session)):
    result = game_detail(session, external_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Game not found")
    return result


@app.get("/api/v1/model/metrics")
def metrics(session: Session = Depends(get_session)):
    return performance_metrics(session)


@app.get("/api/v1/model/backtest")
def backtest(league: str = Query(default="ALL", pattern="^(ALL|KBO|MLB)$"),
             stage: str | None = Query(default=None), session: Session = Depends(get_session)):
    return walk_forward_backtest(session, league, stage)


@app.get("/api/v1/model/lifecycle")
def model_lifecycle(league: str = Query(default="KBO", pattern="^(KBO|MLB)$"),
                    session: Session = Depends(get_session)):
    return lifecycle_status(session, league)


@app.post("/api/v1/admin/refresh", dependencies=[Depends(require_admin)])
def refresh(target_date: date = Query(alias="date", default_factory=lambda: datetime.now(KST).date()),
            force: bool = False, league: str = Query(default="ALL", pattern="^(ALL|KBO|MLB)$")):
    try:
        if league == "ALL":
            kbo = run_full_refresh("KBO", target_date, force=force, trigger="manual_api")
            mlb = run_full_refresh("MLB", target_date, force=force, trigger="manual_api")
            return {"date": target_date.isoformat(), "leagues": {"KBO": kbo, "MLB": mlb}}
        return run_full_refresh(league, target_date, force=force, trigger="manual_api")
    except LockUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/admin/data-integrity/pitchers", dependencies=[Depends(require_admin)])
def pitcher_data_integrity(repair: bool = Query(default=False), session: Session = Depends(get_session)):
    """Admin-only audit; repair deletes only duplicate rows for one game/team side."""
    report = pitcher_stats_integrity(session, repair=repair)
    if repair:
        session.commit()
    return report


@app.post("/api/v1/admin/cron/refresh", dependencies=[Depends(require_admin)])
def cron_refresh(
    league: str = Query(pattern="^(KBO|MLB)$"),
    scope: str = Query(default="full", pattern="^(full|nearby|tomorrow|market|checkpoints|lifecycle|splits|replay)$"),
):
    try:
        return run_cron_refresh(league, scope)
    except LockUnavailable as exc:
        # A concurrent full/nearby run is expected occasionally; Cron can safely retry later.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/admin/replay", dependencies=[Depends(require_admin)])
def historical_replay(league: str = Query(pattern="^(KBO|MLB)$"), limit: int = Query(default=20, ge=1, le=100)):
    """Backfill archive forecasts without presenting them as original live predictions."""
    try:
        return run_replay_refresh(league, limit)
    except LockUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class BullpenTierUpdate(BaseModel):
    """One team's relief profile. Multipliers are relative to that club's own staff average."""

    team_code: str = Field(min_length=1, max_length=8)
    high_leverage: float = Field(ge=.3, le=2.0)
    middle: float = Field(ge=.3, le=2.0)
    chase: float = Field(ge=.3, le=2.0)
    mop_up: float = Field(ge=.3, le=2.0)
    high_leverage_arms: list[str] = Field(default_factory=list, max_length=12)
    middle_arms: list[str] = Field(default_factory=list, max_length=12)
    chase_arms: list[str] = Field(default_factory=list, max_length=12)
    mop_up_arms: list[str] = Field(default_factory=list, max_length=12)
    note: str | None = Field(default=None, max_length=300)


@app.post("/api/v1/admin/bullpen", dependencies=[Depends(require_admin)])
def update_bullpen(league: str = Query(pattern="^(KBO|MLB)$"),
                   source: str = Query(default="CLAUDE", pattern="^(CLAUDE|OFFICIAL|MANUAL)$"),
                   updates: list[BullpenTierUpdate] = Body(...),
                   session: Session = Depends(get_session)):
    """Replace bullpen leverage profiles. Predictions pick the change up on the next refresh
    because the staff plan is part of each prediction's input hash."""
    teams = {team.code: team.id for team in session.scalars(select(Team).where(Team.league == league)).all()}
    unknown = [row.team_code for row in updates if row.team_code not in teams]
    if unknown:
        raise HTTPException(status_code=404, detail=f"Unknown {league} team codes: {unknown}")
    applied = []
    for row in updates:
        result = apply_profile_update(
            session, teams[row.team_code],
            {tier: getattr(row, tier) for tier in TIERS},
            source=source, note=row.note,
            arms={tier: getattr(row, f"{tier}_arms") for tier in TIERS},
        )
        applied.append({"team_code": row.team_code, **result})
    session.commit()
    changed = [row for row in applied if row["changed"]]
    return {"league": league, "source": source, "submitted": len(applied), "changed": len(changed),
            "teams": applied}


@app.get("/api/v1/admin/bullpen", dependencies=[Depends(require_admin)])
def read_bullpen(league: str = Query(pattern="^(KBO|MLB)$"), session: Session = Depends(get_session)):
    codes = {team.id: team.code for team in session.scalars(select(Team).where(Team.league == league)).all()}
    return {"league": league, "teams": [
        {"team_code": codes.get(team_id), **profile} for team_id, profile in load_profiles(session, league).items()
    ]}


@app.post("/api/v1/admin/backup", dependencies=[Depends(require_admin)])
def backup():
    try:
        return backup_database()
    except LockUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/me/claude-key")
def claude_key_status(user: CurrentUser = Depends(require_user), session: Session = Depends(get_session)):
    return public_user_claude_status(session, user.id)


@app.post("/api/v1/me/claude-key/models")
def claude_models(access: ClaudeKeyAccess, user: CurrentUser = Depends(require_user),
                  session: Session = Depends(get_session)):
    try:
        api_key = access.api_key.get_secret_value() if access.api_key else user_claude_configuration(session, user.id).get("api_key")
        if not api_key:
            raise ValueError("Register or enter a Claude API key first")
        return {"models": verify_claude_key(str(api_key))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/me/claude-key")
def register_claude_key(registration: ClaudeKeyRegistration, user: CurrentUser = Depends(require_user),
                        session: Session = Depends(get_session)):
    try:
        api_key = registration.api_key.get_secret_value() if registration.api_key else user_claude_configuration(session, user.id).get("api_key")
        if not api_key:
            raise ValueError("Register or enter a Claude API key first")
        available_models = verify_claude_key(api_key)
        if registration.model not in {item["id"] for item in available_models}:
            raise ValueError("The selected Claude model is not available for this API key")
        status = save_user_claude_key(session, user.id, api_key, registration.model, registration.enabled)
        session.commit()
        clear_claude_cache()
        return {
            **status,
            "connection_verified": True,
            "configured_model_available": True,
        }
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        session.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/me/claude-key/remove")
def delete_claude_key(user: CurrentUser = Depends(require_user), session: Session = Depends(get_session)):
    try:
        status = remove_user_claude_key(session, user.id)
        session.commit()
        clear_claude_cache()
        return status
    except RuntimeError as exc:
        session.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/v1/games/{external_id}/claude-analysis")
def personal_claude_analysis(external_id: str, user: CurrentUser = Depends(require_user),
                             session: Session = Depends(get_session)):
    try:
        return analyze_game_for_user(session, user.id, external_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
