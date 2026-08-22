from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import UserClaudeSetting


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _fernet(master_secret: str | None = None) -> Fernet:
    secret = master_secret or settings.secret_encryption_key
    if not secret or len(secret) < 16:
        raise RuntimeError("SECRET_ENCRYPTION_KEY must be configured with at least 16 characters")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str, master_secret: str | None = None) -> str:
    return _fernet(master_secret).encrypt(value.encode()).decode()


def decrypt_secret(value: str, master_secret: str | None = None) -> str:
    try:
        return _fernet(master_secret).decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Stored API key cannot be decrypted; register it again") from exc


def save_user_claude_key(session: Session, user_id: str, api_key: str, model: str,
                         enabled: bool = True) -> dict[str, Any]:
    normalized = api_key.strip()
    if len(normalized) < 20:
        raise ValueError("Claude API key is too short")
    row = session.get(UserClaudeSetting, user_id)
    values = {
        "ciphertext": encrypt_secret(normalized),
        "fingerprint": _fingerprint(normalized),
        "model": model,
        "enabled": enabled,
        "updated_at": datetime.now(UTC),
    }
    if row is None:
        row = UserClaudeSetting(user_id=user_id, **values)
        session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    session.flush()
    return public_user_claude_status(session, user_id)


def remove_user_claude_key(session: Session, user_id: str) -> dict[str, Any]:
    row = session.get(UserClaudeSetting, user_id)
    if row is not None:
        session.delete(row)
        session.flush()
    return public_user_claude_status(session, user_id)


def user_claude_configuration(session: Session, user_id: str) -> dict[str, Any]:
    row = session.get(UserClaudeSetting, user_id)
    if row is None:
        return _empty_configuration()
    try:
        api_key = decrypt_secret(row.ciphertext)
        error = None
    except RuntimeError:
        api_key = None
        error = "decrypt_error"
    return {
        "configured": bool(api_key),
        "enabled": bool(row.enabled and api_key),
        "source": "user",
        "fingerprint": row.fingerprint,
        "updated_at": row.updated_at,
        "model": row.model or settings.claude_model,
        "api_key": api_key,
        "error": error,
    }


def public_user_claude_status(session: Session, user_id: str) -> dict[str, Any]:
    configuration = user_claude_configuration(session, user_id)
    return {key: value for key, value in configuration.items() if key != "api_key"}


def _empty_configuration() -> dict[str, Any]:
    return {
        "configured": False,
        "enabled": False,
        "source": "none",
        "fingerprint": None,
        "updated_at": None,
        "model": settings.claude_model,
        "api_key": None,
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
