"""
alpaca_broker.py
Paper-only Alpaca broker integration and risk controls.
"""

import os
import requests
from datetime import datetime, timedelta
from loguru import logger
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType
from database.db import get_today_trade_count, get_today_realized_pnl
from config import ACCOUNT

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
PAPER_TRADING = os.getenv("PAPER_TRADING", str(ACCOUNT.get("paper_trading", True))).lower() == "true"
ACCOUNT["paper_trading"] = PAPER_TRADING


def get_alpaca_client() -> TradingClient:
    """Create a paper-enabled Alpaca trading client."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in .env")

    base_url = ALPACA_BASE_URL
    if PAPER_TRADING and "paper-api.alpaca.markets" not in base_url:
        logger.warning("Paper trading enabled but ALPACA_BASE_URL is not a paper endpoint. Overriding to paper URL.")
        base_url = "https://paper-api.alpaca.markets"

    return TradingClient(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
        paper=PAPER_TRADING,
        base_url=base_url,
    )


def send_discord_notification(message: str) -> bool:
    """Send a simple Discord webhook notification for trade events."""
    if not DISCORD_WEBHOOK_URL:
        logger.debug("Discord webhook not configured, skipping notification")
        return False

    payload = {"content": message}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Discord notification sent")
        return True
    except Exception as e:
        logger.warning(f"Discord notification failed: {e}")
        return False


def get_account_status() -> dict:
    """Return Alpaca account balances, buying power, and open position count."""
    try:
        client = get_alpaca_client()
        account = client.get_account()
        positions = client.get_all_positions()

        return {
            "cash": float(account.cash),
            "portfolio_value": float(account.portfolio_value),
            "buying_power": float(account.buying_power),
            "open_positions": len(positions),
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "market_value": float(p.market_value),
                    "current_price": float(p.current_price),
                    "unrealized_pl": float(p.unrealized_pl),
                }
                for p in positions
            ],
            "paper_trading": PAPER_TRADING,
        }
    except Exception as e:
        logger.error(f"Alpaca account status error: {e}")
        return {
            "cash": 0,
            "portfolio_value": 0,
            "buying_power": 0,
            "open_positions": 0,
            "positions": [],
            "paper_trading": PAPER_TRADING,
        }


def get_positions() -> list:
    """Return a list of open Alpaca positions."""
    try:
        client = get_alpaca_client()
        positions = client.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "market_value": float(p.market_value),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
            }
            for p in positions
        ]
    except Exception as e:
        logger.error(f"Failed to fetch Alpaca positions: {e}")
        return []


def find_option_contract_symbol(symbol: str, strike: float, expiry: str, option_type: str) -> str:
    """Find the closest Alpaca option contract symbol for the trade plan."""
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
            limit=10,
        )
        client = get_alpaca_client()
        contracts = client.get_option_contracts(request)
        contract_list = getattr(contracts, "option_contracts", []) or []
        if not contract_list:
            logger.warning(f"No option contracts found for {symbol} {strike} {expiry} {option_type}")
            return None

        best = min(contract_list, key=lambda c: abs(float(c.strike_price) - strike))
        logger.info(f"Found option contract {best.symbol} | Strike {best.strike_price} | Expiry {best.expiration_date}")
        return best.symbol
    except Exception as e:
        logger.error(f"Option contract lookup failed: {e}")
        return None


def place_option_order(contract_symbol: str, qty: int, limit_price: float) -> dict:
    """Submit a paper order for an option contract."""
    try:
        client = get_alpaca_client()
        order_request = LimitOrderRequest(
            symbol=contract_symbol,
            qty=qty,
            side=OrderSide.BUY,
            limit_price=limit_price,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(order_request)
        return {
            "success": True,
            "order_id": str(order.id),
            "filled_qty": float(getattr(order, "filled_qty", 0) or 0),
        }
    except Exception as e:
        logger.error(f"Alpaca order placement failed: {e}")
        return {"success": False, "reason": str(e)}


def close_alpaca_position(option_symbol: str) -> dict:
    """Close a specific Alpaca position by option symbol."""
    try:
        client = get_alpaca_client()
        close_response = client.close_position(option_symbol)
        logger.info(f"Closed Alpaca position: {option_symbol}")
        return {"success": True, "response": close_response}
    except Exception as e:
        logger.error(f"Failed to close Alpaca position {option_symbol}: {e}")
        return {"success": False, "reason": str(e)}


def get_today_trade_metrics() -> tuple[int, float]:
    """Return today's new trade count and realized P/L."""
    count = get_today_trade_count()
    pnl = get_today_realized_pnl()
    return count, pnl


def is_kill_switch_engaged() -> tuple[bool, str]:
    """Check daily loss and trade limits before placing new orders."""
    if not ACCOUNT.get("kill_switch_enabled", True):
        return False, "Kill switch disabled"

    trade_count, daily_pnl = get_today_trade_metrics()
    if trade_count >= ACCOUNT.get("max_trades_per_day", 5):
        return True, f"Daily trade limit reached ({trade_count}/{ACCOUNT['max_trades_per_day']})"

    if daily_pnl <= -abs(ACCOUNT.get("max_daily_loss_usd", 500)):
        return True, f"Daily loss limit exceeded (${daily_pnl:.2f} realized loss)"

    return False, ""


def get_account_balance() -> dict:
    """Return the current Alpaca account balances and buying power."""
    status = get_account_status()
    return {
        "cash": status.get("cash", 0),
        "buying_power": status.get("buying_power", 0),
        "portfolio_value": status.get("portfolio_value", 0),
    }
