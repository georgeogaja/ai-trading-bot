"""
agents/learning_engine.py
The bot's self-improvement system.
Tracks mistakes, identifies patterns, adjusts strategy thresholds,
and generates comprehensive weekly performance reports.
"""

import os
import json
from datetime import datetime, timedelta
from anthropic import Anthropic
from loguru import logger
from config import HARD_RULES, SIGNAL_WEIGHTS
from db import (
    get_mistake_patterns, calculate_weekly_performance,
    get_open_trades, get_connection, get_trade_by_id, get_adjustments, log_mistake
)

claude = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def analyze_trade_outcome(trade: dict) -> dict:
    """
    After a trade closes, analyze what happened and categorize mistakes.
    Uses Claude to provide nuanced analysis.
    """
    pnl     = trade.get('pnl_pct', 0) or 0
    outcome = "WIN" if pnl > 0 else ("SCRATCH" if abs(pnl) < 5 else "LOSS")

    prompt = f"""
You are analyzing a completed options trade for George's swing trading bot.
Your job is to identify any mistakes made and suggest improvements.

GEORGE'S RULES REMINDER:
- RSI must be < 70 at entry
- Strike must be ATM to max 10% OTM
- Stop loss must be defined before entry
- No trades in first 30 min after open
- Max 1 contract, max 20% of account
- Exit on stop without debate

COMPLETED TRADE:
Symbol: {trade.get('symbol')}
Option Type: {trade.get('trade_type')}
Strike: ${trade.get('strike')}
Entry Date: {trade.get('entry_date')}
Exit Date: {trade.get('exit_date')}
Entry Stock Price: ${trade.get('entry_stock_price')}
Exit Stock Price: ${trade.get('exit_stock_price')}
Entry Option Price: ${trade.get('entry_option_price')}
Exit Option Price: ${trade.get('exit_option_price')}
P/L %: {pnl:.1f}%
P/L $: ${trade.get('pnl_dollars', 0):.2f}
Exit Reason: {trade.get('exit_reason')}
RSI at Entry: {trade.get('rsi_at_entry')}
ADX at Entry: {trade.get('adx_at_entry')}
Bias at Entry: {trade.get('bias_at_entry')}
Catalyst: {trade.get('catalyst')}
Thesis: {trade.get('thesis')}
Outcome: {outcome}

ANALYZE THIS TRADE AND RESPOND WITH VALID JSON:
{{
  "outcome": "{outcome}",
  "mistakes": [
    {{
      "category": "MISTAKE_CATEGORY",
      "description": "what went wrong",
      "what_to_do_instead": "specific corrective action",
      "severity": "LOW | MEDIUM | HIGH | CRITICAL",
      "pnl_impact": 0.00
    }}
  ],
  "what_went_right": ["list of things executed correctly"],
  "key_lesson": "single most important lesson from this trade",
  "adjustment_recommendation": "specific change to make to the strategy or thresholds",
  "confidence_in_analysis": 1-10
}}

If no mistakes were made, return an empty mistakes array with what went right.
"""

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        return json.loads(response.content[0].text)
    except Exception:
        return {"mistakes": [], "outcome": outcome, "key_lesson": "Analysis failed"}


def record_trade_outcome(trade_id: int) -> dict:
    """
    Analyze a closed trade and record any mistakes in the database.
    """
    trade = get_trade_by_id(trade_id)
    if not trade:
        logger.error(f"Trade not found for learning analysis: {trade_id}")
        return {}

    analysis = analyze_trade_outcome(trade)
    mistakes = analysis.get("mistakes", []) or []

    for mistake in mistakes:
        log_mistake(trade_id, {
            "symbol":           trade.get("symbol"),
            "category":         mistake.get("category", "UNKNOWN"),
            "description":      mistake.get("description"),
            "what_went_wrong":  mistake.get("description"),
            "what_to_do_instead": mistake.get("what_to_do_instead"),
            "pnl_impact":       mistake.get("pnl_impact"),
            "severity":         mistake.get("severity"),
            "adjustment_made":  analysis.get("adjustment_recommendation"),
        })

    if not mistakes and trade.get("status") == "CLOSED":
        log_mistake(trade_id, {
            "symbol":           trade.get("symbol"),
            "category":         "CORRECT_TRADE",
            "description":      "No mistakes detected in this trade.",
            "what_went_wrong":  "None",
            "what_to_do_instead": "Continue following the strategy.",
            "pnl_impact":       trade.get("pnl_dollars", 0),
            "severity":         "LOW",
            "adjustment_made":  analysis.get("adjustment_recommendation"),
        })

    if mistakes:
        adjustments = self_adjust_thresholds(get_mistake_patterns(days_back=7))
        if adjustments:
            logger.info(f"🧠 Immediate learning adjustment applied after trade {trade_id}: {adjustments}")

    return analysis


def apply_adjustments(adjustments: dict):
    """Apply learned adjustments to active strategy settings."""
    for key, value in adjustments.items():
        if key in HARD_RULES:
            HARD_RULES[key] = value
        if key in SIGNAL_WEIGHTS:
            SIGNAL_WEIGHTS[key] = value


def load_adjustments(days_back: int = 365) -> dict:
    """Load previously saved strategy adjustments and apply them at runtime."""
    rows = get_adjustments(days_back)
    if not rows:
        return {}

    loaded = {}
    for row in rows:
        try:
            adjustments = json.loads(row.get("adjustment", "{}"))
        except Exception:
            continue
        apply_adjustments(adjustments)
        loaded.update(adjustments)

    if loaded:
        logger.info(f"📚 Loaded persisted strategy adjustments: {loaded}")
    return loaded


def generate_weekly_report(week_start: str = None, week_end: str = None) -> str:
    """
    Generate a comprehensive weekly performance report.
    Sent every Saturday at 9:00 AM CT.
    """
    if not week_start:
        today      = datetime.now()
        week_end   = today.strftime("%Y-%m-%d")
        week_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")

    # Pull all the data
    performance = calculate_weekly_performance(week_start, week_end)
    mistakes    = get_mistake_patterns(days_back=7)
    open_trades = get_open_trades()

    # Get macro context for the week
    conn    = get_connection()
    cursor  = conn.cursor()
    cursor.execute("""
        SELECT * FROM macro_log
        WHERE date >= ? AND date <= ?
        ORDER BY date DESC LIMIT 7
    """, (week_start, week_end))
    macro_logs = [dict(row) for row in cursor.fetchall()]

    # Get watchlist changes
    cursor.execute("""
        SELECT * FROM watchlist
        WHERE added_date >= ? OR removed_date >= ?
    """, (week_start, week_start))
    watchlist_changes = [dict(row) for row in cursor.fetchall()]
    conn.close()

    performance_json = json.dumps(performance, indent=2, default=str)
    mistakes_json    = json.dumps(mistakes,    indent=2, default=str)
    open_json        = json.dumps(open_trades, indent=2, default=str)

    prompt = f"""
You are generating the weekly performance report for George's autonomous trading bot.
George is a PMP-certified swing trader using a $10,000 options account.

Write a comprehensive, honest, professional weekly report.
Be direct — George wants the truth about what worked and what did not.

WEEK: {week_start} to {week_end}

PERFORMANCE DATA:
{performance_json}

MISTAKE PATTERNS THIS WEEK:
{mistakes_json}

OPEN POSITIONS:
{open_json}

WATCHLIST CHANGES:
Added: {len([w for w in watchlist_changes if w.get('is_active')])} stocks
Removed: {len([w for w in watchlist_changes if not w.get('is_active')])} stocks

GENERATE A COMPLETE WEEKLY REPORT WITH THESE SECTIONS:

1. EXECUTIVE SUMMARY (3 sentences — P/L, win rate, biggest lesson)

2. TRADE PERFORMANCE TABLE (all trades this week with entry, exit, P/L)

3. WHAT WORKED THIS WEEK (patterns, sectors, timing that produced wins)

4. WHAT FAILED THIS WEEK (honest breakdown of losses and mistakes)

5. MISTAKE ANALYSIS (top mistake categories, cost of each, corrective action)

6. OPEN POSITIONS REVIEW (thesis check for each open position)

7. WATCHLIST UPDATES (why stocks were added/removed this week)

8. STRATEGY ADJUSTMENTS MADE (what the bot changed in response to mistakes)

9. NEXT WEEK FOCUS (top 3 setups to watch, key catalysts, macro risks)

10. ACCOUNT METRICS
    - Starting value: $10,000
    - Current value: [calculated]
    - Weekly P/L: [calculated]
    - YTD return: [calculated]
    - Win rate all-time: [calculated]
    - Profit factor: [calculated]

Format as a clean, readable text report.
Be specific with numbers. Be honest about failures.
End with ONE key insight George should remember for next week.
"""

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )

    report_text = response.content[0].text

    # Save to database
    conn   = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO weekly_reports (week_start, week_end, report_text, 
                                    total_pnl, win_rate, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        week_start,
        week_end,
        report_text,
        performance.get("total_pnl", 0),
        performance.get("win_rate", 0),
        datetime.now().isoformat(),
    ))
    conn.commit()
    conn.close()

    # Save as text file
    report_path = f"reports/weekly_report_{week_start}.txt"
    with open(report_path, "w") as f:
        f.write(f"GEORGE'S TRADING BOT — WEEKLY REPORT\n")
        f.write(f"Week of {week_start} to {week_end}\n")
        f.write("=" * 60 + "\n\n")
        f.write(report_text)

    logger.info(f"📊 Weekly report generated: {report_path}")
    print("\n" + "=" * 60)
    print(report_text)
    print("=" * 60 + "\n")

    return report_text


def self_adjust_thresholds(mistake_patterns: dict) -> dict:
    """
    Based on repeated mistake patterns, adjust strategy thresholds.
    The bot learns from its own errors.
    """
    adjustments = {}

    # If wrong strike mistakes are common, tighten OTM limit
    wrong_strike = mistake_patterns.get("WRONG_STRIKE", {})
    if wrong_strike.get("count", 0) >= 3:
        adjustments["max_otm_pct"] = 0.10  # Tighten from 15% to 10% OTM
        HARD_RULES["max_otm_pct"] = 0.10
        logger.warning("🔧 Adjustment: Tightened OTM limit to 10% due to repeated wrong-strike mistakes")

    # If overbought entries are common, lower the ideal RSI ceiling
    overbought = mistake_patterns.get("HELD_THROUGH_OVERBOUGHT", {})
    if overbought.get("count", 0) >= 2:
        adjustments["rsi_ideal_max"] = max(55, HARD_RULES.get("rsi_ideal_max", 65) - 3)
        HARD_RULES["rsi_ideal_max"] = adjustments["rsi_ideal_max"]
        logger.warning("🔧 Adjustment: Lowered RSI entry max due to repeated overbought holds")

    # If expiry mistakes are common, extend swing expiry targets
    wrong_expiry = mistake_patterns.get("WRONG_EXPIRY", {})
    if wrong_expiry.get("count", 0) >= 2:
        adjustments["options_expiry_days"] = min(HARD_RULES.get("expiry_max_days", 90), HARD_RULES.get("options_expiry_days", 75) + 15)
        HARD_RULES["options_expiry_days"] = adjustments["options_expiry_days"]
        logger.warning("🔧 Adjustment: Increased target expiry to reduce wrong-expiry errors")

    # If too many ignored macro errors, add mandatory macro check
    macro_errors = mistake_patterns.get("IGNORED_MACRO", {})
    if macro_errors.get("count", 0) >= 1:
        adjustments["require_macro_green"] = True
        HARD_RULES["require_macro_green"] = True
        logger.warning("🔧 Adjustment: Now requiring macro GREEN (not just YELLOW) for new entries")

    # If too many early exits, raise minimum hold period
    early_exits = mistake_patterns.get("EARLY_EXIT", {})
    if early_exits.get("count", 0) >= 3:
        adjustments["min_hold_days"] = max(3, HARD_RULES.get("min_hold_days", 0))
        HARD_RULES["min_hold_days"] = adjustments["min_hold_days"]
        logger.warning("🔧 Adjustment: Added a minimum hold period due to early exits")

    if adjustments:
        logger.info(f"🧠 Self-adjustment applied: {adjustments}")
        # Save adjustments to database for tracking
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS adjustments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT,
                adjustment  TEXT,
                reason      TEXT
            )
        """)
        cursor.execute(
            "INSERT INTO adjustments (date, adjustment, reason) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), json.dumps(adjustments), str(mistake_patterns))
        )
        conn.commit()
        conn.close()

    return adjustments
