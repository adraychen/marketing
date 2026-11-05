import os
from contextlib import contextmanager
from datetime import date
from uuid import uuid4

import httpx
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from backend import repositories as repo
from backend.connectors.google_ads import GoogleAdsConnector
from backend.models import Account, Daily, ImportBatch, Resource


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires a migrated Supabase test database")
def test_postgres_upserts_and_rls(monkeypatch):
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    org, other, user = (str(uuid4()) for _ in range(3))
    day = date(2026, 11, 5)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            @contextmanager
            def test_database():
                yield cur

            monkeypatch.setattr(repo, "database", test_database)
            cur.execute("insert into organizations (id,name) values (%s,'Test A'),(%s,'Test B')", (org, other))
            cur.execute("insert into auth.users (id) values (%s)", (user,))
            cur.execute("insert into memberships (organization_id,user_id) values (%s,%s)", (org, user))
            account = Account(resource="fixture-account", name="Fixture", currency="CAD", timezone="America/Toronto")
            aid = repo.connect_account(org, account)
            assert repo.connect_account(org, account) == aid
            assert repo.connect_account(other, account) != aid

            campaign = Resource(resource="fixture-campaign", data={"name": "Campaign"})
            group = Resource(resource="fixture-group", parent=campaign.resource, data={})
            creative = {"headlines": [{"text": "Original", "pin": "HEADLINE_1"}], "descriptions": []}
            ad = Resource(resource="fixture-ad", parent=group.resource,
                          data={"type": "RESPONSIVE_SEARCH_AD", "creative": creative})
            daily = Daily(kind="campaigns", resource=campaign.resource, day=day, currency="CAD", spend=10,
                          impressions=50, clicks=5, interactions=5, conversions=1, value=30)
            batch = ImportBatch(campaigns=[campaign], ad_groups=[group], ads=[ad], performance=[daily])
            repo.write_import(cur, org, aid, batch, day, day, "CAD")
            cur.execute("drop table import_daily")
            cur.execute("select id from ad_assets where organization_id=%s", (org,))
            original_asset_id = cur.fetchone()["id"]
            edited = {"headlines": [{"text": "Revised", "pin": "HEADLINE_1"}], "descriptions": []}
            assert repo.save_draft(org, aid, ad.resource, user, edited, None) == 1

            daily.spend = 12
            repo.write_import(cur, org, aid, batch, day, day, "CAD")
            cur.execute("select id, data from ad_assets where organization_id=%s", (org,))
            assert cur.fetchall() == [{"id": original_asset_id, "data": creative["headlines"][0]}]
            cur.execute("select spend from performance_daily where organization_id=%s", (org,))
            assert [row["spend"] for row in cur.fetchall()] == [12]
            assert repo.drafts(org, aid)[0]["creative"] == edited
            assert repo.drafts(other, aid) == []

            cur.execute("set local role authenticated")
            cur.execute("select set_config('request.jwt.claim.sub', %s, true)", (user,))
            cur.execute("select id::text from organizations where id in (%s,%s)", (org, other))
            assert cur.fetchall() == [{"id": org}]
            cur.execute("select count(*) from ad_accounts where organization_id in (%s,%s)", (org, other))
            assert cur.fetchone()["count"] == 1
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.skipif(os.getenv("TEST_LIVE_GOOGLE_ADS") != "1", reason="requires authorized Google Ads credentials")
def test_live_read_only_connection():
    with httpx.Client(timeout=30) as client:
        connector = GoogleAdsConnector.from_environment(client, os.getenv("GOOGLE_ADS_MANAGER_ID", ""))
        accounts = connector.discover()
        assert accounts, "Grant the service account read access to an eligible Google Ads account."
        assert isinstance(connector.campaigns(accounts[0]), list)
