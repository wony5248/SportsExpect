from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[2]
KST = ZoneInfo("Asia/Seoul")

# Keep third-party HTTP request details out of routine application logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


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
    # The on-screen manual refresh gate is intentionally separate from ADMIN_TOKEN.
    manual_refresh_password: str = os.getenv("MANUAL_REFRESH_PASSWORD", "0930")
    supabase_url: str | None = os.getenv("SUPABASE_URL")
    supabase_publishable_key: str | None = (
        os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    )
    auto_create_schema: bool = os.getenv("AUTO_CREATE_SCHEMA", "false").lower() in {"1", "true", "yes", "on"}
    kbo_base_url: str = "https://www.koreabaseball.com"
    cache_ttl_minutes: int = int(os.getenv("CACHE_TTL_MINUTES", "120"))
    mlb_stats_ttl_minutes: int = int(os.getenv("MLB_STATS_TTL_MINUTES", "720"))
    live_update_window_minutes: int = int(os.getenv("LIVE_UPDATE_WINDOW_MINUTES", "180"))
    # Production forecasts must never silently fall below the audited 20,000-game population.
    simulations: int = max(20_000, int(os.getenv("MONTE_CARLO_SIMS", "20000")))
    retry_attempts: int = int(os.getenv("COLLECTOR_RETRY_ATTEMPTS", "3"))
    retry_base_seconds: float = float(os.getenv("COLLECTOR_RETRY_BASE_SECONDS", "0.75"))
    backup_retention_days: int = int(os.getenv("BACKUP_RETENTION_DAYS", "14"))
    stale_after_minutes: int = int(os.getenv("STALE_AFTER_MINUTES", "360"))
    api_sports_key: str | None = os.getenv("API_SPORTS_KEY")
    # API-Sports currently identifies MLB and KBO by numeric league ids. Keep these deploy-time
    # configurable so a provider catalogue change does not require an application release.
    api_sports_mlb_league_id: int = int(os.getenv("API_SPORTS_MLB_LEAGUE_ID", "1"))
    api_sports_kbo_league_id: int = int(os.getenv("API_SPORTS_KBO_LEAGUE_ID", "3"))
    # This is an HTTP request cap. One collection normally uses one odds request plus one games
    # request for each scheduled date in the next 36 hours (usually 2-3 requests per league/day).
    api_sports_daily_request_budget: int = max(0, int(os.getenv("API_SPORTS_DAILY_REQUEST_BUDGET", "20")))
    # Used exclusively to encrypt user-owned provider keys at rest.
    secret_encryption_key: str | None = os.getenv("SECRET_ENCRYPTION_KEY")
    # Claude runtime safety limits are application policy, not deployment knobs.
    claude_model: str = "claude-sonnet-5"
    claude_blend_weight: float = 0.15
    claude_timeout_seconds: float = 20.0
    claude_max_tokens: int = 600
    cors_origins: tuple[str, ...] = tuple(
        value.strip() for value in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if value.strip()
    )
    cors_origin_regex: str | None = os.getenv("CORS_ORIGIN_REGEX") or None


settings = Settings()
