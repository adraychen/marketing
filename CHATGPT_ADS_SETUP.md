# Connect ChatGPT Ads

This integration reads OpenAI Advertiser API reports. It does not create or update ads, campaigns, budgets, tracking pixels or conversion goals. It does not use a model API key or generate AI analysis.

## Configure the deployed services

1. Keep the downloaded Advertiser API key file outside this repository. Open it privately and copy the actual secret key value, not a filename or the entire JSON/CSV wrapper. Never upload it to Git or paste it into a chat.
2. Apply `supabase/migrations/20261118000100_chatgpt_ads.sql` through the existing Supabase migration workflow. This adds tenant-scoped reporting storage and sync history with RLS.
3. Deploy the changed backend and frontend.
4. Set these environment secrets on the FastAPI backend and the existing content-sync scheduled job (for example, each service's Environment page in Render):

   ```text
   OPENAI_ADS_API_KEY=<actual secret value>
   OPENAI_ADS_ORGANIZATION_ID=<Okoo dashboard organization UUID>
   OPENAI_ADS_LOOKBACK_DAYS=90
   OPENAI_ADS_CLICK_WINDOW_DAYS=30
   OPENAI_ADS_VIEW_WINDOW_DAYS=1
   ```

   Use the same organization UUID as the existing Okoo integrations, not the OpenAI ad-account ID. The dashboard's Connections & Sync > ChatGPT Ads panel displays the organization value. Do not add these secrets to the frontend host. Save/redeploy the backend and worker after changing their environment.
5. As an organization administrator, open **Connections & Sync > ChatGPT Ads > Sync ChatGPT Ads**. The connector verifies `/v1/ad_account`, then downloads all pages of daily ad reporting. Once verified, optionally set `OPENAI_ADS_ACCOUNT_ID` to the returned account ID to reject accidental use of a different account's key.
6. Open **Paid Advertising > ChatGPT Ads**. Select dates and campaigns. All available daily metric charts appear together. Reports & AI Insights also offers a ChatGPT Ads source; exported metrics contain no new AI interpretation. ChatGPT spend appears in Overview/All Paid in the account's own currency.

The existing `python -m backend.content_jobs` job performs the ChatGPT sync when its configured organization matches `OPENAI_ADS_ORGANIZATION_ID`. Its `CONTENT_ORGANIZATION_ID` must be that same organization. Creating a new scheduler is unnecessary when this job already runs daily. Manual sync and scheduled sync share a database lock.

## Measurement definitions

- One row represents one ad on one calendar day in the API account's time zone. The default lookback ends yesterday and refreshes the last 90 days. The maximum is 365 days.
- Daily spend, impressions, clicks, goal conversions and attributed purchase value are collected. Raw API rows, account currency, account ID, original reporting settings and sync timestamp are retained.
- Goal conversions may include actions other than purchases. The integration does not label them completed orders. `order_created_attributed_sales` is platform-attributed purchase value; Shopify remains the canonical completed-order source. GA4 measurements remain independent.
- Attribution is explicitly 30-day click / 1-day view by default and uses `ad_event_time`. Supported environment overrides are 7/14/30-day click and 0/1-day view. These are report settings, not changes to campaign optimization.
- Conversion figures update through daily processing. Spend can finalize later than clicks. Provider CPC/CPM values are preserved in raw/daily rows. Overview ratios computed from the retrieved sums are labeled `calculated_*` and may differ from finalized provider cost metrics.
- Missing/null values and absent rows remain unavailable; explicit numeric zero stays zero. Empty reports are not converted to assumed zero delivery. No daily metrics are cumulatively plotted. Reports with partial date coverage are labeled.
- New imports replace the saved reporting window only after every page passes validation. Failed imports retain the last successful snapshot and record a sanitized error. The latest snapshot is the retained history window; increasing/decreasing the configured lookback changes that window on the next successful sync.
- Original daily ad rows are the sole additive population. Campaign and overview tables summarize those rows and must not be added to them. Attribution models are never combined across advertising providers.
- `chatgpt`/`openai` or recognized chatgpt.com source with an explicit paid medium maps to ChatGPT Paid. Untagged ChatGPT referrals remain ChatGPT Referral — Unattributed. The existence of an ad campaign does not retroactively attribute those referrals to it.

## Validation and live verification

Local tests exercise pagination, GET-only upstream requests, key redaction, account pinning, duplicate rejection, missing-versus-zero metrics, date bounds, tenant isolation, admin-only sync, failed-import preservation, campaign-scoped exports and paid-versus-referral classification.

No downloaded credential has been read or installed by the implementation task, and the production migration has not been applied. A successful deployed sync is required before declaring the account connected. Compare an exported daily Ads Manager report using identical dates, currency, time zone and click/view windows for the first live validation. If access fails, use the sanitized sync status; never include the key in screenshots or logs.

References checked November 18, 2026:
- https://developers.openai.com/ads/api-overview
- https://developers.openai.com/ads/api-reference/insights
- https://developers.openai.com/ads/reporting

## Overview and report metrics

Paid overview and exported dashboard metrics include spend, impressions, clicks, calculated CTR, calculated average CPC and goal conversions. Goal conversions are not necessarily purchases and are not added to other platforms' conversions. Spend sums daily ad observations within the selected dates, not billing charges. Sync ends yesterday in the account timezone; a requested range outside imported coverage is partial. Match report campaign selections and dates to the dashboard when reconciling. Missing metrics remain unavailable.
