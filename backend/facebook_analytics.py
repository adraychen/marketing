from datetime import date, datetime, time, timedelta
from math import isfinite
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg2.extras import execute_values
from pydantic import BaseModel, Field, model_validator

from backend.connectors.content import facebook_reader
from backend.repositories import database

PAGE_METRICS = {
    "page_media_view": ("Page media views", "views"),
    "page_total_media_view_unique": ("Unique Page media viewers", "people"),
    "page_views_total": ("Page profile views", "views"),
    "page_video_views": ("Page video views (3 seconds or nearly full short video)", "views"),
    "page_video_views_60s_excludes_shorter": ("Page 60-second video views", "views"),
    "page_video_view_time": ("Page video watch time", "milliseconds"),
    "page_video_views_organic": ("Organic 3-second video views", "views"),
    "page_video_views_paid": ("Paid 3-second video views", "views"),
    "page_daily_follows_unique": ("New follows (daily unique)", "people"),
    "page_daily_unfollows_unique": ("Unfollows (daily unique)", "people"),
}
AUDIENCE = {"followers_count", "page_daily_follows_unique", "page_daily_unfollows_unique", "net_follows"}
PAGE_MANUAL = {
    **PAGE_METRICS, "followers_count": ("Total followers", "people"),
    "net_follows": ("Net follows", "people"), "reel_views": ("Reel views", "views"),
    "reel_watch_time": ("Reel watch time", "milliseconds"), "posts_views": ("Posts views", "views"),
    "all_views": ("All views in Business Dashboard", "views"),
    "business_video_views_3s": ("Business Dashboard 3-second video views", "views"),
    "business_video_views_1m": ("Business Dashboard 1-minute video views", "views"),
    "page_organic_views": ("Organic Page views", "views"), "page_paid_views": ("Paid Page views", "views"),
}
CONTENT_MANUAL = {
    "post_media_view": ("Post media views", "views"), "post_total_media_view_unique": ("Unique media viewers", "people"),
    "reactions": ("Reactions", "count"), "comments": ("Comments", "count"), "shares": ("Shares", "count"),
    "post_clicks": ("Post clicks", "count"), "link_clicks": ("Link clicks", "count"),
    "outbound_clicks": ("Outbound clicks", "count"), "total_video_views": ("Video views", "views"),
    "total_video_view_total_time": ("Total video watch time", "milliseconds"),
    "total_video_avg_time_watched": ("Average video watch time", "milliseconds"),
    "total_video_60s_excludes_shorter_views": ("60-second video views", "views"),
    "reel_views": ("Reel views", "views"), "reel_watch_time": ("Reel watch time", "milliseconds"),
    "reel_unique_viewers": ("Unique reel viewers", "people"), "reel_retention": ("Reel retention at a noted timestamp", "percent"),
    "business_video_views_3s": ("Business Dashboard 3-second video views", "views"),
    "business_video_views_1m": ("Business Dashboard 1-minute video views", "views"),
}
PACIFIC = ZoneInfo("America/Los_Angeles")


def collect_page_metrics(client, page, token, version, start, end, observed_day, *, skip=()):
    endpoint = f"https://graph.facebook.com/{version}/{page}"
    states = {}
    read = facebook_reader(client, token, states)
    rows = {}
    field = "followers_count"
    followers = read(endpoint, {"fields": field}, field).get(field)
    if type(followers) is int and followers >= 0:
        rows[field, observed_day, "point"] = (field, observed_day, "point", followers, None, None, "Reported")
        states[field] = "Reported"

    for metric in PAGE_METRICS:
        if metric in skip:
            continue
        first = start
        while first <= end:
            last = min(end, first + timedelta(days=26))
            params = {"metric": metric, "period": "day", "since": str(first - timedelta(days=1)),
                      "until": str(last + timedelta(days=2))}
            response = read(f"{endpoint}/insights", params, metric)
            for series in response.get("data", []):
                if series.get("name") != metric or series.get("period") != "day":
                    continue
                for point in series.get("values", []):
                    value = point.get("value")
                    if type(value) not in (int, float) or not isfinite(value) or value < 0:
                        continue
                    try:
                        boundary = datetime.fromisoformat(point["end_time"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if boundary.utcoffset() is None:
                        continue
                    pacific_boundary = boundary.astimezone(PACIFIC)
                    local_boundary = pacific_boundary if pacific_boundary.time() == time.min else boundary
                    if local_boundary.time() != time.min:
                        continue
                    day = local_boundary.date() - timedelta(days=1)
                    if first <= day <= last:
                        offset = local_boundary.strftime("%z")
                        offset = f"{offset[:3]}:{offset[3:]}"
                        rows[metric, day, "day"] = (metric, day, "day", value, boundary, offset, "Reported")
                        states[metric] = "Reported"
            if states[metric] not in {"Reported", "No data returned"}:
                break
            first = last + timedelta(days=1)
        if states[metric] == "No data returned" and any(key[0] == metric for key in rows):
            states[metric] = "Reported"
    return list(rows.values()) + [
        (metric, observed_day, "status", None, None, None, status) for metric, status in states.items()
    ]


def unsupported_page_metrics(org, page, version):
    with database() as cur:
        cur.execute("""
            select metric from (
                select distinct on (metric) metric,availability,collected_at
                from marketing_facebook_page_metrics
                where organization_id=%s and page_id=%s and api_version=%s and basis='status'
                order by metric,collected_at desc
            ) latest where availability='Unsupported metric' and collected_at > now() - interval '7 days'
        """, (org, page, version))
        return {row["metric"] for row in cur.fetchall()}


class ManualObservation(BaseModel):
    page_id: str = Field(pattern=r"^[0-9]{1,30}$")
    item_id: UUID | None = None
    metric: str
    value: float = Field(allow_inf_nan=False, ge=-1e15, le=1e15)
    basis: Literal["period", "point", "lifetime"]
    period_start: date
    period_end: date
    observed_day: date
    reporting_offset: str = Field(default="", pattern=r"^(?:[+-](?:0[0-9]|1[0-4]):[0-5][0-9])?$")
    notes: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_observation(self):
        metrics = CONTENT_MANUAL if self.item_id else PAGE_MANUAL
        if self.metric not in metrics or self.value < 0 and self.metric != "net_follows":
            raise ValueError("Choose a valid metric and value for this scope.")
        if self.period_start > self.period_end or self.period_end > self.observed_day:
            raise ValueError("Use an ordered period ending on or before the observation date.")
        if self.basis != "period" and not self.period_start == self.period_end == self.observed_day:
            raise ValueError("Use the observation date for a point or lifetime value.")
        if (self.metric == "followers_count") != (self.basis == "point"):
            raise ValueError("Use point observations for the current follower count.")
        if not self.item_id and self.metric != "followers_count" and self.basis != "period":
            raise ValueError("Use a reporting period for Page activity.")
        if self.metric == "reel_retention" and (self.value > 100 or not self.notes.strip()):
            raise ValueError("Record retention as a percentage and include the playback timestamp in the notes.")
        return self


def write_page_metrics(org, page, version, rows):
    if not rows:
        return
    with database() as cur:
        execute_values(cur, """
            insert into marketing_facebook_page_metrics
            (organization_id,page_id,api_version,metric,day,basis,value,period_end_at,reporting_offset,availability)
            values %s
            on conflict (organization_id,page_id,metric,api_version,day,basis) do update set
            value=excluded.value,period_end_at=excluded.period_end_at,reporting_offset=excluded.reporting_offset,
            availability=excluded.availability,collected_at=now()
        """, [(org, page, version, *row) for row in rows])


def analytics(org, start, end):
    with database() as cur:
        cur.execute("""
            select * from marketing_facebook_page_metrics where organization_id=%s
            and basis <> 'status' and day between %s and %s order by day, page_id, metric, api_version
        """, (org, start, end))
        page = cur.fetchall()
        cur.execute("""
            select distinct on (page_id,metric) * from marketing_facebook_page_metrics
            where organization_id=%s and basis='status'
            order by page_id,metric,collected_at desc
        """, (org,))
        availability = cur.fetchall()
        cur.execute("""
            select distinct source_account as page_id from marketing_content_items
            where organization_id=%s and source='facebook'
            union select distinct page_id from marketing_facebook_page_metrics where organization_id=%s
        """, (org, org))
        pages = [row["page_id"] for row in cur.fetchall()]
        cur.execute("""
            select m.*, i.title as content_title from marketing_facebook_manual m
            left join marketing_content_items i on (i.organization_id,i.id)=(m.organization_id,m.item_id)
            where m.organization_id=%s and m.period_start <= %s and m.period_end >= %s
            order by m.period_end desc,m.id
        """, (org, end, start))
        manual = cur.fetchall()
    return {"page": page, "availability": availability, "pages": sorted(pages), "manual": manual}


def save_manual(org, row, observation_id=None):
    item_id = str(row.item_id) if row.item_id else None
    with database() as cur:
        cur.execute("""
            select 1 from marketing_content_items
            where organization_id=%s and source='facebook' and source_account=%s
              and (%s::uuid is null or id=%s::uuid)
            union all select 1 from marketing_facebook_page_metrics
            where organization_id=%s and page_id=%s and %s::uuid is null
            limit 1
        """, (org, row.page_id, item_id, item_id, org, row.page_id, item_id))
        if not cur.fetchone():
            raise LookupError
        units = CONTENT_MANUAL if row.item_id else PAGE_MANUAL
        values = (row.page_id, item_id, row.metric, row.value, units[row.metric][1], row.basis,
                  row.period_start, row.period_end, row.observed_day, row.reporting_offset, row.notes)
        if observation_id is None:
            cur.execute("""
                insert into marketing_facebook_manual
                (page_id,item_id,metric,value,unit,basis,period_start,period_end,observed_day,reporting_offset,notes,organization_id)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict do nothing returning id
            """, (*values, org))
        else:
            cur.execute("""
                update marketing_facebook_manual
                set (page_id,item_id,metric,value,unit,basis,period_start,period_end,observed_day,reporting_offset,notes,updated_at)
                    = (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                where organization_id=%s and id=%s returning id
            """, (*values, org, observation_id))
        return cur.fetchone()


def delete_manual(org, observation_id):
    with database() as cur:
        cur.execute("delete from marketing_facebook_manual where organization_id=%s and id=%s", (org, observation_id))
        return cur.rowcount != 0
