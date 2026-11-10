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
from frontend.content_marketing import show_content_marketing
from frontend.reports import show_reports
from frontend.tiktok_ads import show_tiktok_ads
from frontend.weekly_review import show_weekly_review
from frontend.views import dashboard, campaigns, content, keywords, drafts, settings

st.set_page_config(page_title="Okoo Marketing", page_icon="📈", layout="wide")
st.markdown("""
<style>
[data-testid="stSidebar"] {border-right: 1px solid rgba(128,128,128,.22);}
[data-testid="stMetricValue"] {font-size: clamp(1.25rem, 2.5vw, 2rem);}
.block-container {max-width: 1440px; padding-top: 2.5rem;}
</style>
""", unsafe_allow_html=True)

NAVIGATION = ("Dashboard", "Weekly Review", "Campaigns", "Ad Content", "Reports", "Settings")
AD_CONTENT_VIEWS = {"Ads & Assets": content, "Keywords": keywords, "Drafts": drafts}


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
    health = call("/health", timeout=75)
    mode = health["mode"]
    if mode == "live" and not st.session_state.get("token"):
        sign_in()
        return

    orgs = call("/orgs")
    st.sidebar.title("Okoo Marketing")
    st.sidebar.caption("GOOGLE ADS · CONTENT MARKETING")
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
    nav = st.sidebar.radio("Navigation", NAVIGATION, key=f"nav:{org_id}")
    if nav == "Ad Content":
        view = st.radio("Ad Content", ("Ads & Assets", "Keywords", "Content Marketing", "Drafts"),
                        horizontal=True, key=f"ad-content:{org_id}")
        if view == "Content Marketing":
            show_content_marketing(base)
            return
    tiktok_configured = False
    if mode == "live" and nav in {"Campaigns", "Settings"}:
        try:
            tiktok_configured = call(f"{base}/tiktok-ads/status")["configured"]
        except APIError:
            tiktok_configured = None
    if nav == "Campaigns" and tiktok_configured:
        channel = st.radio("Ad channel", ("Google Ads", "TikTok Ads"), horizontal=True, key=f"channel:{org_id}")
        if channel == "TikTok Ads":
            show_tiktok_ads(base)
            return

    accounts = call(f"{base}/accounts")
    account = None
    if accounts:
        mapping = {account["id"]: account for account in accounts}
        account_id = st.selectbox("Ad account", list(mapping),
            format_func=lambda key: f"{mapping[key]['name']} · {mapping[key]['currency']}", key=f"account:{org_id}")
        account = mapping[account_id]
    flash = st.session_state.pop("flash", None)
    if flash:
        st.success(flash)
    if nav == "Settings":
        settings(mode, org, base, account, tiktok_configured)
        return
    if nav == "Reports":
        show_reports(org, base, account, mode)
        return
    if account is None:
        st.title(nav)
        st.info("Connect a Google Ads account in Settings to get started.")
        return

    today = datetime.now(ZoneInfo(account["timezone"])).date()
    end = today - timedelta(days=1)
    route = f"{base}/accounts/{account_id}"
    st.caption(f"{account['name']}  ·  {account['currency']}  ·  {account['timezone']}")
    if nav == "Weekly Review":
        show_weekly_review(account, route, end, health.get("review_ai") and org["role"] == "admin")
        return

    selected = st.radio("Date range", ("7 days", "30 days", "90 days", "Custom"), index=1,
                        horizontal=True, key=f"range:{account_id}")
    if selected == "Custom":
        dates = st.date_input("Start and end", (end - timedelta(days=29), end),
                              max_value=today, key=f"dates:{account_id}")
        if len(dates) != 2:
            st.info("Select a start and end date.")
            return
        start, end = dates
    else:
        start = end - timedelta(days=int(selected.split()[0]) - 1)
    report = call(f"{route}/report", params={"start": start.isoformat(), "end": end.isoformat()})
    render = AD_CONTENT_VIEWS[view] if nav == "Ad Content" else campaigns if nav == "Campaigns" else dashboard
    render(report, account, route, start, end)


try:
    app()
except APIError as exc:
    for message in str(exc).splitlines():
        st.error(message)
