from contextlib import contextmanager
from datetime import timedelta

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor, execute_values

from backend.config import required

RESOURCES = ("campaigns", "ad_groups", "ads", "keywords", "search_terms")
ASSETS = ("headlines", "descriptions")


@contextmanager
def database():
    conn = psycopg2.connect(required("DATABASE_URL"), connect_timeout=8)
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cursor:
            yield cursor
    finally:
        conn.close()


def memberships(user):
    with database() as cur:
        cur.execute("""
            select o.id, o.name, m.role from organizations o join memberships m
            on m.organization_id=o.id where m.user_id=%s order by o.name
        """, (user,))
        return cur.fetchall()


def accounts(org):
    with database() as cur:
        cur.execute("""
            select id, data, synced_from, synced_through from ad_accounts
            where organization_id=%s order by data->>'name'
        """, (org,))
        return [{**row["data"], "id": row["id"], "synced_from": row["synced_from"],
                 "synced_through": row["synced_through"]} for row in cur.fetchall()]


def connect_account(org, account):
    with database() as cur:
        cur.execute("""
            insert into platform_connections (organization_id, platform, credential_ref)
            values (%s, %s, 'backend_service_account')
            on conflict (organization_id, platform) do update set platform=excluded.platform
            returning id
        """, (org, account.platform))
        connection = cur.fetchone()["id"]
        cur.execute("""
            insert into ad_accounts (organization_id, connection_id, resource, data)
            values (%s,%s,%s,%s)
            on conflict (connection_id, resource) do update set
                synced_from=case when ad_accounts.data->>'currency' = excluded.data->>'currency'
                    then ad_accounts.synced_from end,
                synced_through=case when ad_accounts.data->>'currency' = excluded.data->>'currency'
                    then ad_accounts.synced_through end,
                data=excluded.data
            returning id
        """, (org, connection, account.resource, Json(account.model_dump())))
        return cur.fetchone()["id"]


def snapshot(org, account_id, start, end, currency):
    data = {}
    with database() as cur:
        cur.execute("set transaction isolation level repeatable read, read only")
        for table in RESOURCES:
            cur.execute(sql.SQL("""
                select resource, parent, data from {} where organization_id=%s and account_id=%s
                order by resource
            """).format(sql.Identifier(table)), (org, account_id))
            data[table] = cur.fetchall()
        cur.execute("""
            select kind, resource, day, currency, spend::double precision as spend,
                   impressions, clicks, interactions, conversions, value
            from performance_daily where organization_id=%s and account_id=%s
            and day between %s and %s and currency=%s order by day
        """, (org, account_id, start, end, currency))
        data["performance"] = cur.fetchall()
    return data


def write_assets(cur, table, key_cols, scope, creative):
    identifiers = sql.SQL(",").join(map(sql.Identifier, (*key_cols, "kind", "position", "data")))
    predicate = sql.SQL(" and ").join(sql.SQL("{}=%s").format(sql.Identifier(key)) for key in key_cols)
    for kind in ASSETS:
        assets = creative.get(kind, [])
        if assets:
            query = sql.SQL("""
                insert into {} ({}) values %s on conflict ({}, kind, position)
                do update set data=excluded.data where {}.data is distinct from excluded.data
            """).format(sql.Identifier(table), identifiers,
                        sql.SQL(",").join(map(sql.Identifier, key_cols)), sql.Identifier(table))
            execute_values(cur, query, [(*scope, kind, i, Json(asset)) for i, asset in enumerate(assets)])
        cur.execute(sql.SQL("delete from {} where {} and kind=%s and position >= %s").format(
            sql.Identifier(table), predicate), (*scope, kind, len(assets)))


def write_import(cur, org, account_id, batch, start, end, currency):
    for table in RESOURCES:
        items = getattr(batch, table)
        if items:
            query = sql.SQL("""
                insert into {} (organization_id, account_id, resource, parent, data) values %s
                on conflict (organization_id, account_id, resource)
                do update set parent=excluded.parent, data=excluded.data
            """).format(sql.Identifier(table))
            execute_values(cur, query, [(org, account_id, item.resource, item.parent, Json(item.data))
                                        for item in items])
    for ad in batch.ads:
        if ad.data.get("creative") is not None:
            write_assets(cur, "ad_assets", ("organization_id", "account_id", "resource"),
                         (org, account_id, ad.resource), ad.data["creative"])
    cur.execute("""
        create temporary table import_daily (like performance_daily including defaults) on commit drop
    """)
    if batch.performance:
        execute_values(cur, """
            insert into import_daily (organization_id,account_id,kind,resource,day,currency,
                                      spend,impressions,clicks,interactions,conversions,value) values %s
        """, [(org, account_id, row.kind, row.resource, row.day, row.currency, row.spend,
               row.impressions, row.clicks, row.interactions, row.conversions, row.value)
              for row in batch.performance])
    cur.execute("""
        insert into performance_daily
        select * from import_daily where true
        on conflict (organization_id,account_id,kind,resource,day,currency) do update set
            spend=excluded.spend, impressions=excluded.impressions, clicks=excluded.clicks,
            interactions=excluded.interactions, conversions=excluded.conversions, value=excluded.value
    """)
    cur.execute("""
        delete from performance_daily p where organization_id=%s and account_id=%s
        and day between %s and %s and currency=%s
        and not exists (select 1 from import_daily i
            where (i.kind,i.resource,i.day,i.currency)=(p.kind,p.resource,p.day,p.currency))
    """, (org, account_id, start, end, currency))
    cur.execute("""
        update ad_accounts set synced_from=least(synced_from,%s::date),
        synced_through=greatest(synced_through,%s::date)
        where organization_id=%s and id=%s
    """, (start, end, org, account_id))


def drafts(org, account_id):
    with database() as cur:
        cur.execute("""
            select resource, creative, version, updated_at from draft_revisions
            where organization_id=%s and account_id=%s order by updated_at desc
        """, (org, account_id))
        return cur.fetchall()


def save_draft(org, account_id, resource, user, creative, version):
    with database() as cur:
        scope = (org, account_id, resource)
        cur.execute("""
            select 1 from ads where organization_id=%s and account_id=%s and resource=%s
            and data->>'type'='RESPONSIVE_SEARCH_AD'
        """, scope)
        if not cur.fetchone():
            raise LookupError
        if version is None:
            cur.execute("""
                insert into draft_revisions (organization_id,account_id,resource,created_by,creative)
                values (%s,%s,%s,%s,%s)
                on conflict (organization_id,account_id,resource) do nothing returning id,version
            """, (*scope, user, Json(creative)))
        else:
            cur.execute("""
                update draft_revisions set creative=%s,version=version+1
                where organization_id=%s and account_id=%s and resource=%s and version=%s
                returning id,version
            """, (Json(creative), *scope, version))
        row = cur.fetchone()
        if not row:
            raise ValueError
        write_assets(cur, "draft_assets", ("organization_id", "draft_id"), (org, row["id"]), creative)
        return row["version"]


def discard_draft(org, account_id, resource, version):
    with database() as cur:
        cur.execute("""
            delete from draft_revisions where organization_id=%s and account_id=%s
            and resource=%s and version=%s
        """, (org, account_id, resource, version))
        return bool(cur.rowcount)


def sync_history(org, account_id):
    with database() as cur:
        cur.execute("""
            select status, counts, error, created_at, finished_at from sync_runs
            where organization_id=%s and account_id=%s order by created_at desc limit 20
        """, (org, account_id))
        return cur.fetchall()
