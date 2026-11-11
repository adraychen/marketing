# Connect Okoo paid Meta ads

This integration reads ad account **2966294073730346** in the Shopify business portfolio. It imports all Meta placements in that ad account, including Instagram when used. Organic Facebook Page credentials and metrics remain separate. No Meta campaigns, budgets, permissions, or ads are modified.

## 1. Apply the migration

Run `supabase/migrations/20261111000200_meta_paid_ads.sql` in the Supabase SQL Editor for the database used by both the backend and content cron. Apply it once. The four new tables all start with `marketing_meta_ads_` and use organization membership read policies.

Deploy the updated repository to the Render backend, the existing content cron, and Streamlit. Existing commerce migration and configuration are separate prerequisites for Shopify commerce metrics, not for Meta paid imports.

## 2. Set Render environment variables

Add these to **both the FastAPI backend service and okoo-content-marketing-sync**:

```dotenv
META_ADS_ORGANIZATION_ID=<copy the existing CONTENT_ORGANIZATION_ID value>
META_ADS_ACCOUNT_ID=2966294073730346
META_ADS_ACCESS_TOKEN=<paste the existing ads_read system-user token privately in Render>
META_ADS_API_VERSION=v26.0
META_ADS_LOOKBACK_DAYS=90
META_ADS_TOKEN_EXPIRES_AT=<exact ISO timestamp including timezone, or leave blank>
```

Use the organization UUID, not your Supabase user UID. Copy it from the working content cron. Do not enter the literal angle-bracket placeholders. Keep the token out of Git, screenshots, chat, and Streamlit secrets. Do not replace `CONTENT_META_PAGE_ACCESS_TOKEN`, which serves the organic Page integration.

The existing Okoo Integration system user must retain assigned access to the ad account and the token must belong to its authorized Meta app with `ads_read`. No new token or publishing permission is requested by this connector.

Record the exact expiry shown by Meta Access Token Debugger, such as `2027-01-08T00:00:00Z`, only if that is the actual expiry. Do not copy that example date blindly. If unknown, leave the value empty. Expiry is labeled operator supplied, never API verified. The UI warns within seven days and unknown expiry remains explicit. Updating the token also requires updating its recorded expiry. Renew the 60-day token before it expires.

## 3. Verify and import

In the content cron shell, `python -m backend.content_jobs --diagnose` checks the configured account via a read-only GET. Look for `Meta paid account access verified: True`. This check confirms account access, not every creative-field or Insights permission.

Trigger the existing cron command `python -m backend.content_jobs`, or use **Connections & Sync > Meta Paid Ads > Sync Meta paid ads** as an organization admin. No separate cron is needed. Look for `meta_ads: succeeded` with campaigns, adsets, ads, and daily_ad_rows counts. The Meta import runs after content sources when `META_ADS_ORGANIZATION_ID` matches the cron organization.

Check the dashboard at **Paid Advertising > Meta Ads**, the **Overview / All Paid** table, and **Reports & AI Insights > Facebook Paid Ads**. Report campaign checkboxes filter settings, ad sets, creative, and daily metrics together. The source starts selected in a fresh report session once an import exists. Existing checkbox choices are retained.

## Data definitions and limits

- Every run refreshes the configured lookback (default 90 completed days) in the ad account timezone. A complete empty delivery response can establish zero spend/clicks. Missing date coverage never establishes zero.
- All remote Marketing API requests are GET. Only the app's own database is written. Fully fetched responses replace the covered daily window atomically. Failures preserve prior data and record a sanitized error. Manual and scheduled Meta syncs use the same account lock.
- Campaign, ad set, and creative metadata are latest-known settings, not historical settings on each delivery day. Raw budgets/bids use API minor currency units. Spend/Insights monetary fields use the account currency in major units. Do not add campaign and ad-set budgets or assume missing budgets are zero.
- Daily Insights use the ad-set unified attribution setting and `action_report_time=impression`. The returned attribution setting and raw action categories are retained. Website purchase count/value use only `offsite_conversion.fb_pixel_purchase`. Absent categories are unavailable rather than inferred as zero. Purchase, omni-purchase, and website purchase categories can overlap and must not be added.
- Reach and frequency remain daily per-ad observations and are not summed into campaign/account totals. No publisher-placement breakdown is requested, so these are Meta paid totals, not Facebook-only delivery.
- Existing-post, catalog, dynamic, or inaccessible ads may expose partial metadata. Historical delivery rows without accessible campaign metadata are labeled metadata unavailable. Creative stores returned body/title, story/asset specifications, destination/UTM tags, and media references where available.
- Meta paid results are separate from organic Page/post Insights and canonical Shopify order totals. A promoted Page post can appear in both systems. Do not add their overlapping visibility measures or attributed purchase counts.
- AI exports contain paid metrics and creative for further analysis. This integration does not add automatic Meta AI generation or paid publishing.

## Validation

Automated tests cover GET-only requests, trusted-host pagination, sanitized permission/expiry/rate-limit errors, exact conversion-category selection, campaign filtering, no deletion on failed imports, and protected API routes. An optional `TEST_DATABASE_URL` test checks repeated imports and tenant RLS in a migrated test database. Real token access and production database execution require the configuration above and are not implied by local tests.

API references: [Meta Marketing API](https://developers.facebook.com/docs/marketing-api/), [Meta official Python SDK account/Insights parameters](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/adaccount.py), [creative fields](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/adcreative.py).
