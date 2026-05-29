"""Watch manager: activates temporary higher-frequency monitoring for candidate tickers.

This module exposes functions for watch-mode activation logic and research caching.
Perplexity research is fetched via ai_research.research_symbol, which uses the correct
OpenAI-compatible endpoint and handles DB caching internally.
"""
from datetime import datetime, timedelta
from loguru import logger
from typing import Dict, Any

from db import get_research_cache

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


def research_top_candidates(candidates: list, macro: dict, limit: int = 3) -> list:
    """Fetch or reuse cached Perplexity research for the top candidate setups.

    Delegates to ai_research.research_symbol, which uses the correct
    OpenAI-compatible Perplexity endpoint and handles DB caching internally.
    Returns {} per symbol on any failure — never raises.
    """
    if not candidates:
        return []

    top_candidates = sorted(
        candidates,
        key=lambda item: float(item.get("confidence", 0) or 0),
        reverse=True,
    )[:limit]

    results = []
    for candidate in top_candidates:
        symbol = candidate.get("symbol")
        if not symbol:
            continue

        # Check DB before calling to preserve accurate cached/fresh flag for logging
        was_cached = bool(get_research_cache(symbol))

        try:
            from ai_research import research_symbol
            research = research_symbol(symbol)
        except Exception as exc:
            logger.warning(f"Research unavailable for {symbol}: {exc}")
            research = {}

        status = "HIT" if was_cached else ("FETCHED" if research else "UNAVAILABLE")
        logger.info(f"Research {status} for {symbol}")
        results.append({"symbol": symbol, "cached": was_cached, "research": research})

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
