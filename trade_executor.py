"""
trade_executor.py
Executes trades via Alpaca API using the Alpaca broker abstraction.
Handles options order placement, position sizing, stop loss, profit targets, and Discord alerts.
"""

import yfinance as yf
from datetime import datetime
from loguru import logger
from config import ACCOUNT, HARD_RULES
from db import (
    log_trade_entry,
    update_trade_exit,
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
    place_option_order,
    close_alpaca_position,
    is_kill_switch_engaged,
)
from notifier import (
    notify_trade_entered,
    notify_trade_exited,
    notify_risk_blocked,
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


def validate_trade(symbol: str, trade_plan: dict, account: dict) -> tuple:
    """
    Validate a trade against George's hard rules and account constraints.
    Returns (is_valid: bool, reason: str)
    """
    if not trade_plan:
        return False, "Trade plan missing"

    if is_kill_switch_engaged()[0]:
        return False, "Kill switch engaged — no new trades allowed today"

    cash = account.get("cash", 0)
    buying_power = account.get("buying_power", 0)
    open_pos = account.get("open_positions", 0)
    max_spend = float(trade_plan.get("max_to_spend", 0) or 0)
    strike_pct = float(trade_plan.get("strike_pct_otm", 0) or 0)

    if max_spend <= 0:
        return False, "Trade size estimate missing"

    if max_spend > ACCOUNT["total_capital"] * ACCOUNT["max_per_trade_pct"]:
        return False, f"Position too large: ${max_spend:.2f} exceeds 20% limit"

    reserve = ACCOUNT["total_capital"] * ACCOUNT["reserve_cash_pct"]
    if cash - max_spend < reserve:
        return False, f"Insufficient cash after reserve: would leave ${cash - max_spend:.2f}"

    if buying_power < max_spend:
        return False, f"Buying power insufficient: ${buying_power:.2f} available"

    if open_pos >= ACCOUNT["max_open_positions"]:
        return False, f"Max positions reached: {open_pos}/{ACCOUNT['max_open_positions']}"

    if strike_pct > HARD_RULES["max_otm_pct"] * 100:
        return False, f"Strike too far OTM: {strike_pct:.1f}% exceeds 15% limit"

    if not trade_plan.get("stop_loss_stock_price"):
        return False, "No stop loss defined — trade rejected"

    if int(trade_plan.get("contracts", 1)) > HARD_RULES["max_contracts"]:
        return False, "Max 1 contract per trade"

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


def place_options_trade(symbol: str, trade_plan: dict, account: dict) -> dict:
    """Validate and place an options trade in Alpaca paper trading."""
    is_valid, reason = validate_trade(symbol, trade_plan, account)
    if not is_valid:
        logger.warning(f"Trade rejected: {symbol} | {reason}")
        notify_risk_blocked(
            symbol,
            trade_plan.get("option_type", "Unknown"),
            reason,
            "Risk or sizing rule blocked this setup",
            "Reduce position size or improve setup quality before retrying.",
        )
        return {"success": False, "reason": reason}

    option_type = trade_plan.get("option_type", "CALL")
    strike = float(trade_plan.get("strike", 0) or 0)
    expiry = trade_plan.get("expiry", "")
    price_low = float(trade_plan.get("estimated_option_price_low", 0) or 0)
    price_high = float(trade_plan.get("estimated_option_price_high", 0) or 0)
    limit_price = round((price_low + price_high) / 2, 2)

    contract_symbol = find_option_contract_symbol(symbol, strike, expiry, option_type)
    if not contract_symbol:
        return {"success": False, "reason": "Unable to locate option contract"}

    order_result = place_option_order(contract_symbol, 1, limit_price)
    if not order_result.get("success"):
        return order_result

    entry_price = account.get("current_price") or fetch_current_stock_price(symbol)
    trade_id = log_trade_entry({
        "symbol": symbol,
        "option_symbol": contract_symbol,
        "trade_type": option_type,
        "strategy": "SWING",
        "entry_stock_price": entry_price,
        "strike": strike,
        "expiry": expiry,
        "contracts": 1,
        "entry_option_price": limit_price,
        "stop_loss_level": trade_plan.get("stop_loss_stock_price"),
        "target_1": trade_plan.get("target_1_option_price"),
        "target_2": trade_plan.get("target_2_option_price"),
        "catalyst": trade_plan.get("catalyst"),
        "thesis": trade_plan.get("thesis"),
        "alpaca_order_id": str(order_result.get("order_id")),
    })

    notify_trade_entered(
        symbol=symbol,
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        entry_premium=limit_price,
        stop_loss_premium=float(trade_plan.get("stop_loss_option_price", 0) or 0),
        profit_target_premium=float(trade_plan.get("target_1_option_price", 0) or 0),
        max_risk=float(trade_plan.get("max_to_spend", 0) or 0),
        position_size=f"{int(trade_plan.get('contracts', 1))} contract(s)",
        expected_hold=f"Until expiration or invalidation, typically a multi-week swing",
        reason=trade_plan.get("thesis", "Confirmed swing options setup."),
        exit_plan=trade_plan.get("exit_plan", "Exit at target or on invalidation."),
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

        if stop and current_stock <= stop:
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
                    "exit_reason": "STOP_LOSS",
                })
                record_trade_outcome(trade['id'])
                entry_option = float(trade.get('entry_option_price') or 0)
                percent_return = 0.0
                if entry_option:
                    percent_return = ((current_option_price - entry_option) / entry_option) * 100
                notify_trade_exited(
                    symbol,
                    trade.get('trade_type', 'Option'),
                    "Stop loss",
                    entry_option,
                    current_option_price,
                    percent_return,
                    exit_pnl,
                    "Setup invalidated and risk controls executed as designed.",
                )
            else:
                logger.critical(f"LIVE STOP ALERT: {symbol} — review immediately")
            continue

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
