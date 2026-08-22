from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi import Header, HTTPException

from backend.app.config import settings


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str | None


def require_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    """Resolve a Supabase access token without trusting browser-provided identity fields."""
    if not settings.supabase_url or not settings.supabase_publishable_key:
        raise HTTPException(status_code=503, detail="Supabase user authentication is not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Sign in is required")
    try:
        response = httpx.get(
            f"{settings.supabase_url.rstrip('/')}/auth/v1/user",
            headers={
                "apikey": settings.supabase_publishable_key,
                "Authorization": f"Bearer {token}",
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="User authentication service is unavailable") from exc
    if response.status_code in {401, 403}:
        raise HTTPException(status_code=401, detail="The login session is invalid or expired")
    if response.status_code >= 400:
        raise HTTPException(status_code=503, detail="User authentication service returned an error")
    payload = response.json()
    user_id = str(payload.get("id") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="The login session is invalid")
    return CurrentUser(id=user_id, email=payload.get("email"))
