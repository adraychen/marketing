import os
import time
from urllib.parse import urlsplit

import httpx
import streamlit as st
from dotenv import load_dotenv
from streamlit.errors import StreamlitSecretNotFoundError

load_dotenv()


class APIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


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


def call(path, method="GET", params=None, payload=None, timeout=15, _retry_auth=True):
    base = backend_url()
    if not path.startswith("/auth/") and path != "/health":
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
        raise APIError("The dashboard API encountered an error. Check backend configuration.", result.status_code)
    if not result.is_success:
        try:
            detail = result.json().get("detail", "Request failed.")
        except ValueError:
            detail = "Request failed."
        if result.status_code == 401 and not path.startswith("/auth/"):
            if _retry_auth and st.session_state.get("refresh_token"):
                refresh(force=True)
                return call(path, method, params, payload, timeout, _retry_auth=False)
            clear_session()
            st.rerun()
        raise APIError("\n".join(detail) if isinstance(detail, list) else detail, result.status_code)
    return result.json() if result.content else None


def store_session(data):
    st.session_state.token = data["access_token"]
    st.session_state.refresh_token = data["refresh_token"]
    st.session_state.expires = time.monotonic() + data["expires_in"]


def clear_session():
    st.session_state.clear()


def refresh(force=False):
    if not st.session_state.get("token"):
        return
    if not force and time.monotonic() <= st.session_state.get("expires", 0) - 30:
        return
    # Retry only a definite temporary HTTP rejection. A lost network response may
    # already have rotated the refresh token, so don't blindly replay that request.
    for attempt in range(2):
        try:
            data = call("/auth/refresh", "POST",
                        payload={"refresh_token": st.session_state.refresh_token}, timeout=75)
            store_session(data)
            return
        except APIError as exc:
            if exc.status_code == 401:
                clear_session()
                st.rerun()
                raise
            if attempt == 0 and exc.status_code in {502, 503, 504}:
                time.sleep(1)
                continue
            raise APIError("Session renewal is temporarily unavailable. Your sign-in has been retained; "
                           "retry in a moment.", exc.status_code) from None
