"""Future content reporting contract; no API authentication or inferred posts."""
import streamlit as st
from frontend.views import table

FIELDS = ("Posts and Reels", "Reach", "Views", "Engagement", "Profile activity",
          "Outbound website traffic", "Attributed purchases", "Attributed revenue")


def show():
    st.title("Instagram Organic")
    st.info("Owned Instagram content tracking is not enabled. Referral visits can still appear in GA4 as Instagram Referral — Unattributed.")
    table([{"metric": metric, "value": None, "state": "untracked",
            "required_evidence": "Authorized account/content ID and supported metric" if metric in FIELDS[:5]
            else "Unique content UTMs matched to website attribution"} for metric in FIELDS])
    st.caption("Future post/Reel records must retain account ID, content ID, publication date, observation time, metric period and API provenance. "
               "Enable account authorization only when owned content reporting is needed. No Instagram login or tracking changes have been made.")
