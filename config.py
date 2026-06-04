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
    "total_capital":              5_000,     # Live paper account cash/equity (NOT the $20k margin BP;
                                             # long options are paid from cash, and the reserve gate
                                             # checks cash — so this tracks real spendable capital)
    "max_per_trade_pct":          0.40,      # Max 40% per trade = $2,000 max deployed
    "max_open_positions":         5,         # Never more than 5 concurrent positions
    "reserve_cash_pct":           0.20,      # Always keep 20% cash reserve = $1,000
    "paper_trading":              True,      # ALWAYS start in paper mode
    "kill_switch_enabled":        True,      # Enable daily loss and trade count protection
    "max_daily_loss_usd":         1000,      # Stop new trades after this much realized loss in a day
                                             # (20% of a $5k account)
    "max_trades_per_day":         5,         # Stop new trades after this many fills in a day
    "min_confidence_to_execute":  7,         # Minimum confidence score (1-10) before placing a trade.
                                             # AI mode: Claude's confidence. Deterministic mode: the
                                             # rule-based signal score (bull for CALLs, bear for PUTs).
}

# Override PAPER_TRADING from .env when explicitly set
ACCOUNT["paper_trading"] = os.getenv("PAPER_TRADING", str(ACCOUNT["paper_trading"]).lower()).lower() == "true"

# ─────────────────────────────────────────────────────────────
# VOLUME-BASED POSITION SIZING
# Volume ratio = current volume / 50-day average volume
# ─────────────────────────────────────────────────────────────
POSITION_SIZING = {
    "volume_normal_min":        1.20,   # vol_ratio >= 1.20 → full (normal) position size
    "volume_reduced_min":       0.70,   # vol_ratio 0.70–1.19 → reduced size (see multiplier)
    "reduced_size_multiplier":  0.50,   # Reduced tier risks 50% of normal dollar allocation
    # vol_ratio < 0.70 → WATCH ONLY (no trade placed)
}

# ─────────────────────────────────────────────────────────────
# RISK / REWARD GATES
# ─────────────────────────────────────────────────────────────
# Risk/reward is NO LONGER a hard control. The absolute floor was replaced by the
# MAX_LOSS model below. R/R remains only a SOFT quality hint for non-9/10 setups;
# a 9/10 force-execution setup is NEVER blocked on risk/reward (any ratio is fine).
RISK_REWARD = {
    "min_to_execute":   1.80,   # Soft: a NON-force setup below this only watches.
                                # Force (9/10) setups bypass this entirely.
}

# ─────────────────────────────────────────────────────────────
# MAXIMUM-LOSS RISK MODEL  (the single hard reward/risk control)
# A trade's maximum possible loss must not exceed this fraction of CURRENT account
# equity. For a long option the max loss is the full debit paid:
#       max_loss = premium × contracts × 100
# The per-trade budget is capped at (max_loss_pct_of_equity × equity), so sizing
# can never exceed the limit; a setup is rejected only when even ONE contract would
# breach it. Risk/reward ratio is never consulted for the execution decision.
#   equity $5,000 → limit $1,000 :  $700 ✓ allow | $950 ✓ allow | $1,050 ✗ reject
# ─────────────────────────────────────────────────────────────
MAX_LOSS = {
    "max_loss_pct_of_equity": 0.20,   # Max loss per trade <= 20% of account equity
}

# ─────────────────────────────────────────────────────────────
# 9/10 FORCE EXECUTION ATTEMPT RULE
# When a setup is graded at or above `min_confidence`, the bot MUST attempt to
# place a paper trade rather than routing it to WATCH mode. SOFT filters may
# REDUCE size but never block the attempt:
#   - neutral market regime / directional regime gating
#   - moderate (or low) volume
#   - sector not strongest
#   - missing / misaligned TradingView supply-demand zone
#   - AI unavailable (deterministic fallback)
#   - borderline risk/reward (R/R never blocks a 9/10; the MAX_LOSS cap governs)
# HARD safety controls are ALWAYS enforced, even for a 9/10 (see validate_trade
# and place_options_trade): paper-only, kill switch, max daily loss, max trades
# per day, max open positions, buying power, reserve cash, position-size cap,
# valid option contract on the correct underlying, valid bid/ask, spread within
# the hard max, earnings block, and any Alpaca order rejection.
# ─────────────────────────────────────────────────────────────
FORCE_EXECUTION = {
    "enabled":              True,   # Master switch for the 9/10 force-attempt rule
    "min_confidence":       9,      # confidence >= this forces a paper-trade ATTEMPT
    "min_size_multiplier":  0.5,    # forced trades through a soft block size at >= this
}

# ─────────────────────────────────────────────────────────────
# PUT / BEARISH SETUP SUPPORT (Phase 3B)
# Controls when the bot is allowed to take the SHORT side via PUTs.
# All existing CALL/LONG logic and safety controls are unaffected.
# ─────────────────────────────────────────────────────────────
PUT_SUPPORT = {
    "enabled":                      True,  # Master switch for bearish/PUT setups
    "min_put_score":                8,     # bear_score required for an A+ SHORT (mirrors min_score_aplus)
    "require_bear_bias":            True,  # Stock structural BIAS must be BEAR (price < EMA50 < EMA72 < EMA125)
    "require_downside_momentum":    True,  # -DI must exceed +DI (momentum confirms downside)
    # In a BULLISH market regime, only allow a PUT when the stock is clearly weak
    # RELATIVE to the market: its trailing 21-day return must lag SPY by at least
    # this many percentage points. Otherwise the PUT is blocked by the regime.
    "bullish_regime_rel_weakness_pct": 3.0,
}

# ─────────────────────────────────────────────────────────────
# GEORGE'S HARD RULES (NEVER VIOLATE — SYSTEM WILL REJECT)
# ─────────────────────────────────────────────────────────────
HARD_RULES = {
    "max_otm_pct":          0.07,      # Never more than 7% OTM strike (higher-delta, higher win-prob)
    "max_strike_pct_otm":   0.05,      # Strike selector caps OTM distance here (closer to ATM)
    "rsi_overbought":       70,        # Reject any long if RSI >= 70
    "rsi_oversold":         30,        # Alert level for oversold
    "rsi_ideal_min":        40,        # Ideal RSI entry range minimum
    "rsi_ideal_max":        58,        # Ideal RSI entry ceiling — buy pullbacks, not extension
    "adx_trend_min":        20,        # ADX must be above 20 for trend
    "adx_strong":           25,        # ADX above 25 = strong trend
    "min_volume_ratio":     1.10,      # Volume must be at least 110% of 50-day average
    "support_lookback":     20,        # Lookback for support/resistance calculations
    "stop_loss_atr_multiplier": 1.2,   # ATR multiplier to size stops for swing entries
    "no_trade_open_mins":   30,        # No trades first 30 min after open
    "require_macro_green":  False,      # If True, only allow new longs when macro is GREEN
    "min_hold_days":        0,         # Minimum holding period after entry
    "max_contracts":        0,         # 0 = unlimited; the per-trade dollar budget governs quantity
    "max_option_loss_pct":  0.15,      # Hard stop: exit if premium falls 15% from entry (max loss/trade)
    "min_earnings_beats":   4,         # Minimum 4 consecutive beats for A+
    "earnings_block_days":  7,         # Block NEW entries within this many days of earnings (IV-crush risk)
    "min_drawdown_pct":     0.20,      # Prefer stocks down 20%+ from 52wk high
    "target_1_gain":        0.30,      # First profit target: scale half at +30% (win-rate lever)
    "target_2_gain":        1.00,      # Second (runner) target: 100% option gain
    "breakeven_trigger_gain": 0.20,    # Once a position is +20%, lift the stop to breakeven
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
    "min_score_aplus":      8,         # Score >= 8 = A+ LONG signal (quality label)
    "min_score_long":       5,         # Score >= 5 = LONG signal
    # Execution threshold for LONG/CALL setups. A setup is eligible to be sent to
    # the AI + executor when bull_score >= this AND it still clears the SAME
    # quality gates as an A+ LONG (BULL bias, RSI <= rsi_ideal_max, ADX >=
    # adx_trend_min). Lowered to 6 so a TREND-CONFIRMED 6/10 (BULL bias + ADX
    # above adx_trend_min + RSI in range) can execute. The trend-confirmation
    # gates are intentionally KEPT: a 6 that is trendless (ADX < adx_trend_min)
    # or non-BULL bias still only watches — e.g. a NEUTRAL-bias, ADX ~12 name
    # like XOM does NOT qualify. Every downstream control (AI confidence >=
    # min_confidence_to_execute, volume tier, risk/reward floors, regime, sector,
    # zone, liquidity, sizing, kill switch) is unchanged. Raise to 7/8 to
    # restrict execution to strong/A+ setups only.
    "min_score_to_trade_long": 6,

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

# ─────────────────────────────────────────────────────────────
# SECTOR / THEME GROUPS (Phase 3C — Sector Strength Engine)
# Every CORE_WATCHLIST symbol maps to one group. Dynamic-watchlist symbols
# that are not listed here fall through to "UNCLASSIFIED" and are treated as
# NEUTRAL (never preferred, never penalised). Used to rank themes vs SPY/QQQ
# before ranking individual stocks.
# ─────────────────────────────────────────────────────────────
SECTOR_GROUPS = {
    "AI_SEMIS":           ["NVDA", "AMD", "AVGO", "MU", "AMKR", "ONTO", "ENTG", "ANET", "GLW", "IREN"],
    "SOFTWARE_CLOUD":     ["MSFT", "ORCL", "PLTR", "NOW", "CRWD", "PANW", "GOOG", "META"],
    "FINANCIALS":         ["JPM", "HOOD"],
    "INDUSTRIALS":        ["CAT"],
    "CONSUMER_DEFENSIVE": ["WMT", "COST", "KO", "TMUS", "NFLX", "UBER", "TSLA"],
    "ENERGY":             ["XOM"],
    "HEALTHCARE":         ["AMGN"],
}

# Sector-strength ranking + gating thresholds.
SECTOR_STRENGTH = {
    "enabled":                True,
    "lookback_days":          21,     # ~1 month trailing return window
    "strong_rel_threshold":   2.0,    # sector beats the SPY/QQQ blend by >= 2% → STRONG
    "weak_rel_threshold":     2.0,    # sector trails the blend by >= 2% → WEAK
    # How to treat a setup that trades AGAINST sector strength
    # (a CALL in a WEAK sector, or a PUT in a STRONG sector):
    "contra_action":          "REJECT",  # "REDUCE" (size down) or "REJECT" (block). REJECT = no counter-trend.
    "contra_size_multiplier": 0.5,        # size factor when contra_action == "REDUCE"
}

# ─────────────────────────────────────────────────────────────
# OPTIONS LIQUIDITY FILTER (Phase 3D)
# Final pre-execution gate: reject illiquid / untradeable option contracts and
# prefer the most liquid near-the-money contract among candidates.
# ─────────────────────────────────────────────────────────────
OPTIONS_LIQUIDITY = {
    "enabled":              True,
    "max_spread_pct":       0.10,   # bid/ask spread <= 10% of mid passes...
    "max_spread_abs":       0.15,   # ...OR <= $0.15 absolute, but ONLY for cheap contracts
    "cheap_mid_threshold":  1.50,   # the absolute-spread rescue applies only when mid <= this
    "min_open_interest":    100,    # reject if OI is reported and below this
    "min_option_volume":    10,     # reject if daily option volume is reported and below this
    "min_bid":              0.05,   # bid must be at least this (rejects no-bid contracts)

    # Data-availability policy. A valid live quote (bid > 0, ask > bid) is always
    # required. Open interest is NOT exposed by the Alpaca snapshot model, so by
    # default missing OI/volume data does not hard-block (only present-and-low
    # values reject). Flip these to True to require the data to be present.
    "require_quote":              True,
    "require_open_interest_data": False,
    "require_volume_data":        False,

    # If the snapshot fetch itself errors (e.g. no options market-data
    # entitlement on the paper account), reject by default (fail-closed). Set to
    # True to let trades through when liquidity data cannot be retrieved.
    "fail_open_on_error":   False,
}

# ─────────────────────────────────────────────────────────────
# TRADINGVIEW SUPPLY/DEMAND WEBHOOK RECEIVER (Phase 4A)
# Inbound HTTP endpoint that ingests TradingView alert webhooks carrying
# supply/demand zones. Phase 4A is the RECEIVER only: it authenticates,
# validates, persists, logs, and posts to Discord. Zones are exposed for a
# later phase to consume — they are NOT yet wired into scan/execution.
#
# Security: the shared secret is read from the env var TRADINGVIEW_WEBHOOK_SECRET
# (never stored in config). If it is unset, the server refuses to start so an
# unauthenticated endpoint is never exposed.
# ─────────────────────────────────────────────────────────────
TRADINGVIEW = {
    "enabled":         True,
    "host":            "0.0.0.0",                 # bind address (put behind a firewall/proxy)
    "port":            8000,                       # overridable via TRADINGVIEW_WEBHOOK_PORT
    "path":            "/webhook/tradingview",     # POST alerts here; GET = health check
    "zone_ttl_hours":  72,                         # a zone expires after this unless refreshed
    "max_body_bytes":  16384,                      # reject oversized request bodies
}

# ─────────────────────────────────────────────────────────────
# ZONE-AWARE EXECUTION LOGIC (Phase 4B)
# Consumes the supply/demand zones stored by the Phase 4A TradingView receiver
# (db.supply_demand_zones) and turns them into a directional gate + confidence
# nudge in the scheduler, alongside the regime / sector / liquidity gates.
#
#   CALL → prefer DEMAND zones; reject if too close to a SUPPLY zone; boost the
#          score when price is near a DEMAND zone.
#   PUT  → prefer SUPPLY zones; reject if too close to a DEMAND zone; boost the
#          score when price is near a SUPPLY zone.
#
# Distances are FRACTIONS of the current price (0.03 = 3%). A zone counts as
# "near" when price is at-or-inside the band (distance 0.0) up to the threshold.
# All existing protections (paper trading, regime/sector/liquidity gates, volume
# sizing, risk management, kill switch, Discord alerts) are preserved unchanged.
# ─────────────────────────────────────────────────────────────
ZONE_LOGIC = {
    "enabled":                          True,
    "MAX_DISTANCE_TO_ZONE":             0.03,   # price within 3% of a favouring zone → score boost
    "REJECT_DISTANCE_TO_OPPOSITE_ZONE": 0.015,  # price within 1.5% of the opposite zone → reject
    "ZONE_SCORE_BOOST":                 1,      # confidence points added when aligned + near zone
    "max_zone_age_hours":               48,     # ignore zones older than this (stale), independent of TTL
    "max_confidence":                   10,     # cap confidence after the boost
    # When True, if a symbol HAS active zones the entry MUST be at/near a
    # favouring zone (demand for CALL, supply for PUT) or it is rejected — i.e.
    # the manually-drawn zones become entry triggers, not just a nudge. Symbols
    # with NO active zones are unaffected (they fall back to ALLOW), so this
    # never halts names you haven't marked up.
    "require_zone_for_entry":           True,
}

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

CT_TIMEZONE = "America/Chicago"   # Legacy jobs (pre-market, after-hours, weekly) use this
ET_TIMEZONE = "America/New_York"  # Market-relative jobs (scans + watchlist alerts) use Eastern

# Regular US equity session in Eastern time. Scans run every 30 min starting
# SCAN_PREOPEN_LEAD_MIN before the open (for pre-market screening / zone prep)
# through the close; pre-open scans screen only and never place trades.
MARKET_OPEN_ET = (9, 30)
MARKET_CLOSE_ET = (16, 0)
SCAN_PREOPEN_LEAD_MIN = 60   # begin scanning this many minutes before the open
SCAN_INTERVAL_MIN = 30       # baseline cadence during the window

# Opening "power hour": for the first SCAN_OPEN_POWER_DURATION_MIN after the
# open, scan more frequently (every SCAN_OPEN_POWER_INTERVAL_MIN) to actively
# hunt the morning's best entries while the move is fresh. These slots are
# inside RTH, so they look for trades (not screening-only).
SCAN_OPEN_POWER_INTERVAL_MIN = 5    # cadence during the opening power hour
SCAN_OPEN_POWER_DURATION_MIN = 60   # length of the faster window after the open

# Daily zone-marking watchlist alerts (Eastern time). Sent the evening before a
# trading day and again pre-open, so supply/demand zones can be drawn manually
# in TradingView before the market opens.
WATCHLIST_ALERT_EVENING_ET = (20, 0)   # 8:00 PM ET, evenings before trading days (Sun–Thu)
WATCHLIST_ALERT_MORNING_ET = (6, 0)    # 6:00 AM ET, trading mornings (Mon–Fri)

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
LOGGING = {
    "log_dir":      "logs",
    "log_level":    "INFO",
    "rotation":     "1 day",
    "retention":    "30 days",
    "format":       "{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
}
