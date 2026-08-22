from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.config import KST, settings
from backend.app.database import SessionLocal, init_db
from backend.app.repositories.repository import game_cards, game_dates, game_detail, performance_metrics
from backend.app.services.operations import LockUnavailable, backup_database, operational_status
from backend.app.services.backtest import walk_forward_backtest
from backend.app.services.jobs import run_cron_refresh, run_full_refresh
from backend.app.services.claude_advisor import clear_claude_cache
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
    # Let Vercel serve repeated four-user dashboard reads without another DB
    # round trip while keeping scheduled updates visible within one minute.
    response.headers["Cache-Control"] = "public, max-age=0, s-maxage=60, stale-while-revalidate=300"
    return {"date": target_date.isoformat(), "league": league, "games": game_cards(session, target_date, league)}


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


@app.post("/api/v1/admin/cron/refresh", dependencies=[Depends(require_admin)])
def cron_refresh(
    league: str = Query(pattern="^(KBO|MLB)$"),
    scope: str = Query(default="full", pattern="^(full|nearby|tomorrow|market)$"),
):
    try:
        return run_cron_refresh(league, scope)
    except LockUnavailable as exc:
        # A concurrent full/nearby run is expected occasionally; Cron can safely retry later.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
