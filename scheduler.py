"""Event-driven scheduler for the trading bot.

Schedules all recurring tasks:
  - Pre-market macro briefing        08:00 CT  Mon-Fri
  - Normal A+ watchlist scans        09:45 / 12:00 / 14:45 CT  Mon-Fri
  - Watch mode monitor               every 20 min (adaptive)
  - Active trade monitor             every 10 min (adaptive)
  - After-hours intelligence scan    16:30 CT  Mon-Fri
  - Weekly performance report        09:00 CT  Saturday
  - Dynamic watchlist rebuild        20:00 CT  Sunday

DRY_RUN is respected throughout: scans always run, trade placement is skipped.
"""
from pathlib import Path
from datetime import datetime, timedelta
from loguru import logger

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from alpaca_broker import is_kill_switch_engaged
from config import NORMAL_SCAN_TIMES, ACCOUNT, CT_TIMEZONE
from db import get_mistake_patterns
from market_intelligence import get_macro_data, run_market_intelligence
from notifier import notify_watch_mode, notify_daily_summary, send_discord_message
from state_manager import state_manager
from strategy_engine import run_full_scan
from trade_executor import (
    get_account_status,
    get_adaptive_active_interval,
    monitor_open_positions,
    monitor_runners,
    place_options_trade,
)
from watch_manager import should_activate_watch, research_top_candidates

# ── Lazy imports to avoid circular deps at module load time ────
# learning_engine is only needed inside task functions, not at import.

scheduler = BackgroundScheduler(timezone=CT_TIMEZONE)

JOURNAL_PATH = Path("journal")
JOURNAL_PATH.mkdir(parents=True, exist_ok=True)
_JOURNAL_FILE = JOURNAL_PATH / "trade_journal.log"


def _journal(message: str) -> None:
    """Append a timestamped line to the trade journal."""
    line = f"{datetime.now().isoformat()} | {message}\n"
    with _JOURNAL_FILE.open("a", encoding="utf-8") as f:
        f.write(line)


# ─────────────────────────────────────────────────────────────
# PRE-MARKET — 08:00 CT
# ─────────────────────────────────────────────────────────────

def _run_pre_market() -> None:
    """Macro briefing + dynamic watchlist intelligence update."""
    logger.info("=" * 55)
    logger.info("PRE-MARKET | Macro briefing + watchlist intelligence")
    logger.info("=" * 55)
    try:
        intelligence = run_market_intelligence()
        macro = intelligence.get("macro", {})
        added = intelligence.get("added_symbols", [])
        logger.info(
            f"Macro={macro.get('signal')} | "
            f"Oil=${macro.get('oil')} | VIX={macro.get('vix')} | "
            f"10yr={macro.get('yield')}% | WatchlistAdded={added}"
        )
        _journal(
            f"PRE-MARKET | Macro={macro.get('signal')} | "
            f"Oil={macro.get('oil')} | VIX={macro.get('vix')} | Added={added}"
        )
    except Exception as exc:
        logger.error(f"Pre-market task failed: {exc}", exc_info=True)


# ─────────────────────────────────────────────────────────────
# NORMAL SCAN — 09:45 / 12:00 / 14:45 CT
# ─────────────────────────────────────────────────────────────

def _run_normal_scan(dry_run: bool) -> None:
    """
    Full watchlist scan with conditional trade execution.

    Flow:
      1. Fetch account + macro
      2. Early-exit on account error, kill switch, or zero buying power
      3. Run full A+ scan via strategy_engine
      4. Cache Perplexity research for top candidates (no-op if key missing)
      5. For each actionable signal:
         - Skip if confidence < min_confidence_to_execute
         - Skip (log only) if dry_run=True
         - Otherwise place paper trade via place_options_trade()
      6. Activate watch mode for near-miss candidates
    """
    logger.info("NORMAL SCAN starting")
    min_confidence = ACCOUNT.get("min_confidence_to_execute", 8)

    try:
        # ── 1. Account health ─────────────────────────────────
        account = get_account_status()
        if account.get("error"):
            logger.error(f"Account fetch failed — scan aborted: {account['error']}")
            _journal(f"SCAN ABORTED | account error: {account['error']}")
            return

        buying_power = account.get("buying_power", 0)
        if buying_power <= 0:
            logger.warning("No buying power available — skipping scan")
            _journal("SCAN SKIPPED | zero buying power")
            return

        # ── 2. Kill switch ────────────────────────────────────
        engaged, reason = is_kill_switch_engaged()
        if engaged:
            logger.warning(f"Kill switch active — no new trades: {reason}")
            _journal(f"SCAN SKIPPED | kill switch: {reason}")
            return

        # ── 3. Macro data ─────────────────────────────────────
        macro = get_macro_data()
        logger.info(
            f"Macro={macro.get('signal')} | "
            f"Oil=${macro.get('oil')} | VIX={macro.get('vix')} | "
            f"Cash=${account.get('cash', 0):,.0f} | "
            f"BuyingPower=${buying_power:,.0f} | "
            f"OpenPositions={account.get('open_positions', 0)}"
        )

        if macro.get("signal") == "RED":
            logger.warning("Macro RED — scanning logged but no new positions")
            _journal("SCAN | macro RED — no trades")

        # ── 4. A+ signal scan ─────────────────────────────────
        scan_result = run_full_scan(macro, account)
        actionable = scan_result.get("actionable", [])
        watch_candidates = scan_result.get("watch_candidates", [])

        logger.info(
            f"Scan complete | actionable={len(actionable)} | "
            f"watch_candidates={len(watch_candidates)}"
        )

        # ── 5. Research cache (best candidates only) ──────────
        research_targets = actionable or watch_candidates
        try:
            research_results = research_top_candidates(research_targets, macro, limit=3)
            for item in research_results:
                hit = "HIT" if item.get("cached") else "MISS"
                logger.info(f"Research cache {hit} for {item.get('symbol')}")
        except Exception as exc:
            logger.warning(f"Research caching skipped: {exc}")

        # ── 6. Trade execution ────────────────────────────────
        executed = skipped_confidence = skipped_dryrun = rejected = 0

        for signal in actionable:
            symbol = signal.get("symbol")
            trade_plan = signal.get("trade_plan", {})
            confidence = int(trade_plan.get("confidence", 0) or 0)

            # Gate 1: confidence threshold
            if confidence < min_confidence:
                logger.info(
                    f"SKIP {symbol} | confidence {confidence}/10 < "
                    f"threshold {min_confidence} — logged, not executed"
                )
                _journal(
                    f"SKIP (confidence {confidence}/10 < {min_confidence}): "
                    f"{symbol} | {trade_plan.get('decision')} | "
                    f"thesis={str(trade_plan.get('thesis', ''))[:80]}"
                )
                skipped_confidence += 1
                continue

            # Gate 2: DRY_RUN
            if dry_run:
                logger.info(
                    f"DRY_RUN | would execute {symbol} | "
                    f"confidence={confidence}/10 | "
                    f"strike={trade_plan.get('strike')} | "
                    f"expiry={trade_plan.get('expiry')}"
                )
                _journal(
                    f"DRY_RUN IDEA: {symbol} | confidence={confidence}/10 | "
                    f"strike={trade_plan.get('strike')} | expiry={trade_plan.get('expiry')} | "
                    f"thesis={str(trade_plan.get('thesis', ''))[:80]}"
                )
                skipped_dryrun += 1
                continue

            # Execute
            logger.info(f"Executing paper trade: {symbol} | confidence={confidence}/10")
            result = place_options_trade(symbol, trade_plan, account)
            if result.get("success"):
                msg = (
                    f"EXECUTED: {symbol} | order={result.get('order_id')} | "
                    f"confidence={confidence}/10"
                )
                logger.info(msg)
                _journal(msg)
                executed += 1
            else:
                msg = f"REJECTED: {symbol} | {result.get('reason')}"
                logger.warning(msg)
                _journal(msg)
                rejected += 1

        logger.info(
            f"Execution summary | "
            f"executed={executed} | "
            f"dry_run_skipped={skipped_dryrun} | "
            f"below_confidence={skipped_confidence} | "
            f"rejected={rejected}"
        )

        # ── 7. Watch mode activation ──────────────────────────
        candidates_to_watch = []
        for wc in watch_candidates:
            symbol = wc.get("symbol")
            technical = wc.get("technical", {})
            option_plan = technical.get("suggested_option", {})
            passed, reasons = should_activate_watch(symbol, technical, option_plan)
            if passed:
                candidates_to_watch.append({
                    "symbol": symbol,
                    "score": technical.get("bull_score", 0),
                    "reasons": reasons,
                    "technical": technical,
                    "option_plan": option_plan,
                })

        if candidates_to_watch:
            expires = datetime.now() + timedelta(minutes=90)
            symbols = [c["symbol"] for c in candidates_to_watch]
            state_manager.set_watch(symbols, expires, reason="Filtered watch candidates")
            for c in candidates_to_watch:
                symbol = c["symbol"]
                technical = c["technical"]
                option_plan = c["option_plan"]
                strike_pct = float(option_plan.get("strike_pct_otm", 0) or 0)
                logger.info(f"WATCH MODE: {symbol} | reasons: {', '.join(c['reasons'])}")
                notify_watch_mode(
                    symbol=symbol,
                    direction=option_plan.get("option_type", "Unknown"),
                    confidence=c["score"],
                    expiration_window=option_plan.get("expiry", "30-90 DTE"),
                    strike_preference="ITM or near-the-money" if strike_pct <= 5 else "Near-the-money",
                    catalyst=option_plan.get("catalyst") or "Technical catalyst",
                    technical_setup=technical.get("signal", "Momentum / trend confirmation"),
                    invalidation_level=str(option_plan.get("stop_loss_stock_price") or "Setup invalidation"),
                    frequency_minutes=20,
                )

    except Exception as exc:
        logger.error(f"NORMAL SCAN failed: {exc}", exc_info=True)
        _journal(f"SCAN ERROR: {exc}")


# ─────────────────────────────────────────────────────────────
# WATCH MONITOR — every 20 min (adaptive)
# ─────────────────────────────────────────────────────────────

def _watch_monitor() -> None:
    if state_manager.state != "WATCH":
        return
    logger.info(f"WATCH monitor | symbols={state_manager.active_symbols}")
    try:
        macro = get_macro_data()
        account = get_account_status()
        result = run_full_scan(macro, account)
        actionable = result.get("actionable", [])
        watch_candidates = result.get("watch_candidates", [])

        for signal in actionable:
            sym = signal.get("symbol")
            if sym in state_manager.active_symbols:
                state_manager.set_active([sym], reason="Watch -> Active triggered")
                send_discord_message(f"Watch -> ACTIVE for {sym}")
                break

        interval = 20
        for candidate in watch_candidates:
            sym = candidate.get("symbol")
            technical = candidate.get("technical", {})
            option_plan = technical.get("suggested_option", {})
            if sym in state_manager.active_symbols:
                passed, _ = should_activate_watch(sym, technical, option_plan)
                if passed:
                    interval = min(interval, 10)
                    break

        job = scheduler.get_job("watch_monitor")
        if job:
            scheduler.reschedule_job("watch_monitor", trigger="interval", minutes=interval)
            logger.debug(f"Watch monitor rescheduled to {interval} min")
    except Exception as exc:
        logger.error(f"Watch monitor failed: {exc}")


# ─────────────────────────────────────────────────────────────
# ACTIVE MONITOR — every 10 min (adaptive)
# ─────────────────────────────────────────────────────────────

def _active_monitor() -> None:
    if state_manager.state != "ACTIVE":
        return
    logger.info("ACTIVE trade monitor running")
    try:
        monitor_open_positions()
        monitor_runners()
        interval = get_adaptive_active_interval()
        job = scheduler.get_job("active_monitor")
        if job:
            scheduler.reschedule_job("active_monitor", trigger="interval", minutes=interval)
            logger.debug(f"Active monitor rescheduled to {interval} min")
    except Exception as exc:
        logger.error(f"Active monitor failed: {exc}")


# ─────────────────────────────────────────────────────────────
# AFTER-HOURS — 16:30 CT
# ─────────────────────────────────────────────────────────────

def _run_after_hours() -> None:
    """Post-close news and earnings scan to update watchlist for next day."""
    logger.info("AFTER-HOURS intelligence scan starting")
    try:
        intelligence = run_market_intelligence()
        macro = intelligence.get("macro", {})
        added = intelligence.get("added_symbols", [])
        logger.info(
            f"After-hours complete | Macro={macro.get('signal')} | Added={added}"
        )
        _journal(f"AFTER-HOURS | Macro={macro.get('signal')} | Added={added}")
    except Exception as exc:
        logger.error(f"After-hours scan failed: {exc}", exc_info=True)


# ─────────────────────────────────────────────────────────────
# WEEKLY REPORT — Saturday 09:00 CT
# ─────────────────────────────────────────────────────────────

def _run_weekly_report() -> None:
    """Generate weekly performance report, apply learning adjustments, post to Discord."""
    logger.info("WEEKLY REPORT generating...")
    try:
        from learning_engine import generate_weekly_report, self_adjust_thresholds

        Path("reports").mkdir(exist_ok=True)

        mistakes = get_mistake_patterns(days_back=7)
        if mistakes:
            adjustments = self_adjust_thresholds(mistakes)
            if adjustments:
                logger.info(f"Strategy self-adjustment applied: {adjustments}")
                _journal(f"SELF-ADJUSTMENT: {adjustments}")

        report_text = generate_weekly_report()

        # Post the first 1800 chars to Discord (embed limit)
        notify_daily_summary(f"Weekly Report\n\n{report_text[:1800]}")
        logger.info("Weekly report complete and posted")
        _journal("WEEKLY REPORT generated")
    except Exception as exc:
        logger.error(f"Weekly report failed: {exc}", exc_info=True)


# ─────────────────────────────────────────────────────────────
# WATCHLIST REBUILD — Sunday 20:00 CT
# ─────────────────────────────────────────────────────────────

def _run_watchlist_rebuild() -> None:
    """Full dynamic watchlist rebuild ahead of the new trading week."""
    logger.info("WATCHLIST REBUILD starting for next week")
    try:
        intelligence = run_market_intelligence()
        added = intelligence.get("added_symbols", [])
        logger.info(f"Watchlist rebuild complete | Added: {added}")
        _journal(f"WATCHLIST REBUILD | Added={added}")
    except Exception as exc:
        logger.error(f"Watchlist rebuild failed: {exc}", exc_info=True)


# ─────────────────────────────────────────────────────────────
# SCHEDULER CONTROL
# ─────────────────────────────────────────────────────────────

def start(dry_run: bool) -> None:
    """Register all jobs and start the background scheduler."""

    # Normal scans — 3x daily, Mon-Fri, CT timezone
    for scan_time in NORMAL_SCAN_TIMES:
        hour, minute = scan_time.split(":")
        scheduler.add_job(
            _run_normal_scan,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=int(hour),
                minute=int(minute),
                timezone=CT_TIMEZONE,
            ),
            args=(dry_run,),
            id=f"normal_scan_{hour}_{minute}",
        )

    # Pre-market macro + intelligence — 08:00 CT
    scheduler.add_job(
        _run_pre_market,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=8, minute=0, timezone=CT_TIMEZONE
        ),
        id="pre_market",
    )

    # After-hours scan — 16:30 CT
    scheduler.add_job(
        _run_after_hours,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=16, minute=30, timezone=CT_TIMEZONE
        ),
        id="after_hours",
    )

    # Watch mode monitor — adaptive interval, starts at 20 min
    scheduler.add_job(_watch_monitor, "interval", minutes=20, id="watch_monitor")

    # Active trade monitor — adaptive interval, starts at 10 min
    scheduler.add_job(_active_monitor, "interval", minutes=10, id="active_monitor")

    # Weekly performance report — Saturday 09:00 CT
    scheduler.add_job(
        _run_weekly_report,
        trigger=CronTrigger(
            day_of_week="sat", hour=9, minute=0, timezone=CT_TIMEZONE
        ),
        id="weekly_report",
    )

    # Dynamic watchlist rebuild — Sunday 20:00 CT
    scheduler.add_job(
        _run_watchlist_rebuild,
        trigger=CronTrigger(
            day_of_week="sun", hour=20, minute=0, timezone=CT_TIMEZONE
        ),
        id="watchlist_rebuild",
    )

    mode = "DRY_RUN (log only)" if dry_run else "EXECUTE (paper trades active)"
    logger.info(f"Scheduler starting | mode={mode} | timezone={CT_TIMEZONE}")
    for job in scheduler.get_jobs():
        logger.info(f"  Job registered: {job.id} | next={job.next_run_time}")

    scheduler.start()


def shutdown() -> None:
    scheduler.shutdown(wait=False)
