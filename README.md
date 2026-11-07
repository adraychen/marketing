# Okoo Marketing Dashboard · Stage 1

A local Streamlit workspace for Google Ads reporting and unpublished responsive search ad revisions. FastAPI handles authentication, imports, authorization, and draft storage. The six views share the same models in demo and live mode.

## Run the demo

From the `marketing_dashboard` directory, install Python 3.11 or later and create a virtual environment. These commands are for Windows PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Run these in two terminals from the same directory.

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

```powershell
.\.venv\Scripts\python.exe -m streamlit run frontend/app.py --server.address 127.0.0.1
```

Open [the local dashboard](http://127.0.0.1:8501). Select either synthetic ad account and explore Dashboard, Campaigns, Ad Content, Keywords, Drafts, and Settings. Demo mode requires no credentials. Its revisions persist in the SQLite file named by `DEMO_DB`; use it as a local, shared sandbox.

## Configure Supabase

1. Create a Supabase project. Apply [the versioned migration](supabase/migrations/20261105000100_stage_one.sql) as the database owner through the SQL Editor or your Supabase CLI workflow.
   If the original unprefixed Stage 1 schema is already installed, run [the upgrade SQL](supabase/upgrade_existing_stage_one.sql) instead. It renames the existing tables in place and preserves data and memberships. Deploy the updated backend after running the upgrade.
2. Create an email/password user under **Authentication → Users**. Copy their User UID. Run the following in the SQL Editor, replacing the two placeholders with the organization's name and the user's UUID. Copy the organization ID returned by the query.

   ```sql
   with created as (
       insert into public.marketing_organizations (name)
       values ('Your organization name') returning id
   )
   insert into public.marketing_memberships (organization_id, user_id, role)
   select id, 'YOUR-SUPABASE-USER-UUID'::uuid, 'admin' from created
   returning organization_id;
   ```

   To add another user, insert a membership with their UUID and the same organization ID. Use the `member` role for reporting and draft access and `admin` for Google Ads connection and manual synchronization.
3. In the backend environment, set `DASHBOARD_MODE=live`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and `DATABASE_URL`. Get the database URI from **Connect** using the direct connection or session pooler. Use TLS with `sslmode=require` at minimum. The backend uses the database owner credentials for imports and checks Supabase Auth identity and organization membership before accessing application data. The browser never receives those database credentials. Authenticated Data API readers are scoped by RLS, and integration credential references are backend-only.
4. Restart both processes and sign in. When running separate hosts, set `BACKEND_URL` on the Streamlit host and use TLS on both exposed services. Give the frontend process only its frontend settings in deployed environments.

[Supabase Auth](https://supabase.com/docs/guides/auth) · [Database connections](https://supabase.com/docs/guides/database/connecting-to-postgres)

## Connect Google Ads

1. Enable the Google Ads API in the Google Cloud project that owns the backend's Google service account. Request production-account access on the Google Ads API overview in Cloud Console if the project only has Test access. Google Ads API v26 uses the project's access level.
2. Create a Google service account and grant its email **Read-only** access under **Admin → Access and security** in the advertising account or its manager. For a local deployment, store the service-account JSON outside the repository and set `GOOGLE_APPLICATION_CREDENTIALS` to its absolute path. Restrict file access to the backend and scheduled worker. Google Auth uses Application Default Credentials.
3. Set `GOOGLE_ADS_ORGANIZATION_ID` to the UUID from Supabase. This deployment has one backend Google identity and maps it to one organization. If needed, set `GOOGLE_ADS_MANAGER_ID` to the directly accessible manager's 10 digits to limit which manager hierarchy is traversed. Directly accessible advertiser accounts are also discovered.
4. Restart the backend. As an organization admin, open **Settings → Discover accessible accounts**, select a Google Ads account, then select **Connect account**. Choose it in the sidebar and select **Sync now**. Inspect synchronization history for record counts and errors.

[Google Ads API access](https://developers.google.com/google-ads/api/docs/api-policy/developer-token) · [Google service accounts](https://developers.google.com/google-ads/api/docs/oauth/service-accounts) · [v26 release notes](https://developers.google.com/google-ads/api/docs/release-notes)

### Schedule imports

Use Windows Task Scheduler to run a daily task under an identity that can read the backend configuration and Google credentials. Set **Program/script** to the absolute path of `.venv\Scripts\python.exe`, **Add arguments** to `-m backend.jobs`, and **Start in** to the absolute `marketing_dashboard` directory. Run the task once on demand and confirm success under **Settings → Synchronization history**. On other hosts, call the same module from cron or your job runner.

A first import defaults to 180 days and subsequent runs refresh 30 recent days, filling any gap after an outage. Both windows are configurable. Dates use the advertising account's timezone. A transaction applies the import and replaces stale daily rows for the refreshed range after all Google reads succeed. Account-level advisory locks serialize manual and scheduled runs. Incomplete runs retain the prior successful snapshot.

## Architecture and boundaries

- `frontend/` contains the six Streamlit views, interactive Plotly charts, and an HTTP client.
- `backend/` contains FastAPI routes, Supabase Auth verification, metrics, and draft validation.
- `backend/connectors/` contains the connector interface, Meta and TikTok placeholders, and the Google Ads v26 REST adapter using GAQL and Google Auth. The adapter allows account discovery and reporting calls.
- `backend/repositories.py` and `supabase/migrations/` handle persistence, tenant isolation, and stable account-scoped upserts.
- Reporting is account-by-account in the account currency. Spend uses Google cost micros. Conversion rate uses ad interactions. Value and ROAS use Google-attributed conversion value, with missing values left blank. Date presets use completed account days, and comparisons require coverage for both periods.
- Responsive search ad editing supports one active revision per imported ad. The editor preserves asset order and checks counts, lengths, duplicates, destination URLs, and pin positions locally. Google editorial policies, customizer substitutions, and destination checks are outside local validation.
- Expanded text ads are displayed read-only. Other ad types display available identifiers and final URLs. Performance Max asset groups and search terms are outside this implementation, and Google may omit low-volume search queries. Reported conversion value is Google Ads attribution and is not reconciled to Shopify orders.

## Weekly performance review

Open **Weekly Review** after selecting an ad account. It defaults to the latest seven completed account days and compares them with the preceding seven days. Campaign, keyword, and search-term breakdowns rank changes in spend, conversions, or conversion value. The review shows current imported settings as context. It holds recommendations until both periods are covered, uses minimum activity thresholds, and flags delayed conversions. Keyword and search-term tables include ad group names when available. Search-term coverage is limited by Google's reporting.

The evidence and ranked checks run without an AI key. To enable the optional AI summary in live mode, create a key in the Groq Console and set `GROQ_API_KEY` in the FastAPI host environment. The default model is `openai/gpt-oss-20b`. Set `GROQ_REVIEW_MODEL` to override it with another model that supports chat completions. Organization admins can click **Generate AI summary**. Each click sends aggregate metrics and anonymized signals to Groq, subject to the account's rate limits and billing plan. Campaign names, account identifiers, ad content, and search-query text stay in the dashboard.

The AI summary requires at least 20 campaign clicks in each of the two weeks. Low-traffic reviews still show the reported metrics and breakdowns. The threshold is a minimum for AI interpretation, rather than a statistical significance test.

[Groq API keys](https://console.groq.com/keys) · [Groq rate limits](https://console.groq.com/docs/rate-limits)

## Content Marketing pilot

Apply [the additive migration](supabase/migrations/20261106000100_content_marketing.sql) to the Supabase project that already has the prefixed Stage 1 schema. Apply it before deploying the new backend. The migration creates organization-scoped `marketing_content_*` tables and does not alter the Google Ads tables.

Create a separate Render Cron Job from this repository. Use `pip install -r requirements.txt` to build and `python -m backend.content_jobs` to run. A daily schedule such as `0 20 * * *` runs at 20:00 UTC, after typical prior-day GA4 processing for Toronto. Keep the existing Google Ads cron job on its current command.

| Job setting | Value |
| --- | --- |
| `DASHBOARD_MODE`, `DATABASE_URL` | `live` and the existing Supabase database URI |
| `CONTENT_ORGANIZATION_ID` | The organization UUID already used for Okoo Marketing |
| `CONTENT_SHOPIFY_SHOP` | The store's canonical `.myshopify.com` hostname |
| `CONTENT_STOREFRONT_URL`, `CONTENT_SHOPIFY_BLOG` | Defaults to `https://okoostore.com` and `journal`. Use the verified storefront origin. |
| Shopify authorization | An existing offline Admin token in `CONTENT_SHOPIFY_ACCESS_TOKEN`, or `CONTENT_SHOPIFY_CLIENT_ID` and `CONTENT_SHOPIFY_CLIENT_SECRET` for an eligible installed app owned by the same Shopify organization. Requires content read access. The job acquires a short-lived token for the latter configuration. |
| `CONTENT_GA4_PROPERTY_ID`, `CONTENT_GA4_TIMEZONE` | Numeric GA4 property ID and its reporting timezone, defaulting to `America/Toronto` |
| `CONTENT_GSC_SITE` | Exact Search Console property identifier, such as `sc-domain:okoostore.com` if that property is verified |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to an existing Google credential file on the cron service with read access to the configured GA4 and Search Console properties. Google Ads access alone is insufficient. |
| `CONTENT_META_PAGE_ID`, `CONTENT_META_PAGE_ACCESS_TOKEN` | Numeric Facebook Page ID and existing token with Page content and Insights read access |
| `CONTENT_META_API_VERSION` | Defaults to `v26.0`. Confirm that version and the metric set against the Okoo Page before relying on the numbers. |

Configure only the sources you can authorize. Keep secrets in Render. A successful Shopify discovery seeds **Citizen Eco-Drive vs Automatic** using the published Journal article. A Facebook post joins the pilot when its attachment resolves to the article path and carries the exact source, medium, and campaign tags. Unassigned content appears under **All discovered content**. Product and Google Ads associations are explicit. Populate them only after confirming the Shopify product GID or Google Ads account and campaign resource. They are displayed as related entities rather than credited conversions.

Trigger the job once and check **Content Marketing → Source status**. Compare the article's views, tagged landing sessions, and Google Search clicks to the source reports using the same dates. The first run looks back up to 90 days, bounded by publication date. Subsequent runs refresh 14 GA4 days and 7 finalized Search Console days, filling gaps after missed runs. Use `python -m backend.content_jobs --backfill` for a bounded replay after connecting a new Facebook link to an older article. Meta history starts with the first observation. Blank metrics indicate unavailable values, and lifetime observations are never summed.

The pilot measures purchases recorded in sessions that began on the article. Validate the live Facebook link, GA4 checkout continuity, and purchase event parameters before using revenue to judge the campaign. Page discovery and reporting read from the providers. Publishing stays in Shopify and Facebook.

[Shopify access tokens](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens) · [GA4 reporting API](https://developers.google.com/analytics/devguides/reporting/data/v1/quickstart) · [Search Console API](https://developers.google.com/webmaster-tools/v1/searchanalytics/query) · [Render cron jobs](https://render.com/docs/cronjobs)

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The default suite uses mocked external services and a temporary SQLite database. Set `TEST_LOCAL_UI=1` to start both demo servers on temporary loopback ports and exercise the six Streamlit pages. Set `TEST_DATABASE_URL` to a migrated disposable Supabase database owner URI for the PostgreSQL/RLS test. For the live connectivity test, configure Google credentials and set `TEST_LIVE_GOOGLE_ADS=1`. That test discovers accounts and reads campaign metadata.
