"""Event-driven scheduler for the trading bot.

Schedules all recurring tasks:
  - Pre-market macro briefing        08:00 CT  Mon-Fri
  - Normal A+ watchlist scans        every 30 min, 08:30–16:00 ET  Mon-Fri
                                     (starts 1h before the open; pre-open scans
                                      screen only and never place trades)
  - Daily zone-marking watchlist     20:00 ET Sun-Thu + 06:00 ET Mon-Fri
  - Watch mode monitor               every 20 min (adaptive)
  - Active trade monitor             every 10 min (adaptive)
  - After-hours intelligence scan    16:30 CT  Mon-Fri
  - Weekly performance report        09:00 CT  Saturday
  - Dynamic watchlist rebuild        20:00 CT  Sunday

DRY_RUN is respected throughout: scans always run, trade placement is skipped.
"""
import re
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from loguru import logger

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from alpaca_broker import is_kill_switch_engaged, get_positions
from config import (
    ACCOUNT,
    HARD_RULES,
    CT_TIMEZONE,
    ET_TIMEZONE,
    MARKET_OPEN_ET,
    MARKET_CLOSE_ET,
    SCAN_PREOPEN_LEAD_MIN,
    SCAN_INTERVAL_MIN,
    SCAN_OPEN_POWER_INTERVAL_MIN,
    SCAN_OPEN_POWER_DURATION_MIN,
    WATCHLIST_ALERT_EVENING_ET,
    WATCHLIST_ALERT_MORNING_ET,
    PUT_SUPPORT,
    FORCE_EXECUTION,
    MAX_LOSS,
)
from db import (
    get_mistake_patterns,
    get_open_trades,
    get_today_trade_count,
    get_today_realized_pnl,
    get_total_realized_pnl,
    get_trades_entered_today,
    get_trades_exited_today,
)
from market_intelligence import get_macro_data, run_market_intelligence
from market_regime import BEARISH, BULLISH, NEUTRAL, get_market_regime, log_regime
from sector_strength import get_sector_strength, log_sector_strength
from zone_logic import evaluate_zone_alignment, log_zone_alignment
from config import ZONE_LOGIC
from notifier import (
    notify_daily_summary,
    notify_daily_watchlist,
    notify_market_regime,
    notify_sector_strength,
    notify_watch_mode,
    notify_portfolio_report,
    send_discord_message,
)
from state_manager import state_manager
from strategy_engine import run_full_scan, get_days_to_earnings
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

# Eastern timezone object for market-relative scheduling and the pre-open guard.
_ET = ZoneInfo(ET_TIMEZONE)

# Normal-scan cadence (Eastern time): every SCAN_INTERVAL_MIN minutes, starting
# SCAN_PREOPEN_LEAD_MIN before the open (default 08:30 ET) through the close
# (default 16:00 ET), inclusive. The pre-open slots screen only — execution is
# deferred to regular trading hours by _is_pre_market_et() inside _run_normal_scan.
_SCAN_START = (
    datetime(2000, 1, 1, *MARKET_OPEN_ET) - timedelta(minutes=SCAN_PREOPEN_LEAD_MIN)
)
_SCAN_START = (_SCAN_START.hour, _SCAN_START.minute)
_SCAN_END = tuple(MARKET_CLOSE_ET)
_SCAN_INTERVAL_MIN = SCAN_INTERVAL_MIN


def _is_pre_market_et() -> bool:
    """True when the current Eastern time is before the regular open (09:30 ET)."""
    now = datetime.now(_ET)
    return (now.hour, now.minute) < tuple(MARKET_OPEN_ET)


def _slot_range(start: tuple[int, int], end: tuple[int, int], step_min: int) -> list[tuple[int, int]]:
    """(hour, minute) slots from start to end inclusive at a fixed minute step."""
    out = []
    cursor = datetime(2000, 1, 1, *start)
    stop = datetime(2000, 1, 1, *end)
    while cursor <= stop:
        out.append((cursor.hour, cursor.minute))
        cursor += timedelta(minutes=step_min)
    return out


def _normal_scan_times() -> list[tuple[int, int]]:
    """ET scan slots: baseline 30-min grid with a faster 5-min opening power hour.

    The baseline grid runs every SCAN_INTERVAL_MIN across the whole window
    (08:30–16:00 ET). On top of that, the first SCAN_OPEN_POWER_DURATION_MIN
    after the open (09:30–10:30 ET) is overlaid at SCAN_OPEN_POWER_INTERVAL_MIN
    so the bot actively hunts entries while the morning move is fresh. The two
    sets are merged and de-duplicated, so e.g. 09:30/10:00/10:30 appear once.
    """
    baseline = _slot_range(_SCAN_START, _SCAN_END, _SCAN_INTERVAL_MIN)

    power_end = (
        datetime(2000, 1, 1, *MARKET_OPEN_ET)
        + timedelta(minutes=SCAN_OPEN_POWER_DURATION_MIN)
    )
    power = _slot_range(
        tuple(MARKET_OPEN_ET),
        (power_end.hour, power_end.minute),
        SCAN_OPEN_POWER_INTERVAL_MIN,
    )

    slots = sorted(set(baseline) | set(power))
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
# DAILY WATCHLIST NOTIFICATION (zone-marking heads-up)
# ─────────────────────────────────────────────────────────────

def _zone_label_for_bias(bias: str) -> str:
    """Map a directional bias to the TradingView zone-marking instruction."""
    return {
        "CALL": "mark DEMAND zones",
        "PUT": "mark SUPPLY zones",
    }.get(bias, "mark BOTH")


def _build_watchlist_entries(
    actionable: list[dict],
    watch_candidates: list[dict],
    sectors,
    top_n: int = 8,
) -> list[dict]:
    """Assemble the bot's own top screened picks into watchlist entries.

    Read-only over the scan results — it never touches execution, sizing or any
    gate. Actionable A+ signals (already ranked) come first as primary picks
    with their AI confidence; filtered watch candidates fill the remainder as
    secondary picks with their technical screen score. Each entry carries the
    bias, sector/theme, confidence and a brief reason, plus the zone-marking
    label the human acts on in TradingView.
    """
    entries: list[dict] = []
    seen: set[str] = set()

    for sig in actionable:
        symbol = sig.get("symbol")
        if not symbol or symbol in seen:
            continue
        plan = sig.get("trade_plan", {}) or {}
        technical = sig.get("technical", {}) or {}
        opt = str(plan.get("option_type") or "").upper()
        bias = opt if opt in ("CALL", "PUT") else "NEUTRAL"
        reason = plan.get("thesis") or technical.get("signal") or "A+ technical setup"
        entries.append({
            "symbol": symbol,
            "bias": bias,
            "zone_label": _zone_label_for_bias(bias),
            "sector": sectors.group_for(symbol),
            "sector_rank": sectors.rank_for(symbol),
            "confidence_display": f"{int(plan.get('confidence', 0) or 0)}/10",
            "reason": reason,
        })
        seen.add(symbol)

    for wc in watch_candidates:
        symbol = wc.get("symbol")
        if not symbol or symbol in seen:
            continue
        technical = wc.get("technical", {}) or {}
        option_plan = wc.get("option_plan", technical.get("suggested_option", {})) or {}
        opt = str(option_plan.get("option_type") or "").upper()
        bias = opt if opt in ("CALL", "PUT") else "NEUTRAL"
        reasons = wc.get("reasons") or []
        reason = ", ".join(reasons) if reasons else technical.get("signal", "Watch candidate")
        entries.append({
            "symbol": symbol,
            "bias": bias,
            "zone_label": _zone_label_for_bias(bias),
            "sector": sectors.group_for(symbol),
            "sector_rank": sectors.rank_for(symbol),
            "confidence_display": f"score {wc.get('score', technical.get('bull_score', 'n/a'))} (watch)",
            "reason": reason,
        })
        seen.add(symbol)

    return entries[:top_n]


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

    # Pre-open scans (before 09:30 ET) screen only: route them through the
    # existing DRY_RUN path so they evaluate signals and feed watch mode without
    # placing any trades. This keeps execution strictly within regular trading
    # hours even though scanning now starts an hour before the open.
    if not dry_run and _is_pre_market_et():
        logger.info(
            "Pre-market scan (before 09:30 ET) — screening only; "
            "execution deferred to regular trading hours"
        )
        _journal("PRE-MARKET SCAN | screening only, no execution")
        dry_run = True

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
        blocked_regime = blocked_sector = blocked_zone = blocked_earnings = 0

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

            # ── 9/10 FORCE EXECUTION ATTEMPT ─────────────────────
            # A setup graded at/above FORCE_EXECUTION['min_confidence'] MUST be
            # carried through to an order attempt. The SOFT gates below (regime,
            # sector, zone, neutral-bump confidence) only log a bypass instead of
            # routing the setup to WATCH. HARD safety gates (earnings, and every
            # check inside place_options_trade / validate_trade) still apply.
            force = (
                bool(FORCE_EXECUTION.get("enabled", True))
                and confidence >= int(FORCE_EXECUTION.get("min_confidence", 9))
            )
            if force:
                fmsg = (
                    f"🔥 9/10 FORCE EXECUTION ATTEMPT | {symbol} | {option_type} | "
                    f"confidence={confidence}/10"
                )
                logger.info(fmsg)
                _journal(fmsg)
                send_discord_message(fmsg)

            if not is_put and regime.blocks_long_calls and option_type in ("CALL", "LONG"):
                if force:
                    logger.info(
                        f"Soft gate bypassed due to 9/10 | {symbol} | market regime "
                        f"{regime.regime} (long/call) — proceeding (size reduced)"
                    )
                    send_discord_message(
                        f"⚙️ Soft gate bypassed due to 9/10 | {symbol} | market regime "
                        f"{regime.regime}"
                    )
                else:
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
                        if force:
                            logger.info(
                                f"Soft gate bypassed due to 9/10 | {symbol} | "
                                f"PUT in bullish regime — proceeding (size reduced)"
                            )
                            send_discord_message(
                                f"⚙️ Soft gate bypassed due to 9/10 | {symbol} | "
                                f"PUT in bullish regime"
                            )
                        else:
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
                if force:
                    logger.info(
                        f"Soft gate bypassed due to 9/10 | {symbol} | sector "
                        f"{sector_gate['group']} {sector_gate['rank']} — proceeding (size reduced)"
                    )
                    send_discord_message(
                        f"⚙️ Soft gate bypassed due to 9/10 | {symbol} | sector "
                        f"{sector_gate['group']} {sector_gate['rank']}"
                    )
                else:
                    logger.info(msg)
                    send_discord_message(f"🚫 {msg}")
                    blocked_sector += 1
                    continue

            # Gate 0c: supply/demand zone alignment (Phase 4B). Consumes the
            # zones stored by the Phase 4A TradingView receiver. Uses the nearest
            # active, non-stale zone for the symbol to:
            #   CALL → prefer DEMAND, reject if too close to SUPPLY, boost near DEMAND
            #   PUT  → prefer SUPPLY, reject if too close to DEMAND, boost near SUPPLY
            # A REJECT stops here; a PREFER nudges the confidence used by Gate 1.
            current_price = (signal.get("technical", {}) or {}).get("price")
            zone_eval = evaluate_zone_alignment(option_type, symbol, current_price)
            log_zone_alignment(symbol, option_type, zone_eval)
            _journal(
                f"ZONE {symbol} | {option_type} {zone_eval['action']} | "
                f"score_adj={zone_eval['score_adjustment']:+d} | {zone_eval['note']}"
            )
            if zone_eval["action"] == "REJECT":
                msg = f"{option_type} rejected by zone logic | {symbol} | {zone_eval['note']}"
                if force:
                    logger.info(
                        f"Soft gate bypassed due to 9/10 | {symbol} | zone "
                        f"{zone_eval['note']} — proceeding (no zone boost applied)"
                    )
                    send_discord_message(
                        f"⚙️ Soft gate bypassed due to 9/10 | {symbol} | zone REJECT "
                        f"({zone_eval['note']})"
                    )
                else:
                    logger.info(msg)
                    send_discord_message(f"🚫 {msg}")
                    blocked_zone += 1
                    continue

            # Apply the zone score boost to the confidence used for gating,
            # capped so a boost can never exceed the configured ceiling.
            zone_boost = int(zone_eval.get("score_adjustment", 0) or 0)
            effective_confidence = min(
                confidence + zone_boost, int(ZONE_LOGIC.get("max_confidence", 10))
            )
            if zone_boost:
                logger.info(
                    f"ZONE BOOST {symbol} | confidence {confidence} → "
                    f"{effective_confidence} (+{zone_boost}) | {zone_eval['note']}"
                )

            # Gate 0d: earnings proximity (IV-crush avoidance). Block NEW entries
            # within HARD_RULES['earnings_block_days'] of the next earnings date.
            # Fail-open: an unknown earnings date does NOT block (only a
            # positively-known, near date rejects).
            earnings_block_days = int(HARD_RULES.get("earnings_block_days", 0) or 0)
            if earnings_block_days > 0:
                dte_earnings = get_days_to_earnings(symbol)
                if dte_earnings is not None and dte_earnings <= earnings_block_days:
                    msg = (
                        f"{option_type} blocked near earnings | {symbol} | "
                        f"{dte_earnings}d to earnings (<= {earnings_block_days}d)"
                    )
                    if force:
                        # HARD safety: earnings block is configured as a hard rule.
                        hmsg = (
                            f"🛑 9/10 blocked by HARD SAFETY: earnings in {dte_earnings}d "
                            f"(<= {earnings_block_days}d) | {symbol}"
                        )
                        logger.warning(hmsg)
                        _journal(hmsg)
                        send_discord_message(hmsg)
                    else:
                        logger.info(msg)
                        _journal(f"EARNINGS BLOCK: {symbol} | {dte_earnings}d to earnings")
                        send_discord_message(f"🚫 {msg}")
                    blocked_earnings += 1
                    continue

            # Gate 1: confidence threshold (raised in NEUTRAL regimes, plus any
            # zone score boost applied above)
            if effective_confidence < effective_min_confidence:
                if force:
                    logger.info(
                        f"Soft gate bypassed due to 9/10 | {symbol} | confidence "
                        f"{effective_confidence}/10 < threshold {effective_min_confidence} "
                        f"(neutral-regime bump) — proceeding"
                    )
                else:
                    logger.info(
                        f"SKIP {symbol} | confidence {effective_confidence}/10 < "
                        f"threshold {effective_min_confidence} — logged, not executed"
                    )
                    _journal(
                        f"SKIP (confidence {effective_confidence}/10 < {effective_min_confidence}): "
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

            # 9/10 size floor: a forced setup that passed a soft block (e.g. a
            # BEARISH-regime call sized at 0.0) must still carry a real budget so
            # the order attempt is meaningful. Floor the multiplier, never raise
            # it above the normally-computed size.
            if force:
                floor = float(FORCE_EXECUTION.get("min_size_multiplier", 0.5))
                if effective_size_multiplier < floor:
                    logger.info(
                        f"9/10 size floor | {symbol} | {effective_size_multiplier:.0%} → "
                        f"{floor:.0%} (forced attempt needs a tradeable budget)"
                    )
                    effective_size_multiplier = floor

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

            # Execute. `force` flows into the executor so it relaxes its own SOFT
            # filters (low/moderate volume, borderline R/R above the hard floor)
            # while keeping every HARD safety control enforced.
            logger.info(f"Executing paper trade: {symbol} | confidence={confidence}/10")
            result = place_options_trade(symbol, trade_plan, account, force=force)
            if result.get("success"):
                msg = (
                    f"EXECUTED: {symbol} | order={result.get('order_id')} | "
                    f"confidence={confidence}/10"
                )
                logger.info(msg)
                _journal(msg)
                if force:
                    omsg = (
                        f"✅ 9/10 order submitted | {symbol} | "
                        f"order={result.get('order_id')} | confidence={confidence}/10"
                    )
                    logger.info(omsg)
                    send_discord_message(omsg)
                executed += 1
            else:
                reason = result.get("reason")
                msg = f"REJECTED: {symbol} | {reason}"
                logger.warning(msg)
                _journal(msg)
                if force:
                    # Distinguish a contract/option-selection failure from a hard
                    # account/risk safety block for clearer 9/10 telemetry.
                    low = str(reason).lower()
                    selection_keys = (
                        "contract", "underlying", "liquidity", "quote",
                        "spread", "option price", "too expensive",
                    )
                    if any(k in low for k in selection_keys):
                        fmsg = f"🛑 9/10 option selection failed: {reason} | {symbol}"
                    else:
                        fmsg = f"🛑 9/10 blocked by HARD SAFETY: {reason} | {symbol}"
                    logger.warning(fmsg)
                    _journal(fmsg)
                    send_discord_message(fmsg)
                rejected += 1

        logger.info(
            f"Execution summary | "
            f"regime={regime.regime} | "
            f"executed={executed} | "
            f"dry_run_skipped={skipped_dryrun} | "
            f"below_confidence={skipped_confidence} | "
            f"regime_blocked={blocked_regime} | "
            f"sector_blocked={blocked_sector} | "
            f"zone_blocked={blocked_zone} | "
            f"earnings_blocked={blocked_earnings} | "
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
# DAILY ZONE-MARKING WATCHLIST — 20:00 ET (Sun-Thu) + 06:00 ET (Mon-Fri)
# ─────────────────────────────────────────────────────────────

def _run_watchlist_notification(slot_label: str = "") -> None:
    """Screen the universe and post the daily zone-marking watchlist to Discord.

    Runs the SAME selection stack the live scan uses — market regime, sector
    strength and the AI A+ scan (run_full_scan) — but is NOTIFICATION ONLY: it
    never places a trade, never creates a TradingView zone, and touches none of
    the execution gates. It exists so the human gets the bot's top candidates
    ahead of the open (8 PM the evening before and 6 AM ET) with time to draw
    supply/demand zones manually in TradingView, which the bot then treats as
    confirmation/context.
    """
    logger.info(f"DAILY WATCHLIST screen starting{(' | ' + slot_label) if slot_label else ''}")
    try:
        account = get_account_status()
        macro = get_macro_data()

        # Market regime → sector strength (same order as the live scan funnel).
        regime = get_market_regime()
        log_regime(regime)
        sectors = get_sector_strength(
            spy_return=regime.spy.get("return_21d"),
            qqq_return=regime.qqq.get("return_21d"),
        )
        log_sector_strength(sectors)

        # AI A+ screen / scoring. No execution path is invoked.
        scan_result = run_full_scan(macro, account)
        actionable = scan_result.get("actionable", [])
        watch_candidates = scan_result.get("watch_candidates", [])

        # Surface the strongest picks first (confidence desc), mirroring how the
        # live scan prioritises before caps bind.
        actionable.sort(
            key=lambda s: int((s.get("trade_plan", {}) or {}).get("confidence", 0) or 0),
            reverse=True,
        )

        entries = _build_watchlist_entries(actionable, watch_candidates, sectors, top_n=8)
        if not entries:
            logger.info("Daily watchlist: no candidates to post")
            _journal(f"DAILY WATCHLIST{(' | ' + slot_label) if slot_label else ''} | no candidates")
            return

        notify_daily_watchlist(entries, regime=regime.regime, top_n=8)
        logger.info(
            f"Daily watchlist posted | {len(entries)} candidates | regime={regime.regime}"
            f"{(' | ' + slot_label) if slot_label else ''}"
        )
        _journal(
            "DAILY WATCHLIST | "
            + (f"{slot_label} | " if slot_label else "")
            + ", ".join(f"{e['symbol']}({e['bias']})" for e in entries)
        )
    except Exception as exc:
        logger.error(f"Daily watchlist screen failed: {exc}", exc_info=True)
        _journal(f"DAILY WATCHLIST ERROR: {exc}")


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

def _capital_exposure(equity: float, cash: float, options_bp: float, deployed: float,
                      n_pos: int, max_open: int, intended_trade_size: float) -> tuple:
    """Capital-allocation / exposure-control dashboard. Pure function.

    Returns (dashboard_text, warnings_list). For long options, the capital
    deployed equals the positions' current market value, and the max loss if
    every option expired worthless is that same market value (a long option can
    lose 100% of its current value).
    """
    pct_deployed = (deployed / equity) if equity else 0.0
    max_loss_to_zero = deployed
    pct_at_risk = (max_loss_to_zero / equity) if equity else 0.0
    remaining = max(0, max_open - n_pos)

    warnings = []
    if pct_at_risk > 0.60:
        warnings.append(
            f"🔴 High exposure warning: open option risk ${max_loss_to_zero:,.2f} is "
            f"{pct_at_risk:.0%} of equity (> 60%)"
        )
    if options_bp < intended_trade_size:
        warnings.append(
            f"🟠 Low buying power warning: options BP ${options_bp:,.2f} is below the next "
            f"allowed trade size ${intended_trade_size:,.2f}"
        )
    if remaining <= 0:
        warnings.append(f"🟡 Max positions warning: position slots full ({n_pos}/{max_open})")

    dashboard = (
        f"**Capital Allocation & Exposure**\n"
        f"Equity ${equity:,.2f} | Cash ${cash:,.2f} | Options BP ${options_bp:,.2f}\n"
        f"Deployed capital ${deployed:,.2f} ({pct_deployed:.0%} of account)\n"
        f"Max loss if all options → $0: ${max_loss_to_zero:,.2f} "
        f"({pct_at_risk:.0%} of account at risk)\n"
        f"Positions {n_pos}/{max_open} | remaining slots {remaining}"
    )
    return dashboard, warnings


def _run_portfolio_report(slot_label: str, sent_log: str) -> None:
    """Scheduled portfolio P/L report → Discord (read-only).

    Gathers account balances, open positions, per-position P/L + exit plan, and
    next-trade capacity, then posts a Discord embed. Touches NO execution, watch
    state, or webhook logic. `sent_log` is the exact confirmation line to log.
    """
    try:
        account = get_account_status()
        if account.get("error"):
            logger.warning(f"Portfolio report skipped — account error: {account['error']}")
            return

        equity = float(account.get("equity") or account.get("portfolio_value") or 0)
        last_equity = float(account.get("last_equity") or equity)
        cash = float(account.get("cash") or 0)
        options_bp = float(account.get("options_buying_power") or account.get("buying_power") or 0)
        day_pl = round(equity - last_equity, 2)
        realized_today = round(get_today_realized_pnl(), 2)
        realized_total = round(get_total_realized_pnl(), 2)

        positions = {p["symbol"]: p for p in get_positions()}
        trades = get_open_trades()
        n_pos = len(trades)

        unrealized_total = 0.0
        deployed_mv = 0.0
        lines = []
        for t in trades:
            p = positions.get(t.get("option_symbol"), {})
            opt_type = str(t.get("trade_type") or "CALL").upper()
            qty = int(t.get("contracts") or 0)
            entry = float(t.get("entry_option_price") or 0)
            cur = float(p.get("current_price") or 0)
            upl = float(p.get("unrealized_pl") or 0)
            unrealized_total += upl
            deployed_mv += float(p.get("market_value") or 0)
            gain = (cur - entry) / entry if entry else 0.0
            stop_prem = round(entry * (1 - HARD_RULES["max_option_loss_pct"]), 2)
            t1 = float(t.get("target_1") or 0)
            t2 = float(t.get("target_2") or 0)

            # Hold / scale-out / exit decision under the current exit rules.
            if t2 and cur >= t2:
                plan = "EXIT runner (+100% target hit)"
            elif t1 and cur >= t1:
                plan = "SCALE OUT half (+30% target hit)"
            elif stop_prem and cur <= stop_prem:
                plan = "EXIT (stop hit)"
            elif gain >= HARD_RULES["breakeven_trigger_gain"]:
                plan = "HOLD (breakeven stop armed)"
            else:
                plan = "HOLD"

            lines.append(
                f"**{t.get('symbol')} {opt_type}** ×{qty} | strike ${t.get('strike')} exp {t.get('expiry')}\n"
                f"  entry ${entry:.2f} → now ${cur:.2f} | P/L ${upl:+,.2f} ({gain:+.1%})\n"
                f"  stop ${stop_prem:.2f} | target ${t1:.2f} (+30%) / ${t2:.2f} (+100%) | plan: {plan}"
            )
        unrealized_total = round(unrealized_total, 2)

        # Next-trade capacity analysis.
        max_risk_next = round(min(MAX_LOSS["max_loss_pct_of_equity"] * equity, options_bp), 2)
        kill_on, kill_reason = is_kill_switch_engaged()
        trades_today = get_today_trade_count()
        pos_room = n_pos < ACCOUNT["max_open_positions"]
        trade_room = trades_today < ACCOUNT["max_trades_per_day"]
        can_trade = (not kill_on) and pos_room and trade_room and max_risk_next > 0
        blocks = []
        if kill_on:
            blocks.append(f"kill switch ({kill_reason})")
        if not pos_room:
            blocks.append(f"max positions {n_pos}/{ACCOUNT['max_open_positions']}")
        if not trade_room:
            blocks.append(f"max trades/day {trades_today}/{ACCOUNT['max_trades_per_day']}")
        if max_risk_next <= 0:
            blocks.append("no risk budget")
        can_trade_txt = "YES" if can_trade else f"NO — {', '.join(blocks)}"

        # Capital allocation / exposure dashboard + warnings. "Next allowed trade
        # size" is the intended 20%-of-equity max-loss budget (before the BP cap).
        intended_trade_size = round(MAX_LOSS["max_loss_pct_of_equity"] * equity, 2)
        dashboard, warnings = _capital_exposure(
            equity, cash, options_bp, round(deployed_mv, 2), n_pos,
            ACCOUNT["max_open_positions"], intended_trade_size,
        )
        warn_block = ("**⚠️ Warnings**\n" + "\n".join(warnings) + "\n\n") if warnings else ""

        body = (
            warn_block
            + f"**Account**\n"
            f"Equity ${equity:,.2f} | Cash ${cash:,.2f} | Options BP ${options_bp:,.2f}\n"
            f"Day P/L ${day_pl:+,.2f} | Unrealized ${unrealized_total:+,.2f} | "
            f"Realized today ${realized_today:+,.2f} (all-time ${realized_total:+,.2f})\n\n"
            + dashboard + "\n\n"
            f"**Open positions: {n_pos}/{ACCOUNT['max_open_positions']}**\n"
            + ("\n".join(lines) if lines else "_None_")
            + "\n\n**Next trade**\n"
            f"Remaining options buying power ${options_bp:,.2f}\n"
            f"Max allowed risk (20% equity, capped by BP): ${max_risk_next:,.2f} "
            f"→ max premium ${max_risk_next / 100:,.2f}/share (1 contract)\n"
            f"Can take another trade: {can_trade_txt}"
        )

        title = f"📊 {slot_label} Portfolio Report"
        color = 0x2ECC71 if (day_pl + unrealized_total) >= 0 else 0xE74C3C
        notify_portfolio_report(title, body, color=color)
        logger.info(sent_log)
        _journal(
            f"{slot_label.upper()} PORTFOLIO REPORT | equity=${equity:,.2f} "
            f"day_pl=${day_pl:+,.2f} unrealized=${unrealized_total:+,.2f} positions={n_pos}"
        )
    except Exception as exc:
        logger.error(f"Portfolio report failed: {exc}", exc_info=True)

    # The daily learning summary is sent AFTER the post-market portfolio report.
    # It has its own try/except so it can never break the portfolio report.
    if slot_label == "Post-Market":
        _run_daily_learning_summary()


def _summarize_day_from_journal() -> dict:
    """Digest today's trade journal into deduped categories for the learning
    summary. Read-only; tolerant of malformed lines.

    Returns {executed_910, failed_910, rejected, soft_blocked} where the maps are
    {symbol: reason} keyed last-write-wins, so repeated per-scan lines collapse to
    one entry per symbol.
    """
    today = datetime.now().date().isoformat()
    try:
        lines = [ln for ln in _JOURNAL_FILE.read_text(encoding="utf-8").splitlines()
                 if ln.startswith(today)]
    except Exception:
        lines = []

    executed_910, failed_910, rejected, soft_blocked = {}, {}, {}, {}
    for ln in lines:
        msg = ln.split(" | ", 1)[1] if " | " in ln else ln

        m = re.search(r"9/10 (?:option selection failed|blocked by HARD SAFETY):\s*(.*?)\s*\|\s*(\S+)\s*$", msg)
        if m:
            failed_910[m.group(2)] = m.group(1).strip()
            continue
        m = re.match(r"EXECUTED:\s*(\S+)\s*\|.*confidence=(\d+)/10", msg)
        if m and int(m.group(2)) >= int(FORCE_EXECUTION.get("min_confidence", 9)):
            executed_910[m.group(1)] = f"confidence {m.group(2)}/10"
            continue
        m = re.match(r"REJECTED:\s*(\S+)\s*\|\s*(.*)$", msg)
        if m:
            rejected[m.group(1)] = m.group(2).strip()
            continue
        m = re.match(r"REGIME BLOCK[^:]*:\s*(\S+)\s*\|", msg) or re.match(r"EARNINGS BLOCK:\s*(\S+)\s*\|", msg)
        if m:
            soft_blocked[m.group(1)] = "regime/earnings block"
    return {
        "executed_910": executed_910,
        "failed_910": failed_910,
        "rejected": rejected,
        "soft_blocked": soft_blocked,
    }


def _run_daily_learning_summary() -> None:
    """End-of-day performance journal + learning digest → Discord (read-only).

    Summarizes the day's entries/exits/open trades, winners/losers, blocked and
    9/10 outcomes, day P/L and equity change, then derives non-binding lessons
    and rule-adjustment SUGGESTIONS. It NEVER changes any rule automatically.
    """
    try:
        account = get_account_status()
        equity = float(account.get("equity") or account.get("portfolio_value") or 0)
        last_equity = float(account.get("last_equity") or equity)
        day_pl = round(equity - last_equity, 2)
        realized_today = round(get_today_realized_pnl(), 2)

        entered = get_trades_entered_today()
        exited = get_trades_exited_today()
        open_trades = get_open_trades()
        digest = _summarize_day_from_journal()

        winners = [t for t in exited if float(t.get("pnl_dollars") or 0) > 0]
        losers = [t for t in exited if float(t.get("pnl_dollars") or 0) < 0]
        biggest_win = max(exited, key=lambda t: float(t.get("pnl_dollars") or 0), default=None)
        biggest_loss = min(exited, key=lambda t: float(t.get("pnl_dollars") or 0), default=None)

        def _fmt_entered(rows):
            return ", ".join(
                f"{t.get('symbol')} {str(t.get('trade_type') or 'CALL').upper()} "
                f"×{int(t.get('contracts') or 0)} @ ${float(t.get('entry_option_price') or 0):.2f}"
                for t in rows
            ) or "none"

        def _fmt_exited(rows):
            return ", ".join(
                f"{t.get('symbol')} (${float(t.get('pnl_dollars') or 0):+,.0f}, "
                f"{t.get('exit_reason') or 'exit'})" for t in rows
            ) or "none"

        def _fmt_map(m, limit=8):
            items = list(m.items())[:limit]
            extra = f" +{len(m) - limit} more" if len(m) > limit else ""
            return ("; ".join(f"{s}: {r}" for s, r in items) + extra) if items else "none"

        # ── Lessons learned (data-driven, descriptive) ──────────────────────
        lessons = []
        if exited:
            wr = len(winners) / len(exited)
            avg_w = sum(float(t.get("pnl_dollars") or 0) for t in winners) / max(len(winners), 1)
            avg_l = sum(float(t.get("pnl_dollars") or 0) for t in losers) / max(len(losers), 1)
            lessons.append(
                f"Closed {len(exited)} trade(s): win rate {wr:.0%} "
                f"({len(winners)}W/{len(losers)}L), avg win ${avg_w:+,.0f}, avg loss ${avg_l:+,.0f}."
            )
            if biggest_loss and (biggest_loss.get("exit_reason") == "STOP"):
                lessons.append("Largest loss exited via STOP — risk control worked as designed.")
        else:
            lessons.append("No trades closed today; open positions still developing.")
        bp_fails = [s for s, r in digest["failed_910"].items() if "buying power" in r.lower()]
        if bp_fails:
            lessons.append(
                f"{len(bp_fails)} 9/10 setup(s) missed on buying power "
                f"({', '.join(bp_fails[:6])}) — capital was the binding constraint."
            )
        if digest["executed_910"]:
            lessons.append(f"{len(digest['executed_910'])} 9/10 setup(s) executed: "
                           f"{', '.join(digest['executed_910'])}.")
        if not entered and (digest["rejected"] or digest["failed_910"]):
            lessons.append("All candidates filtered before fill — no new entries today.")

        # ── Rule-adjustment SUGGESTIONS (never auto-applied) ────────────────
        suggestions = []
        if bp_fails:
            suggestions.append("Consider freeing capital (close a target-hit position) or trimming "
                               "per-trade size so more high-conviction 9/10 setups can fill.")
        if exited and len(losers) > len(winners) and len(exited) >= 3:
            suggestions.append("Loss count exceeded wins today — review entry timing / setup quality "
                               "before loosening any gate.")
        sel_fails = [s for s, r in digest["failed_910"].items()
                     if any(k in r.lower() for k in ("contract", "underlying", "liquidity"))]
        if sel_fails:
            suggestions.append(f"{len(sel_fails)} 9/10 setup(s) failed at option selection/liquidity "
                               f"({', '.join(sel_fails[:6])}) — review contract/liquidity criteria.")
        if not suggestions:
            suggestions.append("No adjustments suggested today.")

        body = (
            f"**Activity**\n"
            f"Entered: {len(entered)} ({_fmt_entered(entered)})\n"
            f"Exited: {len(exited)} ({_fmt_exited(exited)})\n"
            f"Still open: {len(open_trades)} ({', '.join(t.get('symbol') for t in open_trades) or 'none'})\n\n"
            f"**Results**\n"
            f"Winners: {len(winners)} | Losers: {len(losers)}\n"
            f"Biggest winner: {biggest_win.get('symbol') if biggest_win else '—'} "
            f"(${float((biggest_win or {}).get('pnl_dollars') or 0):+,.0f})\n"
            f"Biggest loser: {biggest_loss.get('symbol') if biggest_loss else '—'} "
            f"(${float((biggest_loss or {}).get('pnl_dollars') or 0):+,.0f})\n"
            f"Day P/L: ${day_pl:+,.2f} | Realized today: ${realized_today:+,.2f} | "
            f"Equity change: ${day_pl:+,.2f}\n\n"
            f"**9/10 outcomes**\n"
            f"Executed: {_fmt_map(digest['executed_910'])}\n"
            f"Failed: {_fmt_map(digest['failed_910'])}\n\n"
            f"**Blocked setups**\n"
            f"Rejected: {_fmt_map(digest['rejected'])}\n"
            f"Regime/earnings: {_fmt_map(digest['soft_blocked'])}\n\n"
            f"**Lessons learned**\n- " + "\n- ".join(lessons) + "\n\n"
            f"**Rule-adjustment suggestions (NOT auto-applied)**\n- " + "\n- ".join(suggestions) +
            "\n\n_No rules were changed automatically._"
        )

        color = 0x2ECC71 if day_pl >= 0 else 0xE74C3C
        notify_portfolio_report("🧠 Daily Learning Summary", body, color=color)
        logger.info("Daily learning summary sent")
        _journal(
            f"DAILY LEARNING SUMMARY | entered={len(entered)} exited={len(exited)} "
            f"open={len(open_trades)} day_pl=${day_pl:+,.2f}"
        )
    except Exception as exc:
        logger.error(f"Daily learning summary failed: {exc}", exc_info=True)


def start(dry_run: bool) -> None:
    """Register all jobs and start the background scheduler."""

    # Normal scans — every 30 min in EASTERN time across the scan window
    # (08:30–16:00 ET by default: 1h before the open through the close), Mon-Fri.
    # Pre-open slots screen only (see _run_normal_scan's pre-market guard). Only
    # the first slot fetches live Perplexity research; the rest are cache-only,
    # so the higher cadence does not increase Perplexity calls.
    for idx, (hour, minute) in enumerate(_normal_scan_times()):
        research_live = idx == 0
        scheduler.add_job(
            _run_normal_scan,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=hour,
                minute=minute,
                timezone=ET_TIMEZONE,
            ),
            args=(dry_run, research_live),
            id=f"normal_scan_{hour:02d}_{minute:02d}_et",
        )

    # Daily zone-marking watchlist alerts (Eastern time). Posted the evening
    # before each trading day (Sun-Thu) and again pre-open (Mon-Fri) so zones can
    # be drawn manually in TradingView before the market opens. Notification only.
    eve_h, eve_m = WATCHLIST_ALERT_EVENING_ET
    scheduler.add_job(
        _run_watchlist_notification,
        trigger=CronTrigger(
            # Evenings before each trading day. Listed explicitly because
            # APScheduler can't express the wrapping range "sun-thu".
            day_of_week="sun,mon,tue,wed,thu", hour=eve_h, minute=eve_m, timezone=ET_TIMEZONE
        ),
        args=(f"{eve_h:02d}:{eve_m:02d} ET evening (next-day prep)",),
        id="daily_watchlist_evening",
    )
    morn_h, morn_m = WATCHLIST_ALERT_MORNING_ET
    scheduler.add_job(
        _run_watchlist_notification,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=morn_h, minute=morn_m, timezone=ET_TIMEZONE
        ),
        args=(f"{morn_h:02d}:{morn_m:02d} ET pre-open",),
        id="daily_watchlist_morning",
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

    # Portfolio P/L reports — 1 hour BEFORE the open and 1 hour AFTER the close,
    # in Eastern market time, Mon-Fri. Derived from MARKET_OPEN_ET/CLOSE_ET so
    # they track the configured session (08:30 ET pre, 17:00 ET post by default).
    _pre_dt = datetime(2000, 1, 1, *MARKET_OPEN_ET) - timedelta(hours=1)
    _post_dt = datetime(2000, 1, 1, *MARKET_CLOSE_ET) + timedelta(hours=1)
    scheduler.add_job(
        _run_portfolio_report,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=_pre_dt.hour, minute=_pre_dt.minute, timezone=ET_TIMEZONE
        ),
        args=("Pre-Market", "Pre-market portfolio report sent"),
        id="portfolio_report_premarket",
    )
    scheduler.add_job(
        _run_portfolio_report,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=_post_dt.hour, minute=_post_dt.minute, timezone=ET_TIMEZONE
        ),
        args=("Post-Market", "Post-market portfolio report sent"),
        id="portfolio_report_postmarket",
    )

    mode = "DRY_RUN (log only)" if dry_run else "EXECUTE (paper trades active)"
    logger.info(f"Scheduler starting | mode={mode} | timezone={CT_TIMEZONE}")
    for job in scheduler.get_jobs():
        next_run = getattr(job, 'next_run_time', None) or getattr(job, '_get_run_times', None)
        logger.info(f"  Job registered: {job.id}")

    scheduler.start()


def shutdown() -> None:
    scheduler.shutdown(wait=False)
