from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[2]
KST = ZoneInfo("Asia/Seoul")


def database_url_from_environment() -> str:
    """Return a SQLAlchemy URL that works with Supabase's psycopg driver."""
    value = (
        os.getenv("BASEBALL_DATABASE_URL")
        or os.getenv("SUPABASE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or f"sqlite:///{ROOT_DIR / 'data' / 'baseball.db'}"
    )
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+psycopg://", 1)
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = database_url_from_environment()
    admin_token: str | None = os.getenv("ADMIN_TOKEN")
    auto_create_schema: bool = os.getenv("AUTO_CREATE_SCHEMA", "false").lower() in {"1", "true", "yes", "on"}
    kbo_base_url: str = "https://www.koreabaseball.com"
    cache_ttl_minutes: int = int(os.getenv("CACHE_TTL_MINUTES", "120"))
    mlb_stats_ttl_minutes: int = int(os.getenv("MLB_STATS_TTL_MINUTES", "720"))
    live_update_window_minutes: int = int(os.getenv("LIVE_UPDATE_WINDOW_MINUTES", "180"))
    simulations: int = int(os.getenv("MONTE_CARLO_SIMS", "20000"))
    retry_attempts: int = int(os.getenv("COLLECTOR_RETRY_ATTEMPTS", "3"))
    retry_base_seconds: float = float(os.getenv("COLLECTOR_RETRY_BASE_SECONDS", "0.75"))
    backup_retention_days: int = int(os.getenv("BACKUP_RETENTION_DAYS", "14"))
    stale_after_minutes: int = int(os.getenv("STALE_AFTER_MINUTES", "360"))
    odds_api_key: str | None = os.getenv("ODDS_API_KEY")
    odds_api_regions: str = os.getenv("ODDS_API_REGIONS", "us")
    odds_refresh_minutes: int = int(os.getenv("ODDS_REFRESH_MINUTES", "360"))
    claude_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    # Explicit opt-in: setting an API key alone must not transmit prediction inputs.
    claude_prediction_enabled: bool = os.getenv("CLAUDE_PREDICTION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
    claude_blend_weight: float = float(os.getenv("CLAUDE_BLEND_WEIGHT", "0.15"))
    claude_timeout_seconds: float = float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "20"))
    claude_max_tokens: int = int(os.getenv("CLAUDE_MAX_TOKENS", "600"))
    cors_origins: tuple[str, ...] = tuple(
        value.strip() for value in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if value.strip()
    )
    cors_origin_regex: str | None = os.getenv("CORS_ORIGIN_REGEX") or None


settings = Settings()
