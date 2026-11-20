"""Server-only adsread OAuth; encrypted tokens, refresh rotation and no credential logging."""
import json
import os
import time

import httpx
from cryptography.fernet import Fernet, InvalidToken

from backend.repositories import database
from backend.connectors.reddit_ads import RedditError

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"


def configuration(org):
    if str(org) != os.getenv("REDDIT_ADS_ORGANIZATION_ID", "").strip():
        return None
    names = ("REDDIT_ADS_CLIENT_ID", "REDDIT_ADS_CLIENT_SECRET", "REDDIT_ADS_TOKEN_KEY", "REDDIT_ADS_USER_AGENT")
    if not all(os.getenv(key, "").strip() for key in names):
        return None
    return {key: os.environ[key].strip() for key in names}


def cipher(config):
    try:
        return Fernet(config["REDDIT_ADS_TOKEN_KEY"].encode())
    except (ValueError, TypeError):
        raise RedditError("Configure a valid REDDIT_ADS_TOKEN_KEY encryption key.") from None


def exchange(http, config, data):
    try:
        response = http.post(TOKEN_URL, data=data,
            auth=(config["REDDIT_ADS_CLIENT_ID"], config["REDDIT_ADS_CLIENT_SECRET"]),
            headers={"User-Agent": config["REDDIT_ADS_USER_AGENT"]}, follow_redirects=False)
        body = response.json()
        if not response.is_success or not body.get("access_token"):
            raise ValueError
        if set(body.get("scope", "").replace(",", " ").split()) != {"adsread"}:
            raise RedditError("Reauthorize Reddit with only the adsread scope.")
        body["expires_at"] = time.time() + int(body["expires_in"])
        return {key: body[key] for key in ("access_token", "refresh_token", "expires_at") if key in body}
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise RedditError("Reddit OAuth failed. Check credentials and adsread authorization; no token details logged.") from None


def save(cur, org, config, tokens):
    encrypted = cipher(config).encrypt(json.dumps(tokens).encode()).decode()
    cur.execute("""insert into marketing_private.reddit_tokens(organization_id,ciphertext) values(%s,%s)
        on conflict(organization_id) do update set ciphertext=excluded.ciphertext,updated_at=now()""", (org, encrypted))


def access_token(org, force=False):
    config = configuration(org)
    if not config:
        raise RedditError("Configure Reddit backend secrets and organization ID before authorization.")
    with database() as cur:
        cur.execute("select ciphertext from marketing_private.reddit_tokens where organization_id=%s for update", (org,))
        row = cur.fetchone()
        if not row:
            raise RedditError("Authorize Reddit using python -m backend.reddit_oauth first.")
        try:
            tokens = json.loads(cipher(config).decrypt(row["ciphertext"].encode()))
        except (InvalidToken, ValueError):
            raise RedditError("Reddit token decryption failed; check the backend encryption key.") from None
        if not force and tokens.get("expires_at", 0) > time.time() + 120:
            return tokens["access_token"]
        with httpx.Client(timeout=30) as http:
            updated = exchange(http, config, {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        tokens.update(updated)
        save(cur, org, config, tokens)
        return tokens["access_token"]
