"""Perplexity-powered stock research for the trading bot.

Provides live news, catalyst, and analyst-sentiment context for a symbol.
Results are cached in SQLite (research_cache table) with a 6-hour TTL so a
30-symbol scan never hits the API more than once per symbol per session.

Fails safely in every error path:
  - PERPLEXITY_API_KEY not set  → returns {}  (logged at DEBUG)
  - API call raises              → returns {}  (logged at WARNING)
  - Unexpected parse error       → returns {}  (logged at WARNING)

Nothing in the trading pipeline should ever crash because this module
returned empty — callers must treat {} as "research unavailable".
"""

import os
from datetime import datetime
from loguru import logger

from db import get_research_cache, save_research_cache

PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "").strip()

_RESEARCH_PROMPT = """
Analyze {symbol} for a short-term swing options trade.

Provide concise, factual information on:
- Latest catalysts (earnings, product launches, analyst upgrades/downgrades, macro events)
- Analyst sentiment and recent price target changes
- Sector and AI-theme tailwinds or headwinds
- Upcoming earnings date (if known)
- Notable options flow or institutional activity
- Key risk factors for the next 30 days
- Overall swing trade probability (1-10 with one sentence justification)

Keep the response under 350 words. Focus only on actionable, time-sensitive information.
"""


def _get_client():
    """Lazily import and construct the Perplexity client (OpenAI-compatible SDK)."""
    from openai import OpenAI
    return OpenAI(
        api_key=PERPLEXITY_API_KEY,
        base_url="https://api.perplexity.ai",
    )


def research_symbol(symbol: str, ttl_hours: int = 6) -> dict:
    """
    Return Perplexity research for a symbol.

    Cache flow:
      1. If a fresh DB entry exists → return it immediately (no API call).
      2. Otherwise fetch live, save to DB, return result.
      3. On any failure → return {} so callers degrade gracefully.

    Returns:
        dict with keys: symbol, summary, fetched_at
        or {} on any failure / missing key.
    """
    if not PERPLEXITY_API_KEY:
        logger.debug(
            f"Perplexity research skipped for {symbol}: PERPLEXITY_API_KEY not configured"
        )
        return {}

    # ── Cache check ───────────────────────────────────────────
    try:
        cached = get_research_cache(symbol)
        if cached:
            logger.info(f"Perplexity research cache HIT for {symbol}")
            return cached.get("data") or {}
    except Exception as exc:
        logger.warning(f"Research cache read failed for {symbol}: {exc}")

    # ── Live fetch ────────────────────────────────────────────
    try:
        client = _get_client()
        prompt = _RESEARCH_PROMPT.format(symbol=symbol)
        response = client.chat.completions.create(
            model="sonar",
            messages=[{"role": "user", "content": prompt}],
        )
        summary = (response.choices[0].message.content or "").strip()
        if not summary:
            logger.warning(f"Perplexity returned empty response for {symbol}")
            return {}

        result = {
            "symbol": symbol,
            "summary": summary,
            "fetched_at": datetime.now().isoformat(),
        }

        # ── Cache save ────────────────────────────────────────
        try:
            save_research_cache(
                symbol=symbol,
                query=prompt,
                source="perplexity",
                data=result,
                ttl_hours=ttl_hours,
            )
        except Exception as exc:
            logger.warning(f"Research cache save failed for {symbol}: {exc}")

        logger.info(f"Perplexity research fetched for {symbol} ({len(summary)} chars)")
        return result

    except Exception as exc:
        logger.warning(f"Perplexity research failed for {symbol}: {exc}")
        return {}


# ── CLI convenience ───────────────────────────────────────────
# Usage: python ai_research.py NVDA
if __name__ == "__main__":
    import sys

    sym = sys.argv[1].upper() if len(sys.argv) > 1 else "NVDA"
    data = research_symbol(sym)
    if data:
        print(f"\n=== {sym} Research ===")
        print(data.get("summary", "No summary available"))
        print(f"\n[cached_at: {data.get('fetched_at', 'N/A')}]")
    else:
        print(f"No research available for {sym} (check PERPLEXITY_API_KEY in .env)")
