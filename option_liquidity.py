"""Options Liquidity Filter — Phase 3D.

Funnel position:

    MARKET REGIME      (3A)
        ↓
    SECTOR STRENGTH    (3C)
        ↓
    TOP STOCKS         (preference)
        ↓
    OPTIONS LIQUIDITY  ← (this module)  — final pre-execution contract gate
        ↓
    AI SCORING / EXECUTION

This module is PURE policy: it takes a quote dict for a single option contract
and decides whether the contract is liquid enough to trade, and scores
contracts so the most liquid near-the-money one can be preferred. It performs
no network I/O and imports nothing from the broker/executor, so it is trivially
testable and free of circular imports.

A quote dict uses these keys (any may be None if the feed omits them):
    bid, ask, bid_size, ask_size, volume, open_interest

Rejection reason codes (stable strings used for logs/Discord):
    INVALID_QUOTE       → quote data invalid (no bid/ask, ask <= bid, bid too low)
    WIDE_SPREAD         → bid/ask spread too wide
    LOW_OPEN_INTEREST   → open interest below threshold
    LOW_OPTION_VOLUME   → option volume below threshold
    MISSING_OI_DATA     → OI required but unavailable
    MISSING_VOLUME_DATA → volume required but unavailable
"""
from __future__ import annotations

from loguru import logger

from config import OPTIONS_LIQUIDITY

# Stable reason codes → human-readable messages (mirrors the required strings).
REASON_MESSAGES = {
    "INVALID_QUOTE": "option rejected: invalid quote data",
    "WIDE_SPREAD": "option rejected: wide spread",
    "LOW_OPEN_INTEREST": "option rejected: low open interest",
    "LOW_OPTION_VOLUME": "option rejected: low option volume",
    "MISSING_OI_DATA": "option rejected: open interest data unavailable",
    "MISSING_VOLUME_DATA": "option rejected: option volume data unavailable",
    "OK": "option approved: liquidity OK",
}


def _num(value):
    """Coerce to float, or None if not a usable number."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _derive(quote: dict) -> dict:
    """Compute bid/ask/mid/spread metrics from a raw quote dict."""
    bid = _num(quote.get("bid"))
    ask = _num(quote.get("ask"))
    volume = _num(quote.get("volume"))
    open_interest = _num(quote.get("open_interest"))

    mid = spread = spread_pct = None
    if bid is not None and ask is not None and ask > 0 and bid > 0:
        mid = round((bid + ask) / 2, 4)
        spread = round(ask - bid, 4)
        spread_pct = round(spread / mid, 4) if mid else None

    return {
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "open_interest": open_interest,
        "mid": mid,
        "spread": spread,
        "spread_pct": spread_pct,
    }


def evaluate_liquidity(quote: dict) -> dict:
    """Decide whether an option contract is liquid enough to trade.

    Returns a dict:
      approved        — bool
      code            — reason code (see REASON_MESSAGES)
      reason          — human-readable message
      details         — derived metrics (bid/ask/mid/spread/spread_pct/volume/OI)
    """
    cfg = OPTIONS_LIQUIDITY
    d = _derive(quote or {})

    # Liquidity filtering can be globally disabled (still report metrics).
    if not cfg.get("enabled", True):
        return {"approved": True, "code": "OK", "reason": REASON_MESSAGES["OK"], "details": d}

    bid, ask, mid = d["bid"], d["ask"], d["mid"]

    # 1. Valid quote required: bid > 0, ask > bid, bid >= floor.
    if cfg.get("require_quote", True):
        min_bid = float(cfg.get("min_bid", 0.0))
        if bid is None or ask is None or bid <= 0 or ask <= bid or bid < min_bid:
            return {
                "approved": False, "code": "INVALID_QUOTE",
                "reason": REASON_MESSAGES["INVALID_QUOTE"], "details": d,
            }

    # 2. Spread: pass if within the % cap, OR — only for cheap contracts where
    #    the minimum tick inflates the percentage — within the absolute cap.
    if d["spread_pct"] is not None:
        within_pct = d["spread_pct"] <= float(cfg.get("max_spread_pct", 0.10))
        cheap = mid is not None and mid <= float(cfg.get("cheap_mid_threshold", 1.50))
        within_abs = cheap and d["spread"] <= float(cfg.get("max_spread_abs", 0.15))
        if not (within_pct or within_abs):
            return {
                "approved": False, "code": "WIDE_SPREAD",
                "reason": REASON_MESSAGES["WIDE_SPREAD"], "details": d,
            }

    # 3. Open interest.
    oi = d["open_interest"]
    if oi is None:
        if cfg.get("require_open_interest_data", False):
            return {
                "approved": False, "code": "MISSING_OI_DATA",
                "reason": REASON_MESSAGES["MISSING_OI_DATA"], "details": d,
            }
    elif oi < float(cfg.get("min_open_interest", 0)):
        return {
            "approved": False, "code": "LOW_OPEN_INTEREST",
            "reason": REASON_MESSAGES["LOW_OPEN_INTEREST"], "details": d,
        }

    # 4. Option volume.
    vol = d["volume"]
    if vol is None:
        if cfg.get("require_volume_data", False):
            return {
                "approved": False, "code": "MISSING_VOLUME_DATA",
                "reason": REASON_MESSAGES["MISSING_VOLUME_DATA"], "details": d,
            }
    elif vol < float(cfg.get("min_option_volume", 0)):
        return {
            "approved": False, "code": "LOW_OPTION_VOLUME",
            "reason": REASON_MESSAGES["LOW_OPTION_VOLUME"], "details": d,
        }

    return {"approved": True, "code": "OK", "reason": REASON_MESSAGES["OK"], "details": d}


def liquidity_score(quote: dict, strike: float | None = None,
                    target_strike: float | None = None) -> float:
    """Score a contract for PREFERENCE (higher = better) among candidates.

    Rewards tight spreads, high open interest, high volume, and near-the-money
    strikes. Returns -inf for a contract that fails the liquidity gate so it is
    never preferred over a tradeable one.
    """
    verdict = evaluate_liquidity(quote)
    if not verdict["approved"]:
        return float("-inf")

    d = verdict["details"]
    score = 0.0

    # Tighter spread is better (spread_pct of 0 → +10, 10% → ~0).
    if d["spread_pct"] is not None:
        score += max(0.0, 10.0 - d["spread_pct"] * 100.0)
    # Open interest and volume (log-ish reward, capped).
    if d["open_interest"]:
        score += min(d["open_interest"] / 100.0, 20.0)
    if d["volume"]:
        score += min(d["volume"] / 50.0, 20.0)
    # Near-the-money preference: penalise strike distance from the target.
    if strike is not None and target_strike:
        dist_pct = abs(float(strike) - float(target_strike)) / float(target_strike)
        score += max(0.0, 5.0 - dist_pct * 100.0)

    return round(score, 4)


def describe(details: dict) -> str:
    """One-line liquidity summary for logs/journal."""
    bid = details.get("bid")
    ask = details.get("ask")
    spr = details.get("spread_pct")
    vol = details.get("volume")
    oi = details.get("open_interest")
    spr_txt = f"{spr*100:.1f}%" if spr is not None else "n/a"
    return (
        f"bid={bid} ask={ask} spread={spr_txt} "
        f"vol={vol if vol is not None else 'n/a'} "
        f"OI={oi if oi is not None else 'n/a'}"
    )
