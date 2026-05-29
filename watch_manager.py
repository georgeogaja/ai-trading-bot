"""Watch manager: activates temporary higher-frequency monitoring for candidate tickers.

This module exposes functions to activate/deactivate watch mode and a research cache
for Perplexity results with a 6-hour TTL.
"""
import os
import json
import requests
from datetime import datetime, timedelta
from loguru import logger
from typing import Dict, Any

from db import get_research_cache, save_research_cache

_research_cache: Dict[str, Dict[str, Any]] = {}


def cache_research(key: str, data: Any, ttl_hours: int = 6):
    _research_cache[key] = {
        "data": data,
        "expires_at": datetime.now() + timedelta(hours=ttl_hours),
    }
    logger.debug(f"Cached research key={key} ttl_hours={ttl_hours}")


def get_cached_research(key: str):
    entry = _research_cache.get(key)
    if not entry:
        return None
    if datetime.now() >= entry["expires_at"]:
        del _research_cache[key]
        return None
    return entry["data"]


def build_research_query(symbol: str, technical: dict, macro: dict) -> str:
    candidate_note = technical.get("notes") or technical.get("summary") or "Potential options setup"
    trigger = technical.get("suggested_option", {}).get("trigger_price") or technical.get("price")
    stop = technical.get("suggested_option", {}).get("stop_loss_stock_price")
    macro_signal = macro.get("macro_signal", "unknown")

    query = (
        f"Research {symbol} for a short-term options setup. "
        f"What are the latest catalysts, earnings, analyst sentiment, sector strength, and macro risks? "
        f"Current technical note: {candidate_note}. "
        f"Expected trigger around {trigger}. Stop area around {stop}. "
        f"Macro context: {macro_signal}. "
        f"Summarize news, analyst headlines, and any events that could affect the name in the next 1-3 days."
    )
    return query


def fetch_perplexity_research(query: str) -> dict:
    api_key = os.getenv("PERPLEXITY_API_KEY")
    api_url = os.getenv("PERPLEXITY_API_URL", "https://www.perplexity.ai/api/search")
    if not api_key:
        logger.warning("Perplexity API key missing; skipping external research call.")
        return {
            "summary": "Perplexity research unavailable (PERPLEXITY_API_KEY not configured).",
            "details": [],
        }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }
    payload = {
        "query": query,
        "source": "api",
        "top_k": 3,
    }

    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
        return {
            "summary": data.get("summary") or data.get("answer") or "Perplexity research returned no summary.",
            "raw": data,
        }
    except Exception as exc:
        logger.warning(f"Perplexity request failed: {exc}")
        return {
            "summary": "Perplexity research failed or returned invalid response.",
            "details": [],
        }


def research_top_candidates(candidates: list, macro: dict, limit: int = 3) -> list:
    """Run or reuse cached research for the best candidate setups."""
    if not candidates:
        return []

    sorted_candidates = sorted(
        candidates,
        key=lambda item: float(item.get("confidence", 0) or 0),
        reverse=True,
    )
    top_candidates = sorted_candidates[:limit]
    results = []

    for candidate in top_candidates:
        symbol = candidate.get("symbol")
        if not symbol:
            continue

        cached = get_research_cache(symbol)
        if cached:
            logger.info(f"Research cache HIT for {symbol}")
            results.append({
                "symbol": symbol,
                "cached": True,
                "research": cached,
            })
            continue

        query = build_research_query(symbol, candidate.get("technical", {}), macro)
        research = fetch_perplexity_research(query)
        save_research_cache(symbol, query, "perplexity", research, ttl_hours=6)
        results.append({
            "symbol": symbol,
            "cached": False,
            "research": research,
        })
        logger.info(f"Research cache MISS for {symbol} — stored new result")

    return results


def should_activate_watch(symbol: str, technical: dict, trade_plan: dict) -> tuple[bool, list]:
    """Decision helper: return True if watch mode should enter WATCH MODE.

    The bot needs at least 4 of 6 strict conditions:
    1) Price within 1.5% of trigger
    2) Relative volume > 1.5x average
    3) RSI momentum improving
    4) ADX > 20
    5) Trend aligned with moving averages
    6) Risk/reward >= 2:1
    """
    reasons = []
    hits = 0

    price = float(technical.get("price", 0) or 0)
    suggested = technical.get("suggested_option", {}) or {}
    trigger = suggested.get("trigger_price") or technical.get("price")
    try:
        trigger = float(trigger or 0)
    except Exception:
        trigger = 0

    if price > 0 and trigger > 0:
        pct = abs(price - trigger) / trigger * 100
        if pct <= 1.5:
            hits += 1
            reasons.append("Price within 1.5% of trigger")
        else:
            reasons.append(f"Price {pct:.1f}% from trigger")
    else:
        reasons.append("Missing trigger or price")

    vol_ratio = float(technical.get("vol_ratio", 0) or 0)
    if vol_ratio >= 1.5:
        hits += 1
        reasons.append("Relative volume > 1.5x average")
    else:
        reasons.append(f"Volume ratio {vol_ratio:.2f} below 1.5")

    rsi_momentum = float(technical.get("rsi_momentum", 0) or 0)
    if rsi_momentum > 0:
        hits += 1
        reasons.append(f"RSI momentum improving ({rsi_momentum:+.2f})")
    else:
        reasons.append(f"RSI momentum not improving ({rsi_momentum:+.2f})")

    adx = float(technical.get("adx", 0) or 0)
    if adx > 20:
        hits += 1
        reasons.append(f"ADX > 20 ({adx:.1f})")
    else:
        reasons.append(f"ADX below 20 ({adx:.1f})")

    trend_aligned = bool(technical.get("trend_aligned", False))
    if trend_aligned:
        hits += 1
        reasons.append("Trend aligned with moving averages")
    else:
        reasons.append("Trend not aligned with moving averages")

    rr = float(technical.get("risk_reward_ratio", 0) or 0)
    if rr >= 2:
        hits += 1
        reasons.append(f"Risk/reward >= 2:1 ({rr:.2f})")
    else:
        reasons.append(f"Risk/reward below 2:1 ({rr:.2f})")

    passed = hits >= 4
    if passed:
        logger.info(f"WATCH MODE candidate {symbol} passed {hits}/6 criteria: {', '.join(reasons)}")
    else:
        logger.debug(f"WATCH MODE candidate {symbol} failed {hits}/6 criteria: {', '.join(reasons)}")
    return passed, reasons


def notify_watch_activation(symbols: list, webhook_send):
    try:
        msg = f"Watch Mode activated for symbols: {', '.join(symbols)}"
        logger.info(msg)
        if webhook_send:
            webhook_send(msg)
    except Exception as e:
        logger.warning(f"Failed to send watch activation notification: {e}")
