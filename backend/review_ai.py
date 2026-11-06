import json
import os

import httpx


class ReviewAIError(Exception):
    pass


def enabled():
    return bool(os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_REVIEW_MODEL"))


def explain(review, currency):
    # Keep identifiers, customer search queries, and campaign names out of the request.
    evidence = {key: review[key] for key in ("start", "end", "prior_start", "prior_end", "current", "previous")}
    evidence["currency"] = currency
    evidence["signals"] = [
        {key: item[key] for key in ("kind", "current", "previous", "spend_delta", "conversion_delta")}
        for item in review["actions"]
    ]
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            json={
                "model": os.environ["OPENAI_REVIEW_MODEL"],
                "store": False,
                "max_output_tokens": 900,
                "instructions": (
                    "Summarize the supplied Google Ads evidence for a store operator in at most 150 words. "
                    "Use plain text. Describe observed changes and up to two manual checks. "
                    "Use only supplied metrics. Treat absent values as unknown. "
                    "Distinguish hypotheses from observed changes and mention delayed conversion attribution. "
                    "Do not claim causation, estimate savings, or recommend automatic changes. "
                    "Refer to signals by their position if needed. Campaign and search term totals can overlap."
                ),
                "input": json.dumps(evidence, allow_nan=False),
            },
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        notes = "\n".join(
            part["text"] for item in data["output"] if item["type"] == "message"
            for part in item["content"] if part["type"] == "output_text"
        ).strip()
        if data["status"] == "completed" and notes:
            return notes
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        pass
    raise ReviewAIError("AI summary is temporarily unavailable. The review evidence is still available.")
