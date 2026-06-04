"""
agents/strategy_engine.py
George's complete A+ trading strategy implemented in Python.
Mirrors the ThinkorSwim script indicators and signal logic.
Uses Claude to make the final trade decision with full context.
"""

import os
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import yfinance as yf
import ta
from loguru import logger
from config import ACCOUNT, HARD_RULES, PUT_SUPPORT, SIGNAL_WEIGHTS, ET_TIMEZONE
from db import get_active_watchlist, get_mistake_patterns, log_signal
from ai_client import get_claude_client, log_fallback_mode


# ─────────────────────────────────────────────────────────────
# TECHNICAL INDICATOR CALCULATIONS
# Mirrors George's ThinkorSwim script exactly
# ─────────────────────────────────────────────────────────────

def _rth_session_elapsed_fraction(bar_timestamp) -> float:
    """Fraction in (0, 1] of the regular-hours session (09:30–16:00 ET) elapsed
    *right now* for the given daily bar.

    Returns 1.0 — i.e. "treat as a complete session, do not project" — whenever:
      • the bar is not today's (it is an already-closed prior session), or
      • the current time is before the open or at/after the close.
    Returns a value in (0, 1) only while today's RTH session is in progress, so
    callers know the latest bar's volume is still accumulating. The fraction is
    floored so a few prints right after the open can't scale to an implausible
    full-day estimate. Fails safe to 1.0 (no projection) on any error.
    """
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(ET_TIMEZONE))
    except Exception:
        return 1.0

    bar_date = getattr(bar_timestamp, "date", lambda: None)()
    if bar_date != now.date():
        return 1.0  # latest bar is a completed prior session

    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    if now <= open_t or now >= close_t:
        return 1.0  # outside RTH → today's bar is (effectively) complete

    total_secs   = (close_t - open_t).total_seconds()
    elapsed_secs = (now - open_t).total_seconds()
    return max(0.15, min(1.0, elapsed_secs / total_secs))


def _adjust_latest_volume_ratio(df: pd.DataFrame) -> None:
    """Make the latest bar's VOL_ratio an apples-to-apples figure.

    yfinance's most recent daily bar is the *in-progress* session while the
    market is open: its cumulative volume is only partial, so dividing it by a
    full-day 50-day average understates relative volume the entire session (the
    raw ratio only approaches the truth at the close). When the latest bar is a
    live session we instead compare a TIME-PROJECTED full-day volume estimate
    against the average of the 50 COMPLETED sessions before it. Outside market
    hours the latest bar is already complete and this is a no-op.

    Mutates df['VOL_ratio'] (and df['VOL_avg50']) for the final row in place.
    """
    if df is None or len(df) < 51:
        return

    frac = _rth_session_elapsed_fraction(df.index[-1])
    if frac >= 1.0:
        return  # completed session — nothing to project

    completed_avg = df['Volume'].iloc[-51:-1].mean()  # 50 sessions before today
    if pd.isna(completed_avg) or completed_avg <= 0:
        return

    partial_volume   = float(df['Volume'].iloc[-1])
    projected_volume = partial_volume / frac
    last = df.index[-1]
    df.at[last, 'VOL_avg50'] = completed_avg
    df.at[last, 'VOL_ratio'] = round(projected_volume / completed_avg, 4)


def calculate_indicators(symbol: str, period: str = "6mo") -> pd.DataFrame:
    """
    Download price data and calculate all indicators shown
    in George's ThinkorSwim charts:
    - EMA 50, 72, 125, 200
    - RSI (14) Wilder's smoothing
    - ADX (14)
    - Volume vs 50-day average
    - ATR, support/resistance, and trend slope for swing option entries
    """
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period)

        if df.empty or len(df) < 50:
            logger.warning(f"{symbol}: Insufficient data")
            return None

        # EMAs (exactly as shown on George's charts)
        df['EMA_50']  = ta.trend.EMAIndicator(df['Close'], window=50).ema_indicator()
        df['EMA_72']  = ta.trend.EMAIndicator(df['Close'], window=72).ema_indicator()
        df['EMA_125'] = ta.trend.EMAIndicator(df['Close'], window=125).ema_indicator()
        df['EMA_200'] = ta.trend.EMAIndicator(df['Close'], window=200).ema_indicator()

        # RSI — Wilder's smoothing, 14-period
        df['RSI'] = ta.momentum.RSIIndicator(df['Close'], window=14).rsi()

        # ADX — 14-period with DI lines
        adx_indicator  = ta.trend.ADXIndicator(df['High'], df['Low'], df['Close'], window=14)
        df['ADX']      = adx_indicator.adx()
        df['DI_plus']  = adx_indicator.adx_pos()
        df['DI_minus'] = adx_indicator.adx_neg()

        # ATR for volatility-aware stops
        df['ATR'] = ta.volatility.AverageTrueRange(
            df['High'], df['Low'], df['Close'], window=14
        ).average_true_range()

        # Volume analysis
        df['VOL_avg50'] = df['Volume'].rolling(window=50).mean()
        df['VOL_ratio'] = df['Volume'] / df['VOL_avg50']
        # The latest daily bar may be today's still-open session (partial
        # cumulative volume); rescale its relative-volume figure to a full-session,
        # apples-to-apples comparison so intraday scans don't systematically
        # under-read volume (which both starves the A+ volume points and fails the
        # watch-mode relative-volume test).
        _adjust_latest_volume_ratio(df)

        # MACD (bonus indicator)
        macd = ta.trend.MACD(df['Close'])
        df['MACD']        = macd.macd()
        df['MACD_signal'] = macd.macd_signal()
        df['MACD_hist']   = macd.macd_diff()

        # Support and resistance for swing setup validation
        support_window = HARD_RULES.get('support_lookback', 20)
        df['support_20'] = df['Low'].rolling(window=support_window).min()
        df['resistance_20'] = df['High'].rolling(window=support_window).max()
        df['range_20'] = df['resistance_20'] - df['support_20']

        # Trend slope for EMAs
        df['EMA_50_slope'] = df['EMA_50'].diff(5)
        df['EMA_72_slope'] = df['EMA_72'].diff(5)

        # 52-week metrics
        df['52wk_high'] = df['Close'].rolling(window=252).max()
        df['52wk_low']  = df['Close'].rolling(window=252).min()
        df['drawdown_from_high'] = (df['52wk_high'] - df['Close']) / df['52wk_high'] * 100

        return df

    except Exception as e:
        logger.error(f"{symbol} indicator error: {e}")
        return None


def detect_rsi_divergence(df: pd.DataFrame, lookback: int = 21) -> str:
    """
    Detect RSI bullish or bearish divergence.
    Bullish: price makes lower low, RSI makes higher low
    Bearish: price makes higher high, RSI makes lower high
    """
    if len(df) < lookback * 2:
        return "NONE"

    recent = df.tail(lookback * 2)
    prices = recent['Close'].values
    rsi    = recent['RSI'].values

    valid = ~(np.isnan(prices) | np.isnan(rsi))
    prices, rsi = prices[valid], rsi[valid]

    if len(prices) < lookback * 2:
        return "NONE"

    mid = len(prices) // 2
    p_first, p_last = prices[:mid], prices[mid:]
    r_first, r_last = rsi[:mid], rsi[mid:]

    price_drop = (min(p_first) - min(p_last)) / min(p_first) if min(p_first) else 0
    if min(p_last) < min(p_first) and min(r_last) > min(r_first) and price_drop > 0.008:
        return "BULLISH"

    price_rise = (max(p_last) - max(p_first)) / max(p_first) if max(p_first) else 0
    if max(p_last) > max(p_first) and max(r_last) < max(r_first) and price_rise > 0.008:
        return "BEARISH"

    return "NONE"


def detect_falling_wedge(df: pd.DataFrame, lookback: int = 30) -> bool:
    """
    Detect falling wedge pattern:
    - Lower highs converging toward lower lows
    - Both trendlines sloping down but converging
    - Volume should contract and range should compress
    """
    if len(df) < lookback:
        return False

    recent = df.tail(lookback)
    highs = recent['High'].values
    lows  = recent['Low'].values

    x = np.arange(len(highs))
    high_slope = np.polyfit(x, highs, 1)[0]
    low_slope  = np.polyfit(x, lows, 1)[0]

    first_half = slice(0, len(highs) // 2)
    second_half = slice(len(highs) // 2, len(highs))
    range_first = highs[first_half].max() - lows[first_half].min()
    range_last = highs[second_half].max() - lows[second_half].min()

    return (
        high_slope < 0 and
        low_slope < 0 and
        high_slope < low_slope and
        range_last < range_first * 0.85
    )


def calculate_bias(row: pd.Series) -> str:
    """
    BIAS calculation matching George's ThinkorSwim script.
    BULL: price > EMA50 > EMA72 > EMA125
    BEAR: price < EMA50 < EMA72 < EMA125
    """
    price = row['Close']
    e50   = row['EMA_50']
    e72   = row['EMA_72']
    e125  = row['EMA_125']

    if any(pd.isna([e50, e72, e125])):
        return "NEUTRAL"

    if price > e50 > e72 > e125:
        return "BULL"
    elif price < e50 < e72 < e125:
        return "BEAR"
    else:
        return "NEUTRAL"


def calculate_signal_scores(row: pd.Series, divergence: str, falling_wedge: bool) -> tuple:
    """
    Calculate BULL and BEAR scores matching George's platform.
    Returns (bull_score, bear_score, put_score)
    """
    bias = calculate_bias(row)
    rsi  = row.get('RSI', 50)
    adx  = row.get('ADX', 0)
    vol  = row.get('VOL_ratio', 1)
    di_p = row.get('DI_plus', 0)
    di_n = row.get('DI_minus', 0)

    bull = 0
    bear = 0

    # BULL scoring
    if bias == "BULL":                             bull += SIGNAL_WEIGHTS["bias_bull"]
    if HARD_RULES["rsi_ideal_min"] <= rsi <= HARD_RULES["rsi_ideal_max"]:
        bull += SIGNAL_WEIGHTS["rsi_ideal_zone"]
    elif 60 < rsi < HARD_RULES["rsi_overbought"]:
        bull += SIGNAL_WEIGHTS["rsi_bullish"]
    if adx >= HARD_RULES["adx_trend_min"]:
        bull += SIGNAL_WEIGHTS["adx_trending"]
    if adx > HARD_RULES["adx_strong"]:
        bull += SIGNAL_WEIGHTS["adx_strong"]
    if di_p > di_n:
        bull += SIGNAL_WEIGHTS["di_positive"]
    if divergence == "BULLISH":
        bull += SIGNAL_WEIGHTS["rsi_divergence"]
    if falling_wedge:
        bull += SIGNAL_WEIGHTS["falling_wedge"]
    if vol > HARD_RULES["min_volume_ratio"]:
        bull += SIGNAL_WEIGHTS["volume_expansion"]
    if row.get('Close', 0) > row.get('EMA_50', 0) and row.get('Close', 0) > row.get('support_20', 0):
        bull += SIGNAL_WEIGHTS["trend_support"]
    if row.get('drawdown_from_high', 0) >= HARD_RULES["min_drawdown_pct"] * 100:
        bull += SIGNAL_WEIGHTS["drawdown_bonus"]
    if vol > 1.2:
        bull += SIGNAL_WEIGHTS["above_avg_volume"]

    # BEAR scoring
    if bias == "BEAR":
        bear += SIGNAL_WEIGHTS["bias_bear"]
    if rsi > HARD_RULES["rsi_overbought"]:
        bear += SIGNAL_WEIGHTS["rsi_overbought_bear"]
    if rsi > 75:
        bear += SIGNAL_WEIGHTS["rsi_severe_ob"]
    if adx > HARD_RULES["adx_strong"] and bias == "BEAR":
        bear += SIGNAL_WEIGHTS["adx_bear_trend"]
    if di_n > di_p:
        bear += SIGNAL_WEIGHTS["di_negative"]

    put_score = min(bear + (3 if rsi > 70 else 0), 10)

    return min(bull, 10), min(bear, 10), put_score


def get_days_to_earnings(symbol: str) -> int | None:
    """Calendar days until the next earnings date, or None if unknown.

    Uses yfinance's `.calendar` (no lxml dependency). Fail-open by design: any
    error or missing data returns None so callers never block a trade on bad or
    absent earnings data — only a positively-known, near date should gate.
    """
    try:
        cal = yf.Ticker(symbol).calendar or {}
        dates = cal.get("Earnings Date")
        if not dates:
            return None
        if not isinstance(dates, (list, tuple)):
            dates = [dates]
        today = datetime.now().date()
        upcoming = [d for d in dates if d and d >= today]
        if not upcoming:
            return None
        return (min(upcoming) - today).days
    except Exception as exc:
        logger.debug(f"Earnings date unavailable for {symbol}: {exc}")
        return None


def generate_signal(bull_score: int, bear_score: int, rsi: float, bias: str, adx: float) -> str:
    """
    Final signal matching George's ThinkorSwim output:
    A+ LONG | A+ SHORT | LONG | SHORT | NO SIGNAL
    """
    if rsi >= HARD_RULES["rsi_overbought"] and bias != "BEAR":
        return "A+ SHORT" if bear_score >= SIGNAL_WEIGHTS["min_score_aplus"] else "NO SIGNAL"

    if bias == "BULL" and bull_score >= SIGNAL_WEIGHTS["min_score_aplus"] and rsi <= HARD_RULES["rsi_ideal_max"] and adx >= HARD_RULES["adx_trend_min"]:
        return "A+ LONG"
    if bull_score >= SIGNAL_WEIGHTS["min_score_long"] and rsi < HARD_RULES["rsi_overbought"]:
        return "LONG"
    if bear_score >= SIGNAL_WEIGHTS["min_score_aplus"]:
        return "A+ SHORT"
    if bear_score >= SIGNAL_WEIGHTS["min_score_long"]:
        return "SHORT"
    return "NO SIGNAL"


def estimate_swing_option_plan(symbol: str, row: pd.Series, signal: str) -> dict:
    """
    Suggest option entry parameters for a swing options setup.
    Uses strike distance, expiry window, and volatility-aware stop placement.
    """
    price = float(row['Close'])
    rsi   = float(row.get('RSI', 50))
    atr   = float(row.get('ATR', price * 0.03)) if not pd.isna(row.get('ATR', np.nan)) else price * 0.03
    expiry_date = (datetime.now() + timedelta(days=HARD_RULES['options_expiry_days'])).strftime("%Y-%m-%d")

    # Keep strikes near the money (higher delta → higher probability the position
    # finishes in profit). Capped at HARD_RULES['max_strike_pct_otm'] and floored
    # at ~1% so we never reach for far-OTM lottos. Staying near ATM (rather than
    # ITM) keeps a single contract inside the per-trade dollar cap.
    otm_cap = HARD_RULES.get('max_strike_pct_otm', 0.05)
    if signal in ("A+ LONG", "LONG"):
        option_type = "CALL"
        strike_pct = min(otm_cap, max(0.01, 0.02 + (rsi - HARD_RULES['rsi_ideal_min']) / 360))
        strike = round(price * (1 + strike_pct), 2)
        stop_loss = round(max(price - atr * HARD_RULES['stop_loss_atr_multiplier'], row.get('support_20', price * 0.95)), 2)
    elif signal in ("A+ SHORT", "SHORT"):
        option_type = "PUT"
        strike_pct = min(otm_cap, max(0.01, 0.02 + (HARD_RULES['rsi_overbought'] - rsi) / 360))
        strike = round(price * (1 - strike_pct), 2)
        stop_loss = round(min(price + atr * HARD_RULES['stop_loss_atr_multiplier'], row.get('resistance_20', price * 1.05)), 2)
    else:
        option_type = "CALL"
        strike_pct = min(otm_cap, 0.03)
        strike = round(price * (1 + strike_pct), 2)
        stop_loss = round(price - atr * HARD_RULES['stop_loss_atr_multiplier'], 2)

    return {
        "option_type": option_type,
        "strike": strike,
        "strike_pct_otm": round(strike_pct * 100, 1),
        "expiry": expiry_date,
        "stop_loss_stock_price": stop_loss,
        "estimated_option_price_low": 0.00,
        "estimated_option_price_high": 0.00,
        "max_to_spend": ACCOUNT["total_capital"] * ACCOUNT["max_per_trade_pct"],
        "contracts": 1,
    }


def analyze_stock(symbol: str) -> dict:
    """
    Full analysis of a single stock.
    Returns signal data formatted exactly like George's ThinkorSwim display.
    """
    df = calculate_indicators(symbol)
    if df is None:
        return {"symbol": symbol, "signal": "ERROR", "reason": "No data"}

    latest      = df.iloc[-1]
    divergence  = detect_rsi_divergence(df)
    wedge       = detect_falling_wedge(df)
    bias        = calculate_bias(latest)
    rsi         = round(latest.get('RSI', 50), 1)
    adx         = round(latest.get('ADX', 0), 1)
    vol_ratio   = latest.get('VOL_ratio', 1)
    bull, bear, put_sc = calculate_signal_scores(latest, divergence, wedge)
    signal      = generate_signal(bull, bear, rsi, bias, adx)

    option_plan = estimate_swing_option_plan(symbol, latest, signal)

    vol_status = "LOW" if vol_ratio < 0.75 else ("HIGH" if vol_ratio > 1.25 else "OK")

    current_price = round(float(latest['Close']), 2)
    high_52wk     = round(float(latest.get('52wk_high', current_price)), 2)
    drawdown_pct  = round(float(latest.get('drawdown_from_high', 0)), 1)

    rsi_momentum = 0.0
    if len(df) >= 4:
        prior_rsi = df['RSI'].iloc[-4]
        if not pd.isna(prior_rsi):
            rsi_momentum = round(rsi - float(prior_rsi), 2)

    ema_125 = round(float(latest.get('EMA_125', 0)), 2)
    ema_50 = latest.get('EMA_50', np.nan)
    ema_72 = latest.get('EMA_72', np.nan)
    ema_125_val = latest.get('EMA_125', np.nan)
    trend_aligned = (
        not pd.isna(ema_50)
        and not pd.isna(ema_72)
        and not pd.isna(ema_125_val)
        and current_price > float(ema_50) > float(ema_72) > float(ema_125_val)
    )

    # Directional momentum lines (used for PUT/downside confirmation)
    di_plus  = round(float(latest.get('DI_plus', 0) or 0), 1)
    di_minus = round(float(latest.get('DI_minus', 0) or 0), 1)

    # Trailing ~21-day return for relative-strength checks vs the market.
    return_21d = None
    if len(df) > 21:
        past_close = df['Close'].iloc[-22]
        if not pd.isna(past_close) and float(past_close):
            return_21d = round((current_price - float(past_close)) / float(past_close) * 100, 2)

    # Direction-aware risk/reward. For a CALL the stop sits BELOW entry and the
    # reward target is overhead resistance; for a PUT the stop sits ABOVE entry
    # and the reward target is the downside support level. Computing this with
    # the wrong sign would make a PUT's R/R meaningless, so we branch on the
    # signal direction.
    stop_loss = option_plan.get('stop_loss_stock_price', current_price)
    is_bear_setup = signal in ("A+ SHORT", "SHORT")
    if is_bear_setup:
        downside_target = min(
            float(latest.get('support_20', current_price * 0.92)),
            current_price * 0.92,
        )
        risk = max(float(stop_loss) - current_price, 0.01)
        risk_reward_ratio = round((current_price - downside_target) / risk, 2) if risk > 0 else 0.0
    else:
        reward_target = max(
            float(latest.get('resistance_20', current_price * 1.08)),
            current_price * 1.08,
        )
        risk = max(current_price - float(stop_loss), 0.01)
        risk_reward_ratio = round((reward_target - current_price) / risk, 2) if risk > 0 else 0.0

    # ── Bearish / PUT qualification (Phase 3B) ────────────────
    # A PUT only qualifies when the SHORT side is genuinely confirmed, mirroring
    # how qualifies_aplus gates the LONG side. All conditions are required:
    #   - PUT support enabled
    #   - an A+ SHORT signal
    #   - bearish trend structure (BIAS == BEAR)
    #   - downside momentum (-DI > +DI)
    #   - bear score at/above the PUT threshold
    qualifies_aplus_put = (
        PUT_SUPPORT.get("enabled", False)
        and signal == "A+ SHORT"
        and bear >= PUT_SUPPORT.get("min_put_score", SIGNAL_WEIGHTS["min_score_aplus"])
    )
    if qualifies_aplus_put and PUT_SUPPORT.get("require_bear_bias", True):
        qualifies_aplus_put = qualifies_aplus_put and bias == "BEAR"
    if qualifies_aplus_put and PUT_SUPPORT.get("require_downside_momentum", True):
        qualifies_aplus_put = qualifies_aplus_put and di_minus > di_plus

    # ── LONG/CALL execution eligibility ───────────────────────
    # Mirrors the A+ LONG quality gates (BULL bias, RSI <= ideal max, ADX >=
    # trend min) but uses the configurable execution score threshold
    # (min_score_to_trade_long, currently 6) instead of the A+ label score (8).
    # This is what routes a setup to the AI/executor; the "A+ LONG" label itself
    # is unchanged and still requires a full 8. The quality gates are KEPT, so a
    # TREND-CONFIRMED 6 (BULL bias + ADX >= trend min + RSI in range) now trades,
    # while an extended (RSI > ideal max) or trendless (ADX < min) / non-BULL 6
    # still only watches.
    qualifies_trade_long = (
        signal in ("A+ LONG", "LONG")
        and bias == "BULL"
        and rsi <= HARD_RULES["rsi_ideal_max"]
        and adx >= HARD_RULES["adx_trend_min"]
        and bull >= SIGNAL_WEIGHTS["min_score_to_trade_long"]
    )

    # Format exactly like George's ThinkorSwim label
    signal_bar = (
        f"BIAS: {bias} | RSI {rsi} | ADX {adx} | "
        f"VOL {vol_status} | LIQ OK | EARN CLEAR | "
        f"PUT SCORE {put_sc} | {signal} B:{bull} S:{bear}"
    )

    return {
        "symbol":           symbol,
        "price":            current_price,
        "signal":           signal,
        "bull_score":       bull,
        "bear_score":       bear,
        "put_score":        put_sc,
        "rsi":              rsi,
        "rsi_momentum":     rsi_momentum,
        "adx":              adx,
        "di_plus":          di_plus,
        "di_minus":         di_minus,
        "bias":             bias,
        "return_21d":       return_21d,
        "vol_status":       vol_status,
        "vol_ratio":        round(float(vol_ratio), 2) if not pd.isna(vol_ratio) else 1.0,
        "rsi_divergence":   divergence,
        "falling_wedge":    wedge,
        "ema_50":           round(float(latest.get('EMA_50', 0)), 2),
        "ema_72":           round(float(latest.get('EMA_72', 0)), 2),
        "ema_125":          ema_125,
        "trend_aligned":    trend_aligned,
        "risk_reward_ratio": risk_reward_ratio,
        "drawdown_pct":     drawdown_pct,
        "high_52wk":        high_52wk,
        "signal_bar":       signal_bar,
        "qualifies_aplus":  signal == "A+ LONG" and rsi < HARD_RULES["rsi_overbought"],
        "qualifies_trade_long": qualifies_trade_long,
        "qualifies_aplus_put": qualifies_aplus_put,
        "suggested_option": option_plan,
    }


def merge_trade_plan_with_suggestion(claude_result: dict, suggested_option: dict) -> dict:
    """
    Combine Claude's trade plan with the internal suggested option plan.
    The bot will always fall back to its own swing option plan for missing values.
    """
    if not isinstance(claude_result, dict):
        return {"decision": "SKIP", "trade_plan": suggested_option}

    plan = claude_result.get("trade_plan", {}) or {}
    merged = {
        "option_type": plan.get("option_type") or suggested_option.get("option_type"),
        "strike": plan.get("strike") or suggested_option.get("strike"),
        "strike_pct_otm": plan.get("strike_pct_otm") or suggested_option.get("strike_pct_otm"),
        "expiry": plan.get("expiry") or suggested_option.get("expiry"),
        "estimated_option_price_low": plan.get("estimated_option_price_low", suggested_option.get("estimated_option_price_low", 0.0)),
        "estimated_option_price_high": plan.get("estimated_option_price_high", suggested_option.get("estimated_option_price_high", 0.0)),
        "max_to_spend": plan.get("max_to_spend") or suggested_option.get("max_to_spend"),
        "stop_loss_stock_price": plan.get("stop_loss_stock_price") or suggested_option.get("stop_loss_stock_price"),
        "contracts": plan.get("contracts") or suggested_option.get("contracts", 1),
        "catalyst": plan.get("catalyst") or suggested_option.get("catalyst", "Unknown catalyst"),
        "thesis": plan.get("thesis") or suggested_option.get("thesis", "Swing options setup based on trend and support"),
        "pattern": plan.get("pattern") or suggested_option.get("pattern", "swing setup"),
    }

    merged_plan = claude_result.copy()
    merged_plan["trade_plan"] = merged
    return merged_plan


# ─────────────────────────────────────────────────────────────
# TRADE DECISION ENGINE (deterministic core, optional AI overlay)
# ─────────────────────────────────────────────────────────────

def deterministic_trade_plan(symbol: str, technical: dict) -> dict:
    """Rule-based trade decision used when AI is unavailable.

    This is the deterministic core of the bot. `run_full_scan` only calls into
    the decision engine for setups that have ALREADY cleared the qualification
    and quality gates (qualifies_trade_long / qualifies_aplus_put), so a
    qualified setup here is a TRADE. Confidence comes straight from the
    rule-based signal score (bull for longs, bear for PUTs), which is on the
    same 1–10 scale as ACCOUNT['min_confidence_to_execute'] — so the downstream
    confidence gate, regime bump, volume/risk and liquidity controls all keep
    working unchanged with no AI in the loop.

    The returned trade_plan is the internal swing-option plan; downstream
    `merge_trade_plan_with_suggestion` fills any gaps from the same suggested
    plan, so the shape matches the AI path exactly.
    """
    suggested = technical.get("suggested_option", {}) or {}
    is_put = str(suggested.get("option_type", "CALL")).upper() == "PUT"
    base_score = technical.get("bear_score" if is_put else "bull_score", 0) or 0
    confidence = int(max(1, min(10, base_score)))
    direction = "bearish PUT" if is_put else "bullish CALL"

    thesis = (
        f"Deterministic {direction} setup on {symbol}: signal "
        f"{technical.get('signal')}, RSI {technical.get('rsi')}, "
        f"ADX {technical.get('adx')}, bias {technical.get('bias')}, "
        f"vol {technical.get('vol_ratio')}x, R/R "
        f"{technical.get('risk_reward_ratio')}. Qualified by the rule-based "
        f"engine (AI commentary unavailable)."
    )

    plan = dict(suggested)
    plan["thesis"] = thesis
    plan["catalyst"] = suggested.get("catalyst", "Rule-based technical setup")
    plan["pattern"] = suggested.get("pattern", "rule-based swing setup")

    return {
        "decision": "TRADE",
        "confidence": confidence,
        "reason": (
            "Deterministic fallback (AI unavailable): setup met all rule-based "
            "A+/strong criteria — proceeding on technicals and risk controls."
        ),
        "skip_reason": "",
        "trade_plan": plan,
        "mistake_risk": "",
        "lessons_applied": "Deterministic engine — no AI lesson review.",
    }


def get_claude_trade_plan(symbol: str, technical: dict, macro: dict,
                           account_info: dict) -> dict:
    """
    Returns a complete trade plan for a qualified setup.

    Deterministic-first: when no Claude client is available (no API key, SDK
    missing, or a runtime API failure) the bot falls back to
    `deterministic_trade_plan` so scanning, ranking and paper-trade execution
    continue uninterrupted. When a client IS available, Claude refines the
    decision using George's strategy, recent mistake patterns, and optional
    live Perplexity research — a purely additive overlay on the rule-based core.
    """
    client = get_claude_client()
    if client is None:
        log_fallback_mode()
        plan = deterministic_trade_plan(symbol, technical)
        logger.info(
            f"Deterministic decision for {symbol}: {plan['decision']} "
            f"(confidence {plan['confidence']}/10) — AI unavailable"
        )
        return plan

    # Get recent mistake patterns to inform the decision
    mistakes = get_mistake_patterns(days_back=30)
    mistake_summary = json.dumps(mistakes, indent=2)

    # Optional Perplexity research enrichment — fail-safe, never blocks execution
    research_context = "Not available (PERPLEXITY_API_KEY not configured or research failed)."
    try:
        from ai_research import research_symbol
        research = research_symbol(symbol)
        if research.get("summary"):
            research_context = research["summary"][:800]
    except Exception as exc:
        logger.debug(f"Perplexity research unavailable for {symbol}: {exc}")

    # Direction-aware guidance. The hard-rejection rules below are long-only;
    # for a bearish PUT setup they do not apply, so we tell Claude explicitly
    # what to confirm on the short side instead.
    suggested_type = technical['suggested_option'].get('option_type', 'CALL')
    if suggested_type == "PUT":
        direction_guidance = (
            "DIRECTION OF THIS SETUP: BEARISH PUT (short side).\n"
            "This is a swing PUT, NOT a long. The long-only rejection rules below "
            "(RSI >= 70 blocks longs, macro RED blocks new longs) DO NOT apply here.\n"
            "Approve only if downside is confirmed: bearish EMA structure "
            "(price < EMA50 < EMA72 < EMA125), negative directional momentum "
            "(-DI > +DI), and a credible breakdown level or bearish catalyst.\n"
            "Strike must be ATM to a maximum of 10% OTM BELOW the current price, and the "
            "stop loss is a STOCK PRICE ABOVE the entry. Keep option_type = \"PUT\"."
        )
    else:
        direction_guidance = (
            "DIRECTION OF THIS SETUP: BULLISH CALL (long side). "
            "Apply all of George's long-only hard rejection rules below."
        )

    prompt = f"""
You are George's autonomous AI trading agent. You manage a $10,000 swing options account
connected to Alpaca. You must follow George's strategy EXACTLY.

═══════════════════════════════════════════════════
GEORGE'S STRATEGY — NON-NEGOTIABLE RULES
═══════════════════════════════════════════════════

{direction_guidance}

HARD REJECTION CRITERIA (reject trade if ANY are true):
✗ RSI >= 70 → NEVER go long (overbought rejection)
✗ Strike > 15% OTM → NEVER — ATM to max 10% OTM only
✗ No defined stop loss → NEVER enter without stop
✗ Macro signal = RED → No new long positions
✗ Account already at 80% deployed → Wait for exits first

INTERNAL SUGGESTED OPTION PLAN:
- Option Type: {technical['suggested_option']['option_type']}
- Strike: ${technical['suggested_option']['strike']} ({technical['suggested_option']['strike_pct_otm']}% OTM)
- Expiry: {technical['suggested_option']['expiry']}
- Stop Loss Stock Price: ${technical['suggested_option']['stop_loss_stock_price']}
- Max Spend: ${technical['suggested_option']['max_to_spend']}

Use these suggested option values as a default. Only override them if you have a very strong reason and the result still meets all George's hard rules.

A+ SETUP REQUIRES ALL 6:
1. Pattern confirmed (falling wedge, cup/handle, RSI divergence, bull flag)
2. RSI between 40-65 (ideal: 45-60)
3. Price above key support level
4. Catalyst within 30 days (earnings, product launch, macro event)
5. Macro environment GREEN or YELLOW
6. ADX above 20 (trend has conviction)

STRIKE SELECTION:
- ATM to 10% OTM ONLY
- Target delta: 0.45-0.60
- Expiry: 75-90 days out for swing trades
- Order type: LIMIT at midpoint of bid/ask

POSITION SIZING for $10,000 account:
- Max per trade: $2,000 (20% of account)
- Max contracts: 1 (always 1 contract)
- Keep $3,000 cash reserve always
- Max 5 open positions total

STOP LOSS:
- Define as a STOCK PRICE level, not option price
- Falling wedge: close below breakout level
- General: 5-8% below entry stock price

═══════════════════════════════════════════════════
RECENT MISTAKE PATTERNS (LEARN FROM THESE)
═══════════════════════════════════════════════════
{mistake_summary}

═══════════════════════════════════════════════════
CURRENT ANALYSIS FOR: {symbol}
═══════════════════════════════════════════════════
Stock Price: ${technical['price']}
Signal: {technical['signal_bar']}
RSI: {technical['rsi']} | ADX: {technical['adx']} | BIAS: {technical['bias']}
RSI Divergence: {technical['rsi_divergence']}
Falling Wedge: {technical['falling_wedge']}
52-week High: ${technical['high_52wk']}
Drawdown from High: {technical['drawdown_pct']}%
EMA 50: ${technical['ema_50']} | EMA 72: ${technical['ema_72']}

SUGGESTED OPTION PLAN FROM INTERNAL SWING MODEL:
Option Type: {technical['suggested_option']['option_type']}
Strike: ${technical['suggested_option']['strike']} ({technical['suggested_option']['strike_pct_otm']}% OTM)
Expiry: {technical['suggested_option']['expiry']}
Stop Loss: ${technical['suggested_option']['stop_loss_stock_price']}
Max Spend: ${technical['suggested_option']['max_to_spend']}

MACRO CONDITIONS:
Oil: ${macro.get('oil', 'N/A')}
VIX: {macro.get('vix', 'N/A')}
10yr Yield: {macro.get('yield', 'N/A')}%
Macro Signal: {macro.get('signal', 'YELLOW')}

ACCOUNT STATUS:
Cash Available: ${account_info.get('cash', 0):.2f}
Open Positions: {account_info.get('open_positions', 0)}
Portfolio Value: ${account_info.get('portfolio_value', 0):.2f}

═══════════════════════════════════════════════════
LIVE RESEARCH & CATALYST CONTEXT (Perplexity AI)
═══════════════════════════════════════════════════
{research_context}

═══════════════════════════════════════════════════
DECISION REQUIRED
═══════════════════════════════════════════════════
Based on ALL the above, should we place a trade?

RESPOND WITH VALID JSON ONLY:
{{
  "decision": "TRADE" | "SKIP" | "WAIT",
  "confidence": 1-10,
  "reason": "2-3 sentence explanation",
  "skip_reason": "if SKIP — which hard rule was violated",
  "trade_plan": {{
    "option_type": "CALL" | "PUT",
    "strike": 0.00,
    "strike_pct_otm": 0.0,
    "expiry": "YYYY-MM-DD",
    "estimated_option_price_low": 0.00,
    "estimated_option_price_high": 0.00,
    "max_to_spend": 0.00,
    "stop_loss_stock_price": 0.00,
    "target_1_option_price": 0.00,
    "target_2_option_price": 0.00,
    "catalyst": "specific catalyst within 30 days",
    "thesis": "3 sentences on why this trade makes sense",
    "pattern": "which pattern triggered this"
  }},
  "mistake_risk": "any mistake pattern from history this might repeat",
  "lessons_applied": "which past mistake lessons influenced this decision"
}}
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
    except Exception as exc:
        # AI became unavailable at call time (network/auth/rate limit). Do NOT
        # drop the setup — fall back to the deterministic decision so execution
        # continues exactly as it would with no key configured.
        logger.warning(
            f"Claude call failed for {symbol}: {exc} — using deterministic fallback"
        )
        log_fallback_mode(str(exc))
        return deterministic_trade_plan(symbol, technical)

    try:
        result = json.loads(response.content[0].text)
        return result
    except json.JSONDecodeError:
        # The AI responded but the JSON was malformed. This is a bad AI answer,
        # not AI being unavailable, so stay conservative and SKIP.
        return {"decision": "SKIP", "reason": "Claude response parse error"}


# ─────────────────────────────────────────────────────────────
# FULL WATCHLIST SCANNER
# ─────────────────────────────────────────────────────────────

def run_full_scan(macro: dict, account_info: dict) -> dict:
    """
    Scans the full watchlist (core + dynamic).
    Returns actionable A+ signals plus watch-mode candidate symbols.
    """
    from config import CORE_WATCHLIST

    # Get dynamic watchlist from database
    dynamic = [w['symbol'] for w in get_active_watchlist()]

    # Combine and deduplicate
    full_watchlist = list(dict.fromkeys(CORE_WATCHLIST + dynamic))

    logger.info(f"📊 Scanning {len(full_watchlist)} stocks...")
    actionable = []
    watch_candidates = []

    for symbol in full_watchlist:
        try:
            technical = analyze_stock(symbol)

            # Log every signal to database
            log_signal({
                "date":       datetime.now().isoformat(),
                "symbol":     symbol,
                "signal":     technical.get('signal'),
                "bull_score": technical.get('bull_score'),
                "bear_score": technical.get('bear_score'),
                "rsi":        technical.get('rsi'),
                "adx":        technical.get('adx'),
                "bias":       technical.get('bias'),
            })

            # Print the ThinkorSwim-style signal bar for every stock
            print(f"  {symbol:6s}: {technical.get('signal_bar', 'ERROR')}")

            # Proceed to Claude decision for any LONG/CALL setup that clears the
            # execution threshold (bull >= min_score_to_trade_long) and the A+
            # quality gates. A+ LONGs (score 8) are a strict subset of this, so
            # this single branch covers both A+ and strong (e.g. 7/10) longs.
            macro_ok = macro.get('signal') != 'RED'
            if HARD_RULES.get('require_macro_green'):
                macro_ok = macro.get('signal') == 'GREEN'

            if technical.get('qualifies_trade_long') and macro_ok:
                trade_plan = get_claude_trade_plan(symbol, technical, macro, account_info)

                if trade_plan.get('decision') == 'TRADE':
                    trade_plan = merge_trade_plan_with_suggestion(trade_plan, technical.get('suggested_option', {}))
                    # Carry volume and risk/reward through to the executor so the
                    # volume-tier sizing and R/R gates can evaluate this setup.
                    for _dst in (trade_plan, trade_plan.get('trade_plan', {})):
                        if isinstance(_dst, dict):
                            _dst['vol_ratio'] = technical.get('vol_ratio', 1.0)
                            _dst['risk_reward_ratio'] = technical.get('risk_reward_ratio', 0.0)
                    actionable.append({
                        "symbol":     symbol,
                        "technical":  technical,
                        "trade_plan": trade_plan,
                    })
                    tier = "A+" if technical.get('qualifies_aplus') else "STRONG"
                    logger.info(
                        f"🚨 {tier} TRADE SIGNAL: {symbol} | "
                        f"score {technical.get('bull_score')}/10 | "
                        f"Confidence: {trade_plan.get('confidence')}/10"
                    )
                else:
                    # AI declined (SKIP/WAIT) — keep the name under watch-mode
                    # monitoring rather than dropping it entirely.
                    watch_candidates.append({
                        "symbol":    symbol,
                        "technical": technical,
                    })

            # ── Bearish PUT path (Phase 3B) ───────────────────
            # A confirmed A+ SHORT goes to Claude exactly like a CALL setup.
            # Macro RED does not block a PUT — a risk-off macro is favorable for
            # the short side — so PUTs intentionally bypass the long-only
            # macro_ok gate. Final direction gating happens in the regime layer.
            elif technical.get('qualifies_aplus_put'):
                trade_plan = get_claude_trade_plan(symbol, technical, macro, account_info)

                if trade_plan.get('decision') == 'TRADE':
                    trade_plan = merge_trade_plan_with_suggestion(trade_plan, technical.get('suggested_option', {}))
                    # Force the short side regardless of any Claude drift, and
                    # carry volume + (direction-aware) risk/reward to the executor.
                    for _dst in (trade_plan, trade_plan.get('trade_plan', {})):
                        if isinstance(_dst, dict):
                            _dst['option_type'] = "PUT"
                            _dst['vol_ratio'] = technical.get('vol_ratio', 1.0)
                            _dst['risk_reward_ratio'] = technical.get('risk_reward_ratio', 0.0)
                    actionable.append({
                        "symbol":     symbol,
                        "technical":  technical,
                        "trade_plan": trade_plan,
                    })
                    logger.info(f"🔻 A+ PUT (SHORT) SIGNAL: {symbol} | Confidence: {trade_plan.get('confidence')}/10")

            # Bearish signal present but downside not confirmed → no PUT.
            elif technical.get('signal') in ("A+ SHORT", "SHORT"):
                logger.info(
                    f"PUT rejected: bearish confirmation failed | {symbol} | "
                    f"signal={technical.get('signal')} bias={technical.get('bias')} "
                    f"bear={technical.get('bear_score')} -DI={technical.get('di_minus')} "
                    f"+DI={technical.get('di_plus')}"
                )

            elif technical.get('signal') == 'LONG' and technical.get('bull_score', 0) >= SIGNAL_WEIGHTS['min_score_long']:
                watch_candidates.append({
                    "symbol":    symbol,
                    "technical": technical,
                })

        except Exception as e:
            logger.error(f"  {symbol}: Scan error — {e}")

    logger.info(f"✅ Scan complete | {len(actionable)} actionable signals | {len(watch_candidates)} watch candidates")
    return {
        "actionable": actionable,
        "watch_candidates": watch_candidates,
    }
