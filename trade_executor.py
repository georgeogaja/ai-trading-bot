"""
trade_executor.py
Executes trades via Alpaca API using the Alpaca broker abstraction.
Handles options order placement, position sizing, stop loss, profit targets, and Discord alerts.
"""

import yfinance as yf
from datetime import datetime
from loguru import logger
from config import ACCOUNT, FORCE_EXECUTION, HARD_RULES, OPTIONS_LIQUIDITY, POSITION_SIZING, RISK_REWARD
from db import (
    log_trade_entry,
    update_trade_exit,
    update_trade_fields,
    get_open_trades,
    log_runner_entry,
    update_runner,
    get_active_runners,
)
from learning_engine import record_trade_outcome
from alpaca_broker import (
    get_account_status as broker_get_account_status,
    get_positions as broker_get_positions,
    find_option_contract_symbol,
    find_best_option_contract,
    place_option_order,
    close_alpaca_position,
    is_kill_switch_engaged,
)
from option_liquidity import evaluate_liquidity, describe
from notifier import (
    notify_trade_entered,
    notify_trade_exited,
    notify_risk_blocked,
    notify_liquidity_blocked,
    notify_runner_created,
    notify_runner_stop_moved,
    notify_runner_target_hit,
    notify_runner_closed,
    notify_runner_summary,
)
from state_manager import state_manager


def get_account_status() -> dict:
    """Return the Alpaca account status via the broker module."""
    return broker_get_account_status()


def resolve_trade_plan(trade_plan: dict) -> dict:
    """
    Normalize a trade plan into a single flat dict.

    The scanner hands us the full Claude decision, which nests the option
    details under a "trade_plan" key. Surface those nested fields to the top
    level (nested values win) so downstream sizing/validation can read them
    consistently regardless of which shape it was given.
    """
    if not isinstance(trade_plan, dict):
        return {}
    resolved = dict(trade_plan)
    nested = trade_plan.get("trade_plan")
    if isinstance(nested, dict):
        for key, value in nested.items():
            resolved[key] = value
    return resolved


def determine_volume_size_tier(vol_ratio: float) -> dict:
    """
    Map a volume ratio (current volume vs 50-day average) to a sizing tier.

    Returns a dict with:
      action          — "TRADE" or "WATCH"
      size_multiplier — fraction of the normal dollar allocation to risk
      tier            — human-readable tier label
    """
    vol_ratio = float(vol_ratio or 0)
    if vol_ratio >= POSITION_SIZING["volume_normal_min"]:
        return {"action": "TRADE", "size_multiplier": 1.0, "tier": "NORMAL"}
    if vol_ratio >= POSITION_SIZING["volume_reduced_min"]:
        return {
            "action": "TRADE",
            "size_multiplier": POSITION_SIZING["reduced_size_multiplier"],
            "tier": "REDUCED",
        }
    return {"action": "WATCH", "size_multiplier": 0.0, "tier": "WATCH_ONLY"}


def validate_trade(symbol: str, trade_plan: dict, account: dict, force: bool = False) -> tuple:
    """
    Validate a trade against George's hard rules and account constraints.
    Returns (is_valid: bool, reason: str)

    `force` is True for a 9/10 force-execution attempt. It bypasses ONLY the
    SOFT gates (low/moderate volume, borderline risk/reward above the hard
    floor). Every HARD safety control below — kill switch, reserve cash, buying
    power, max open positions, position-size cap, the absolute R/R hard floor,
    OTM cap, stop-loss requirement — is enforced regardless of `force`.
    """
    if not trade_plan:
        return False, "Trade plan missing"

    plan = resolve_trade_plan(trade_plan)

    if is_kill_switch_engaged()[0]:
        return False, "Kill switch engaged — no new trades allowed today"

    cash = account.get("cash", 0)
    buying_power = account.get("buying_power", 0)
    open_pos = account.get("open_positions", 0)
    max_spend = float(plan.get("max_to_spend", 0) or 0)
    strike_pct = float(plan.get("strike_pct_otm", 0) or 0)
    vol_ratio = float(plan.get("vol_ratio", 0) or 0)
    risk_reward = float(plan.get("risk_reward_ratio", 0) or 0)

    # Volume gate — below the reduced-size floor we never trade (watch only).
    # SOFT: a 9/10 force attempt bypasses this (size is already reduced upstream).
    if vol_ratio < POSITION_SIZING["volume_reduced_min"]:
        if force:
            logger.info(
                f"Soft gate bypassed due to 9/10 | {symbol} | volume "
                f"{vol_ratio:.2f}x below {POSITION_SIZING['volume_reduced_min']:.2f}x"
            )
        else:
            return False, (
                f"Volume too low: {vol_ratio:.2f}x below "
                f"{POSITION_SIZING['volume_reduced_min']:.2f}x — watch only"
            )

    # Risk/reward gates — absolute floor first (HARD, never bypassed), then the
    # standard minimum (SOFT: a 9/10 may proceed if it is above the hard floor).
    if risk_reward < RISK_REWARD["hard_floor"]:
        return False, (
            f"Risk/reward {risk_reward:.2f} below absolute floor "
            f"{RISK_REWARD['hard_floor']:.2f} — never trade"
        )
    if risk_reward < RISK_REWARD["min_to_execute"]:
        if force:
            logger.info(
                f"Soft gate bypassed due to 9/10 | {symbol} | risk/reward "
                f"{risk_reward:.2f} below minimum {RISK_REWARD['min_to_execute']:.2f} "
                f"(above hard floor {RISK_REWARD['hard_floor']:.2f})"
            )
        else:
            return False, (
                f"Risk/reward {risk_reward:.2f} below minimum "
                f"{RISK_REWARD['min_to_execute']:.2f}"
            )

    if max_spend <= 0:
        return False, "Trade size estimate missing"

    max_per_trade = ACCOUNT["total_capital"] * ACCOUNT["max_per_trade_pct"]
    if max_spend > max_per_trade:
        return False, (
            f"Position too large: ${max_spend:.2f} exceeds "
            f"{ACCOUNT['max_per_trade_pct'] * 100:.0f}% limit (${max_per_trade:.2f})"
        )

    reserve = ACCOUNT["total_capital"] * ACCOUNT["reserve_cash_pct"]
    if cash - max_spend < reserve:
        return False, f"Insufficient cash after reserve: would leave ${cash - max_spend:.2f}"

    if buying_power < max_spend:
        return False, f"Buying power insufficient: ${buying_power:.2f} available"

    if open_pos >= ACCOUNT["max_open_positions"]:
        return False, f"Max positions reached: {open_pos}/{ACCOUNT['max_open_positions']}"

    if strike_pct > HARD_RULES["max_otm_pct"] * 100:
        return False, (
            f"Strike too far OTM: {strike_pct:.1f}% exceeds "
            f"{HARD_RULES['max_otm_pct'] * 100:.0f}% limit"
        )

    if not plan.get("stop_loss_stock_price"):
        return False, "No stop loss defined — trade rejected"

    contract_ceiling = int(HARD_RULES.get("max_contracts", 0) or 0)
    if contract_ceiling > 0 and int(plan.get("contracts", 1)) > contract_ceiling:
        return False, f"Exceeds max {contract_ceiling} contracts per trade"

    return True, "All checks passed"


def fetch_current_stock_price(symbol: str) -> float:
    """Get the latest stock price for the underlying symbol."""
    try:
        ticker = yf.Ticker(symbol)
        quote = ticker.history(period="1d", interval="1m")
        if not quote.empty:
            return float(quote['Close'].iloc[-1])
    except Exception as e:
        logger.warning(f"Failed to fetch stock price for {symbol}: {e}")
    return 0.0


def is_volatility_high(symbol: str) -> bool:
    """Estimate whether the symbol has elevated intraday volatility."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d")
        if hist.empty:
            return False
        recent = hist.tail(3)
        avg_close = recent['Close'].mean()
        high_low = (recent['High'] - recent['Low']).abs() / avg_close
        if high_low.max() >= 0.035:
            return True
    except Exception as e:
        logger.debug(f"Volatility estimate failed for {symbol}: {e}")
    return False


def get_adaptive_active_interval() -> int:
    """Return a monitoring interval for active positions in minutes."""
    open_trades = get_open_trades()
    active_runners = get_active_runners()
    if not open_trades and not active_runners:
        return 20

    interval = 10
    for trade in open_trades:
        symbol = trade.get('symbol')
        stop = trade.get('stop_loss_level')
        current_stock = fetch_current_stock_price(symbol)

        if current_stock and stop:
            try:
                pct_from_stop = abs(current_stock - float(stop)) / float(stop) * 100
                if pct_from_stop <= 2:
                    return 5
            except Exception:
                pass

        if is_volatility_high(symbol):
            interval = min(interval, 5)

    for runner in active_runners:
        days_left = runner.get('days_to_expiration') or 999
        if days_left <= 7:
            return 3
        if days_left < 14:
            interval = min(interval, 5)

    return interval


def place_options_trade(symbol: str, trade_plan: dict, account: dict, force: bool = False) -> dict:
    """Validate and place an options trade in Alpaca paper trading.

    `force` carries the 9/10 force-execution flag down from the scheduler. It
    only relaxes SOFT filters (low/moderate volume, borderline R/R above the
    hard floor). The liquidity gate, contract/underlying validation, sizing
    math, and every hard rule in validate_trade remain fully enforced.
    """
    plan = resolve_trade_plan(trade_plan)

    # Volume-based position sizing. Below the reduced floor is normally
    # watch-only and never reaches order placement; the reduced tier halves the
    # dollar risk allocation (contracts stay at 1 per George's hard rule).
    # SOFT for a 9/10: instead of blocking, reduce size and proceed.
    vol_ratio = float(plan.get("vol_ratio", 0) or 0)
    size_tier = determine_volume_size_tier(vol_ratio)
    if size_tier["action"] == "WATCH":
        reason = (
            f"Volume {vol_ratio:.2f}x below "
            f"{POSITION_SIZING['volume_reduced_min']:.2f}x — watch only, no trade"
        )
        if force:
            logger.info(
                f"Soft gate bypassed due to 9/10 | {symbol} | {reason} → "
                f"reducing size instead of blocking"
            )
            size_tier = {
                "action": "TRADE",
                "size_multiplier": float(POSITION_SIZING["reduced_size_multiplier"]),
                "tier": "FORCED_REDUCED",
            }
        else:
            logger.info(f"Watch only: {symbol} | {reason}")
            notify_risk_blocked(
                symbol,
                plan.get("option_type", "Unknown"),
                reason,
                "Volume below tradeable threshold",
                "Monitor for a volume expansion before considering an entry.",
            )
            return {"success": False, "reason": reason}

    if size_tier["size_multiplier"] != 1.0:
        base_spend = float(plan.get("max_to_spend", 0) or 0)
        plan["max_to_spend"] = round(base_spend * size_tier["size_multiplier"], 2)
        logger.info(
            f"Reduced sizing: {symbol} | vol {vol_ratio:.2f}x | "
            f"max_to_spend ${base_spend:.2f} → ${plan['max_to_spend']:.2f}"
        )

    is_valid, reason = validate_trade(symbol, plan, account, force=force)
    if not is_valid:
        logger.warning(f"Trade rejected: {symbol} | {reason}")
        notify_risk_blocked(
            symbol,
            plan.get("option_type", "Unknown"),
            reason,
            "Risk or sizing rule blocked this setup",
            "Reduce position size or improve setup quality before retrying.",
        )
        return {"success": False, "reason": reason}

    option_type = plan.get("option_type", "CALL")
    strike = float(plan.get("strike", 0) or 0)
    expiry = plan.get("expiry", "")
    price_low = float(plan.get("estimated_option_price_low", 0) or 0)
    price_high = float(plan.get("estimated_option_price_high", 0) or 0)
    limit_price = round((price_low + price_high) / 2, 2)

    # Select the most liquid near-the-money contract among the candidates.
    contract = find_best_option_contract(symbol, strike, expiry, option_type)
    if not contract:
        return {"success": False, "reason": "Unable to locate option contract"}
    contract_symbol = contract["symbol"]
    liquidity = contract.get("liquidity", {})

    # ── Options liquidity gate (Phase 3D) ─────────────────────
    # Final pre-execution check: reject illiquid / untradeable contracts. When
    # the snapshot feed returns nothing, honour the fail-open/closed policy.
    verdict = evaluate_liquidity(liquidity)
    details_txt = describe(verdict["details"])
    no_data = not any(v is not None for v in (liquidity or {}).values())

    if no_data and not OPTIONS_LIQUIDITY.get("fail_open_on_error", False):
        reason = "option rejected: liquidity data unavailable"
        logger.warning(f"{reason} | {symbol} {contract_symbol}")
        notify_liquidity_blocked(symbol, option_type, contract_symbol, reason, details_txt)
        return {"success": False, "reason": reason}

    if not no_data and not verdict["approved"]:
        logger.warning(
            f"{verdict['reason']} | {symbol} {contract_symbol} | {details_txt}"
        )
        notify_liquidity_blocked(
            symbol, option_type, contract_symbol, verdict["reason"], details_txt
        )
        return {"success": False, "reason": verdict["reason"]}

    logger.info(f"option approved: liquidity OK | {symbol} {contract_symbol} | {details_txt}")

    # Use the live mid as the limit price when the plan carries no estimate, so
    # the order is actually marketable; otherwise keep the planned price.
    mid = verdict["details"].get("mid")
    if limit_price <= 0 and mid:
        limit_price = round(float(mid), 2)
        logger.info(f"Limit price set from live mid for {contract_symbol}: ${limit_price}")

    # Size the order from the dollar budget — buy as many contracts as the
    # per-trade spend allows (one contract = 100 shares of premium exposure).
    # HARD_RULES['max_contracts'] is an optional safety ceiling (0 = unlimited).
    # The volume-tier reduction above is already baked into max_to_spend, so a
    # reduced tier naturally lands on fewer contracts.
    budget = float(plan.get("max_to_spend", 0) or 0)
    per_contract_cost = limit_price * 100
    if per_contract_cost <= 0:
        return {"success": False, "reason": "Invalid option price for contract sizing"}
    qty = int(budget // per_contract_cost)
    contract_ceiling = int(HARD_RULES.get("max_contracts", 0) or 0)
    if contract_ceiling > 0:
        qty = min(qty, contract_ceiling)
    qty = max(0, qty)
    if qty < 1:
        reason = (
            f"Contract too expensive for budget: 1 contract ${per_contract_cost:,.0f} "
            f"exceeds max spend ${budget:,.0f}"
        )
        logger.warning(f"Trade rejected: {symbol} | {reason}")
        return {"success": False, "reason": reason}
    logger.info(
        f"Sizing {symbol}: {qty} contract(s) @ ${limit_price:.2f} "
        f"= ${qty * per_contract_cost:,.0f} of ${budget:,.0f} budget"
    )

    order_result = place_option_order(contract_symbol, qty, limit_price)
    if not order_result.get("success"):
        return order_result

    entry_price = account.get("current_price") or fetch_current_stock_price(symbol)

    # Derive profit targets deterministically from the ACTUAL fill premium and
    # the configured gains, rather than trusting AI estimates. This guarantees
    # the first scale-out fires at +HARD_RULES['target_1_gain'] (e.g. +30%) and
    # the runner target at +target_2_gain — the win-rate exit discipline.
    target_1_price = round(limit_price * (1 + HARD_RULES["target_1_gain"]), 2) if limit_price > 0 \
        else plan.get("target_1_option_price")
    target_2_price = round(limit_price * (1 + HARD_RULES["target_2_gain"]), 2) if limit_price > 0 \
        else plan.get("target_2_option_price")

    trade_id = log_trade_entry({
        "symbol": symbol,
        "option_symbol": contract_symbol,
        "trade_type": option_type,
        "strategy": "SWING",
        "entry_stock_price": entry_price,
        "strike": strike,
        "expiry": expiry,
        "contracts": qty,
        "entry_option_price": limit_price,
        "stop_loss_level": plan.get("stop_loss_stock_price"),
        "target_1": target_1_price,
        "target_2": target_2_price,
        "catalyst": plan.get("catalyst"),
        "thesis": plan.get("thesis"),
        "alpaca_order_id": str(order_result.get("order_id")),
    })

    notify_trade_entered(
        symbol=symbol,
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        entry_premium=limit_price,
        stop_loss_premium=float(plan.get("stop_loss_option_price", 0) or 0),
        profit_target_premium=float(target_1_price or 0),
        max_risk=round(qty * per_contract_cost, 2),
        position_size=f"{qty} contract(s) ({size_tier['tier']} size, vol {vol_ratio:.2f}x)",
        expected_hold=f"Until expiration or invalidation, typically a multi-week swing",
        reason=plan.get("thesis", "Confirmed swing options setup."),
        exit_plan=plan.get("exit_plan", "Exit at target or on invalidation."),
    )

    # Transition to ACTIVE monitoring for this symbol
    try:
        state_manager.set_active([symbol], reason="Trade executed")
    except Exception:
        pass

    return {
        "success": True,
        "trade_id": trade_id,
        "order_id": order_result.get("order_id"),
        "symbol": contract_symbol,
        "limit_price": limit_price,
    }


def monitor_open_positions():
    """Monitor open option trades and auto-close on stop loss or profit targets."""
    open_trades = get_open_trades()
    if not open_trades:
        logger.info("No open positions to monitor")
        return

    positions = broker_get_positions()
    option_prices = {pos['symbol']: float(pos['current_price']) for pos in positions}

    stock_symbols = list({trade['symbol'] for trade in open_trades})
    current_stock_prices = {symbol: fetch_current_stock_price(symbol) for symbol in stock_symbols}

    for trade in open_trades:
        symbol = trade['symbol']
        stop = trade.get('stop_loss_level')
        target_1 = trade.get('target_1')
        target_2 = trade.get('target_2')
        current_stock = current_stock_prices.get(symbol)
        if current_stock is None:
            continue

        option_symbol = trade.get('option_symbol')
        current_option_price = option_prices.get(option_symbol, trade.get('entry_option_price', 0))

        # Direction-aware stop. A CALL's stop is a stock price BELOW entry
        # (trigger when price falls to it); a PUT's stop is a stock price ABOVE
        # entry (trigger when price rises to it). Using the wrong comparison
        # would stop every PUT out immediately, so branch on the trade type.
        trade_type = str(trade.get('trade_type') or 'CALL').upper()
        if trade_type == 'PUT':
            stock_stop_hit = bool(stop) and current_stock >= stop
        else:
            stock_stop_hit = bool(stop) and current_stock <= stop

        # Hard premium stop: cap the loss on the option itself at
        # HARD_RULES['max_option_loss_pct'] (e.g. 15%), independent of the
        # stock-based stop. Premium falls when a CALL or a PUT loses, so this is
        # direction-agnostic. This is the "can't lose more than 15%" guarantee
        # (subject to fills/gaps).
        entry_option_chk = float(trade.get('entry_option_price') or 0)
        max_loss_pct = float(HARD_RULES.get("max_option_loss_pct", 0) or 0)
        option_stop_hit = bool(
            max_loss_pct and entry_option_chk and current_option_price > 0
            and current_option_price <= entry_option_chk * (1 - max_loss_pct)
        )

        stop_hit = stock_stop_hit or option_stop_hit
        stop_label = "OPTION_HARD_STOP" if (option_stop_hit and not stock_stop_hit) else "STOP_LOSS"

        if stop_hit:
            if stop_label == "OPTION_HARD_STOP":
                logger.warning(
                    f"🚨 OPTION HARD STOP (-{max_loss_pct:.0%}): {symbol} "
                    f"@ ${current_option_price:.2f} (entry ${entry_option_chk:.2f})"
                )
            else:
                logger.warning(f"🚨 STOP LOSS: {symbol} @ ${current_stock:.2f} | stop ${stop:.2f}")
            exit_pnl = 0.0
            if trade.get('entry_option_price') is not None:
                entry_option = float(trade.get('entry_option_price') or 0)
                contracts = float(trade.get('contracts', 1) or 1)
                exit_pnl = (current_option_price - entry_option) * 100 * contracts
            if ACCOUNT["paper_trading"]:
                close_alpaca_position(option_symbol)
                update_trade_exit(trade['id'], {
                    "exit_stock_price": current_stock,
                    "exit_option_price": current_option_price,
                    "status": "STOPPED",
                    "exit_reason": stop_label,
                })
                record_trade_outcome(trade['id'])
                entry_option = float(trade.get('entry_option_price') or 0)
                percent_return = 0.0
                if entry_option:
                    percent_return = ((current_option_price - entry_option) / entry_option) * 100
                notify_trade_exited(
                    symbol,
                    trade.get('trade_type', 'Option'),
                    f"Hard stop (-{max_loss_pct:.0%} premium)" if stop_label == "OPTION_HARD_STOP" else "Stop loss",
                    entry_option,
                    current_option_price,
                    percent_return,
                    exit_pnl,
                    "Setup invalidated and risk controls executed as designed.",
                )
            else:
                logger.critical(f"LIVE STOP ALERT: {symbol} — review immediately")
            continue

        # ── Breakeven stop trail ──────────────────────────────
        # Once the option is up by HARD_RULES['breakeven_trigger_gain'] (e.g.
        # +20%), pull the stock stop up to the entry price so a winner can't turn
        # into a loser. Direction-aware and idempotent: only tightens the stop
        # (never loosens it) and only persists when it actually moves.
        entry_option = float(trade.get('entry_option_price') or 0)
        entry_stock = trade.get('entry_stock_price')
        if entry_option and entry_stock and current_option_price:
            gain = (current_option_price - entry_option) / entry_option
            if gain >= HARD_RULES.get("breakeven_trigger_gain", 0.20):
                entry_stock = float(entry_stock)
                if trade_type == 'PUT':
                    # PUT stop sits ABOVE price; breakeven tightens it DOWN to entry.
                    moves = stop is None or entry_stock < float(stop)
                else:
                    # CALL stop sits BELOW price; breakeven tightens it UP to entry.
                    moves = stop is None or entry_stock > float(stop)
                if moves:
                    update_trade_fields(trade['id'], {"stop_loss_level": entry_stock})
                    logger.info(
                        f"🔒 Breakeven stop: {symbol} {trade_type} up {gain:+.0%} "
                        f"| stop → ${entry_stock:.2f} (was "
                        f"{('$' + format(float(stop), '.2f')) if stop else 'n/a'})"
                    )
                    stop = entry_stock  # use the tightened stop for the rest of this pass

        if current_option_price and trade.get('entry_option_price'):
            if target_2 and current_option_price >= target_2:
                logger.info(f"🎯 TARGET 2 HIT: {symbol} @ ${current_option_price:.2f}")
                entry_option = float(trade.get('entry_option_price') or 0)
                contracts = float(trade.get('contracts', 1) or 1)
                exit_pnl = (current_option_price - entry_option) * 100 * contracts
                percent_return = 0.0
                if entry_option:
                    percent_return = ((current_option_price - entry_option) / entry_option) * 100
                if ACCOUNT["paper_trading"]:
                    close_alpaca_position(option_symbol)
                    update_trade_exit(trade['id'], {
                        "exit_stock_price": current_stock,
                        "exit_option_price": current_option_price,
                        "contracts": contracts,
                        "status": "CLOSED",
                        "exit_reason": "TARGET_2",
                    })
                    record_trade_outcome(trade['id'])
                    notify_trade_exited(
                        symbol,
                        trade.get('trade_type', 'Option'),
                        "Target 2",
                        entry_option,
                        current_option_price,
                        percent_return,
                        exit_pnl,
                        "Target achieved under planned swing exit rules.",
                    )
                continue

            if target_1 and current_option_price >= target_1:
                logger.info(f"📈 TARGET 1 HIT: {symbol} @ ${current_option_price:.2f}")
                entry_option = float(trade.get('entry_option_price') or 0)
                contracts = int(trade.get('contracts', 1) or 1)
                sold_contracts = max(1, contracts // 2)
                remaining_contracts = contracts - sold_contracts
                exit_pnl = (current_option_price - entry_option) * 100 * sold_contracts
                percent_return = 0.0
                if entry_option:
                    percent_return = ((current_option_price - entry_option) / entry_option) * 100

                if ACCOUNT["paper_trading"]:
                    if remaining_contracts > 0:
                        runner_stop = entry_option
                        runner_target = float(trade.get('target_2') or current_option_price * 1.5)
                        days_to_expiration = None
                        expiration_date = trade.get('expiry')
                        try:
                            expiration_dt = datetime.fromisoformat(expiration_date)
                            days_to_expiration = max(0, (expiration_dt.date() - datetime.now().date()).days)
                        except Exception:
                            days_to_expiration = None

                        runner_id = log_runner_entry({
                            "trade_id": trade['id'],
                            "symbol": symbol,
                            "option_symbol": option_symbol,
                            "option_type": trade.get('trade_type', 'Option'),
                            "original_contracts": contracts,
                            "contracts_sold": sold_contracts,
                            "contracts_remaining": remaining_contracts,
                            "entry_option_price": entry_option,
                            "current_option_price": current_option_price,
                            "realized_pnl": exit_pnl,
                            "unrealized_pnl": (current_option_price - entry_option) * 100 * remaining_contracts,
                            "runner_stop_price": runner_stop,
                            "runner_target_price": runner_target,
                            "expiration_date": expiration_date,
                            "days_to_expiration": days_to_expiration,
                            "reason_still_holding": "Holding runner after partial profit take with protected stop.",
                        })

                        update_trade_exit(trade['id'], {
                            "exit_stock_price": current_stock,
                            "exit_option_price": current_option_price,
                            "contracts": sold_contracts,
                            "status": "PARTIAL",
                            "exit_reason": "TARGET_1",
                        })
                        record_trade_outcome(trade['id'])
                        notify_runner_created(
                            symbol,
                            trade.get('trade_type', 'Option'),
                            remaining_contracts,
                            entry_option,
                            current_option_price,
                            runner_stop,
                            runner_target,
                            days_to_expiration or 0,
                            "Runner retained after first target with stop moved to breakeven.",
                        )
                        active_runners = get_active_runners()
                        notify_runner_summary(active_runners)
                    else:
                        close_alpaca_position(option_symbol)
                        update_trade_exit(trade['id'], {
                            "exit_stock_price": current_stock,
                            "exit_option_price": current_option_price,
                            "status": "CLOSED",
                            "exit_reason": "TARGET_1",
                        })
                        record_trade_outcome(trade['id'])
                        notify_trade_exited(
                            symbol,
                            trade.get('trade_type', 'Option'),
                            "Target 1",
                            entry_option,
                            current_option_price,
                            percent_return,
                            exit_pnl,
                            "Partial profit realization consistent with swing plan.",
                        )
                continue


def monitor_runners():
    """Monitor active runners separately from original trade entries."""
    runners = get_active_runners()
    if not runners:
        logger.info("No active runners to monitor")
        return

    positions = broker_get_positions()
    option_prices = {pos['symbol']: float(pos['current_price']) for pos in positions}

    for runner in runners:
        option_symbol = runner.get('option_symbol')
        current_option_price = option_prices.get(option_symbol, runner.get('current_option_price') or 0)
        entry_option = float(runner.get('entry_option_price') or 0)
        contracts = int(runner.get('contracts_remaining') or 1)
        unrealized = (current_option_price - entry_option) * 100 * contracts
        percent_return = 0.0
        if entry_option:
            percent_return = ((current_option_price - entry_option) / entry_option) * 100

        runner_stop = float(runner.get('runner_stop_price') or entry_option)
        runner_target = float(runner.get('runner_target_price') or 0)
        days_left = int(runner.get('days_to_expiration') or 0)
        should_close = False
        close_reason = ""

        if current_option_price <= runner_stop:
            should_close = True
            close_reason = "Runner stop hit"
        elif runner_target and current_option_price >= runner_target:
            should_close = True
            close_reason = "Runner target hit"
        elif days_left < 14 and current_option_price < entry_option * 1.05:
            should_close = True
            close_reason = "Momentum faded near expiration"
        elif days_left < 7 and runner_target and current_option_price < runner_target * 0.85:
            should_close = True
            close_reason = "Expiration under 7 days with weak momentum"

        if should_close:
            update_runner(runner['id'], {
                'current_option_price': current_option_price,
                'unrealized_pnl': unrealized,
                'status': 'closed_runner',
                'notes': close_reason,
            })
            if close_reason == 'Runner target hit':
                notify_runner_target_hit(
                    runner['symbol'],
                    runner.get('option_type', 'Option'),
                    entry_option,
                    current_option_price,
                    percent_return,
                    unrealized,
                    days_left,
                )
            else:
                notify_runner_closed(
                    runner['symbol'],
                    runner.get('option_type', 'Option'),
                    entry_option,
                    current_option_price,
                    percent_return,
                    unrealized,
                    days_left,
                    close_reason,
                )
        else:
            update_runner(runner['id'], {
                'current_option_price': current_option_price,
                'unrealized_pnl': unrealized,
            })


def close_position(symbol: str, trade_id: int, reason: str):
    """Manually close a position."""
    result = close_alpaca_position(symbol)
    if result.get('success'):
        update_trade_exit(trade_id, {
            "status": "CLOSED",
            "exit_reason": reason,
        })
        record_trade_outcome(trade_id)
        logger.info(f"✅ Position closed: {symbol} | Reason: {reason}")
        return True

    logger.warning(f"Position close failed: {symbol} | {result.get('reason')}")
    return False
