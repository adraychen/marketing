# Organic post measurement

## Findings, November 17, 2026

The previous GA4 content job iterated Shopify blog articles only. A Facebook link to `/pages/black-friday-watches` therefore had no destination reporting. The new content job queries discovered first-party Facebook destinations with the existing GA4 credentials, stores daily rows in the existing tenant-scoped table, and replaces fully fetched windows on repeated sync. Identical destinations are queried once per run. Rows attached to different posts are shared page context, never additive post conversions.

UI and report exports now share Facebook visibility, engagement, destination diagnostics and daily website outcomes. No AI interpretation is added. Reports read stored data, so deploy and run content sync before generating a new export. No schema migration is required.

Facebook metrics remain lifetime observations, not November-only actions. The supplied 11 views, 4 unique media viewers, 2 reactions and zero post clicks are regression fixtures, not independently verified live counts. Missing click breakdown categories stay unavailable. Historical shares remain in raw snapshots; an earlier share is not carried forward as a current share count. Comments retain the provider's permission error. Reach, impressions and organic-only distribution remain unavailable rather than substituting Page metrics or media viewers. The current importer does not request deprecated reach fields; post media-view distribution breakdown is not yet collected.

GA4 rows distinguish page views, daily active users and sessions containing a page_view from landing sessions, engaged sessions, engagement rate, engagement seconds, purchases and revenue. Source, medium, campaign, property, time zone, currency and date accompany each row. Average engagement seconds per session uses duration/sessions. Users must not be summed across days; scopes and shared destinations must not be summed together. Hostname is filtered; outcomes occurring on another hostname are outside this report. No rows means no available observations, not an asserted zero. API failures retain the prior stored window. Post-specific UTM attribution is not inferred from Facebook referrals.

## Reminder signup investigation

A read-only fetch of the public page found the Shopify Forms loader and inline `data-forms-id='forms-root-1253121'`. The reminder anchor leads to this form. The public HTML does not establish submission counts, timestamps, successful-submit GA4 events or session linkage. No form was submitted and no customer records were retrieved. Shopify Forms provides form analytics in its admin; its submission records and timestamp availability for this form still require authorized verification. Customer creation dates are not a substitute for submission timestamps.

Before installing tracking, inspect existing GA4 events and the Shopify Forms integration for a confirmed success event scoped to form 1253121. Generic form_submit or button clicks alone are insufficient evidence. If absent, implement exactly one consent-aware event after confirmed acceptance, using a stable non-PII submission identifier for deduplication, form ID and page path. Reuse a suitable existing event name; otherwise define one success event and register the needed form dimension. Preserve the existing session campaign context, include no email/name/phone, and verify one success produces one event while validation errors produce none. Do not mark multiple duplicate events as conversions. This tracking change is not implemented because storefront/tracking changes were excluded.

## Remaining validation

Local runtime has no configured Meta Page token or GA4 property. Live permission grants, November records, Shopify Forms submissions, GA4 signup events and deployed exports therefore remain unverified. Existing Page token permissions should be diagnosed before requesting any new permission; a permission change cannot restore retired metrics. Run the deployed sync and inspect per-metric availability before claiming new counts. The new daily reporting intentionally does not invent a deduplicated full-period active-user total.

References:
- https://developers.facebook.com/docs/graph-api/reference/insights/
- https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema
- https://help.shopify.com/en/manual/promoting-marketing/create-marketing/forms-app/manage

## Overview and article export parity

Shopify Blog uses a combined article/performance table, with no campaign filter. Google search clicks and Google search impressions are Search Console measurements. Article views are GA4 page views from all sources, including repeat views. Article landing sessions begin on the article; their purchases and currency-labeled revenue are GA4 session outcomes, not canonical Shopify orders. Exports reuse this table's aggregation for selected articles and report dates. Multiple reporting properties or missing metric values produce unavailable values rather than combined or zero totals.

Meta overview summarizes latest lifetime observations within the report period for content published in that UTC period. Views and post clicks are additive content counts; interactions require reactions, comments and shares for every included item. Unique reach is not summed. Exports preserve this cohort definition. Instagram is untracked, not zero, and referrals are not evidence of owned-content attribution.
