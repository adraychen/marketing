from datetime import timedelta


def previous_period(start, end):
    return start - (end - start + timedelta(days=1)), start - timedelta(days=1)


def ratio(numerator, denominator):
    return numerator / denominator if numerator is not None and denominator else None


def summarize(rows, currency):
    rows = list(rows)
    if any(row["currency"] != currency for row in rows):
        raise ValueError("Performance currencies must match the selected account.")
    result = {key: sum(row[key] for row in rows) for key in
              ("spend", "impressions", "clicks", "interactions", "conversions")}
    value = None
    if rows and all(row["value"] is not None for row in rows):
        value = sum(row["value"] for row in rows)
    return {
        **result,
        "value": value,
        "ctr": ratio(result["clicks"], result["impressions"]),
        "cpc": ratio(result["spend"], result["clicks"]),
        "conversion_rate": ratio(result["conversions"], result["interactions"]),
        "cpa": ratio(result["spend"], result["conversions"]),
        "roas": ratio(value, result["spend"]),
    }
