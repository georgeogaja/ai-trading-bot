"""Event-driven scheduler for the trading bot.

Schedules all recurring tasks:
  - Pre-market macro briefing        08:00 CT  Mon-Fri
  - Normal A+ watchlist scans        every 30 min, 09:30–15:30 CT  Mon-Fri
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
from config import ACCOUNT, CT_TIMEZONE, PUT_SUPPORT
from db import get_mistake_patterns
from market_intelligence import get_macro_data, run_market_intelligence
from market_regime import BEARISH, BULLISH, NEUTRAL, get_market_regime, log_regime
from sector_strength import get_sector_strength, log_sector_strength
from notifier import (
    notify_daily_summary,
    notify_market_regime,
    notify_sector_strength,
    notify_watch_mode,
    send_discord_message,
)
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

# Last market regime observed, so the Discord regime summary only re-posts when
# the regime changes (the opening scan always posts an initial reading).
_last_regime: str | None = None

# Last (strongest, weakest) sector pair posted, so the sector summary only
# re-posts when the leadership changes (the opening scan always posts).
_last_sector_key: tuple | None = None

# Normal-scan cadence: every 30 min during market hours, 09:30–15:30 CT inclusive.
_SCAN_START = (9, 30)
_SCAN_END = (15, 30)
_SCAN_INTERVAL_MIN = 30


def _normal_scan_times() -> list[tuple[int, int]]:
    """Return (hour, minute) slots every 30 min from 09:30 to 15:30 CT inclusive."""
    slots = []
    cursor = datetime(2000, 1, 1, *_SCAN_START)
    end = datetime(2000, 1, 1, *_SCAN_END)
    while cursor <= end:
        slots.append((cursor.hour, cursor.minute))
        cursor += timedelta(minutes=_SCAN_INTERVAL_MIN)
    return slots


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
# NORMAL SCAN — every 30 min, 09:30–15:30 CT
# ─────────────────────────────────────────────────────────────

def _run_normal_scan(dry_run: bool, research_live: bool = False) -> None:
    """
    Full watchlist scan with conditional trade execution.

    Flow:
      1. Fetch account + macro
      2. Early-exit on account error, kill switch, or zero buying power
      3. Run full A+ scan via strategy_engine
      4. Cache Perplexity research for top candidates (no-op if key missing).
         Live Perplexity fetches only happen when research_live=True (the
         opening 09:30 scan); every later intraday scan reads cache-only so the
         30-minute cadence never increases Perplexity API usage.
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

        # ── 3b. Market regime (Phase 3A) ──────────────────────
        # Determine the overall market regime BEFORE evaluating any individual
        # stock. This sits at the top of the funnel and only produces gates;
        # the volume/risk sizing, kill switch, DRY_RUN and execution logic below
        # are all preserved unchanged.
        global _last_regime
        regime = get_market_regime()
        log_regime(regime)
        _journal(regime.summary_line)

        if regime.regime == BEARISH:
            posture = "CALLs blocked; confirmed PUT setups allowed at normal size."
        elif regime.regime == NEUTRAL:
            posture = "Reduced size and stronger confirmation; CALLs and PUTs both allowed."
        else:
            posture = "Normal CALL setups allowed; PUTs only if clearly weak vs market."

        # Post the regime summary to Discord on the opening scan or whenever the
        # regime changes, so the channel reflects the current regime without spam.
        if research_live or regime.regime != _last_regime:
            notify_market_regime(
                regime=regime.regime,
                score=regime.score,
                spy_trend=f"{regime.spy['trend']} ({regime.spy['score']}/3)",
                qqq_trend=f"{regime.qqq['trend']} ({regime.qqq['score']}/3)",
                vix=regime.vix,
                vol_source=regime.vol_source,
                posture=posture,
                reasons=regime.reasons,
            )
        _last_regime = regime.regime

        # Regime raises the confidence bar in NEUTRAL (stronger confirmation).
        effective_min_confidence = min_confidence + regime.confidence_bump
        if regime.confidence_bump:
            logger.info(
                f"Regime {regime.regime} | confidence threshold raised "
                f"{min_confidence} → {effective_min_confidence}"
            )

        # ── 3c. Sector strength (Phase 3C) ────────────────────
        # Rank sectors/themes vs the SPY/QQQ blend BEFORE ranking individual
        # stocks. Reuses the regime's SPY/QQQ trailing returns so no extra
        # benchmark fetch is needed. Produces gates/preferences only — regime
        # logic, PUT support and the volume/risk gates below are unchanged.
        global _last_sector_key
        sectors = get_sector_strength(
            spy_return=regime.spy.get("return_21d"),
            qqq_return=regime.qqq.get("return_21d"),
        )
        log_sector_strength(sectors)

        strongest = sectors.strongest
        weakest = sectors.weakest
        sector_key = (
            strongest[0][0] if strongest else None,
            weakest[0][0] if weakest else None,
        )
        if research_live or sector_key != _last_sector_key:
            notify_sector_strength(strongest, weakest, sectors.benchmark)
        _last_sector_key = sector_key

        # ── 4. A+ signal scan ─────────────────────────────────
        scan_result = run_full_scan(macro, account)
        actionable = scan_result.get("actionable", [])
        watch_candidates = scan_result.get("watch_candidates", [])

        # Prefer sector-aligned setups (CALL in STRONG / PUT in WEAK) so they are
        # evaluated first when caps (max positions / trades-per-day) bind. Within
        # the same alignment tier, higher confidence goes first.
        def _sector_priority(sig: dict) -> tuple:
            plan = sig.get("trade_plan", {}) or {}
            opt = str(plan.get("option_type") or "CALL").upper()
            aligned = 1 if sectors.gate(opt, sig.get("symbol")).get("action") == "PREFER" else 0
            return (aligned, int(plan.get("confidence", 0) or 0))

        actionable.sort(key=_sector_priority, reverse=True)

        logger.info(
            f"Scan complete | actionable={len(actionable)} | "
            f"watch_candidates={len(watch_candidates)}"
        )

        # ── 5. Research cache (best candidates only) ──────────
        research_targets = actionable or watch_candidates
        try:
            research_results = research_top_candidates(
                research_targets, macro, limit=3, cache_only=not research_live
            )
            for item in research_results:
                hit = "HIT" if item.get("cached") else "MISS"
                logger.info(f"Research cache {hit} for {item.get('symbol')}")
        except Exception as exc:
            logger.warning(f"Research caching skipped: {exc}")

        # ── 6. Trade execution ────────────────────────────────
        executed = skipped_confidence = skipped_dryrun = rejected = 0
        blocked_regime = blocked_sector = 0

        for signal in actionable:
            symbol = signal.get("symbol")
            trade_plan = signal.get("trade_plan", {})
            confidence = int(trade_plan.get("confidence", 0) or 0)
            option_type = str(trade_plan.get("option_type") or "CALL").upper()

            # Gate 0: market regime direction gating (Phase 3A + 3B).
            #
            #   CALL/LONG  → blocked in a BEARISH regime (aggressive longs stand
            #                down); allowed in NEUTRAL/BULLISH.
            #   PUT        → allowed in BEARISH; allowed in NEUTRAL (reduced size,
            #                stronger confirmation handled by the regime gates);
            #                in BULLISH it is blocked UNLESS the stock is clearly
            #                weak relative to the market.
            is_put = option_type == "PUT"

            if not is_put and regime.blocks_long_calls and option_type in ("CALL", "LONG"):
                logger.info(
                    f"BLOCKED {symbol} | MARKET REGIME: {regime.regime} — "
                    f"aggressive long/call setups blocked"
                )
                _journal(
                    f"REGIME BLOCK ({regime.regime}): {symbol} | {option_type} | "
                    f"thesis={str(trade_plan.get('thesis', ''))[:80]}"
                )
                blocked_regime += 1
                continue

            if is_put:
                technical = signal.get("technical", {})
                stock_ret = technical.get("return_21d")
                spy_ret = regime.spy.get("return_21d")
                rel_strength = (
                    round(stock_ret - spy_ret, 2)
                    if stock_ret is not None and spy_ret is not None
                    else None
                )

                if regime.regime == BULLISH:
                    # Only allow a PUT when the stock is clearly weak vs market.
                    weakness_floor = -abs(PUT_SUPPORT.get("bullish_regime_rel_weakness_pct", 3.0))
                    clearly_weak = rel_strength is not None and rel_strength <= weakness_floor
                    if not clearly_weak:
                        msg = (
                            f"PUT blocked by bullish regime | {symbol} | "
                            f"rel strength vs SPY="
                            f"{rel_strength if rel_strength is not None else 'n/a'}% "
                            f"(needs <= {weakness_floor}%)"
                        )
                        logger.info(msg)
                        _journal(f"REGIME BLOCK (BULLISH PUT): {symbol} | {msg}")
                        send_discord_message(f"🔻 {msg}")
                        blocked_regime += 1
                        continue
                    approve = (
                        f"PUT setup approved | {symbol} | clearly weak vs market "
                        f"(rel {rel_strength}%) despite BULLISH regime"
                    )
                    logger.info(approve)
                    send_discord_message(f"🔻 {approve}")
                elif regime.regime == NEUTRAL:
                    logger.info(
                        f"PUT reduced size due to neutral regime | {symbol} | "
                        f"size {regime.size_multiplier:.0%}, stronger confirmation required"
                    )
                else:  # BEARISH
                    approve = f"PUT setup approved | {symbol} | BEARISH regime favors downside"
                    logger.info(approve)
                    send_discord_message(f"🔻 {approve}")

            # Gate 0b: sector strength (Phase 3C). Prefer aligned setups, reduce
            # or reject contra ones (CALL in WEAK / PUT in STRONG). The reduce
            # factor is folded into the sizing block below; a REJECT stops here.
            sector_gate = sectors.gate(option_type, symbol)
            logger.info(
                f"Sector assigned: {symbol} → {sector_gate['group']} "
                f"({sector_gate['rank']}) | {sector_gate['note']}"
            )
            _journal(
                f"SECTOR {symbol} | {sector_gate['group']} {sector_gate['rank']} | "
                f"{option_type} {sector_gate['action']}"
            )
            if sector_gate["action"] == "REJECT":
                msg = (
                    f"{option_type} rejected by sector strength | {symbol} | "
                    f"{sector_gate['group']} is {sector_gate['rank']}"
                )
                logger.info(msg)
                send_discord_message(f"🚫 {msg}")
                blocked_sector += 1
                continue

            # Gate 1: confidence threshold (raised in NEUTRAL regimes)
            if confidence < effective_min_confidence:
                logger.info(
                    f"SKIP {symbol} | confidence {confidence}/10 < "
                    f"threshold {effective_min_confidence} — logged, not executed"
                )
                _journal(
                    f"SKIP (confidence {confidence}/10 < {effective_min_confidence}): "
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

            # Regime sizing (direction-aware). The Phase 3A multiplier was built
            # for the long side (BEARISH = 0.0 to stand longs down). PUTs invert
            # that: a BEARISH regime favors the short side, so PUTs size NORMAL
            # there, while NEUTRAL and (counter-regime) BULLISH PUTs are reduced.
            #   CALL: BULLISH 1.0 | NEUTRAL 0.5 | BEARISH blocked at Gate 0
            #   PUT : BEARISH 1.0 | NEUTRAL 0.5 | BULLISH 0.5 (only if allowed)
            # The reduction is applied before the executor's own volume-tier
            # sizing, so the two stack. We write max_to_spend onto both the
            # top-level and nested plan so resolve_trade_plan (nested wins) sees it.
            if is_put:
                regime_size = 1.0 if regime.regime == BEARISH else 0.5
            else:
                regime_size = regime.size_multiplier

            # Sector strength stacks on top of the regime factor: a contra-sector
            # CALL/PUT (action REDUCE) sizes down further; aligned/neutral = 1.0.
            sector_size = float(sector_gate.get("size_multiplier", 1.0))
            effective_size_multiplier = round(regime_size * sector_size, 4)

            if effective_size_multiplier != 1.0:
                for _plan in (trade_plan, trade_plan.get("trade_plan", {})):
                    if isinstance(_plan, dict) and _plan.get("max_to_spend"):
                        base_spend = float(_plan["max_to_spend"] or 0)
                        _plan["max_to_spend"] = round(base_spend * effective_size_multiplier, 2)
                logger.info(
                    f"Sizing | {symbol} ({option_type}) → {effective_size_multiplier:.0%} "
                    f"of normal (regime {regime.regime} {regime_size:.0%} × "
                    f"sector {sector_gate['rank']} {sector_size:.0%})"
                )

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
            f"regime={regime.regime} | "
            f"executed={executed} | "
            f"dry_run_skipped={skipped_dryrun} | "
            f"below_confidence={skipped_confidence} | "
            f"regime_blocked={blocked_regime} | "
            f"sector_blocked={blocked_sector} | "
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

    # Normal scans — every 30 min, 09:30–15:30 CT, Mon-Fri.
    # Only the opening scan fetches live Perplexity research; the rest are
    # cache-only, so the higher cadence does not increase Perplexity calls.
    for idx, (hour, minute) in enumerate(_normal_scan_times()):
        research_live = idx == 0
        scheduler.add_job(
            _run_normal_scan,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=hour,
                minute=minute,
                timezone=CT_TIMEZONE,
            ),
            args=(dry_run, research_live),
            id=f"normal_scan_{hour:02d}_{minute:02d}",
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
        next_run = getattr(job, 'next_run_time', None) or getattr(job, '_get_run_times', None)
        logger.info(f"  Job registered: {job.id}")

    scheduler.start()


def shutdown() -> None:
    scheduler.shutdown(wait=False)
