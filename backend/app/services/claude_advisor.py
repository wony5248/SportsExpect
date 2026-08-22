from __future__ import annotations

import json
from collections import OrderedDict
from threading import Lock
from typing import Any

import httpx

from backend.app.config import settings


_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "home_win_probability": {"type": "number"},
        "home_expected_runs": {"type": "number"},
        "away_expected_runs": {"type": "number"},
        "confidence": {"type": "number"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "caution": {"type": "string"},
    },
    "required": [
        "home_win_probability", "home_expected_runs", "away_expected_runs",
        "confidence", "reasons", "caution",
    ],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """You are a conservative baseball forecasting advisor.
Use only the structured pregame facts supplied by the application. Never infer injuries, weather,
news, roster moves, motivation, or any fact that is not in the input. Treat the statistical baseline
as the anchor and change it only when interactions among supplied features justify a small correction.
Return calibrated probabilities and expected runs, not betting advice. Keep each Korean reason short
and explicitly tied to supplied data."""

_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
_cache_lock = Lock()
_CACHE_LIMIT = 256


def clear_claude_cache() -> None:
    with _cache_lock:
        _cache.clear()


def claude_prediction_advice(cache_key: str, context: dict[str, Any],
                             configuration: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return optional structured Claude advice and non-sensitive execution metadata."""
    runtime = configuration
    api_key = runtime.get("api_key")
    enabled = bool(runtime.get("enabled") and api_key)
    metadata: dict[str, Any] = {
        "enabled": enabled,
        "used": False,
        "model": str(runtime.get("model") or settings.claude_model) if enabled else None,
        "key_source": runtime.get("source"),
        "key_fingerprint": runtime.get("fingerprint"),
    }
    if not enabled:
        metadata["status"] = "missing_api_key" if not runtime.get("configured") else "disabled"
        return None, metadata

    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            _cache.move_to_end(cache_key)
            return cached, {**metadata, "used": True, "status": "cached"}

    try:
        advice, usage = _request_advice(context, str(api_key), str(runtime.get("model") or settings.claude_model))
    except Exception as exc:  # External AI must never block the statistical forecast.
        metadata["status"] = "fallback"
        metadata["error"] = _safe_error(exc)
        return None, metadata

    with _cache_lock:
        _cache[cache_key] = advice
        _cache.move_to_end(cache_key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    metadata.update({"used": True, "status": "applied", "usage": usage})
    return advice, metadata


def blend_with_claude(base_home_probability: float, base_home_runs: float, base_away_runs: float,
                      advice: dict[str, Any] | None) -> tuple[float, float, float, float]:
    """Apply a deliberately bounded ensemble weight and return the effective weight."""
    if advice is None:
        return base_home_probability, base_home_runs, base_away_runs, 0.0
    confidence = _clip(_number(advice.get("confidence"), 0.0) / 100, 0.0, 1.0)
    weight = _clip(settings.claude_blend_weight, 0.0, 0.25) * (0.50 + 0.50 * confidence)
    ai_probability = _clip(_number(advice.get("home_win_probability"), base_home_probability),
                           base_home_probability - 0.12, base_home_probability + 0.12)
    ai_home_runs = _clip(_number(advice.get("home_expected_runs"), base_home_runs),
                         base_home_runs - 1.20, base_home_runs + 1.20)
    ai_away_runs = _clip(_number(advice.get("away_expected_runs"), base_away_runs),
                         base_away_runs - 1.20, base_away_runs + 1.20)
    return (
        _clip((1 - weight) * base_home_probability + weight * ai_probability, 0.05, 0.95),
        _clip((1 - weight) * base_home_runs + weight * ai_home_runs, 0.6, 10.0),
        _clip((1 - weight) * base_away_runs + weight * ai_away_runs, 0.6, 10.0),
        weight,
    )


def _request_advice(context: dict[str, Any], api_key: str, model: str) -> tuple[dict[str, Any], dict[str, int]]:
    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": settings.claude_max_tokens,
            "temperature": 0,
            "system": _SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": "Evaluate this pregame model snapshot. JSON input:\n" + json.dumps(
                    context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            }],
            "output_config": {"format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}},
        },
        timeout=settings.claude_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    text = next((block.get("text") for block in payload.get("content", []) if block.get("type") == "text"), None)
    if not text:
        raise ValueError("Claude response did not contain a text block")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Claude response was not an object")
    usage = payload.get("usage") or {}
    return parsed, {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return "network_error"
    return type(exc).__name__


def _number(value: Any, fallback: float) -> float:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else fallback
    except (TypeError, ValueError):
        return fallback


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
