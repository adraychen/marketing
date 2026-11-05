import re
import time

import google.auth
import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request

from backend.connectors.base import AdvertisingConnector
from backend.models import Account, Asset, Creative, Daily, Resource

SCOPE = "https://www.googleapis.com/auth/adwords"
API_ROOT = "https://googleads.googleapis.com/v26"
CUSTOMER = re.compile(r"customers/(\d{10})")
METRICS = """segments.date, metrics.cost_micros, metrics.impressions, metrics.clicks,
             metrics.interactions, metrics.conversions, metrics.conversions_value"""
CUSTOMERS = """select customer.resource_name, customer.descriptive_name, customer.currency_code,
               customer.time_zone, customer.manager from customer"""
CLIENTS = """select customer_client.client_customer, customer_client.descriptive_name,
             customer_client.currency_code, customer_client.time_zone, customer_client.manager,
             customer_client.status from customer_client where customer_client.level = 1"""
CAMPAIGNS = """select campaign.resource_name, campaign.id, campaign.name, campaign.status,
               campaign.advertising_channel_type, campaign_budget.amount_micros,
               campaign_budget.period from campaign"""
GROUPS = "select ad_group.resource_name, ad_group.id, ad_group.name, ad_group.status, campaign.resource_name from ad_group"
ADS = """select ad_group_ad.resource_name, ad_group_ad.status, ad_group_ad.ad_strength,
         ad_group.resource_name, ad_group_ad.ad.id, ad_group_ad.ad.type,
         ad_group_ad.ad.name, ad_group_ad.ad.final_urls,
         ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions,
         ad_group_ad.ad.responsive_search_ad.path1, ad_group_ad.ad.responsive_search_ad.path2,
         ad_group_ad.ad.expanded_text_ad.headline_part1, ad_group_ad.ad.expanded_text_ad.headline_part2,
         ad_group_ad.ad.expanded_text_ad.headline_part3, ad_group_ad.ad.expanded_text_ad.description,
         ad_group_ad.ad.expanded_text_ad.description2,
         ad_group_ad.ad.expanded_text_ad.path1, ad_group_ad.ad.expanded_text_ad.path2
         from ad_group_ad"""
KEYWORDS = """select ad_group_criterion.resource_name, ad_group_criterion.status,
              ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type,
              ad_group.resource_name from ad_group_criterion
              where ad_group_criterion.type = KEYWORD and ad_group_criterion.negative = false"""


class GoogleAdsError(Exception):
    pass


def normalize_ad(row):
    link = row["adGroupAd"]
    ad = link["ad"]
    kind = ad.get("type", "UNKNOWN")
    urls = ad.get("finalUrls", [])
    data = {"id": ad.get("id"), "name": ad.get("name"), "type": kind,
            "status": link.get("status"), "strength": link.get("adStrength"), "final_urls": urls}
    if kind == "RESPONSIVE_SEARCH_AD":
        rsa = ad.get("responsiveSearchAd", {})
        def assets(kind):
            return [
                Asset(text=asset["text"], pin=pin if pin != "UNSPECIFIED" else "")
                for asset in rsa.get(kind, []) for pin in [asset.get("pinnedField", "")]
            ]

        data["creative"] = Creative(
            headlines=assets("headlines"), descriptions=assets("descriptions"),
            final_urls=urls, path1=rsa.get("path1", ""), path2=rsa.get("path2", ""),
        ).model_dump()
    elif kind == "EXPANDED_TEXT_AD":
        expanded = ad.get("expandedTextAd", {})
        data["read_only"] = {
            "headlines": [expanded[key] for key in ("headlinePart1", "headlinePart2", "headlinePart3") if expanded.get(key)],
            "descriptions": [expanded[key] for key in ("description", "description2") if expanded.get(key)],
            "paths": [expanded[key] for key in ("path1", "path2") if expanded.get(key)],
        }
    return Resource(resource=link["resourceName"], parent=row["adGroup"]["resourceName"], data=data)


def daily(row, kind, resource, currency):
    m = row.get("metrics", {})
    return Daily(kind=kind, resource=resource, day=row["segments"]["date"], currency=currency,
                 spend=int(m.get("costMicros", 0)) / 1_000_000,
                 impressions=int(m.get("impressions", 0)), clicks=int(m.get("clicks", 0)),
                 interactions=int(m.get("interactions", 0)), conversions=float(m.get("conversions", 0)),
                 value=m.get("conversionsValue"))


class GoogleAdsConnector(AdvertisingConnector):
    """Official Google Ads REST reporting API with Google Auth credentials."""

    def __init__(self, client, credentials, manager=""):
        if manager and not re.fullmatch(r"\d{10}", manager):
            raise GoogleAdsError("Configure a 10-digit Google Ads manager ID.")
        self.client = client
        self.credentials = credentials
        self.manager = manager

    @classmethod
    def from_environment(cls, client, manager=""):
        try:
            credentials, _ = google.auth.default(scopes=[SCOPE])
        except (GoogleAuthError, OSError, ValueError):
            raise GoogleAdsError("Google credentials are unavailable on the backend.") from None
        return cls(client, credentials, manager)

    def request(self, method, path, manager="", payload=None):
        if (method, path) != ("GET", "/customers:listAccessibleCustomers") and not (
            method == "POST" and re.fullmatch(r"/customers/\d{10}/googleAds:search", path)
        ):
            raise GoogleAdsError("Only Google Ads discovery and reporting are supported.")
        headers = {"login-customer-id": manager} if manager else {}
        try:
            self.credentials.before_request(Request(), method, API_ROOT + path, headers)
        except (GoogleAuthError, OSError):
            raise GoogleAdsError("Google authorization failed. Check the service account credentials.") from None
        for attempt in range(4):
            try:
                response = self.client.request(method, API_ROOT + path, headers=headers, json=payload)
            except httpx.TransportError:
                response = None
            retry = response is None or response.status_code in {429, 500, 502, 503, 504}
            if not retry or attempt == 3:
                break
            time.sleep(2 ** attempt)
        if response is None:
            raise GoogleAdsError("Google Ads could not be reached.")
        if response.status_code in {401, 403}:
            raise GoogleAdsError("Google Ads access was denied. Check Cloud API access and account permissions.")
        if response.status_code == 429:
            raise GoogleAdsError("Google Ads quota or rate limit reached. Try again later.")
        if not response.is_success:
            request_id = response.headers.get("request-id", "")
            suffix = f" Request ID {request_id}." if re.fullmatch(r"[\w-]{1,100}", request_id) else ""
            raise GoogleAdsError(f"Google Ads reporting request failed (HTTP {response.status_code}).{suffix}")
        return response.json()

    def search(self, customer, query, manager=""):
        if not CUSTOMER.fullmatch(customer):
            raise GoogleAdsError("Invalid Google Ads customer resource.")
        page = None
        while True:
            payload = {"query": query}
            if page:
                payload["pageToken"] = page
            data = self.request("POST", f"/{customer}/googleAds:search", manager, payload)
            yield from data.get("results", [])
            page = data.get("nextPageToken")
            if not page:
                break

    def discover(self):
        direct = self.request("GET", "/customers:listAccessibleCustomers").get("resourceNames", [])
        if self.manager and f"customers/{self.manager}" not in direct:
            raise GoogleAdsError("The configured manager is unavailable to the service account.")
        leaves, managers = {}, []
        for resource in direct:
            for row in self.search(resource, CUSTOMERS):
                data = row["customer"]
                if data.get("manager"):
                    if not self.manager or resource == f"customers/{self.manager}":
                        managers.append((resource, resource.split("/")[-1]))
                else:
                    leaves[resource] = self.account(data, resource)
        seen = set()
        while managers:
            resource, root = managers.pop()
            if (resource, root) in seen:
                continue
            seen.add((resource, root))
            for row in self.search(resource, CLIENTS, root):
                data = row["customerClient"]
                child = data["clientCustomer"]
                if data.get("status") not in {None, "ENABLED"}:
                    continue
                if data.get("manager"):
                    managers.append((child, root))
                elif child not in leaves:
                    leaves[child] = self.account(data, child, root)
        return sorted(leaves.values(), key=lambda item: (item.name, item.resource))

    @staticmethod
    def account(data, resource, manager=""):
        return Account(resource=resource, name=data.get("descriptiveName") or resource,
                       currency=data["currencyCode"], timezone=data["timeZone"], manager=manager)

    def rows(self, account, query):
        return self.search(account.resource, query, account.manager)

    def campaigns(self, account):
        items = []
        for row in self.rows(account, CAMPAIGNS):
            c, budget = row["campaign"], row.get("campaignBudget", {})
            micros = budget.get("amountMicros") if budget.get("period") == "DAILY" else None
            items.append(Resource(resource=c["resourceName"], data={
                "id": c["id"], "name": c["name"], "status": c.get("status"),
                "type": c.get("advertisingChannelType"),
                "daily_budget": int(micros) / 1_000_000 if micros is not None else None,
            }))
        return items

    def content(self, account):
        groups = [
            Resource(resource=row["adGroup"]["resourceName"], parent=row["campaign"]["resourceName"],
                     data={key: row["adGroup"].get(key) for key in ("id", "name", "status")})
            for row in self.rows(account, GROUPS)
        ]
        keywords = [
            Resource(resource=item["resourceName"], parent=row["adGroup"]["resourceName"], data={
                "text": item["keyword"]["text"], "match_type": item["keyword"].get("matchType"),
                "status": item.get("status"),
            })
            for row in self.rows(account, KEYWORDS) for item in [row["adGroupCriterion"]]
        ]
        return {"ad_groups": groups, "ads": [normalize_ad(row) for row in self.rows(account, ADS)],
                "keywords": keywords}

    def performance(self, account, start, end):
        period = f"segments.date between '{start.isoformat()}' and '{end.isoformat()}'"
        metrics, terms = [], {}
        sources = (
            ("campaigns", "campaign.resource_name", "campaign", "campaign", ""),
            ("keywords", "ad_group_criterion.resource_name", "keyword_view", "adGroupCriterion",
             "and ad_group_criterion.negative = false"),
            ("search_terms", """search_term_view.resource_name, search_term_view.search_term,
                search_term_view.status, ad_group.resource_name""", "search_term_view", "searchTermView", ""),
        )
        for kind, fields, source, key, extra in sources:
            query = f"select {fields}, {METRICS} from {source} where {period} {extra}"
            for row in self.rows(account, query):
                item = row[key]
                resource = item["resourceName"]
                if kind == "search_terms":
                    terms[resource] = Resource(resource=resource, parent=row["adGroup"]["resourceName"],
                                               data={"text": item["searchTerm"], "status": item.get("status")})
                metrics.append(daily(row, kind, resource, account.currency))
        return metrics, list(terms.values())
