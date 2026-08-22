from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from backend.app.models import Game, Prediction
from backend.app.services.claude_advisor import blend_with_claude, claude_prediction_advice
from backend.app.services.runtime_secrets import user_claude_configuration


def analyze_game_for_user(session: Session, user_id: str, external_id: str) -> dict[str, Any]:
    """Return an ephemeral Claude overlay; never mutate the shared forecast tables."""
    game = session.scalar(
        select(Game)
        .options(joinedload(Game.away_team), joinedload(Game.home_team))
        .where(Game.external_id == external_id)
    )
    if game is None:
        raise LookupError("Game not found")
    prediction = session.scalar(
        select(Prediction)
        .where(Prediction.game_id == game.id)
        .order_by(Prediction.created_at.desc())
        .limit(1)
    )
    if prediction is None:
        raise ValueError("A shared statistical forecast must exist before requesting Claude analysis")

    configuration = user_claude_configuration(session, user_id)
    if not configuration["configured"]:
        raise ValueError("Connect your Claude API key first")
    if not configuration["enabled"]:
        raise ValueError("Claude personal analysis is disabled in your settings")

    payload = prediction.payload or {}
    features = payload.get("features")
    if not isinstance(features, dict):
        raise ValueError("This forecast does not contain the feature snapshot required for Claude analysis")
    context = {
        "game": {
            "league": game.league,
            "date": game.game_date.isoformat(),
            "stadium": game.stadium,
            "away_team": game.away_team.name,
            "home_team": game.home_team.name,
        },
        "baseline": {
            "home_win_probability": prediction.home_win_probability,
            "home_expected_runs": prediction.home_expected_runs,
            "away_expected_runs": prediction.away_expected_runs,
            "league_average_runs_per_team": payload.get("league_average_runs"),
        },
        "features": features,
    }
    cache_key = hashlib.sha256(
        f"{user_id}:{prediction.input_hash}:{configuration['fingerprint']}:{configuration['model']}".encode()
    ).hexdigest()
    advice, metadata = claude_prediction_advice(cache_key, context, configuration)
    if advice is None:
        raise RuntimeError(f"Claude personal analysis failed ({metadata.get('error') or metadata.get('status')})")

    home_probability, home_runs, away_runs, weight = blend_with_claude(
        prediction.home_win_probability,
        prediction.home_expected_runs,
        prediction.away_expected_runs,
        advice,
    )
    return {
        "game_id": game.external_id,
        "created_from_prediction_at": prediction.created_at,
        "model": metadata.get("model"),
        "cached": metadata.get("status") == "cached",
        "blend_weight": round(weight, 4),
        "baseline": {
            "home_win_probability": prediction.home_win_probability,
            "away_win_probability": prediction.away_win_probability,
            "home_expected_runs": prediction.home_expected_runs,
            "away_expected_runs": prediction.away_expected_runs,
        },
        "personalized": {
            "home_win_probability": round(home_probability, 4),
            "away_win_probability": round(1 - home_probability, 4),
            "home_expected_runs": round(home_runs, 2),
            "away_expected_runs": round(away_runs, 2),
            "expected_total": round(home_runs + away_runs, 2),
            "confidence": round(float(advice["confidence"]), 1),
        },
        "reasons": [str(reason)[:180] for reason in advice.get("reasons", [])[:3]],
        "caution": str(advice.get("caution", ""))[:240],
        "usage": metadata.get("usage"),
        "disclaimer": "개인 Claude API 키로 생성한 보조 분석이며 공용 예측과 다른 사용자 화면에는 반영되지 않습니다.",
    }
