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

Open [the local dashboard](http://127.0.0.1:8501). Start at Overview and open Paid Advertising → Google Ads for the synthetic account dashboard, Weekly Review, Campaigns, and Ad Content. Keywords and Drafts are under Ad Content. Facebook and Shopify Blog are under Organic Marketing. Connections & Sync is in the sidebar. Demo mode requires no credentials. Its revisions persist in the SQLite file named by `DEMO_DB`; use it as a local, shared sandbox.

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
4. Restart the backend. As an organization admin, open **Connections & Sync → Google Ads → Connect a Google Ads account**. Select **Discover accessible accounts**, choose an account, and select **Connect account**. Select the connected account and choose **Sync Google Ads data**. Check the latest result and record counts on the same page.

[Google Ads API access](https://developers.google.com/google-ads/api/docs/api-policy/developer-token) · [Google service accounts](https://developers.google.com/google-ads/api/docs/oauth/service-accounts) · [v26 release notes](https://developers.google.com/google-ads/api/docs/release-notes)

### Schedule imports

Use Windows Task Scheduler to run a daily task under an identity that can read the backend configuration and Google credentials. Set **Program/script** to the absolute path of `.venv\Scripts\python.exe`, **Add arguments** to `-m backend.jobs`, and **Start in** to the absolute `marketing_dashboard` directory. Run the task once on demand and confirm success under **Connections & Sync → Google Ads**. On other hosts, call the same module from cron or your job runner.

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

The evidence and ranked checks run without an AI key. To enable optional AI campaign analysis in live mode, set `GROQ_API_KEY` in the FastAPI host environment. The default model is `openai/gpt-oss-120b`. If `GROQ_REVIEW_MODEL` is already set, update it to use 120B. In Weekly Review, organization admins can choose an analysis focus and click **Generate AI analysis**. Each click sends aggregate metrics and anonymized campaign evidence to Groq, subject to the account's rate limits and billing plan. Campaign names, account identifiers, ad content, and search-query text stay in the dashboard. Run a Google Ads sync after deployment to import current bidding strategies for comparison.

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

Trigger the job once and check **Connections & Sync → Shopify** and **Connections & Sync → Facebook**. Compare the article's views, tagged landing sessions, and Google Search clicks to the source reports using the same dates. The first run looks back up to 90 days, bounded by publication date. Subsequent runs refresh 14 GA4 days and 7 finalized Search Console days, filling gaps after missed runs. Use `python -m backend.content_jobs --backfill` for a bounded replay after connecting a new Facebook link to an older article. Meta history starts with the first observation. Blank metrics indicate unavailable values, and lifetime observations are never summed.

The pilot measures purchases recorded in sessions that began on the article. Validate the live Facebook link, GA4 checkout continuity, and purchase event parameters before using revenue to judge the campaign. Page discovery and reporting read from the providers. Publishing stays in Shopify and Facebook.

[Shopify access tokens](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens) · [GA4 reporting API](https://developers.google.com/analytics/devguides/reporting/data/v1/quickstart) · [Search Console API](https://developers.google.com/webmaster-tools/v1/searchanalytics/query) · [Render cron jobs](https://render.com/docs/cronjobs)

## Connections & Sync

| Platform | Select an account | Refresh data | Check the result |
| --- | --- | --- | --- |
| Google Ads | Choose a connected ad account. Admins can discover and connect more accounts. | Select **Sync Google Ads data** as an admin, or use the scheduled Google Ads job. | See the latest run and counts for campaigns, ad groups, ads, keywords, search terms, and performance. Earlier runs are listed below. |
| Facebook | Select a Page with imported content. The selection shows that Page's published item count. | The Render content job uses the Page configured by CONTENT_META_PAGE_ID. Trigger that job in Render for an on-demand run. | See posts, post insights, and Page insights, including record counts and any permission warnings. |
| Shopify | Select a store with imported content. The selection shows that store's published item count. | The same Render content job uses the store configured by CONTENT_SHOPIFY_SHOP. | See article discovery, GA4 article performance and traffic, Search Console performance, and indexing results. |
| TikTok Ads | View the advertiser configured for the current organization. | In **Campaigns → TikTok Ads**, choose dates and select **Load TikTok report**. | See the loaded report in Campaigns and its date range and record count in Connections & Sync during the same session. |

Facebook and Shopify run status is recorded per source and organization. To change which Page or store the job imports, update its Render environment. Selecting a Page or store in the dashboard changes the content shown in the account summary.

**No data returned** means a successful run returned zero records for that data source. **Permission needed** means the provider denied access to some or all requested data. Check **Details** for the provider message and **Records** for what was saved.

For TikTok Ads, set TIKTOK_ADS_ORGANIZATION_ID, TIKTOK_ADS_ADVERTISER_ID, and TIKTOK_ADS_ACCESS_TOKEN on the FastAPI backend. Each backend deployment supports one configured TikTok advertiser for the specified organization. Store the access token as a secret with reporting access.

## Marketing Performance overview

Use the overview date range to compare available Google Ads delivery and Shopify article metrics with the previous equal-length period. Account spend stays in its original currency and totals are grouped by currency. Google Ads conversions follow the account's configured actions. GA4 article purchases and revenue describe sessions that began on the article, and tagged Facebook rows may overlap that group. The overview keeps those results separate from canonical Shopify orders and revenue, which are currently unavailable.

Use Organic Marketing → UTM Link Builder to generate a tagged link for an external placement. Select a known item or enter an identifier for new content. Draft links remain in the current session. Use the canonical URL for internal blog links, then publish the tagged link manually where the placement supports clicks. Existing Facebook-to-article associations come from discovered links.

Paid Advertising includes a Meta Ads placeholder and exposes TikTok reporting when configured. Facebook organic sync uses the existing Page token on the content cron. A paid Meta connection will require separate access to the ad account. The current Shopify connector imports articles. Canonical purchase reporting will require separately verified order access, an order eligibility policy, and a deduplicated order-level attribution store. GA4 and Search Console continue using the existing read credentials and content sync job. The content job now requests GA4 product view events, add-to-cart events, and checkout starts for article landing sessions and Facebook-tagged landing sessions. These are event counts. Use GA4 to verify the existing events and campaign dimensions before relying on purchase counts. Rerun the content job to populate the new fields for its refresh window. No additional storefront tag is required by these views.

- **Shopify orders** require an installed app with authorized read access to orders. Confirm the granted scopes before adding an importer. Shopify typically limits order access to the last 60 days unless the app also has approved access to older orders. See the [Shopify Order API](https://shopify.dev/docs/api/admin-graphql/latest/objects/Order).
- **Meta paid** needs a dedicated, server-side ad-account credential. Grant read access to the intended ad account and verify ads_read with a read-only Insights query before using any metrics. The existing Page credential serves organic content.
- **Facebook post outcomes** use existing GA4 imports for article links with a content-specific UTM that is unique to the post for that article. Shared or missing tags stay unavailable for post-level reporting. These results are GA4 session outcomes.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The default suite uses mocked external services and a temporary SQLite database. Set `TEST_LOCAL_UI=1` to start both demo servers on temporary loopback ports and exercise the six Streamlit pages. Set `TEST_DATABASE_URL` to a migrated disposable Supabase database owner URI for the PostgreSQL/RLS test. For the live connectivity test, configure Google credentials and set `TEST_LIVE_GOOGLE_ADS=1`. That test discovers accounts and reads campaign metadata.


## Commerce outcomes and report controls

Apply `supabase/migrations/20261111000100_commerce_metrics.sql` to the same Supabase database used by the API and content cron. All new tables use the `marketing_` prefix and tenant-scoped read policies. Deploy the backend, frontend, and cron code together.

On the existing Shopify app, verify installed `read_orders` access before enabling order imports. Article read access alone does not grant order access. The connector reuses `CONTENT_SHOPIFY_ACCESS_TOKEN` or the existing client credentials. It does not change permissions. Shopify may impose additional restrictions on customer journey fields. No customer names, addresses, or emails are imported.

On the Render content cron, set `COMMERCE_SYNC_ENABLED=true`. Reuse its existing Shopify and Google service account configuration, numeric `CONTENT_GA4_PROPERTY_ID`, and `CONTENT_GA4_TIMEZONE` matching the GA4 property. Run `python -m backend.content_jobs`. Look for `shopify_orders: succeeded` and `ga4_store: succeeded`. Failures are recorded separately in Connections & Sync and commerce coverage. Apply the migration before enabling the flag.

The order import refreshes at most the last 59 completed days on every run, within default Shopify order access. Orders are grouped by creation date in the store timezone. Eligible orders are non-test, non-canceled, currently PAID or PARTIALLY_REFUNDED. Fully refunded orders are excluded. Revenue uses Shopify current total shop money, including tax and shipping adjustments, and stays in the original currency. This is not a lifetime sales or payment-date accounting report. Older orders are retained but dates outside the latest verified window remain unavailable.

Order attribution uses Shopify's last recorded visit only when its journey is ready and explicit UTM source and medium are present. It does not claim last non-direct attribution. Missing evidence remains unattributed. Google Ads conversion totals, GA4 purchases, and Shopify orders are separate measures and must not be added together. GA4 ecommerce tracking must be verified independently even when sessions import successfully.

Overview and exports show zeros only for successfully imported, fully covered dates. Missing coverage remains Not available. Imports over 48 hours old are flagged. GA4 storewide rows include sessions, product views, add-to-carts, checkouts, purchases, and purchase revenue by session source, medium, and campaign. Thresholded or incomplete reports preserve the previous import.



## Report builder selection flow

Reports & AI Insights uses one scope for selection, preview, and export. The report UI requires no new database migration or credentials.

1. Choose Custom period with inclusive dates, or Weekly report with one week-ending date. A week ending November 10 covers November 4-10. Optional comparison covers the preceding equal-length period, October 28-November 3. There is no independent Weekly Review date.
2. Select source checkboxes: Google Ads, Facebook Organic, Facebook Paid Ads, Shopify Blog, Shopify Store Results, and TikTok Ads. All remain visible with status. Facebook paid and TikTok start unchecked. Selected unavailable sources generate warnings rather than zeros.
3. Select campaign and content checkboxes grouped by source and Google Ads account. Every discovered item starts selected. Each group has Select all / Clear all. Turning off a source excludes its items while retaining their checkbox state. Reports cover all connected Google accounts without the old sidebar account selector. TikTok lists campaigns only when its configured connection returns data for the dates.
4. Only three optional details remain: saved ad drafts, AI interpretations and recommendations, and sync status/data availability details. Available performance, settings, search, and creative/SEO metadata are automatic. Saved drafts are Google Ads revisions where supported. Exporting uses existing matching weekly AI results, does not request new AI analysis, and flags unsupported scopes. Account-wide AI results are excluded for campaign subsets.
5. Review dates, sources, selected item counts, optional details, and warnings, then Generate report. Download Markdown or a ZIP with full CSV tables. Changing selections invalidates the old download.

Google campaign descendants, performance, and drafts are filtered by selection. Only selected organic items are retrieved. Facebook Page-wide totals are omitted for post subsets. Storewide Shopify/GA4 outcomes remain a separate storewide selection. Previous-period sections are labeled explicitly. Detailed sync history can be excluded, but coverage and missing-value warnings remain.
