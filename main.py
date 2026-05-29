"""Safe paper trading runner for George's bot.

This script loads .env, validates Alpaca paper-only settings, scans the watchlist on
schedule, checks strategy signals, validates risk rules, and executes paper trades
only when DRY_RUN=false.
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from alpaca_broker import is_kill_switch_engaged
from config import ACCOUNT, CORE_WATCHLIST, NORMAL_SCAN_TIMES
from db import initialize_database
from market_intelligence import get_macro_data
from notifier import notify_daily_summary
from strategy_engine import run_full_scan
from trade_executor import get_account_status, place_options_trade
from scheduler import start as start_scheduler, shutdown as shutdown_scheduler

CT_TIMEZONE = "America/Chicago"
JOURNAL_PATH = Path("journal")
JOURNAL_PATH.mkdir(parents=True, exist_ok=True)
JOURNAL_FILE = JOURNAL_PATH / "trade_journal.log"


def validate_environment() -> None:
    paper_trading = os.getenv("PAPER_TRADING", "true").strip().lower()
    alpaca_base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").strip()

    if paper_trading != "true":
        raise SystemExit(
            "ERROR: PAPER_TRADING must be true for this runner. "
            "Set PAPER_TRADING=true in your .env file."
        )

    if "paper-api.alpaca.markets" not in alpaca_base_url:
        raise SystemExit(
            f"ERROR: ALPACA_BASE_URL must be the paper endpoint. "
            f"Current ALPACA_BASE_URL={alpaca_base_url}"
        )

    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
        raise SystemExit(
            "ERROR: ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env."
        )

    if not ACCOUNT.get("paper_trading", True):
        raise SystemExit(
            "ERROR: config.ACCOUNT.paper_trading must remain True for safe paper trading."
        )

    if ACCOUNT.get("kill_switch_enabled", True) and is_kill_switch_engaged()[0]:
        raise SystemExit(
            f"ERROR: Kill switch engaged before startup: {is_kill_switch_engaged()[1]}"
        )

    logger.info("✅ Environment validated for Alpaca paper trading")
    logger.info(f"✅ Loaded watchlist from config: {len(CORE_WATCHLIST)} symbols")


def print_account_summary(account: dict) -> None:
    print("\n=== Alpaca Account Summary ===")
    print(f"Paper trading: {account.get('paper_trading', True)}")
    print(f"Account status: {account.get('account_status', 'UNKNOWN')}")
    print(f"Cash: ${account.get('cash', 0):,.2f}")
    print(f"Buying power: ${account.get('buying_power', 0):,.2f}")
    print(f"Portfolio value: ${account.get('portfolio_value', 0):,.2f}")
    print(f"Open positions: {account.get('open_positions', 0)}")
    print("==============================\n")


def log_trade_journal(message: str) -> None:
    line = f"{datetime.now().isoformat()} | {message}\n"
    with JOURNAL_FILE.open("a", encoding="utf-8") as journal:
        journal.write(line)


def execute_scan(dry_run: bool) -> None:
    print("\n--- Running watchlist scan ---")
    try:
        account = get_account_status()
        
        # Check if account fetch failed
        if account.get("error"):
            error_msg = f"❌ ERROR: {account.get('error')}"
            print(error_msg)
            logger.error(error_msg)
            log_trade_journal(error_msg)
            return {"error": error_msg, "account": account}
        
        print_account_summary(account)

        kill_switch, reason = is_kill_switch_engaged()
        if kill_switch:
            message = f"⛔ Kill switch active: {reason}"
            print(message)
            log_trade_journal(message)
            return {"kill_switch": True, "reason": reason, "account": account}

        # Only skip if it's a legitimate zero balance, not an error
        if account.get("buying_power", 0) <= 0 and not account.get("error"):
            message = "⛔ No buying power available (zero balance) — skipping scan."
            print(message)
            log_trade_journal("Skipped scan due to zero buying power.")
            return {"error": message, "account": account}

        macro = get_macro_data()
        print(f"Macro: {macro.get('signal', 'N/A')} | Oil: {macro.get('oil', 'N/A')} | VIX: {macro.get('vix', 'N/A')}")

        if macro.get('signal') == 'RED':
            message = "⛔ Macro RED — no new trades today."
            print(message)
            log_trade_journal("Skipped scan because macro signal is RED.")
            return {"error": message, "account": account, "macro": macro}

        print(f"Scanning {len(CORE_WATCHLIST)} configured watchlist symbols...")
        scan_result = run_full_scan(macro, account)
        actionable_signals = scan_result.get("actionable", [])
        watch_candidates = scan_result.get("watch_candidates", [])

        if watch_candidates:
            message = f"🔎 Watch mode candidates detected: {len(watch_candidates)} symbols"
            print(message)
            log_trade_journal(f"Watch candidates identified: {', '.join([wc['symbol'] for wc in watch_candidates[:5]])}")

        if not actionable_signals:
            message = "No actionable trading signals found."
            print(message)
            log_trade_journal("Scan completed: no actionable signals.")
            return {
                "actionable": actionable_signals,
                "watch_candidates": watch_candidates,
                "account": account,
                "macro": macro,
                "message": message,
            }

        print(f"\nFound {len(actionable_signals)} actionable signal(s). { 'DRY RUN - logging ideas only.' if dry_run else 'EXECUTE mode - placing paper trades.' }")

        for signal in actionable_signals:
            symbol = signal.get('symbol')
            trade_plan = signal.get('trade_plan', {})
            confidence = trade_plan.get('confidence', 0)
            entry_text = (
                f"IDEA: {symbol} | Decision={trade_plan.get('decision')} | Confidence={confidence}/10 | "
                f"MaxSpend=${trade_plan.get('max_to_spend', 0):,.2f} | Stop={trade_plan.get('stop_loss_stock_price')} | "
                f"Expiry={trade_plan.get('expiry')} | Strike={trade_plan.get('strike')} ({trade_plan.get('strike_pct_otm')}% OTM) | "
                f"Thesis={trade_plan.get('thesis', '')[:120]}"
            )
            print(f"\n=== {symbol} ===")
            print(entry_text)
            log_trade_journal(entry_text)

            if dry_run:
                print("DRY_RUN=true: skipping paper order placement.")
                continue

            print("Validating risk rules and placing paper trade...")
            result = place_options_trade(symbol, trade_plan, account)
            if result.get('success'):
                message = f"EXECUTED: {symbol} | Order ID={result.get('order_id')} | {trade_plan.get('decision')}"
                print(f"✅ {message}")
                log_trade_journal(message)
            else:
                message = f"REJECTED: {symbol} | Reason={result.get('reason')}"
                print(f"❌ {message}")
                log_trade_journal(message)

        return {
            "actionable": actionable_signals,
            "watch_candidates": watch_candidates,
            "account": account,
            "macro": macro,
        }
    except Exception as exc:
        error_text = f"Scan error: {exc}"
        print(error_text)
        logger.error(error_text)
        log_trade_journal(error_text)
        return {"error": error_text, "account": {}, "macro": {}}


def schedule_scans(dry_run: bool) -> None:
    # Delegate scheduling to the new scheduler module (background scheduler)
    start_scheduler(dry_run)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe paper trading runner for Alpaca")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run in execute mode and place paper trades when approved.",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run one scan immediately before starting the schedule.",
    )
    parser.add_argument(
        "--no-schedule",
        action="store_true",
        help="Run a single scan and exit without starting the scheduler.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logger.remove()
    logger.add(sys.stdout, level="INFO", format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")

    dry_run_env = os.getenv("DRY_RUN", "true").strip().lower() == "true"
    dry_run = not args.execute and dry_run_env

    if dry_run:
        print("RUNNER MODE: DRY_RUN=true — trade ideas will be logged but not executed.")
    else:
        print("RUNNER MODE: EXECUTE=true — approved paper trades may be placed.")

    initialize_database()
    validate_environment()

    if args.now:
        scan_summary = execute_scan(dry_run=dry_run)
        if scan_summary:
            notify_daily_summary(
                f"Scan completed with {len(scan_summary.get('actionable', []))} actionable signal(s) and "
                f"{len(scan_summary.get('watch_candidates', []))} watch candidate(s). "
                f"Account cash ${scan_summary.get('account', {}).get('cash', 0):,.2f}, "
                f"buying power ${scan_summary.get('account', {}).get('buying_power', 0):,.2f}."
            )

    if args.no_schedule:
        print("No-schedule mode enabled. Exiting after one-off scan.")
        sys.exit(0)

    schedule_scans(dry_run=dry_run)
    print("Scheduler running. Press Ctrl-C to exit.")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Shutting down scheduler...")
        shutdown_scheduler()
