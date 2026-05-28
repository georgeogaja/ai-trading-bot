"""
agents/trade_executor.py
Executes trades via Alpaca API.
Handles options order placement, position sizing, and stop monitoring.
"""

import os
from datetime import datetime, timedelta

import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest,
    GetOptionContractsRequest, OptionLegRequest
)
from alpaca.trading.enums import (
    OrderSide, TimeInForce, ContractType,
    OrderClass, PositionSide
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from loguru import logger
from config import ACCOUNT, HARD_RULES
from database.db import (
    log_trade_entry, update_trade_exit,
    get_open_trades, log_mistake
)
from learning_engine import record_trade_outcome

# Initialize Alpaca clients
trading_client = TradingClient(
    os.getenv("ALPACA_API_KEY"),
    os.getenv("ALPACA_SECRET_KEY"),
    paper=ACCOUNT["paper_trading"],
)

data_client = StockHistoricalDataClient(
    os.getenv("ALPACA_API_KEY"),
    os.getenv("ALPACA_SECRET_KEY"),
)


def get_account_status() -> dict:
    """Get current account status from Alpaca."""
    try:
        account  = trading_client.get_account()
        positions = trading_client.get_all_positions()
        return {
            "cash":             float(account.cash),
            "portfolio_value":  float(account.portfolio_value),
            "buying_power":     float(account.buying_power),
            "open_positions":   len(positions),
            "positions":        [
                {
                    "symbol": p.symbol,
                    "qty":    float(p.qty),
                    "market_value": float(p.market_value),
                    "unrealized_pl": float(p.unrealized_pl),
                }
                for p in positions
            ]
        }
    except Exception as e:
        logger.error(f"Account status error: {e}")
        return {"cash": 0, "portfolio_value": 0, "buying_power": 0, "open_positions": 0}


def validate_trade(symbol: str, trade_plan: dict, account: dict) -> tuple:
    """
    Validate a trade against ALL of George's hard rules.
    Returns (is_valid: bool, reason: str)
    """
    cash        = account.get("cash", 0)
    open_pos    = account.get("open_positions", 0)
    max_spend   = trade_plan.get("max_to_spend", 0)
    option_low  = trade_plan.get("estimated_option_price_low", 0)
    strike_pct  = trade_plan.get("strike_pct_otm", 0)

    # Check 1: Never exceed 20% of account per trade
    if max_spend > ACCOUNT["total_capital"] * ACCOUNT["max_per_trade_pct"]:
        return False, f"Position too large: ${max_spend:.2f} exceeds 20% limit"

    # Check 2: Always keep 30% cash reserve
    reserve = ACCOUNT["total_capital"] * ACCOUNT["reserve_cash_pct"]
    if cash - max_spend < reserve:
        return False, f"Insufficient cash after reserve: would leave ${cash - max_spend:.2f}"

    # Check 3: Never more than 5 open positions
    if open_pos >= ACCOUNT["max_open_positions"]:
        return False, f"Max positions reached: {open_pos}/{ACCOUNT['max_open_positions']}"

    # Check 4: Strike cannot exceed 15% OTM
    if strike_pct > HARD_RULES["max_otm_pct"] * 100:
        return False, f"Strike too far OTM: {strike_pct:.1f}% exceeds 15% limit"

    # Check 5: Must have a stop loss defined
    if not trade_plan.get("stop_loss_stock_price"):
        return False, "No stop loss defined — trade rejected"

    # Check 6: Max 1 contract
    if trade_plan.get("contracts", 1) > HARD_RULES["max_contracts"]:
        return False, "Max 1 contract per trade"

    return True, "All checks passed"


def find_option_contract(symbol: str, strike: float,
                          expiry: str, option_type: str) -> str:
    """
    Find the exact Alpaca option contract symbol.
    Returns the option symbol string or None.
    """
    try:
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d")
        contract_type = ContractType.CALL if option_type == "CALL" else ContractType.PUT

        request = GetOptionContractsRequest(
            underlying_symbol=symbol,
            expiration_date_gte=(expiry_date - timedelta(days=7)).strftime("%Y-%m-%d"),
            expiration_date_lte=(expiry_date + timedelta(days=7)).strftime("%Y-%m-%d"),
            strike_price_gte=str(strike * 0.95),
            strike_price_lte=str(strike * 1.05),
            type=contract_type,
            limit=5,
        )

        contracts = trading_client.get_option_contracts(request)

        if contracts and hasattr(contracts, 'option_contracts') and contracts.option_contracts:
            # Find closest strike
            best = min(
                contracts.option_contracts,
                key=lambda c: abs(float(c.strike_price) - strike)
            )
            logger.info(f"Found contract: {best.symbol} | Strike: {best.strike_price} | Expiry: {best.expiration_date}")
            return best.symbol
        else:
            logger.warning(f"No contracts found for {symbol} {strike} {expiry} {option_type}")
            return None

    except Exception as e:
        logger.error(f"Contract search error: {e}")
        return None


def place_options_trade(symbol: str, trade_plan: dict, account: dict) -> dict:
    """
    Places an options trade via Alpaca API.
    Uses limit order at midpoint of bid/ask (George's rule).
    """

    # Validate first
    is_valid, reason = validate_trade(symbol, trade_plan, account)
    if not is_valid:
        logger.warning(f"Trade rejected: {symbol} | {reason}")
        return {"success": False, "reason": reason}

    option_type = trade_plan.get("option_type", "CALL")
    strike      = float(trade_plan.get("strike", 0))
    expiry      = trade_plan.get("expiry", "")
    price_low   = float(trade_plan.get("estimated_option_price_low", 0))
    price_high  = float(trade_plan.get("estimated_option_price_high", 0))
    limit_price = round((price_low + price_high) / 2, 2)

    # Find the exact contract
    contract_symbol = find_option_contract(symbol, strike, expiry, option_type)
    if not contract_symbol:
        return {"success": False, "reason": "Could not find matching option contract"}

    try:
        # Place limit order at midpoint (George's hard rule)
        order_request = LimitOrderRequest(
            symbol=contract_symbol,
            qty=1,
            side=OrderSide.BUY,
            limit_price=limit_price,
            time_in_force=TimeInForce.DAY,
        )

        order = trading_client.submit_order(order_request)
        logger.info(f"✅ Order placed: {symbol} {option_type} ${strike} {expiry} @ ${limit_price} | ID: {order.id}")

        # Log to database
        trade_id = log_trade_entry({
            "symbol":              symbol,
            "option_symbol":       contract_symbol,
            "trade_type":          option_type,
            "strategy":            "SWING",
            "entry_stock_price":   account.get("current_price", 0),
            "strike":              strike,
            "expiry":              expiry,
            "contracts":           1,
            "entry_option_price":  limit_price,
            "stop_loss_level":     trade_plan.get("stop_loss_stock_price"),
            "target_1":            trade_plan.get("target_1_option_price"),
            "target_2":            trade_plan.get("target_2_option_price"),
            "catalyst":            trade_plan.get("catalyst"),
            "thesis":              trade_plan.get("thesis"),
            "alpaca_order_id":     str(order.id),
        })

        return {
            "success":     True,
            "trade_id":    trade_id,
            "order_id":    str(order.id),
            "symbol":      contract_symbol,
            "limit_price": limit_price,
        }

    except Exception as e:
        logger.error(f"Order placement error: {symbol} | {e}")
        return {"success": False, "reason": str(e)}


def monitor_open_positions():
    """
    Check all open positions against stop loss levels.
    Auto-exit in paper mode, alert in live mode.
    """
    open_trades = get_open_trades()
    if not open_trades:
        logger.info("No open positions to monitor")
        return

    # Get current option prices for open positions and latest stock prices for stop checks
    try:
        positions = trading_client.get_all_positions()
        option_prices = {pos.symbol: float(pos.current_price) for pos in positions}
    except Exception as e:
        logger.error(f"Position fetch error: {e}")
        return

    stock_symbols = list({trade['symbol'] for trade in open_trades})
    current_stock_prices = {}
    for symbol in stock_symbols:
        try:
            ticker = yf.Ticker(symbol)
            quote = ticker.history(period="1d", interval="1m")
            if not quote.empty:
                current_stock_prices[symbol] = float(quote['Close'].iloc[-1])
        except Exception as e:
            logger.warning(f"Failed to fetch stock quote for {symbol}: {e}")

    for trade in open_trades:
        symbol    = trade['symbol']
        stop      = trade.get('stop_loss_level')
        target_1  = trade.get('target_1')
        target_2  = trade.get('target_2')

        current_stock = current_stock_prices.get(symbol)
        if current_stock is None:
            continue

        # CHECK STOP LOSS
        if stop and current_stock <= stop:
            logger.warning(f"🚨 STOP TRIGGERED: {symbol} @ ${current_stock} | Stop: ${stop}")

            if ACCOUNT["paper_trading"]:
                # Auto-exit in paper mode and capture the latest option price for learning
                try:
                    option_symbol = trade.get('option_symbol')
                    option_price = option_prices.get(option_symbol, trade.get('entry_option_price', 0))
                    trading_client.close_position(option_symbol)
                    update_trade_exit(trade['id'], {
                        "exit_stock_price": current_stock,
                        "exit_option_price": option_price,
                        "status":           "STOPPED",
                        "exit_reason":      "STOP_LOSS",
                    })
                    record_trade_outcome(trade['id'])
                except Exception as e:
                    logger.error(f"Stop exit error: {symbol} | {e}")
            else:
                logger.critical(f"🚨 LIVE STOP ALERT: {symbol} — EXIT NOW @ ${current_stock}")

        # LOG TARGET ALERTS (don't auto-exit on targets — let George decide)
        elif target_1 and trade.get('entry_option_price'):
            entry = trade['entry_option_price']
            gain_50 = entry * 1.50
            gain_100 = entry * 2.00
            logger.info(f"📊 {symbol}: Target 1 (50%): ${gain_50:.2f} | Target 2 (100%): ${gain_100:.2f}")


def close_position(symbol: str, trade_id: int, reason: str):
    """Manually close a position."""
    try:
        positions = trading_client.get_all_positions()
        for pos in positions:
            if symbol in pos.symbol:
                trading_client.close_position(pos.symbol)
                update_trade_exit(trade_id, {
                    "status":      "CLOSED",
                    "exit_reason": reason,
                })
                record_trade_outcome(trade_id)
                logger.info(f"✅ Position closed: {symbol} | Reason: {reason}")
                return True
        logger.warning(f"Position not found for close: {symbol}")
        return False
    except Exception as e:
        logger.error(f"Close position error: {symbol} | {e}")
        return False
