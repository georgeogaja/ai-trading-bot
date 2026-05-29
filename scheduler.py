"""Event-driven scheduler for the trading bot.

Schedules NORMAL scans, WATCH monitoring, and ACTIVE trade monitoring.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
from loguru import logger

from config import NORMAL_SCAN_TIMES
from market_intelligence import get_macro_data
from strategy_engine import run_full_scan
from trade_executor import get_account_status, monitor_open_positions, monitor_runners, get_adaptive_active_interval
from state_manager import state_manager
from watch_manager import should_activate_watch, research_top_candidates
from notifier import notify_watch_mode, send_discord_message

scheduler = BackgroundScheduler()


def _run_normal_scan(dry_run: bool):
    logger.info("Running NORMAL scan")
    try:
        macro = get_macro_data()
        account = get_account_status()
        result = run_full_scan(macro, account)
        actionable = result.get("actionable", [])
        watch_candidates = result.get("watch_candidates", [])

        if actionable:
            logger.info(f"Found {len(actionable)} actionable signals")

        research_targets = actionable or watch_candidates
        research_results = research_top_candidates(research_targets, macro, limit=3)
        for item in research_results:
            status = "HIT" if item.get("cached") else "MISS"
            logger.info(f"Research {status} for {item.get('symbol')}")

        activate = []
        for wc in watch_candidates:
            symbol = wc.get('symbol')
            technical = wc.get('technical', {})
            trade_plan = technical.get('suggested_option', {})
            passed, reasons = should_activate_watch(symbol, technical, trade_plan)
            if passed:
                activate.append({
                    "symbol": symbol,
                    "score": technical.get("bull_score", 0),
                    "reasons": reasons,
                })

        if activate:
            expires = datetime.now() + timedelta(minutes=90)
            symbols = [item['symbol'] for item in activate]
            state_manager.set_watch(symbols, expires, reason="Filtered watch candidates")
            for item in activate:
                symbol = item['symbol']
                technical = next((wc['technical'] for wc in watch_candidates if wc['symbol'] == symbol), {})
                option_plan = technical.get('suggested_option', {})
                strike_pct = float(option_plan.get('strike_pct_otm', 0) or 0)
                strike_preference = "ITM or near-the-money" if strike_pct <= 5 else "Near-the-money"
                expiration_window = option_plan.get('expiry', '30-90 DTE')
                catalyst = option_plan.get('catalyst') or technical.get('thesis') or "Technical catalyst"
                invalidation = option_plan.get('stop_loss_stock_price') or technical.get('support_level') or "Setup invalidation"

                logger.info(f"Watch MODE entered for {symbol} | Reason: {', '.join(item['reasons'])}")
                notify_watch_mode(
                    symbol=symbol,
                    direction=option_plan.get('option_type', 'Unknown'),
                    confidence=item['score'],
                    expiration_window=expiration_window,
                    strike_preference=strike_preference,
                    catalyst=catalyst,
                    technical_setup=technical.get('signal', 'Momentum / trend confirmation'),
                    invalidation_level=str(invalidation),
                    frequency_minutes=20,
                )

    except Exception as e:
        logger.error(f"NORMAL scan failed: {e}")


def _watch_monitor():
    if state_manager.state != "WATCH":
        return
    logger.info("Running WATCH monitor for symbols: %s" % state_manager.active_symbols)
    try:
        macro = get_macro_data()
        account = get_account_status()
        result = run_full_scan(macro, account)
        actionable = result.get("actionable", [])
        watch_candidates = result.get("watch_candidates", [])

        for a in actionable:
            sym = a.get('symbol')
            if sym in state_manager.active_symbols:
                state_manager.set_active([sym], reason="Watch->Active triggered")
                send_discord_message(f"Watch -> ACTIVE for {sym}")
                break

        interval = 20
        for candidate in watch_candidates:
            symbol = candidate.get('symbol')
            technical = candidate.get('technical', {})
            trade_plan = technical.get('suggested_option', {})
            if symbol in state_manager.active_symbols:
                passed, _ = should_activate_watch(symbol, technical, trade_plan)
                if passed:
                    interval = min(interval, 10)
                    break

        current_job = scheduler.get_job('watch_monitor')
        if current_job:
            scheduler.reschedule_job('watch_monitor', trigger='interval', minutes=interval)
            logger.debug(f"Watch monitor interval rescheduled to {interval} minutes")
    except Exception as e:
        logger.error(f"Watch monitor failed: {e}")


def _active_monitor():
    if state_manager.state != "ACTIVE":
        return
    logger.info("Running ACTIVE trade monitor")
    try:
        monitor_open_positions()
        monitor_runners()
        interval = get_adaptive_active_interval()
        current_job = scheduler.get_job('active_monitor')
        if current_job:
            scheduler.reschedule_job('active_monitor', trigger='interval', minutes=interval)
            logger.debug(f"Active monitor interval rescheduled to {interval} minutes")
    except Exception as e:
        logger.error(f"Active monitor failed: {e}")


def start(dry_run: bool):
    # Schedule NORMAL scans at configured times (Central Time assumed in CronTrigger usage)
    for scan_time in NORMAL_SCAN_TIMES:
        hour, minute = scan_time.split(":")
        scheduler.add_job(
            _run_normal_scan,
            trigger=CronTrigger(day_of_week="mon-fri", hour=int(hour), minute=int(minute)),
            args=(dry_run,),
            id=f"normal_scan_{hour}_{minute}",
        )

    # Watch mode monitor: default every 20 minutes, with rescheduling driven by conditions
    scheduler.add_job(_watch_monitor, 'interval', minutes=20, id='watch_monitor')

    # Active trade monitor: default every 10 minutes, with faster polling when risk is elevated
    scheduler.add_job(_active_monitor, 'interval', minutes=10, id='active_monitor')

    logger.info("Scheduler starting (background)")
    scheduler.start()


def shutdown():
    scheduler.shutdown(wait=False)
