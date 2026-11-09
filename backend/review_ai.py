import json
import os

import httpx

DEFAULT_MODEL = "openai/gpt-oss-120b"
FOCUS_PROMPTS = {
    "performance": "Interpret the biggest week-over-week changes and identify which campaigns contributed.",
    "strategies": (
        "Compare observed results across campaign types and bidding strategies where known. "
        "Account for differences in spend, conversion volume, and campaign objectives when known. "
        "Explain what makes the comparison inconclusive where applicable."
    ),
    "decline": (
        "Investigate declining results. Separate traffic volume, click cost, and conversion changes "
        "where supported. If there is no meaningful decline, say so. Give checks that could distinguish hypotheses."
    ),
    "optimization": (
        "Recommend up to three prioritized actions to check or test manually. For each, give the "
        "supporting metric, what to check or change, and how to judge the result."
    ),
}
INSTRUCTIONS = (
    "Analyze the supplied Google Ads evidence for a store operator in at most 250 words. Use plain text. "
    "Use only supplied metrics. Treat absent values as unknown. "
    "Distinguish hypotheses from observed changes and mention delayed conversion attribution. "
    "Do not claim causation, estimate savings, or recommend automatic changes. "
    "Zero recorded conversions alone are not evidence of a tracking problem. "
    "Refer to campaigns by their supplied aliases and signals by position. Campaign and search term totals can overlap. "
    "The campaign sample is partial. Configuration is current only, so do not infer historical changes from it. "
    "A comparison across campaigns is observational. Flag thin samples and avoid unsupported conclusions."
)

class ReviewAIError(Exception):
    pass


def enabled():
    return bool(os.getenv("GROQ_API_KEY", "").strip())


def explain(review, currency, focus="performance"):
    # Keep identifiers, customer search queries, and campaign names out of the request.
    evidence = {key: review[key] for key in ("start", "end", "prior_start", "prior_end", "current", "previous")}
    evidence["currency"] = currency
    campaigns = review["drivers"]["campaigns"]
    if focus == "decline":
        ranked = sorted(campaigns, key=lambda item: item["conversion_delta"])
    else:
        ranked = sorted(campaigns, key=lambda item: max(item["current"]["spend"], item["previous"]["spend"]),
                        reverse=True)
    campaign_labels = {}
    evidence["campaigns"] = []
    for index, item in enumerate(ranked[:8], 1):
        alias = f"Campaign {index}"
        campaign_labels[alias] = item["label"]
        evidence["campaigns"].append({
            "alias": alias,
            **{key: item[key] for key in ("campaign_type", "bidding_strategy", "daily_budget", "current", "previous")},
        })
    evidence["signals"] = [
        {key: item[key] for key in ("kind", "current", "previous", "spend_delta", "conversion_delta")}
        for item in review["actions"]
    ]
    model = os.getenv("GROQ_REVIEW_MODEL", "").strip() or DEFAULT_MODEL
    try:
        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '').strip()}"},
            json={
                "model": model,
                "max_completion_tokens": 2000,
                "messages": [
                    {"role": "system", "content": f"{INSTRUCTIONS} {FOCUS_PROMPTS[focus]}"},
                    {"role": "user", "content": json.dumps(evidence, allow_nan=False)},
                ],
            },
            timeout=45,
        )
        if response.status_code == 429:
            raise ReviewAIError("Groq's rate limit was reached. Try again later. The review evidence is still available.")
        response.raise_for_status()
        choice = response.json()["choices"][0]
        notes = choice["message"]["content"]
        if choice["finish_reason"] == "stop" and isinstance(notes, str) and notes.strip():
            return {"summary": notes.strip(), "model": model, "campaign_labels": campaign_labels}
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        pass
    raise ReviewAIError("AI summary is temporarily unavailable. The review evidence is still available.")
