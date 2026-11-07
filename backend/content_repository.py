from urllib.parse import parse_qs, urlsplit

from psycopg2.extras import Json, execute_values

from backend.repositories import database

PILOT = "citizen_eco_drive_vs_automatic"
PILOT_PATH = "/blogs/journal/citizen-eco-drive-vs-automatic"
PILOT_UTM = {"utm_source": "facebook", "utm_medium": "organic_social", "utm_campaign": PILOT}


def url_key(url):
    parsed = urlsplit(url)
    return (parsed.hostname or "").removeprefix("www."), parsed.path.rstrip("/")


def link_utm(url):
    query = parse_qs(urlsplit(url).query)
    return {key: query[key][0] for key in (*PILOT_UTM, "utm_content") if query.get(key)}


def write_items(cur, org, source, source_account, items):
    if items:
        execute_values(cur, """
            insert into marketing_content_items
                (organization_id,source,source_account,external_id,title,url,published_at,data) values %s
            on conflict (organization_id,source,source_account,external_id) do update set
                title=excluded.title,url=excluded.url,published_at=excluded.published_at,data=excluded.data,active=true
        """, [(org, source, source_account, item["external_id"], item["title"], item["url"],
               item["published_at"], Json(item["data"])) for item in items])
    cur.execute("""
        update marketing_content_items set active=false where organization_id=%s and source=%s and source_account=%s
        and active and not (external_id=any(%s::text[]))
    """, (org, source, source_account, [item["external_id"] for item in items]))


def read_items(cur, org, source, source_account):
    cur.execute("""
        select id, external_id, url, published_at, data from marketing_content_items
        where organization_id=%s and source=%s and source_account=%s and active order by id
    """, (org, source, source_account))
    return cur.fetchall()


def associate(cur, org, shop, page):
    articles = {url_key(item["url"]): item for item in read_items(cur, org, "shopify", shop)}
    posts = read_items(cur, org, "facebook", page)
    pilot = next((item for (_, path), item in articles.items() if path == PILOT_PATH), None)
    if pilot:
        cur.execute("""
            insert into marketing_content_campaigns values (%s,%s,%s) on conflict do nothing
        """, (org, PILOT, "Citizen Eco-Drive vs Automatic"))
        cur.execute("insert into marketing_content_campaign_items values (%s,%s,%s) on conflict do nothing",
                    (org, PILOT, pilot["id"]))
    for post in posts:
        for url in post["data"].get("links", []):
            article = articles.get(url_key(url))
            if not article:
                continue
            utm = link_utm(url)
            cur.execute("""
                insert into marketing_content_links values (%s,%s,%s,%s,%s) on conflict do nothing
            """, (org, post["id"], article["id"], url, Json(utm)))
            if pilot and article["id"] == pilot["id"] and all(utm.get(key) == val for key, val in PILOT_UTM.items()):
                cur.execute("insert into marketing_content_campaign_items values (%s,%s,%s) on conflict do nothing",
                            (org, PILOT, post["id"]))


def article_utms(cur, org, article_id):
    cur.execute("""
        select distinct utm from marketing_content_links
        where organization_id=%s and article_id=%s and utm->>'utm_source'='facebook'
          and utm->>'utm_medium'='organic_social' and utm ? 'utm_campaign'
    """, (org, article_id))
    return [row["utm"] for row in cur.fetchall()]


def write_daily(cur, org, item_id, source_account, reports, start, end, rows):
    # Each call replaces only a fully fetched window for one article and source.
    cur.execute("""
        delete from marketing_content_daily_metrics where organization_id=%s and item_id=%s
        and source_account=%s and report=any(%s) and day between %s and %s
    """, (org, item_id, source_account, reports, start, end))
    if rows:
        execute_values(cur, """
            insert into marketing_content_daily_metrics
                (organization_id,item_id,source_account,report,day,segment,timezone,currency,metrics) values %s
        """, [(org, item_id, source_account, row["report"], row["day"], row["segment"], row["timezone"],
               row["currency"], Json(row["metrics"])) for row in rows])


def sync_status(org, source, status, records=0, message=None):
    with database() as cur:
        cur.execute("""
            insert into marketing_content_sync_runs (organization_id,source,status,records,message)
            values (%s,%s,%s,%s,%s)
        """, (org, source, status, records, message))


def catalog(org):
    with database() as cur:
        cur.execute("select slug,name from marketing_content_campaigns where organization_id=%s order by name", (org,))
        campaigns = cur.fetchall()
        cur.execute("""
            select i.id,i.title,i.source,i.url,i.published_at,i.active,
                coalesce(array_agg(c.campaign_slug) filter (where c.campaign_slug is not null), '{}') as campaigns
            from marketing_content_items i left join marketing_content_campaign_items c
                on (i.organization_id,i.id)=(c.organization_id,c.item_id)
            where i.organization_id=%s group by i.organization_id,i.id order by i.published_at desc
        """, (org,))
        items = cur.fetchall()
        cur.execute("""
            select distinct on (source) source,status,records,message,finished_at
            from marketing_content_sync_runs where organization_id=%s order by source,finished_at desc
        """, (org,))
        return {"campaigns": campaigns, "items": items, "sources": cur.fetchall()}


def details(org, item_id, start, end):
    with database() as cur:
        cur.execute("select id from marketing_content_items where organization_id=%s and id=%s", (org, item_id))
        if not cur.fetchone():
            return None
        cur.execute("""
            select report,source_account,day,segment,timezone,currency,metrics
            from marketing_content_daily_metrics where organization_id=%s and item_id=%s
            and day between %s and %s order by day,report,segment
        """, (org, item_id, start, end))
        daily = cur.fetchall()
        cur.execute("""
            select observed_day,observed_at,api_version,metrics
            from marketing_content_metric_snapshots where organization_id=%s and item_id=%s
            and observed_day between %s and %s order by observed_day,observed_at
        """, (org, item_id, start, end))
        snapshots = cur.fetchall()
        cur.execute("""
            select l.url,l.utm,i.title as article,p.title as post from marketing_content_links l
            join marketing_content_items i on (i.organization_id,i.id)=(l.organization_id,l.article_id)
            join marketing_content_items p on (p.organization_id,p.id)=(l.organization_id,l.post_id)
            where l.organization_id=%s and (l.post_id=%s or l.article_id=%s) order by l.url
        """, (org, item_id, item_id))
        return {"daily": daily, "snapshots": snapshots, "links": cur.fetchall()}


def linked_entities(org, slug):
    with database() as cur:
        cur.execute("""
            select label,shopify_product_gid from marketing_content_products
            where organization_id=%s and campaign_slug=%s order by label
        """, (org, slug))
        products = cur.fetchall()
        cur.execute("""
            select c.data->>'name' as name,c.data->>'status' as status,c.resource
            from marketing_content_ad_campaigns l join marketing_campaigns c
              on (c.organization_id,c.account_id,c.resource)=(l.organization_id,l.ad_account_id,l.ad_campaign_resource)
            where l.organization_id=%s and l.campaign_slug=%s order by name
        """, (org, slug))
        return {"products": products, "ads": cur.fetchall()}
