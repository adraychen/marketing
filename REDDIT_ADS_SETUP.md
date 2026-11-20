# Reddit Ads integration

This read-only integration uses the stable [Reddit Ads API v3](https://ads-api.reddit.com/docs/v3/api/reddit-advertising-api), not v2 (deprecated) or v4 (preview). It covers every account accessible through the authorized user's businesses, discovers campaigns/ad groups/ads on every sync, and does not filter out paused or archived entities. It never changes ads, budgets, targeting, pixels, or storefront tracking. No new AI analysis is generated.

## Deployment and authorization

1. Apply `supabase/migrations/20261119000100_reddit_ads.sql` after the existing migrations. Deploy backend, frontend and content cron together, installing the updated `requirements.txt`.
2. Use your existing Reddit developer application, or [create one](https://ads-api.reddit.com/docs/v3/guides/quick-start/create-dev-app) in the advertising business. Register `http://localhost:8765/reddit/callback` as its redirect URI (or set `REDDIT_ADS_REDIRECT_URI` to the exact registered alternative).
3. Set these **server secrets** on the API and the existing content cron. Never put them in frontend settings, Git, reports, screenshots or chat:

   - `REDDIT_ADS_ORGANIZATION_ID`: the dashboard organization UUID, matching the content cron organization.
   - `REDDIT_ADS_CLIENT_ID` and `REDDIT_ADS_CLIENT_SECRET`: developer application credentials.
   - `REDDIT_ADS_TOKEN_KEY`: a Fernet encryption key. Generate once with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` in a trusted terminal and save it securely. Use the same key for authorization, API and cron. Losing/changing it without reauthorization makes saved tokens unreadable.
   - `REDDIT_ADS_USER_AGENT`: an honest identifier, e.g. `web:okoo-marketing:v1 (by /u/YOUR_REDDIT_USERNAME)`.
   - Reuse the existing backend `DATABASE_URL`; it must be able to access the private schema.

4. In a trusted terminal with the deployed backend dependencies and these secrets, run `python -m backend.reddit_oauth`. Open the displayed authorization URL as a Reddit user with access to the advertising business. It requests **only `adsread`**, with permanent authorization. After approval, copy the redirect URL into the terminal's hidden prompt. A localhost connection-error page is expected if no listener is running; the address bar still contains the redirect. Do not share that URL. State and redirect URI are validated. The code is exchanged immediately; tokens are encrypted before database storage and never printed. This setup command needs an interactive terminal (e.g. a trusted local backend shell), not a noninteractive cron.
5. Open **Connections & Sync → Reddit Ads**. Verify configured/authorized status, then run the initial sync, preferably through the content cron for long histories. The frontend does not collect or display credentials.
6. Check **Paid Advertising → Reddit Ads**, select the account, reporting dates and campaigns, then compare with Ads Manager before relying on live totals.

OAuth refresh is automatic before expiry and retried once after a 401. A database row lock serializes refreshes; rotated refresh tokens are saved encrypted. Changing credentials or revoking access requires authorization again. Follow [Reddit's authentication guide](https://ads-api.reddit.com/docs/v3/guides/quick-start/authenticate) for the provider's requirements. No advertising-write scope is accepted.

## Storage and isolation

- Reuses `marketing_platform_connections` (platform `Reddit Ads`), `marketing_ad_accounts`, `marketing_campaigns`, `marketing_ad_groups`, and `marketing_ads`. Original platform IDs are retained within account/organization scope. The Google account listing explicitly excludes Reddit accounts.
- Adds `marketing_paid_observations`: nullable JSON metrics, raw provider values, event availability, attribution, currency, UTC day, campaign/ad-group/ad IDs, breakdown and segment. Existing Google daily tables require non-null fields and cannot represent missing Reddit events or alternative breakdowns without changing their semantics.
- Uses the existing `marketing_content_sync_runs` with source `reddit_ads` for successes and sanitized failures.
- Adds `marketing_private.reddit_tokens` for encrypted OAuth material, inaccessible to anonymous/authenticated Supabase roles. Public observation rows have tenant read policies; writes occur only through the backend connection.
- A successful sync atomically replaces its fetched date window. Repeated syncs do not append duplicates. Older dates and previously discovered inactive metadata remain stored. Failed account imports roll back the batch. Coverage records actual fetched dates, not a fabricated continuous range.

## Synchronization

Use the existing Render `okoo-marketing-sync` job with `python -m backend.content_jobs`. Set its schedule to **`0 */6 * * *` (UTC)** if it is not already every six hours. This code does not change the hosted Render schedule automatically. Reddit runs only when `REDDIT_ADS_ORGANIZATION_ID` matches that job's organization.

Initial sync requests approximately the supported 24-month history in 28-day chunks. Regular sync re-fetches **35 days**, including the current UTC day, and catches up from an older last successful date if the job was stopped. `REDDIT_ADS_LOOKBACK_DAYS` accepts 31–730; temporarily increase it to re-fetch older corrections. Historical records outside each refresh window remain stored. Current-day data is provisional; Reddit says delivery can take six hours to stabilize and older conversions can be revised daily. See [reporting documentation](https://ads-api.reddit.com/docs/v3/api/get-a-report).

Reporting calls are paced at roughly one per second. Pagination follows validated Reddit v3 `next_url` values; repeated pages/duplicate ad-date rows are rejected. Transient network, 429 and server errors receive bounded retries, respecting numeric Retry-After / RateLimit reset values up to 60 seconds. Longer delays fail safely for a later cron retry. No provider response body or credential is written into application error messages. An organization advisory lock prevents overlapping syncs.

## Metrics and definitions

| Category | Integrated fields / calculation |
| --- | --- |
| Delivery | Spend, impressions, clicks; CTR = clicks / impressions; CPC = spend / clicks; CPM = spend × 1,000 / impressions |
| Unique audience | Provider daily-ad reach and frequency. No sum across ads/dates; aggregate unique reach/frequency remains unavailable |
| Events | Page visit, view content, add to cart, signup, lead and purchase, each with separate click-through and view-through counts |
| Overview conversions | Purchases selected by the account's recorded attribution type; no total across unrelated event types |
| Value | Reddit purchase conversion value; ROAS = purchase value / spend |
| CPA / conversion rate | User-selected explicit event/attribution; spend / count and count / clicks. View-through conversions divided by clicks are not a click-only conversion rate |
| Video | Starts, viewable impressions, 3/10-second watches, 25/50/75/100% milestones; completion rate from completed plays / starts |
| Metadata | Account currency/timezone; campaign objective/status/budget; ad-group bidding, targeting and schedule; available ad creative/post, destination and preview references |

Reddit [report monetary units](https://ads-api.reddit.com/docs/v3/api/get-a-report) are normalized: spend is divided by **1,000,000**, purchase conversion value by **100**. Currency comes from the account and is checked against returned rows; CAD is never assumed. Budgets/bids include normalized amounts alongside original microcurrency values. Do not combine different account currencies.

Counts and ratios use base daily-ad rows only. CTR/CPA/CPC/CPM/ROAS are calculated from totals, not averaged row rates. Missing data and zero denominators remain unavailable. A reported conversion zero is accepted only when the associated ad group's pixel has evidence that the event fired; otherwise it is unavailable. Positive reported events remain usable even if pixel diagnostics are inaccessible. Last-fired evidence does not prove tracking was healthy throughout the historical period. Raw values and availability reasons remain in exports.

Account attribution type and click/view windows are stored with every observation. Mixed attribution definitions or currencies block combined totals; a full backfill under the new definitions is needed for a comparable period. Reddit conversions are not Shopify canonical orders or GA4 conversions.

## Optional breakdowns and limitations

`REDDIT_ADS_BREAKDOWNS` defaults to `PLACEMENT,OS_TYPE,COUNTRY`. It also accepts `COMMUNITY,INTEREST,KEYWORD,ASSET_ID`. Each is requested independently with DATE + AD_ID (three dimensions), avoiding incompatible combinations. Dynamic-template/asset-bearing ads automatically request ASSET_ID. Rejected optional breakdowns are recorded as unavailable and do not invalidate base delivery metrics; incomplete optional results are discarded. Alternative breakdowns are never added to base totals.

Reddit Max top asset combinations are not supported by the reporting API. Asset-level rows depend on actual campaign support. Creative metadata can be unavailable for some post types or permissions. Last-fired pixel diagnostics can be inaccessible; no new pixel/CAPI event is installed by this integration. Reach/frequency for an arbitrary multi-day or multi-ad selection are unavailable because daily unique counts cannot be added. UTC is the explicit reporting timezone even if the account uses another timezone; use UTC in Ads Manager for reconciliation.

## Dashboard and exports

Reddit appears in the existing paid overview/platform comparison (including purchases in Conversions), Paid Advertising selector, Connections & Sync, and Reports & AI Insights. The paid page supports account/date/campaign/status filters, campaign and ad-group/ad performance, all daily charts, conversion/video metrics and metadata. It uses existing Streamlit/Plotly components and the Markdown/CSV ZIP report writer. Exports retain raw daily evidence, attribution, coverage and alternative breakdowns and respect selected campaigns and accounts. No overall conversion total is added across platforms.

## Validation before production use

Automated fixtures cover read-only request boundaries, discovery/pagination, daily IDs, missing/zero events, currency units, ratios, duplicate rejection, repeated window replacement, preservation of older history, OAuth encryption/rotation, retry/redaction, account/campaign export scope and attribution conflicts. No Reddit credentials or live database are available in the implementation workspace; **live retrieval, persistence and Ads Manager reconciliation are not yet verified**.

After authorizing, run a sync twice. Confirm unchanged row counts and updated values, existing/future campaigns and archived metadata. In Reddit Ads Manager select the same account, campaigns, UTC dates and attribution windows; compare spend, impressions, clicks, CTR, CPC and purchases with an exported report. Compare completed days first, allow reporting delay, and document any differences without forcing totals to match. Run the migration against a staging database before production. Check failed/optional metric diagnostics in Connections & Sync and the report coverage section.

### Local validation result

The targeted Reddit and existing reporting regression suite completed with **99 passed, 1 skipped**. The skipped test requires external credentials. Python compilation and `git diff --check` also passed. Reddit page controls and all five charts were exercised with mocked data; this is not a deployed browser or live-account check. API field names, breakdown enums, UTC support and adsread permissions were checked against Reddit's published v3 OpenAPI specification. The database migration and live Ads Manager comparison still require deployment credentials and authorization.
