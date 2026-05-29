"""
config.py — George's Trading Bot Configuration
All strategy parameters, rules, and constants in one place.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────
# ACCOUNT CONFIGURATION
# ─────────────────────────────────────────────────────────────
ACCOUNT = {
    "total_capital":        10_000,    # Total account size
    "max_per_trade_pct":    0.20,      # Max 20% per trade = $2,000 max
    "max_open_positions":   5,         # Never more than 5 concurrent positions
    "reserve_cash_pct":     0.30,      # Always keep 30% cash reserve = $3,000
    "paper_trading":        True,      # ALWAYS start in paper mode
    "kill_switch_enabled":  True,      # Enable daily loss and trade count protection
    "max_daily_loss_usd":   500,       # Stop new trades after this much realized loss in a day
    "max_trades_per_day":   5,         # Stop new trades after this many fills in a day
}

# Override PAPER_TRADING from .env when explicitly set
ACCOUNT["paper_trading"] = os.getenv("PAPER_TRADING", str(ACCOUNT["paper_trading"]).lower()).lower() == "true"

# ─────────────────────────────────────────────────────────────
# GEORGE'S HARD RULES (NEVER VIOLATE — SYSTEM WILL REJECT)
# ─────────────────────────────────────────────────────────────
HARD_RULES = {
    "max_otm_pct":          0.15,      # Never more than 15% OTM strike
    "rsi_overbought":       70,        # Reject any long if RSI >= 70
    "rsi_oversold":         30,        # Alert level for oversold
    "rsi_ideal_min":        40,        # Ideal RSI entry range minimum
    "rsi_ideal_max":        65,        # Ideal RSI entry range maximum
    "adx_trend_min":        20,        # ADX must be above 20 for trend
    "adx_strong":           25,        # ADX above 25 = strong trend
    "min_volume_ratio":     1.10,      # Volume must be at least 110% of 50-day average
    "support_lookback":     20,        # Lookback for support/resistance calculations
    "stop_loss_atr_multiplier": 1.2,   # ATR multiplier to size stops for swing entries
    "no_trade_open_mins":   30,        # No trades first 30 min after open
    "require_macro_green":  False,      # If True, only allow new longs when macro is GREEN
    "min_hold_days":        0,         # Minimum holding period after entry
    "max_contracts":        1,         # Never more than 1 contract per position
    "min_earnings_beats":   4,         # Minimum 4 consecutive beats for A+
    "min_drawdown_pct":     0.20,      # Prefer stocks down 20%+ from 52wk high
    "target_1_gain":        0.50,      # First profit target: 50% option gain
    "target_2_gain":        1.00,      # Second target: 100% option gain
    "options_expiry_days":  75,        # Target ~75-90 days to expiry
    "leap_expiry_days":     365,       # LEAP target ~12 months
}

# ─────────────────────────────────────────────────────────────
# A+ SIGNAL SCORING (mirrors George's ThinkorSwim script)
# ─────────────────────────────────────────────────────────────
SIGNAL_WEIGHTS = {
    # BULL conditions
    "bias_bull":            2,         # Price > EMA50 > EMA72 > EMA125
    "rsi_ideal_zone":       2,         # RSI 40-60
    "rsi_bullish":          1,         # RSI 60-70 (bullish but elevated)
    "adx_trending":         1,         # ADX > 25
    "adx_strong":           1,         # ADX > 40
    "di_positive":          1,         # +DI above -DI
    "rsi_divergence":       2,         # Bullish RSI divergence detected
    "falling_wedge":         2,         # Falling wedge pattern adds strong swing bias
    "trend_support":         1,         # Price above EMA50 and support zone
    "volume_expansion":      1,         # Volume above the 50-day average
    "drawdown_bonus":        1,         # Favor stocks with smart 52-week drawdown bounce
    "above_avg_volume":     1,         # Volume > 1.2x 50-day average
    "insider_buy":          2,         # Form 4 P-type purchase last 30 days
    "earnings_beats_4":     1,         # 4+ consecutive EPS beats
    "catalyst_14days":      1,         # Known catalyst within 14 days
    "macro_green":          1,         # Macro environment favorable

    # A+ threshold
    "min_score_aplus":      8,         # Score >= 8 = A+ LONG signal
    "min_score_long":       5,         # Score >= 5 = LONG signal

    # BEAR conditions
    "bias_bear":            2,
    "rsi_overbought_bear":  3,         # RSI > 70 = strong bear signal
    "rsi_severe_ob":        2,         # RSI > 75 = severe overbought
    "adx_bear_trend":       1,
    "di_negative":          1,
}

# ─────────────────────────────────────────────────────────────
# SECTOR TRACKING (for dynamic watchlist building)
# ─────────────────────────────────────────────────────────────
SECTORS = {
    "AI_INFRASTRUCTURE": {
        "keywords": ["AI", "artificial intelligence", "data center", "GPU", "NVIDIA", 
                     "machine learning", "LLM", "inference", "training"],
        "base_stocks": ["NVDA", "AMD", "AVGO", "MSFT", "GOOG", "META"],
    },
    "AI_PACKAGING": {
        "keywords": ["semiconductor packaging", "CoWoS", "HBM", "OSAT", "chiplet",
                     "advanced packaging", "3D stacking"],
        "base_stocks": ["AMKR", "KLAC", "ONTO", "ENTG", "MU"],
    },
    "AI_NETWORKING": {
        "keywords": ["AI networking", "optical", "ethernet", "bandwidth", "interconnect",
                     "data center networking", "switches"],
        "base_stocks": ["ANET", "GLW", "COHR"],
    },
    "AI_POWER": {
        "keywords": ["data center power", "cooling", "energy AI", "liquid cooling",
                     "power management"],
        "base_stocks": ["VRT", "IREN"],
    },
    "ENTERPRISE_SOFTWARE": {
        "keywords": ["SaaS", "enterprise software", "cloud", "workflow", "AI agents",
                     "platform", "ARR", "cRPO"],
        "base_stocks": ["NOW", "MSFT", "ORCL", "PLTR", "CRM"],
    },
    "ENERGY": {
        "keywords": ["oil", "crude", "Hormuz", "OPEC", "energy", "LNG", "natural gas"],
        "base_stocks": ["XOM", "EQNR", "LIN"],
    },
    "CONSUMER": {
        "keywords": ["consumer spending", "retail sales", "consumer confidence"],
        "base_stocks": ["WMT", "COST", "KO"],
    },
    "FINTECH": {
        "keywords": ["crypto", "Bitcoin", "trading volume", "retail investing",
                     "brokerage", "payment"],
        "base_stocks": ["HOOD", "JPM"],
    },
    "SEMICONDUCTOR": {
        "keywords": ["chip", "semiconductor", "fab", "wafer", "foundry", "TSMC"],
        "base_stocks": ["AMD", "MU", "AVGO", "PANW", "CRWD"],
    },
}

# ─────────────────────────────────────────────────────────────
# GEORGE'S CORE 30-STOCK WATCHLIST (always included)
# ─────────────────────────────────────────────────────────────
CORE_WATCHLIST = [
    "WMT", "UBER", "TSLA", "PLTR", "ORCL", "NVDA", "NFLX",
    "MSFT", "META", "HOOD", "GOOG", "CAT", "AMGN", "AMD",
    "PANW", "CRWD", "AVGO", "MU", "COST", "NOW", "TMUS",
    "KO", "JPM", "XOM", "GLW", "AMKR", "ANET", "IREN",
    "ONTO", "ENTG",
]

# NORMAL MODE: Low resource scan schedule (3 scans per trading day in CT)
NORMAL_SCAN_TIMES = ["09:45", "12:00", "14:45"]

# ─────────────────────────────────────────────────────────────
# MACRO SIGNALS (Iran war / oil / rates framework)
# ─────────────────────────────────────────────────────────────
MACRO = {
    "oil_risk_threshold":   95,        # WTI above $95 = macro headwind
    "oil_danger_threshold": 110,       # WTI above $110 = macro RED
    "vix_elevated":         25,        # VIX above 25 = elevated risk
    "vix_crisis":           35,        # VIX above 35 = crisis — no new longs
    "yield_headwind":       4.50,      # 10yr above 4.50% = growth stock headwind
    "yield_danger":         5.00,      # 10yr above 5.00% = significant multiple compression
}

# ─────────────────────────────────────────────────────────────
# MISTAKE CATEGORIES (for learning engine)
# ─────────────────────────────────────────────────────────────
MISTAKE_CATEGORIES = [
    "WRONG_STRIKE",             # Entered too far OTM
    "NO_STOP_DEFINED",          # Entered without stop
    "CHASED_OPEN",              # Traded in first 30 minutes
    "HELD_THROUGH_OVERBOUGHT",  # Held when RSI > 70
    "IGNORED_MACRO",            # Entered despite RED macro
    "OVERSIZED_POSITION",       # Exceeded 20% of account
    "WRONG_EXPIRY",             # Chose expiry too short
    "EARLY_EXIT",               # Exited before thesis played out
    "LATE_EXIT",                # Held past stop signal
    "EARNINGS_HOLD_ERROR",      # Held through earnings incorrectly
    "SECTOR_ROTATION_MISSED",   # Missed sector move
    "CORRECT_TRADE",            # No mistake — for baseline
]

# ─────────────────────────────────────────────────────────────
# SCHEDULING (Cron-style times in CT timezone)
# ─────────────────────────────────────────────────────────────
SCHEDULE = {
    "pre_market_scan":      "08:00",   # Macro briefing
    "normal_scan_1":        "09:45",   # Watchlist A+ scan
    "normal_scan_2":        "12:00",   # Midday watchlist check
    "normal_scan_3":        "14:45",   # Afternoon watchlist scan
    "close_check":          "15:30",   # Pre-close review
    "after_hours":          "16:30",   # Earnings / news scan
    "insider_scan":         "18:00",   # Form 4 P-type scan
    "weekly_report":        "SAT 09:00",  # Saturday morning report
    "watchlist_rebuild":    "SUN 20:00",  # Sunday night watchlist update
}
