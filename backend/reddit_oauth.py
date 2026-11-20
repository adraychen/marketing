"""Run in a trusted terminal with backend secrets; tokens are never printed."""
import getpass
import os
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from backend.reddit_auth import configuration, exchange, save
from backend.repositories import database
from backend.connectors.reddit_ads import RedditError


def main():
    org = os.getenv("REDDIT_ADS_ORGANIZATION_ID", "")
    config = configuration(org)
    redirect = os.getenv("REDDIT_ADS_REDIRECT_URI", "http://localhost:8765/reddit/callback")
    if not config:
        raise RedditError("Configure Reddit client ID, client secret, token encryption key, user agent and organization ID first.")
    state = secrets.token_urlsafe(32)
    print("Open this authorization URL in your browser:")
    print("https://www.reddit.com/api/v1/authorize?" + urlencode({"client_id": config["REDDIT_ADS_CLIENT_ID"],
          "response_type": "code", "state": state, "redirect_uri": redirect, "duration": "permanent", "scope": "adsread"}))
    print("After approval, copy the redirect URL even if localhost does not load. Input is hidden.")
    callback = urlsplit(getpass.getpass("Redirect URL: "))
    expected = urlsplit(redirect)
    query = parse_qs(callback.query)
    if (callback.scheme, callback.netloc, callback.path) != (expected.scheme, expected.netloc, expected.path) or not secrets.compare_digest(query.get("state", [""])[0], state):
        raise RedditError("OAuth redirect or state did not match. Restart authorization.")
    if not query.get("code"):
        raise RedditError("Reddit authorization was denied or returned no code.")
    with httpx.Client(timeout=30) as http:
        tokens = exchange(http, config, {"grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": redirect})
    if not tokens.get("refresh_token"):
        raise RedditError("Authorization omitted the refresh token. Use permanent authorization.")
    with database() as cur:
        save(cur, org, config, tokens)
    print("Reddit adsread authorization saved encrypted. Run the sync from Connections & Sync.")


if __name__ == "__main__":
    try:
        main()
    except RedditError as exc:
        raise SystemExit(str(exc)) from None
