"""Optional campaign evidence. Read-only providers; tenant-scoped local snapshots."""
from datetime import datetime, timedelta, timezone

from psycopg2.errors import UndefinedColumn, UndefinedTable
from psycopg2.extras import Json
from pydantic import BaseModel, Field, ConfigDict

from backend.commerce import CONTEXT_QUERY, shopify_query
from backend.connectors.content import ContentError
from backend.repositories import database

PRODUCTS_QUERY = """
query EvaluationProducts($after: String, $costs: Boolean!) {
  productVariants(first: 100, after: $after) {
    nodes {
      id sku price
      product { id handle title onlineStoreUrl }
      inventoryItem @include(if: $costs) { unitCost { amount currencyCode } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class CostModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    shipping_per_order: float = Field(ge=0, allow_inf_nan=False)
    payment_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    payment_per_order: float = Field(ge=0, allow_inf_nan=False)
    other_per_order: float = Field(ge=0, allow_inf_nan=False)
    note: str = Field(min_length=1, max_length=500)


def save_snapshot(cur, org, source, account, data, start=None, end=None):
    cur.execute("""insert into marketing_evaluation_snapshots
        (organization_id,source,account,start_day,end_day,data) values (%s,%s,%s,%s,%s,%s)
        on conflict(organization_id,source,account) do update set
        start_day=excluded.start_day,end_day=excluded.end_day,data=excluded.data,collected_at=now()""",
        (org, source, account, start, end, Json(data)))


def import_products(client, org, shop, token):
    context = shopify_query(client, shop, token, CONTEXT_QUERY)
    scopes = {row['handle'] for row in context['currentAppInstallation']['accessScopes']}
    if not scopes & {'read_products', 'write_products'}:
        raise ContentError('Product matching needs read_products. Existing order reporting is unaffected.')
    costs = bool(scopes & {'read_inventory', 'write_inventory'})
    cost_note = None
    rows, cursor, seen = [], None, set()
    while True:
        try:
            data = shopify_query(client, shop, token, PRODUCTS_QUERY, {'after': cursor, 'costs': costs})['productVariants']
        except ContentError as exc:
            if not costs or rows or 'access denied' not in str(exc).lower():
                raise
            costs = False
            cost_note = 'Shopify denied inventory cost access; product matching remains available.'
            data = shopify_query(client, shop, token, PRODUCTS_QUERY, {'after': cursor, 'costs': False})['productVariants']
        rows.extend(data['nodes'])
        page = data['pageInfo']
        if not page['hasNextPage']:
            break
        cursor = page['endCursor']
        if not cursor or cursor in seen:
            raise ContentError('Shopify product pagination incomplete; previous snapshot retained.')
        seen.add(cursor)
    with database() as cur:
        save_snapshot(cur, org, 'shopify_products', shop,
                      {'variants': rows, 'cost_access': costs, 'cost_note': cost_note, 'currency': context['shop']['currencyCode']})
    return len(rows)


def evidence(org, start, end):
    result = {'snapshots': [], 'orders': [], 'cost_models': [], 'warnings': [], 'runs': []}
    try:
        with database() as cur:
            cur.execute('select source,account,start_day,end_day,data,collected_at from marketing_evaluation_snapshots where organization_id=%s', (org,))
            result['snapshots'] = cur.fetchall()
            for row in result['snapshots']:
                row['complete'] = row['source'] == 'shopify_products' or bool(row['start_day'] and row['start_day'] <= start <= end <= row['end_day'])
                row['stale'] = datetime.now(timezone.utc) - row['collected_at'] > timedelta(hours=48)
            cur.execute("""select o.shop,o.order_id,o.currency,o.amount,o.utm,o.evaluation
                from marketing_shopify_orders o join marketing_commerce_coverage c
                on c.organization_id=o.organization_id and c.account=o.shop and c.source='shopify_orders'
                where o.organization_id=%s and o.eligible and o.attributed
                and c.start_day<=%s and c.end_day>=%s
                and (o.created_at at time zone c.timezone)::date between %s and %s""", (org,start,end,start,end))
            result['orders'] = cur.fetchall()
            cur.execute('select account_id,campaign_id,data,updated_at from marketing_campaign_cost_models where organization_id=%s', (org,))
            result['cost_models'] = cur.fetchall()
            cur.execute("""select distinct on (source) source,status,records,message,finished_at
                from marketing_content_sync_runs where organization_id=%s
                and source in ('shopify_products','ga4_landing') order by source,finished_at desc""", (org,))
            result['runs'] = cur.fetchall()
    except (UndefinedTable, UndefinedColumn):
        return {'snapshots': [], 'orders': [], 'cost_models': [],
                'warnings': ['Apply 20261112000100_campaign_evaluation.sql and run content sync for product evidence.']}
    return result


def save_cost_model(org, account, campaign, model):
    with database() as cur:
        cur.execute("""insert into marketing_campaign_cost_models(organization_id,account_id,campaign_id,data)
            values(%s,%s,%s,%s) on conflict(organization_id,account_id,campaign_id)
            do update set data=excluded.data,updated_at=now()""", (org,account,campaign,Json(model.model_dump())))
