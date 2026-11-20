from backend.metrics import ratio
from backend.connectors.reddit_ads import EVENTS, VIDEO

ADDITIVE = ("spend", "impressions", "clicks", "purchases", "purchase_value", *VIDEO,
            *[f"conversion_{event}_{suffix}" for event in EVENTS for suffix in ("clicks", "views")])


def totals(rows, conversion="purchases"):
    if conversion not in ("purchases", *[f"conversion_{e}_{s}" for e in EVENTS for s in ("clicks", "views")]):
        raise ValueError("Choose an explicit Reddit conversion event and attribution type.")
    base = [r for r in rows if r.get("breakdown", "base") == "base"]
    currencies = {r.get("currency") for r in base}
    definitions = {str(sorted(r.get("attribution", {}).items())) for r in base}
    compatible = len(currencies) <= 1 and len(definitions) <= 1
    result = {}
    for key in ADDITIVE:
        values = [r["metrics"].get(key) for r in base]
        result[key] = sum(values) if compatible and values and all(v is not None for v in values) else None
    result.update(ctr=ratio(result["clicks"], result["impressions"]), cpc=ratio(result["spend"], result["clicks"]),
                  cpm=ratio(result["spend"] * 1000 if result["spend"] is not None else None, result["impressions"]),
                  cpa=ratio(result["spend"], result[conversion]), conversion_rate=ratio(result[conversion], result["clicks"]),
                  roas=ratio(result["purchase_value"], result["spend"]),
                  video_completion_rate=ratio(result["video_watched_100_percent"], result["video_started"]),
                  reach=base[0]["metrics"].get("reach") if len(base) == 1 else None,
                  frequency=base[0]["metrics"].get("frequency") if len(base) == 1 else None)
    return result
