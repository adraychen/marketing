import os
import time
from urllib.parse import urlsplit

import httpx
import streamlit as st
from dotenv import load_dotenv
from streamlit.errors import StreamlitSecretNotFoundError

load_dotenv()


class APIError(Exception):
    pass


def backend_url():
    try:
        configured = st.secrets["BACKEND_URL"]
    except (KeyError, StreamlitSecretNotFoundError):
        configured = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
    if isinstance(configured, str):
        base = configured.strip().rstrip("/")
        try:
            url = urlsplit(base)
            if (url.scheme in {"http", "https"} and url.hostname and not url.username
                    and not url.password and not url.query and not url.fragment and url.path != "/health"):
                return base
        except ValueError:
            pass
    raise APIError("Set BACKEND_URL to the FastAPI base URL without /health.")


def call(path, method="GET", params=None, payload=None, timeout=15):
    base = backend_url()
    if not path.startswith("/auth/"):
        refresh()
    headers = {}
    if st.session_state.get("token"):
        headers["Authorization"] = "Bearer " + st.session_state.token
    try:
        result = httpx.request(method, base + path, params=params, json=payload, headers=headers,
                               timeout=timeout, trust_env=False)
    except httpx.TimeoutException:
        raise APIError(f"The dashboard API at {urlsplit(base).netloc} timed out. Open its /health page and retry.") from None
    except httpx.RequestError:
        raise APIError(
            f"The dashboard API at {urlsplit(base).netloc} could not be reached. Check BACKEND_URL and the API's /health page."
        ) from None
    if result.status_code >= 500 and result.status_code != 502 and result.status_code != 503:
        raise APIError("The dashboard API encountered an error. Check backend configuration.")
    if not result.is_success:
        try:
            detail = result.json().get("detail", "Request failed.")
        except ValueError:
            detail = "Request failed."
        if result.status_code == 401 and not path.startswith("/auth/"):
            clear_session()
            st.rerun()
        raise APIError("\n".join(detail) if isinstance(detail, list) else detail)
    return result.json() if result.content else None


def store_session(data):
    st.session_state.token = data["access_token"]
    st.session_state.refresh_token = data["refresh_token"]
    st.session_state.expires = time.monotonic() + data["expires_in"]


def clear_session():
    st.session_state.clear()


def refresh():
    if st.session_state.get("token") and time.monotonic() > st.session_state.expires - 30:
        try:
            store_session(call("/auth/refresh", "POST", payload={"refresh_token": st.session_state.refresh_token}))
        except APIError:
            clear_session()
            st.rerun()
