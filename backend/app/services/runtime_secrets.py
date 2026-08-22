from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import RuntimeSecret


CLAUDE_SECRET_NAME = "anthropic_api_key"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _fernet(master_secret: str | None = None) -> Fernet:
    secret = master_secret or settings.secret_encryption_key or settings.admin_token
    if not secret or len(secret) < 16:
        raise RuntimeError("SECRET_ENCRYPTION_KEY or a sufficiently long ADMIN_TOKEN is required")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str, master_secret: str | None = None) -> str:
    return _fernet(master_secret).encrypt(value.encode()).decode()


def decrypt_secret(value: str, master_secret: str | None = None) -> str:
    try:
        return _fernet(master_secret).decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Stored API key cannot be decrypted; register it again") from exc


def save_claude_key(session: Session, api_key: str, model: str, enabled: bool = True) -> dict[str, Any]:
    normalized = api_key.strip()
    if len(normalized) < 20:
        raise ValueError("Claude API key is too short")
    row = session.get(RuntimeSecret, CLAUDE_SECRET_NAME)
    values = {
        "ciphertext": encrypt_secret(normalized),
        "fingerprint": _fingerprint(normalized),
        "model": model,
        "enabled": enabled,
        "updated_at": datetime.now(UTC),
    }
    if row is None:
        row = RuntimeSecret(name=CLAUDE_SECRET_NAME, **values)
        session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    session.flush()
    return public_claude_status(session)


def remove_claude_key(session: Session) -> dict[str, Any]:
    row = session.get(RuntimeSecret, CLAUDE_SECRET_NAME)
    if row is not None:
        session.delete(row)
        session.flush()
    return public_claude_status(session)


def claude_configuration(session: Session | None = None) -> dict[str, Any]:
    owns_session = session is None
    active_session = session or SessionLocal()
    try:
        try:
            row = active_session.get(RuntimeSecret, CLAUDE_SECRET_NAME)
        except SQLAlchemyError:
            # Forecasting remains available while a hosted database is waiting for its migration.
            configuration = _environment_configuration()
            configuration["error"] = "runtime_secret_store_unavailable"
            return configuration
        if row is not None:
            try:
                api_key = decrypt_secret(row.ciphertext)
                error = None
            except RuntimeError:
                api_key = None
                error = "decrypt_error"
            return {
                "configured": bool(api_key),
                "enabled": bool(row.enabled and api_key),
                "source": "admin_ui",
                "fingerprint": row.fingerprint,
                "updated_at": row.updated_at,
                "model": row.model or settings.claude_model,
                "api_key": api_key,
                "error": error,
            }
        return _environment_configuration()
    finally:
        if owns_session:
            active_session.close()


def public_claude_status(session: Session) -> dict[str, Any]:
    configuration = claude_configuration(session)
    return {key: value for key, value in configuration.items() if key != "api_key"}


def _environment_configuration() -> dict[str, Any]:
    env_key = (settings.claude_api_key or "").strip() or None
    return {
        "configured": bool(env_key),
        "enabled": bool(settings.claude_prediction_enabled and env_key),
        "source": "environment" if env_key else "none",
        "fingerprint": _fingerprint(env_key) if env_key else None,
        "updated_at": None,
        "model": settings.claude_model,
        "api_key": env_key,
        "error": None,
    }


def verify_claude_key(api_key: str) -> list[dict[str, str]]:
    """Validate a standard Claude API key without creating a billable message."""
    try:
        response = httpx.get(
            "https://api.anthropic.com/v1/models",
            params={"limit": 100},
            headers={
                "x-api-key": api_key.strip(),
                "anthropic-version": "2023-06-01",
            },
            timeout=settings.claude_timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise ValueError("Claude API key authentication failed") from exc
        raise RuntimeError(f"Claude model lookup failed with HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("Claude API connection failed") from exc
    payload = response.json()
    return [
        {"id": str(item["id"]), "display_name": str(item.get("display_name") or item["id"])}
        for item in payload.get("data", []) if item.get("id")
    ]
