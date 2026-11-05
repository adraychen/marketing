import argparse
import os
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from psycopg2.extras import Json

from backend import repositories as repo
from backend.config import MODE
from backend.connectors.google_ads import GoogleAdsConnector, GoogleAdsError
from backend.models import Account, ImportBatch


class SyncBusy(Exception):
    pass


def configured_org():
    value = os.getenv("GOOGLE_ADS_ORGANIZATION_ID")
    return str(UUID(value)) if value else None


def connector(client):
    return GoogleAdsConnector.from_environment(client, os.getenv("GOOGLE_ADS_MANAGER_ID", ""))


def import_dates(account_row, account, backfill, refresh, today=None):
    today = today or datetime.now(ZoneInfo(account.timezone)).date()
    through = account_row["synced_through"]
    if through:
        start = min(today - timedelta(days=refresh - 1), through + timedelta(days=1))
    else:
        start = today - timedelta(days=backfill - 1)
    return start, today


def run_sync(org, account_row):
    account_id = str(account_row["id"])
    if org != configured_org():
        raise GoogleAdsError("Google Ads credentials are unavailable for this organization.")
    with repo.database() as lock:
        lock.execute("select pg_try_advisory_xact_lock(hashtextextended(%s, 0)) as acquired", (account_id,))
        if not lock.fetchone()["acquired"]:
            raise SyncBusy
        lock.execute("""select data,synced_through from ad_accounts
                     where organization_id=%s and id=%s for no key update""",
                     (org, account_id))
        current = lock.fetchone()
        account = Account.model_validate(current["data"])
        with repo.database() as audit:
            audit.execute("""update sync_runs set status='failed', finished_at=now(),
                error='Previous worker exited before finishing.' where organization_id=%s and account_id=%s
                and status='running'""", (org, account_id))
            audit.execute("""insert into sync_runs (organization_id,account_id,status)
                values (%s,%s,'running') returning id""", (org, account_id))
            run_id = audit.fetchone()["id"]
        try:
            backfill = int(os.getenv("SYNC_BACKFILL_DAYS", "180"))
            refresh = int(os.getenv("SYNC_REFRESH_DAYS", "30"))
            if not 1 <= refresh <= backfill <= 730:
                raise GoogleAdsError("Configure sync windows between 1 and 730 days, with refresh within backfill.")
            start, end = import_dates(current, account, backfill, refresh)
            with httpx.Client(timeout=45) as client:
                ads = connector(client)
                campaigns = ads.campaigns(account)
                content = ads.content(account)
                performance, terms = ads.performance(account, start, end)
            batch = ImportBatch(campaigns=campaigns, **content, performance=performance, search_terms=terms)
            repo.write_import(lock, org, account_id, batch, start, end, account.currency)
            counts = {key: len(getattr(batch, key)) for key in type(batch).model_fields}
            lock.execute("""update sync_runs set status='succeeded', counts=%s, finished_at=now()
                where id=%s""", (Json(counts), run_id))
        except Exception as exc:
            reason = str(exc) if isinstance(exc, GoogleAdsError) else (
                "Import failed before any changes were committed. Check report compatibility and database setup."
            )
            with repo.database() as audit:
                audit.execute("""update sync_runs set status='failed', error=%s, finished_at=now()
                    where id=%s""", (reason, run_id))
            raise GoogleAdsError(reason) from None
    return counts


def main():
    parser = argparse.ArgumentParser(description="Refresh connected Google Ads accounts once.")
    parser.parse_args()
    org = configured_org()
    if MODE != "live" or not org:
        parser.exit(2, "Configure live mode and GOOGLE_ADS_ORGANIZATION_ID before scheduling sync.\n")
    failed = False
    for account in repo.accounts(org):
        try:
            counts = run_sync(org, account)
            print(f"{account['id']} synced {sum(counts.values())} records.")
        except SyncBusy:
            print(f"{account['id']} already syncing.")
        except GoogleAdsError as exc:
            failed = True
            print(f"{account['id']}: {exc}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
