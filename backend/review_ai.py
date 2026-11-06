import json
import os

import httpx

DEFAULT_MODEL = "openai/gpt-oss-20b"
INSTRUCTIONS = (
    "Summarize the supplied Google Ads evidence for a store operator in at most 150 words. "
    "Use plain text. Describe observed changes and up to two manual checks. "
    "Use only supplied metrics. Treat absent values as unknown. "
    "Distinguish hypotheses from observed changes and mention delayed conversion attribution. "
    "Do not claim causation, estimate savings, or recommend automatic changes. "
    "Zero recorded conversions alone are not evidence of a tracking problem. "
    "Refer to signals by their position if needed. Campaign and search term totals can overlap."
)

class ReviewAIError(Exception):
    pass


def enabled():
    return bool(os.getenv("GROQ_API_KEY", "").strip())


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
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '').strip()}"},
            json={
                "model": os.getenv("GROQ_REVIEW_MODEL", "").strip() or DEFAULT_MODEL,
                "max_completion_tokens": 1200,
                "messages": [
                    {"role": "system", "content": INSTRUCTIONS},
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
            return notes.strip()
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        pass
    raise ReviewAIError("AI summary is temporarily unavailable. The review evidence is still available.")
