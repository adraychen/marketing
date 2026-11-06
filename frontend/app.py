import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

# Streamlit adds the entry point's directory to its import path. The app also
# imports packages from the repository root.
project_root = str(Path(__file__).resolve().parents[1])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from frontend.client import APIError, call, clear_session, store_session
from frontend.views import dashboard, campaigns, content, keywords, drafts, settings

st.set_page_config(page_title="Okoo Marketing", page_icon="📈", layout="wide")
st.markdown("""
<style>
[data-testid="stSidebar"] {border-right: 1px solid rgba(128,128,128,.22);}
[data-testid="stMetricValue"] {font-size: clamp(1.25rem, 2.5vw, 2rem);}
.block-container {max-width: 1440px; padding-top: 2.5rem;}
</style>
""", unsafe_allow_html=True)

PAGES = {
    "Dashboard": dashboard, "Campaigns": campaigns, "Ad Content": content,
    "Keywords": keywords, "Drafts": drafts, "Settings": settings,
}


def sign_in():
    st.title("Okoo Marketing")
    st.caption("Sign in to your organization's marketing workspace.")
    with st.form("sign_in", clear_on_submit=True):
        email = st.text_input("Email", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        if st.form_submit_button("Sign in", type="primary"):
            store_session(call("/auth/login", "POST", payload={"email": email, "password": password}))
            st.rerun()


def app():
    mode = call("/health", timeout=75)["mode"]
    if mode == "live" and not st.session_state.get("token"):
        sign_in()
        return

    orgs = call("/orgs")
    st.sidebar.title("Okoo Marketing")
    st.sidebar.caption("STAGE 1 · GOOGLE ADS")
    if mode == "live" and st.sidebar.button("Sign out"):
        clear_session()
        st.rerun()
    if mode == "demo":
        st.info("Demo workspace · All advertising data shown here is synthetic.", icon="ℹ️")
    if not orgs:
        st.warning("Your sign-in has no organization membership. Ask your organization admin for access.")
        return

    org_id = st.sidebar.selectbox("Organization", [org["id"] for org in orgs],
                                  format_func=lambda key: next(o["name"] for o in orgs if o["id"] == key))
    org = next(org for org in orgs if org["id"] == org_id)
    base = f"/orgs/{org_id}"
    accounts = call(f"{base}/accounts")
    nav = st.sidebar.radio("Workspace", list(PAGES), key=f"nav:{org_id}")
    account = None
    if accounts:
        mapping = {account["id"]: account for account in accounts}
        account_id = st.sidebar.selectbox("Ad account", list(mapping),
            format_func=lambda key: f"{mapping[key]['name']} · {mapping[key]['currency']}", key=f"account:{org_id}")
        account = mapping[account_id]
    flash = st.session_state.pop("flash", None)
    if flash:
        st.success(flash)
    if nav == "Settings":
        settings(mode, org, base, account)
        return
    if account is None:
        st.title(nav)
        st.info("Connect a Google Ads account in Settings to get started.")
        return

    today = datetime.now(ZoneInfo(account["timezone"])).date()
    end = today - timedelta(days=1)
    selected = st.sidebar.radio("Date range", ["7 days", "30 days", "90 days", "Custom"], index=1,
                                key=f"range:{account_id}")
    if selected == "Custom":
        dates = st.sidebar.date_input("Start and end", (end - timedelta(days=29), end),
                                      max_value=today, key=f"dates:{account_id}")
        if len(dates) != 2:
            st.info("Select a start and end date.")
            return
        start, end = dates
    else:
        start = end - timedelta(days=int(selected.split()[0]) - 1)
    route = f"{base}/accounts/{account_id}"
    st.caption(f"{account['name']}  ·  {account['currency']}  ·  {account['timezone']}")
    report = call(f"{route}/report", params={"start": start.isoformat(), "end": end.isoformat()})
    PAGES[nav](report, account, route, start, end)


try:
    app()
except APIError as exc:
    for message in str(exc).splitlines():
        st.error(message)
