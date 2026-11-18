# Channel classification and attribution

Rules version: `2026-11-17.1`. Labels are a reporting projection over retained evidence. Existing Shopify stored channel labels are exposed as `stored_channel`; no historical order rows are rewritten by viewing a report. Existing UTM-attributed order/revenue counters still mean explicit UTM presence, not a verified advertising conversion or matched campaign.

## Audit findings

- Shopify imports eligible completed orders and last recorded visit UTMs; it does not import Shopify traffic sessions or untagged referrer domains. Unready/missing journeys cannot establish a channel. Canonical eligibility, refund policy, currency and order-creation dates remain unchanged.
- The legacy Shopify classifier grouped Google organic with Shopify Blog, combined Facebook/Instagram paid, and treated Facebook organic_social tags alone as owned organic traffic. The reporting projection corrects these labels while retaining the legacy label and original UTMs for audit.
- Storewide GA4 previously retained session source/medium/campaign but not manual content/term tags. It already included Instagram referral sessions; the missing distinction was classification, not proof of Instagram content publication.
- Google Ads reports its own campaign inventory and conversions. Its campaign type remains authoritative: a `google / cpc` website label is not evidence that every Google Ads campaign is Search.
- Meta originally imported daily ad results without publisher_platform. New platform observations are stored separately; they never enter the original overview/campaign purchase totals.
- Facebook imports Page content and lifetime observations. Those observations do not establish website sessions, and Page views are not individual-post reach. No owned Instagram content/API access is assumed.

## Classification rules

| Channel | Evidence |
| --- | --- |
| Google Paid Search | google/google_ads source and cpc, ppc or paid_search medium; a source/medium designation, not a claim about Ads campaign type |
| Google Organic Search | google / organic, without known blog landing-page evidence |
| Meta Paid — Facebook | Facebook alias or exact recognized Facebook referral hostname and an explicit paid medium |
| Meta Paid — Instagram | Instagram alias or exact recognized Instagram referral hostname and an explicit paid medium |
| Meta Paid — Platform Unknown | meta source with paid medium; Facebook versus Instagram cannot be inferred |
| Facebook Organic Content | organic_social plus an exact source/medium/campaign/content tuple uniquely matching one imported Facebook post, with publication-date evidence |
| Instagram Organic Content | Same evidence rule for a future imported Instagram item; no such items are currently imported |
| Facebook Referral — Unattributed | Facebook origin without a unique owned-content match or explicit paid medium |
| Instagram Referral — Unattributed | Instagram origin without a unique owned-content match or explicit paid medium; applies even if Okoo has no Instagram posts |
| Shopify Blog / Organic Search | Explicit shopify_blog / content tag; alternatively google / organic with a known `/blogs/` landing path. Storewide imports do not currently supply that path to the classifier |
| Other Referral | Other nonempty source with referral medium |
| Direct / Unknown | Direct, missing or unsupported channel evidence, including channels not in this taxonomy; original source/medium stays visible |

Aliases include facebook/fb, instagram/ig and exact common referral domains (including l.instagram.com and l/m/lm.facebook.com). Lookalike domains do not match. Matching normalizes source/medium for labels only; raw values are retained. Campaign names alone never establish a channel or content match. A new tag does not assign historical untagged traffic to that post.

Owned-content matching is intentionally conservative. Shared tags are ambiguous. Publication must precede the evidence timestamp; daily GA4 rows use the start of that day in the property time zone, so publication-day rows may remain unattributed. Shopify groups with an earliest order before publication remain source-only. Raw daily/source evidence is retained for further review. Instagram content support currently stops at a reporting contract and untracked UI state; a future authorized importer/schema extension is required.

## Collection and reporting

The new GA4 channel import requests nine session-scoped dimensions: date, sessionSource, sessionMedium, sessionCampaignName, sessionManualSource, sessionManualMedium, sessionManualCampaignName, sessionManualAdContent and sessionManualTerm. It stores sessions, product-view events, add-to-carts, checkout starts, GA4 purchase counts and purchase revenue. The manual UTM evidence and storewide report are alternative breakdowns, not additional traffic. Manual tags must agree with session origin before owned-content matching. Other UTM extensions such as utm_id are not collected.

GA4 thresholded, sampled, restricted, malformed or incompletely paginated responses do not replace saved data. A complete successful sync replaces its date window atomically. Tenant/property/day/dimension keys prevent append-style duplication. Coverage records identify source, period, currency, time zone and last successful sync. No order/customer identity is added to these reports.

Meta uses `breakdowns=publisher_platform` with the same daily ad grain, unified ad-set attribution and `action_report_time=impression` as unbroken delivery. It retains spend, impressions, per-row reach, link clicks, landing_page_view actions, and only offsite_conversion.fb_pixel_purchase counts/values. Missing action categories stay unavailable. Other publisher platforms are retained, not relabeled Facebook or Instagram. Reach is never summed across ads, days or platforms. Platform purchases are never added to unbroken totals or Shopify orders.

Reconciliation flags platform/unbroken discrepancies when comparable metrics are present, missing counterpart rows and different attribution settings. It does not silently choose a replacement. A failed platform request leaves previous platform data in place and exposes the error/last successful sync; original Meta collection can still succeed. Detailed causes of actual provider discrepancies require examining the live report and attribution settings.

UI and exports show channel coverage, provider-specific outcomes, original attribution and classification version. Google Ads, Facebook content and Meta platform sections also carry their own coverage/limitations. An excluded report source is marked unavailable for that export; its data is not fetched. States distinguish reported numeric zero, unavailable metrics, disconnected reporting sources and untracked owned content. Absence of a channel row is not proof of zero activity.

The Shopify/GA4 comparison uses identical requested date ranges and shows time zone and currency. It preserves independent purchase/revenue measurements. Consent, attribution model, refunds, identity, reporting lag and local date boundaries can produce differences; no values are forced to match. Shopify traffic-session comparison is unavailable because the existing Shopify importer collects orders, not Shopify Analytics sessions.

## Deployment and remaining setup

Apply both new migrations before running sync:

1. `supabase/migrations/20261117000100_meta_platform_delivery.sql`
2. `supabase/migrations/20261117000200_channel_attribution.sql`

Then deploy backend/frontend and run the existing content and Meta sync jobs for the desired lookback. The new GA4 channel job uses the existing CONTENT_GA4_PROPERTY_ID, CONTENT_GA4_TIMEZONE and Google read credentials. It is independent of AI/campaign-evaluation flags. The new Meta breakdown uses existing ads_read/ad-account authorization. A denied breakdown is reported, not solved by silently broadening permissions. No Shopify API fields, scopes, tracking installations, ad settings or budgets were changed.

Instagram Organic intentionally requests no authentication. When needed, enable a supported professional-account authorization flow, verify the then-current permissions/metric support, and import account/content identifiers, publication timestamps and metric periods. Assign unique UTMs to future owned links before publication. Do not retroactively infer existing referrals as owned content.

Live November Shopify/GA4 comparisons and platform availability are not verified locally without deployed credentials and migrations. Local tests use provider fixtures; live API/DB checks must be recorded separately. The new schema has not been applied to production by this task.

Validation: full existing/new suite at the main implementation checkpoint: 144 passed, 6 skipped. After adding repeat-sync/export regressions and final coverage changes: focused suite 59 passed, 2 skipped. Python compilation and `git diff --check` passed. Tests include referral classification without owned posts, paid-platform separation, ambiguous/reused UTMs, publication-date protection, canonical/GA4 separation, exact Meta purchase categories, nonadditive reach, duplicate platform rejection, repeat-sync replacement, failed-fetch preservation and Markdown/CSV provenance. Database migrations and live provider totals were not exercised; local database and provider credentials are absent. One existing Starlette/httpx deprecation warning remains.

## References

- [GA4 Data API schema](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema)
- [Meta Insights breakdowns](https://developers.facebook.com/docs/marketing-api/insights/breakdowns/)
