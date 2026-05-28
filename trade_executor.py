"""
trade_executor.py
Executes trades via Alpaca API using the Alpaca broker abstraction.
Handles options order placement, position sizing, stop loss, profit targets, and Discord alerts.
"""

import yfinance as yf
from loguru import logger
from config import ACCOUNT, HARD_RULES
from database.db import log_trade_entry, update_trade_exit, get_open_trades
from learning_engine import record_trade_outcome
from alpaca_broker import (
    get_account_status as broker_get_account_status,
    get_positions as broker_get_positions,
    find_option_contract_symbol,
    place_option_order,
    close_alpaca_position,
    send_discord_notification,
    is_kill_switch_engaged,
)


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


def place_options_trade(symbol: str, trade_plan: dict, account: dict) -> dict:
    """Validate and place an options trade in Alpaca paper trading."""
    is_valid, reason = validate_trade(symbol, trade_plan, account)
    if not is_valid:
        logger.warning(f"Trade rejected: {symbol} | {reason}")
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

    send_discord_notification(
        f"Paper trade ENTERED: {symbol} {option_type} {contract_symbol} @ ${limit_price:.2f}"
    )

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
            if ACCOUNT["paper_trading"]:
                close_alpaca_position(option_symbol)
                update_trade_exit(trade['id'], {
                    "exit_stock_price": current_stock,
                    "exit_option_price": current_option_price,
                    "status": "STOPPED",
                    "exit_reason": "STOP_LOSS",
                })
                record_trade_outcome(trade['id'])
                send_discord_notification(
                    f"Paper trade EXITED: {symbol} stopped out at ${current_stock:.2f}"
                )
            else:
                logger.critical(f"LIVE STOP ALERT: {symbol} — review immediately")
            continue

        if current_option_price and trade.get('entry_option_price'):
            if target_2 and current_option_price >= target_2:
                logger.info(f"🎯 TARGET 2 HIT: {symbol} @ ${current_option_price:.2f}")
                if ACCOUNT["paper_trading"]:
                    close_alpaca_position(option_symbol)
                    update_trade_exit(trade['id'], {
                        "exit_stock_price": current_stock,
                        "exit_option_price": current_option_price,
                        "status": "CLOSED",
                        "exit_reason": "TARGET_2",
                    })
                    record_trade_outcome(trade['id'])
                    send_discord_notification(
                        f"Paper trade EXITED: {symbol} hit Target 2 at ${current_option_price:.2f}"
                    )
                continue

            if target_1 and current_option_price >= target_1:
                logger.info(f"📈 TARGET 1 HIT: {symbol} @ ${current_option_price:.2f}")
                if ACCOUNT["paper_trading"]:
                    close_alpaca_position(option_symbol)
                    update_trade_exit(trade['id'], {
                        "exit_stock_price": current_stock,
                        "exit_option_price": current_option_price,
                        "status": "CLOSED",
                        "exit_reason": "TARGET_1",
                    })
                    record_trade_outcome(trade['id'])
                    send_discord_notification(
                        f"Paper trade EXITED: {symbol} hit Target 1 at ${current_option_price:.2f}"
                    )


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
