"""
orchestrator.py — George's Trading Bot Main Controller
Ties all agents together and runs the full automated trading system.
Manages scheduling, execution flow, and error handling.

USAGE:
    python orchestrator.py              # Run continuously
    python orchestrator.py --scan       # Run single scan now
    python orchestrator.py --report     # Generate weekly report now
    python orchestrator.py --paper      # Paper trading mode (default)
"""

import os
import sys
import time
from datetime import datetime, timedelta
import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from loguru import logger

# Load environment variables
load_dotenv()

# Configure logging
logger.add(
    "logs/trading_bot_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
)

# Import all agents
from db import initialize_database
from market_intelligence import run_market_intelligence, get_macro_data
from strategy_engine import run_full_scan, analyze_stock
from trade_executor import (
    get_account_status, place_options_trade, monitor_open_positions, is_kill_switch_engaged
)
from learning_engine import (
    generate_weekly_report, get_mistake_patterns, self_adjust_thresholds, load_adjustments
)
from config import ACCOUNT, NORMAL_SCAN_TIMES, HARD_RULES, SIGNAL_WEIGHTS

CT_TIMEZONE = pytz.timezone("America/Chicago")

WATCH_MODE_STATE = {
    "active": False,
    "symbols": [],
    "expires_at": None,
    "frequency_minutes": 10,
}


def is_market_open() -> bool:
    """Check if US market is currently open."""
    now = datetime.now(CT_TIMEZONE)
    if now.weekday() >= 5:  # Weekend
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0)
    market_close = now.replace(hour=15, minute=59, second=0)
    return market_open <= now <= market_close


def is_past_open_restriction() -> bool:
    """
    George's Rule: No trades in first 30 minutes after open.
    Returns True if it's safe to trade (after 10:00 AM CT).
    """
    now = datetime.now(CT_TIMEZONE)
    no_trade_until = now.replace(hour=10, minute=0, second=0)
    return now >= no_trade_until


def activate_watch_mode(symbols: list, duration_minutes: int = 90, frequency_minutes: int = 10):
    """Activate short-term watch mode for a small set of candidate tickers."""
    if not symbols:
        return

    WATCH_MODE_STATE["active"] = True
    WATCH_MODE_STATE["symbols"] = list(dict.fromkeys(symbols))
    WATCH_MODE_STATE["expires_at"] = datetime.now(CT_TIMEZONE) + timedelta(minutes=duration_minutes)
    WATCH_MODE_STATE["frequency_minutes"] = frequency_minutes
    logger.info(
        f"👀 Watch Mode activated for {len(WATCH_MODE_STATE['symbols'])} symbols "
        f"until {WATCH_MODE_STATE['expires_at'].strftime('%H:%M %Z')}"
    )


def deactivate_watch_mode():
    """Disable watch mode and return to normal scan cadence."""
    if WATCH_MODE_STATE["active"]:
        logger.info("✅ Watch Mode expired or deactivated")
    WATCH_MODE_STATE["active"] = False
    WATCH_MODE_STATE["symbols"] = []
    WATCH_MODE_STATE["expires_at"] = None
    WATCH_MODE_STATE["frequency_minutes"] = 10


# ─────────────────────────────────────────────────────────────
# SCHEDULED TASKS
# ─────────────────────────────────────────────────────────────

def task_pre_market_macro():
    """8:00 AM CT — Macro briefing and watchlist intelligence update."""
    logger.info("=" * 60)
    logger.info("🌅 PRE-MARKET TASK: Macro + Intelligence Scan")
    logger.info("=" * 60)

    try:
        # Run full market intelligence (news + sectors + watchlist update)
        intelligence = run_market_intelligence()
        macro = intelligence.get("macro", {})

        print(f"\n📊 MACRO BRIEFING — {datetime.now(CT_TIMEZONE).strftime('%Y-%m-%d')}")
        print(f"   Oil (WTI):      ${macro.get('oil', 'N/A')}")
        print(f"   VIX:            {macro.get('vix', 'N/A')}")
        print(f"   10yr Yield:     {macro.get('yield', 'N/A')}%")
        print(f"   S&P 500:        {macro.get('sp500', 'N/A')}")
        print(f"   Macro Signal:   {macro.get('signal', 'N/A')}")
        print(f"\n📋 Watchlist additions: {intelligence.get('added_symbols', [])}")

        for sector in intelligence.get("trending_sectors", []):
            print(f"   Trending: {sector['sector']} ({sector['strength']}) — {sector['reason']}")

    except Exception as e:
        logger.error(f"Pre-market task error: {e}")


def task_morning_scan():
    """Normal watchlist scan — 3x daily low-resource A+ signal checks."""
    logger.info("=" * 60)
    logger.info("🔍 NORMAL SCAN: Watchlist A+ Signal Scan")
    logger.info("=" * 60)

    if not is_market_open():
        logger.info("Market closed — skipping scan")
        return

    if not is_past_open_restriction():
        logger.info("⏸️  First 30 minutes — waiting per George's rules")
        return

    try:
        # Get current macro and account status
        macro   = get_macro_data()
        account = get_account_status()

        logger.info(f"💰 Account: ${account['portfolio_value']:.2f} | "
                    f"Cash: ${account['cash']:.2f} | "
                    f"Buying Power: ${account['buying_power']:.2f} | "
                    f"Positions: {account['open_positions']}")
        logger.info(f"📊 Macro: {macro.get('signal')} | Oil: ${macro.get('oil')} | VIX: {macro.get('vix')}")

        kill_switch_active, kill_reason = is_kill_switch_engaged()
        if kill_switch_active:
            logger.warning(f"⛔ Kill switch engaged — no new trades today: {kill_reason}")
            return

        # Don't trade in RED macro
        if macro.get("signal") == "RED":
            logger.warning("🚨 MACRO RED — No new positions today")
            return

        # Run the full scan
        print(f"\n{'='*60}")
        print("SCANNING ALL WATCHLIST STOCKS:")
        print(f"{'='*60}")
        scan_result = run_full_scan(macro, account)
        signals = scan_result.get("actionable", [])
        watch_candidates = scan_result.get("watch_candidates", [])

        if watch_candidates:
            activate_watch_mode(
                [candidate['symbol'] for candidate in watch_candidates],
                duration_minutes=90,
                frequency_minutes=10,
            )

        if not signals:
            logger.info("💤 No A+ setups found today — patience is the strategy")
            return

        # Execute trades for A+ signals with confidence >= 8
        logger.info(f"\n🚨 Found {len(signals)} A+ signals — evaluating for execution")

        for signal in signals:
            symbol     = signal['symbol']
            trade_plan = signal.get('trade_plan', {})
            confidence = trade_plan.get('confidence', 0)

            print(f"\n{'─'*40}")
            print(f"🎯 SIGNAL: {symbol}")
            print(f"   Confidence:  {confidence}/10")
            print(f"   Strike:      ${trade_plan.get('strike', 'N/A')}")
            print(f"   Expiry:      {trade_plan.get('expiry', 'N/A')}")
            print(f"   Stop:        ${trade_plan.get('stop_loss_stock_price', 'N/A')}")
            print(f"   Thesis:      {trade_plan.get('thesis', 'N/A')[:100]}")

            # Only auto-execute with confidence >= 8
            if confidence >= 8:
                result = place_options_trade(symbol, trade_plan, account)

                if result.get('success'):
                    logger.info(f"✅ TRADE PLACED: {symbol} | Order: {result.get('order_id')}")
                else:
                    logger.warning(f"❌ TRADE FAILED: {symbol} | {result.get('reason')}")
            else:
                logger.info(f"📋 Signal logged but not executed: {symbol} | "
                            f"Confidence {confidence}/10 below 8 threshold")

    except Exception as e:
        logger.error(f"Morning scan error: {e}")


def task_position_monitor():
    """Every 15 minutes during market hours — monitor open trades and risk."""
    if not is_market_open():
        return

    logger.info("👁️  Position monitor running...")
    try:
        monitor_open_positions()
    except Exception as e:
        logger.error(f"Position monitor error: {e}")


def task_watch_mode_monitor():
    """Every 10 minutes while watch mode is active — monitor candidate symbols."""
    if not WATCH_MODE_STATE["active"]:
        return

    now = datetime.now(CT_TIMEZONE)
    if WATCH_MODE_STATE["expires_at"] and now >= WATCH_MODE_STATE["expires_at"]:
        deactivate_watch_mode()
        return

    logger.info(f"👀 Watch Mode monitor checking {len(WATCH_MODE_STATE['symbols'])} symbols")
    updated_symbols = []

    for symbol in WATCH_MODE_STATE["symbols"]:
        try:
            technical = analyze_stock(symbol)
            if technical.get('signal') in ('LONG', 'A+ LONG') and technical.get('bull_score', 0) >= SIGNAL_WEIGHTS['min_score_long']:
                updated_symbols.append(symbol)
            else:
                logger.info(f"🛑 Watch candidate invalidated: {symbol} | Signal: {technical.get('signal')}")
        except Exception as e:
            logger.warning(f"Watch monitor failed for {symbol}: {e}")

    if not updated_symbols:
        deactivate_watch_mode()
        return

    WATCH_MODE_STATE["symbols"] = updated_symbols
    logger.info(f"✅ Watch Mode continues for {len(updated_symbols)} symbols")


def task_after_hours():
    """4:30 PM CT — After-hours news and earnings scan."""
    logger.info("🌆 After-hours scan running...")
    try:
        # Light news scan for earnings and breaking news
        intelligence = run_market_intelligence()
        logger.info(f"After-hours intelligence complete | "
                    f"Macro: {intelligence.get('macro', {}).get('signal')}")
    except Exception as e:
        logger.error(f"After-hours error: {e}")


def task_weekly_report():
    """Saturday 9:00 AM CT — Generate and display weekly report."""
    logger.info("=" * 60)
    logger.info("📊 GENERATING WEEKLY PERFORMANCE REPORT")
    logger.info("=" * 60)

    try:
        # Check for patterns and self-adjust
        mistakes = get_mistake_patterns(days_back=7)
        if mistakes:
            adjustments = self_adjust_thresholds(mistakes)
            if adjustments:
                logger.info(f"🧠 Strategy adjustments applied: {adjustments}")

        # Generate the full report
        report = generate_weekly_report()
        logger.info("✅ Weekly report complete")

    except Exception as e:
        logger.error(f"Weekly report error: {e}")


def task_watchlist_rebuild():
    """Sunday 8:00 PM CT — Full watchlist rebuild for next week."""
    logger.info("📋 Rebuilding watchlist for next week...")
    try:
        intelligence = run_market_intelligence()
        logger.info(f"Watchlist rebuilt | Added: {intelligence.get('added_symbols', [])}")
    except Exception as e:
        logger.error(f"Watchlist rebuild error: {e}")


# ─────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("🤖 GEORGE'S AUTONOMOUS TRADING BOT")
    print(f"   Mode: {'📄 PAPER TRADING' if ACCOUNT['paper_trading'] else '💰 LIVE TRADING'}")
    print(f"   Capital: ${ACCOUNT['total_capital']:,}")
    print(f"   Max per trade: {ACCOUNT['max_per_trade_pct']*100:.0f}% = "
          f"${ACCOUNT['total_capital'] * ACCOUNT['max_per_trade_pct']:,.0f}")
    print(f"   Max positions: {ACCOUNT['max_open_positions']}")
    print(f"   Cash reserve: {ACCOUNT['reserve_cash_pct']*100:.0f}% = "
          f"${ACCOUNT['total_capital'] * ACCOUNT['reserve_cash_pct']:,.0f}")
    print("=" * 60 + "\n")

    # Initialize database and load any persisted learning adjustments
    initialize_database()
    load_adjustments()

    # Handle CLI arguments
    if "--scan" in sys.argv:
        logger.info("Running single scan...")
        macro   = get_macro_data()
        account = get_account_status()
        scan_result = run_full_scan(macro, account)
        signals = scan_result.get("actionable", [])
        logger.info(f"Scan complete: {len(signals)} actionable signals found")
        return

    if "--report" in sys.argv:
        logger.info("Generating weekly report...")
        generate_weekly_report()
        return

    if "--intelligence" in sys.argv:
        logger.info("Running market intelligence...")
        run_market_intelligence()
        return

    # Setup the scheduler
    scheduler = BlockingScheduler(timezone=CT_TIMEZONE)

    # ── DAILY TASKS (Weekdays only) ──────────────────────────
    scheduler.add_job(
        task_pre_market_macro,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=CT_TIMEZONE),
        id="pre_market",
        name="Pre-Market Macro + Intelligence",
    )

    for scan_time in NORMAL_SCAN_TIMES:
        hour, minute = scan_time.split(":")
        scheduler.add_job(
            task_morning_scan,
            CronTrigger(day_of_week="mon-fri", hour=int(hour), minute=int(minute), timezone=CT_TIMEZONE),
            id=f"normal_scan_{hour}_{minute}",
            name=f"Normal Watchlist Scan {scan_time}",
        )

    # Position monitor — every 15 minutes while there are open trades
    for hour in range(10, 16):
        for minute in [0, 15, 30, 45]:
            scheduler.add_job(
                task_position_monitor,
                CronTrigger(
                    day_of_week="mon-fri",
                    hour=hour, minute=minute,
                    timezone=CT_TIMEZONE
                ),
                id=f"monitor_{hour}_{minute}",
                name=f"Position Monitor {hour}:{minute:02d}",
            )

    scheduler.add_job(
        task_watch_mode_monitor,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/10", timezone=CT_TIMEZONE),
        id="watch_mode_monitor",
        name="Watch Mode Monitor",
    )

    scheduler.add_job(
        task_after_hours,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone=CT_TIMEZONE),
        id="after_hours",
        name="After-Hours Intelligence Scan",
    )

    # ── WEEKLY TASKS ─────────────────────────────────────────
    scheduler.add_job(
        task_weekly_report,
        CronTrigger(day_of_week="sat", hour=9, minute=0, timezone=CT_TIMEZONE),
        id="weekly_report",
        name="Saturday Weekly Report",
    )

    scheduler.add_job(
        task_watchlist_rebuild,
        CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=CT_TIMEZONE),
        id="watchlist_rebuild",
        name="Sunday Watchlist Rebuild",
    )

    logger.info("✅ Scheduler configured with all tasks:")
    for job in scheduler.get_jobs():
        logger.info(f"   • {job.name} — Next: {job.next_run_time}")

    print("\n🚀 Bot is running. Press Ctrl+C to stop.\n")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
