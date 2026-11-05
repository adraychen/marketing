from uuid import UUID

import httpx
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field, SecretStr

from backend.config import MODE, required
from backend.demo import ORG


class Login(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: SecretStr


class Refresh(BaseModel):
    refresh_token: SecretStr


def auth_call(method, path, token=None, payload=None):
    base = required("SUPABASE_URL").rstrip("/")
    headers = {"apikey": required("SUPABASE_PUBLISHABLE_KEY")}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        result = httpx.request(method, f"{base}/auth/v1/{path}", headers=headers, json=payload, timeout=10)
    except httpx.RequestError:
        raise HTTPException(503, "Supabase Auth is temporarily unavailable.") from None
    if result.status_code == 429 or result.status_code >= 500:
        raise HTTPException(503, "Supabase Auth is temporarily unavailable.")
    if not result.is_success:
        raise HTTPException(401, "Sign-in failed or your session expired.")
    return result.json()


def session(grant, payload):
    if MODE != "live":
        raise HTTPException(404, "Sign-in is available in live mode.")
    data = auth_call("POST", f"token?grant_type={grant}", payload=payload)
    return {key: data[key] for key in ("access_token", "refresh_token", "expires_in")}


def current_user(authorization: str | None = Header(default=None)):
    if MODE == "demo":
        return ORG
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Sign in to continue.")
    data = auth_call("GET", "user", token=token)
    try:
        return str(UUID(data["id"]))
    except (ValueError, TypeError, KeyError):
        raise HTTPException(401, "Supabase returned an invalid identity.") from None
