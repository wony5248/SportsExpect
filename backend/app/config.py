from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[2]
KST = ZoneInfo("Asia/Seoul")

# httpx's INFO message contains the complete request URL. The Odds API uses a
# query-string credential, so successful request logs must never include it.
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
    supabase_url: str | None = os.getenv("SUPABASE_URL")
    supabase_publishable_key: str | None = (
        os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    )
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
    # US books rarely post a KBO run line, so the KBO call defaults to eu (Pinnacle) plus us.
    # Each extra region multiplies The Odds API credit cost of that call.
    odds_api_regions_kbo: str = os.getenv("ODDS_API_REGIONS_KBO", "eu,us")
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
