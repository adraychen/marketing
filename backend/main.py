import os
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from psycopg2.errors import UndefinedTable, UniqueViolation
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend import commerce, content_repository, demo, facebook_analytics, repositories as repo
from backend.auth import Login, Refresh, current_user, session
from backend.config import MODE
from backend.connectors import tiktok
from backend.connectors.google_ads import GoogleAdsError
from backend.jobs import SyncBusy, configured_org, connector, run_sync
from backend.metrics import previous_period
from backend.models import DraftInput
from backend.review import MIN_CLICKS, weekly_review
from backend.review_ai import FOCUS_PROMPTS, ReviewAIError, enabled as review_ai_enabled, explain
from backend.validation import validate

app = FastAPI(title="Okoo Marketing Dashboard", version="1.0", debug=False)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request, _exc):
    return JSONResponse({"detail": "Check the submitted fields and try again."}, status_code=422)


@app.get("/health")
def health():
    return {"mode": MODE, "review_ai": MODE == "live" and review_ai_enabled()}


@app.post("/auth/login")
def login(credentials: Login, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return session("password", {"email": credentials.email, "password": credentials.password.get_secret_value()})


@app.post("/auth/refresh")
def refresh(credentials: Refresh, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return session("refresh_token", {"refresh_token": credentials.refresh_token.get_secret_value()})


def orgs(user):
    return [{"id": demo.ORG, "name": "Okoo Demo", "role": "admin"}] if MODE == "demo" else repo.memberships(user)


@app.get("/orgs")
def organizations(user=Depends(current_user)):
    return orgs(user)


def member(org: str, user=Depends(current_user)):
    row = next((row for row in orgs(user) if str(row["id"]) == org), None)
    if row is None:
        raise HTTPException(404, "Organization unavailable.")
    return row


@app.get("/orgs/{org}/commerce")
def commerce_report(org: str, start: date, end: date, _member=Depends(member)):
    if start > end or end - start > timedelta(days=365):
        raise HTTPException(422, "Choose a valid date range of up to 366 days.")
    if MODE == "demo":
        return {"orders": [], "ga4": [], "coverage": [], "policy": commerce.POLICY,
                "attribution_model": commerce.ATTRIBUTION}
    try:
        result = commerce.summary(org, start, end)
    except UndefinedTable:
        raise HTTPException(503, "Apply the commerce metrics migration and run commerce sync.") from None
    return result


@app.get("/orgs/{org}/content")
def content_catalog(org: str, _member=Depends(member)):
    if MODE == "demo":
        return {"campaigns": [], "items": [], "sources": []}
    return content_repository.catalog(org)


@app.get("/orgs/{org}/content/organic-summary")
def organic_summary(org: str, start: date, end: date, _member=Depends(member)):
    if start > end or end - start > timedelta(days=365):
        raise HTTPException(422, "Choose a valid date range of up to 366 days.")
    if MODE == "demo":
        return {"rows": []}
    try:
        rows = content_repository.organic_summary(org, start, end)
    except UndefinedTable:
        raise HTTPException(503, "Organic performance data has not been set up.") from None
    return {"rows": rows}


@app.get("/orgs/{org}/content/facebook-analytics")
def facebook_analytics_report(org: str, start: date, end: date, membership=Depends(member)):
    if start > end or end - start > timedelta(days=365):
        raise HTTPException(422, "Choose a valid date range of up to 366 days.")
    if MODE == "demo":
        return {"page": [], "availability": [], "pages": [], "manual": [], "can_edit": False}
    try:
        data = facebook_analytics.analytics(org, start, end)
    except UndefinedTable:
        raise HTTPException(503, "Apply the Facebook analytics database migration to enable this section.") from None
    return {**data, "can_edit": membership["role"] == "admin"}


def facebook_editor(membership=Depends(member)):
    if MODE != "live" or membership["role"] != "admin":
        raise HTTPException(403, "An organization admin can manage manual Facebook observations.")
    return membership


def save_facebook_observation(org, observation, observation_id=None):
    try:
        saved = facebook_analytics.save_manual(org, observation, str(observation_id) if observation_id else None)
    except LookupError:
        raise HTTPException(404, "Facebook Page or content is unavailable in this organization.") from None
    except UniqueViolation:
        raise HTTPException(409, "An observation already exists for this metric and period.") from None
    if saved is None:
        if observation_id:
            raise HTTPException(404, "Manual observation unavailable.")
        raise HTTPException(409, "An observation already exists for this metric and period.")
    return saved


@app.post("/orgs/{org}/content/facebook-analytics/manual")
def create_facebook_observation(org: str, observation: facebook_analytics.ManualObservation,
                                _editor=Depends(facebook_editor)):
    return save_facebook_observation(org, observation)


@app.put("/orgs/{org}/content/facebook-analytics/manual/{observation_id}")
def edit_facebook_observation(org: str, observation_id: UUID, observation: facebook_analytics.ManualObservation,
                              _editor=Depends(facebook_editor)):
    return save_facebook_observation(org, observation, observation_id)


@app.delete("/orgs/{org}/content/facebook-analytics/manual/{observation_id}")
def delete_facebook_observation(org: str, observation_id: UUID, _editor=Depends(facebook_editor)):
    if not facebook_analytics.delete_manual(org, str(observation_id)):
        raise HTTPException(404, "Manual observation unavailable.")
    return {"deleted": True}


@app.get("/orgs/{org}/tiktok-ads/status")
def tiktok_status(org: str, _member=Depends(member)):
    configured = MODE == "live" and tiktok.configured(org)
    return {"configured": configured, "advertiser_id": tiktok.advertiser_for(org) if configured else None}


@app.get("/orgs/{org}/tiktok-ads/report")
def tiktok_report(org: str, start: date, end: date, _member=Depends(member)):
    if not tiktok_status(org, _member)["configured"]:
        raise HTTPException(404, "TikTok Ads is not connected for this organization.")
    if start > end or end - start > timedelta(days=365):
        raise HTTPException(422, "Choose a valid TikTok date range of up to 366 days.")
    try:
        with httpx.Client(timeout=15) as client:
            rows = tiktok.campaign_report(
                client, tiktok.advertiser_for(org), os.environ["TIKTOK_ADS_ACCESS_TOKEN"].strip(), start, end,
            )
    except tiktok.TikTokReportError as exc:
        raise HTTPException(502, str(exc)) from None
    except (httpx.HTTPError, KeyError, TypeError, ValueError, OverflowError):
        raise HTTPException(502, "TikTok reporting is temporarily unavailable.") from None
    return {"rows": rows}


@app.get("/orgs/{org}/content/items/{item_id}")
def content_details(org: str, item_id: UUID, start: date, end: date, _member=Depends(member)):
    if start > end or end - start > timedelta(days=365):
        raise HTTPException(422, "Choose a valid content date range of up to 366 days.")
    item = None if MODE == "demo" else content_repository.details(org, str(item_id), start, end)
    if item is None:
        raise HTTPException(404, "Content item unavailable.")
    return item


@app.get("/orgs/{org}/content/campaigns/{slug}/links")
def content_campaign_links(org: str, slug: str, _member=Depends(member)):
    if MODE == "demo":
        return {"products": [], "ads": []}
    return content_repository.linked_entities(org, slug)


def integration(org: str, membership=Depends(member)):
    if MODE != "live" or org != configured_org():
        raise HTTPException(409, "Google Ads integration is unavailable for this organization.")
    if membership["role"] != "admin":
        raise HTTPException(403, "An organization admin must manage Google Ads connections.")
    return membership


def account_list(org):
    return demo.accounts() if MODE == "demo" else repo.accounts(org)


@app.get("/orgs/{org}/accounts")
def accounts(org: str, _membership=Depends(member)):
    return account_list(org)


def selected_account(org: str, account_id: str, _membership=Depends(member)):
    row = next((row for row in account_list(org) if str(row["id"]) == account_id), None)
    if row is None:
        raise HTTPException(404, "Advertising account unavailable.")
    return row


@app.get("/orgs/{org}/discovery")
def discover(org: str, _admin=Depends(integration)):
    try:
        with httpx.Client(timeout=30) as client:
            return connector(client).discover()
    except GoogleAdsError as exc:
        raise HTTPException(502, str(exc)) from None


@app.post("/orgs/{org}/accounts")
def connect(org: str, resource: str, admin=Depends(integration)):
    account = next((account for account in discover(org, admin) if account.resource == resource), None)
    if account is None:
        raise HTTPException(404, "Google Ads account unavailable to this connection.")
    return {"id": repo.connect_account(org, account)}


@app.get("/orgs/{org}/accounts/{account_id}/report")
def report(org: str, account_id: str, start: date, end: date, account=Depends(selected_account)):
    if start > end or end - start > timedelta(days=365) or start < date(2000, 1, 1):
        raise HTTPException(422, "Choose a valid report range of up to 366 days.")
    previous_start, _ = previous_period(start, end)
    if MODE == "demo":
        return demo.snapshot(account_id, previous_start, end)
    return repo.snapshot(org, account_id, previous_start, end, account["currency"])


@app.post("/orgs/{org}/accounts/{account_id}/weekly-review/ai")
def weekly_review_ai(org: str, account_id: str, end: date,
                     account=Depends(selected_account), membership=Depends(member), focus: str = "performance"):
    if MODE != "live" or not review_ai_enabled():
        raise HTTPException(409, "AI summaries are unavailable on this backend.")
    if membership["role"] != "admin":
        raise HTTPException(403, "An organization admin must request the AI summary.")
    if focus not in FOCUS_PROMPTS:
        raise HTTPException(422, "Choose a supported AI review focus.")
    today = datetime.now(ZoneInfo(account["timezone"])).date()
    if not date(2000, 1, 14) <= end < today:
        raise HTTPException(422, "Choose a completed week for this account.")
    start = end - timedelta(days=6)
    snapshot = repo.snapshot(org, account_id, end - timedelta(days=13), end, account["currency"])
    review = weekly_review(snapshot, account, start, end)
    if not review["has_activity"]:
        raise HTTPException(409, "The review needs two imported weeks and campaign activity.")
    if not review["ai_ready"]:
        raise HTTPException(409, f"The AI summary needs at least {MIN_CLICKS} clicks in each week.")
    try:
        return explain(review, account["currency"], focus)
    except ReviewAIError as exc:
        raise HTTPException(502, str(exc)) from None


@app.get("/orgs/{org}/accounts/{account_id}/drafts")
def drafts(org: str, account_id: str, _account=Depends(selected_account)):
    return demo.drafts(account_id) if MODE == "demo" else repo.drafts(org, account_id)


@app.put("/orgs/{org}/accounts/{account_id}/drafts")
def save_draft(org: str, account_id: str, resource: str, draft: DraftInput,
               user=Depends(current_user), _account=Depends(selected_account)):
    issues = validate(draft.creative)
    if issues:
        raise HTTPException(422, issues)
    creative = draft.creative.model_dump()
    try:
        if MODE == "demo":
            version = demo.save_draft(account_id, resource, creative, draft.version)
        else:
            version = repo.save_draft(org, account_id, resource, user, creative, draft.version)
    except LookupError:
        raise HTTPException(404, "Responsive search ad unavailable.") from None
    except ValueError:
        raise HTTPException(409, "The draft changed. Reload the editor and try again.") from None
    return {"version": version}


@app.delete("/orgs/{org}/accounts/{account_id}/drafts", status_code=204)
def discard_draft(org: str, account_id: str, resource: str, version: int = Query(ge=1),
                  _account=Depends(selected_account)):
    if MODE == "demo":
        deleted = demo.discard_draft(account_id, resource, version)
    else:
        deleted = repo.discard_draft(org, account_id, resource, version)
    if not deleted:
        raise HTTPException(409, "The draft changed. Reload the editor and try again.")


@app.get("/orgs/{org}/accounts/{account_id}/sync")
def history(org: str, account_id: str, _account=Depends(selected_account)):
    return [] if MODE == "demo" else repo.sync_history(org, account_id)


@app.post("/orgs/{org}/accounts/{account_id}/sync")
def synchronize(org: str, _admin=Depends(integration), account=Depends(selected_account)):
    try:
        return run_sync(org, account)
    except SyncBusy:
        raise HTTPException(409, "A sync is already running for this account.") from None
    except GoogleAdsError as exc:
        raise HTTPException(502, str(exc)) from None
