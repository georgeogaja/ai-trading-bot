"""Market Regime Engine — Phase 3A.

Top of the decision funnel:

    MARKET REGIME   ← (this module)
        ↓
    SECTOR STRENGTH        (not yet implemented)
        ↓
    TOP STOCKS IN SECTOR   (not yet implemented)
        ↓
    OPTIONS LIQUIDITY      (not yet implemented)
        ↓
    AI SCORING
        ↓
    EXECUTION

Before any individual stock is evaluated, the bot reads the broad market
direction from SPY and QQQ trend, filtered by the VIX/VXX volatility gauge,
and classifies the environment as BULLISH, NEUTRAL or BEARISH.

This module never places or blocks a trade by itself. It only produces a set
of gates that the existing scan/execution pipeline applies:

  BULLISH  → normal long / call setups allowed
  NEUTRAL  → reduced position size + stronger confirmation required
  BEARISH  → aggressive long / call setups blocked
             (put logic is reserved for a later phase and is NOT implemented
              here — bearish simply stands down on longs/calls)

Paper trading, DRY_RUN, EXECUTE mode, volume/risk sizing, Discord alerts and
the scheduler are all untouched by this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from config import MACRO

# ─────────────────────────────────────────────────────────────
# REGIME LABELS
# ─────────────────────────────────────────────────────────────
BULLISH = "BULLISH"
NEUTRAL = "NEUTRAL"
BEARISH = "BEARISH"

# Index ETFs used as the primary market-direction signal.
_INDEX_SYMBOLS = ("SPY", "QQQ")
# Volatility gauges in priority order. VIX is the true index; VXX is a tradeable
# ETN used only as a fallback when VIX data is unavailable.
_VIX_SYMBOL = "^VIX"
_VXX_SYMBOL = "VXX"

# Combined SPY+QQQ trend score runs 0–6 (each index contributes 0–3).
# >= 4 leans bullish, <= 1 leans bearish, the middle band is neutral.
_BULLISH_SCORE_MIN = 4
_BEARISH_SCORE_MAX = 1

# Per-regime gates consumed by the scheduler / executor.
_SIZE_MULTIPLIER = {BULLISH: 1.0, NEUTRAL: 0.5, BEARISH: 0.0}
_CONFIDENCE_BUMP = {BULLISH: 0, NEUTRAL: 1, BEARISH: 0}


@dataclass
class MarketRegime:
    """Resolved market regime plus the gates downstream code should apply."""

    regime: str
    score: int                       # combined SPY+QQQ trend score, 0–6
    spy: dict                        # per-index trend detail
    qqq: dict
    vix: float | None                # latest volatility reading (VIX or VXX)
    vol_source: str                  # "VIX", "VXX" or "NONE"
    reasons: list[str] = field(default_factory=list)

    # ── Gates ────────────────────────────────────────────────
    @property
    def size_multiplier(self) -> float:
        """Fraction of the normal dollar allocation this regime permits.

        BULLISH = 1.0 (normal), NEUTRAL = 0.5 (reduced), BEARISH = 0.0
        (longs/calls stand down entirely).
        """
        return _SIZE_MULTIPLIER[self.regime]

    @property
    def confidence_bump(self) -> int:
        """Extra confidence points required before executing in this regime.

        NEUTRAL adds +1 so only stronger-confirmation setups get through.
        """
        return _CONFIDENCE_BUMP[self.regime]

    @property
    def blocks_long_calls(self) -> bool:
        """True when aggressive long / call setups must be blocked (BEARISH)."""
        return self.regime == BEARISH

    @property
    def summary_line(self) -> str:
        """One-line human summary, e.g. for the trade journal."""
        vix_txt = f"{self.vix:.2f}" if self.vix is not None else "n/a"
        return (
            f"MARKET REGIME: {self.regime} | score={self.score}/6 | "
            f"{self.vol_source}={vix_txt}"
        )


def _index_trend(symbol: str) -> dict:
    """Score one index ETF's trend on a 0–3 scale.

    +1 price above its 20-day average  (short-term momentum)
    +1 price above its 50-day average  (intermediate trend)
    +1 50-day average above 200-day    (long-term trend / golden-cross posture)

    Returns a detail dict; score is 0 and trend "UNKNOWN" if data is missing.
    """
    detail = {
        "symbol": symbol,
        "price": None,
        "sma20": None,
        "sma50": None,
        "sma200": None,
        "score": 0,
        "trend": "UNKNOWN",
        "return_21d": None,   # trailing ~1-month % return, for relative-strength checks
    }
    try:
        import yfinance as yf

        hist = yf.Ticker(symbol).history(period="1y")
        close = hist["Close"].dropna()
        if close.empty:
            logger.warning(f"Regime: no price history for {symbol}")
            return detail

        price = float(close.iloc[-1])
        sma20 = float(close.tail(20).mean())
        sma50 = float(close.tail(50).mean())
        sma200 = float(close.tail(200).mean())

        score = 0
        if price > sma20:
            score += 1
        if price > sma50:
            score += 1
        if sma50 > sma200:
            score += 1

        trend = "UP" if score >= 2 else ("FLAT" if score == 1 else "DOWN")

        # Trailing ~21-trading-day (≈1 month) return, used downstream to gauge a
        # stock's strength relative to the market.
        return_21d = None
        if len(close) > 21:
            past = float(close.iloc[-22])
            if past:
                return_21d = round((price - past) / past * 100, 2)

        detail.update(
            price=round(price, 2),
            sma20=round(sma20, 2),
            sma50=round(sma50, 2),
            sma200=round(sma200, 2),
            score=score,
            trend=trend,
            return_21d=return_21d,
        )
    except Exception as exc:
        logger.warning(f"Regime: trend fetch failed for {symbol}: {exc}")
    return detail


def _volatility_reading() -> tuple[float | None, str]:
    """Return (value, source) for the volatility gauge.

    Prefers the real VIX index; falls back to the VXX ETN's last close when VIX
    is unavailable. Returns (None, "NONE") if neither resolves.
    """
    try:
        import yfinance as yf

        for symbol, source in ((_VIX_SYMBOL, "VIX"), (_VXX_SYMBOL, "VXX")):
            try:
                close = yf.Ticker(symbol).history(period="5d")["Close"].dropna()
                if not close.empty:
                    return round(float(close.iloc[-1]), 2), source
            except Exception:
                continue
    except Exception as exc:
        logger.warning(f"Regime: volatility fetch failed: {exc}")
    return None, "NONE"


def get_market_regime() -> MarketRegime:
    """Resolve the current market regime from SPY/QQQ trend + VIX/VXX.

    Direction comes from the combined SPY+QQQ trend score; the VIX/VXX reading
    is a one-way risk-off filter — it can only downgrade the regime, never
    upgrade it:

      VIX > vix_crisis   → force BEARISH
      VIX > vix_elevated → cap at NEUTRAL (a bullish read is knocked down)

    The VXX fallback is a raw ETN price, so its absolute level is not comparable
    to VIX thresholds; when VXX is the only gauge available it is reported for
    visibility but does not trigger the threshold downgrades.
    """
    spy = _index_trend(_INDEX_SYMBOLS[0])
    qqq = _index_trend(_INDEX_SYMBOLS[1])
    vix, vol_source = _volatility_reading()

    score = int(spy["score"] + qqq["score"])
    reasons: list[str] = []

    # ── Primary direction from index trend ────────────────────
    if score >= _BULLISH_SCORE_MIN:
        regime = BULLISH
    elif score <= _BEARISH_SCORE_MAX:
        regime = BEARISH
    else:
        regime = NEUTRAL
    reasons.append(
        f"SPY {spy['trend']} ({spy['score']}/3), QQQ {qqq['trend']} "
        f"({qqq['score']}/3) → trend score {score}/6"
    )

    # ── Volatility risk-off filter (one-way: only downgrades) ──
    if vol_source == "VIX" and vix is not None:
        if vix > MACRO["vix_crisis"]:
            if regime != BEARISH:
                reasons.append(
                    f"VIX {vix:.1f} > crisis {MACRO['vix_crisis']} → forced BEARISH"
                )
            regime = BEARISH
        elif vix > MACRO["vix_elevated"]:
            if regime == BULLISH:
                reasons.append(
                    f"VIX {vix:.1f} > elevated {MACRO['vix_elevated']} → "
                    f"capped at NEUTRAL"
                )
                regime = NEUTRAL
            else:
                reasons.append(f"VIX {vix:.1f} elevated (risk-off)")
    elif vol_source == "VXX" and vix is not None:
        reasons.append(f"VIX unavailable; VXX={vix:.2f} (informational only)")
    else:
        reasons.append("Volatility gauge unavailable — trend-only classification")

    return MarketRegime(
        regime=regime,
        score=score,
        spy=spy,
        qqq=qqq,
        vix=vix,
        vol_source=vol_source,
        reasons=reasons,
    )


def log_regime(regime: MarketRegime) -> None:
    """Emit the required regime log lines.

    Always prints one of:
      MARKET REGIME: BULLISH
      MARKET REGIME: NEUTRAL
      MARKET REGIME: BEARISH
    followed by the supporting detail.
    """
    logger.info(f"MARKET REGIME: {regime.regime}")
    for reason in regime.reasons:
        logger.info(f"  Regime detail: {reason}")
