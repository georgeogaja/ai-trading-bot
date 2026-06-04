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
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest
from db import get_today_trade_count, get_today_realized_pnl
from config import ACCOUNT
from option_liquidity import liquidity_score

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

    if PAPER_TRADING and "paper-api.alpaca.markets" not in ALPACA_BASE_URL:
        logger.warning(
            "Paper trading enabled but ALPACA_BASE_URL is not a paper endpoint. "
            "Please set ALPACA_BASE_URL=https://paper-api.alpaca.markets in .env."
        )

    return TradingClient(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
        paper=PAPER_TRADING,
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
    """Return Alpaca account balances, buying power, and open position count.
    
    Returns dict with 'error' field if account fetch fails.
    """
    try:
        client = get_alpaca_client()
        account = client.get_account()
        positions = client.get_all_positions()

        return {
            "account_status": account.status,
            "cash": float(account.cash),
            "portfolio_value": float(account.portfolio_value),
            "equity": float(getattr(account, "equity", account.portfolio_value)),
            # Prior-session close equity — used to compute intraday day P/L.
            "last_equity": float(getattr(account, "last_equity", None) or account.equity),
            "buying_power": float(account.buying_power),
            # Long options are paid from cash, so Alpaca's OPTIONS buying power
            # (≈ cash / non-marginable BP) is the real affordability limit — the
            # general margin buying_power overstates what an option order can use.
            "options_buying_power": float(
                getattr(account, "options_buying_power", None) or account.cash
            ),
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
            "error": None,
        }
    except Exception as e:
        error_msg = f"Failed to fetch Alpaca account status: {e}"
        logger.error(f"❌ {error_msg}")
        return {
            "cash": 0,
            "portfolio_value": 0,
            "buying_power": 0,
            "open_positions": 0,
            "positions": [],
            "paper_trading": PAPER_TRADING,
            "error": error_msg,
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
            underlying_symbols=[symbol],
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

        # Safety guard: only consider contracts on the requested underlying (the
        # broker/paper sandbox can return sample contracts that ignore the filter).
        requested = symbol.strip().upper()
        contract_list = [
            c for c in contract_list
            if str(getattr(c, "underlying_symbol", "") or "").strip().upper() == requested
        ]
        if not contract_list:
            logger.error(
                f"Option contract underlying mismatch for {symbol} — "
                f"refusing to return a wrong-underlying contract"
            )
            return None

        best = min(contract_list, key=lambda c: abs(float(c.strike_price) - strike))
        logger.info(f"Found option contract {best.symbol} | Strike {best.strike_price} | Expiry {best.expiration_date}")
        return best.symbol
    except Exception as e:
        logger.error(f"Option contract lookup failed: {e}")
        return None


_option_data_client = None


def get_option_data_client() -> OptionHistoricalDataClient:
    """Lazily create a single options market-data client."""
    global _option_data_client
    if _option_data_client is None:
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            raise RuntimeError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in .env")
        _option_data_client = OptionHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return _option_data_client


def get_option_snapshots(contract_symbols: list[str]) -> dict:
    """Fetch live liquidity quotes for one or more option contracts.

    Returns {contract_symbol: {bid, ask, bid_size, ask_size, volume,
    open_interest}}. Fields are None when the feed omits them (open interest is
    not exposed by the Alpaca snapshot model). Returns {} on any error — the
    caller decides how to treat missing data via OPTIONS_LIQUIDITY policy.
    """
    if not contract_symbols:
        return {}
    try:
        client = get_option_data_client()
        request = OptionSnapshotRequest(symbol_or_symbols=contract_symbols)
        snapshots = client.get_option_snapshot(request)
        items = snapshots.items() if hasattr(snapshots, "items") else []
        out = {}
        for sym, snap in items:
            quote = getattr(snap, "latest_quote", None)
            daily = getattr(snap, "daily_bar", None)
            out[sym] = {
                "bid": float(quote.bid_price) if quote and quote.bid_price is not None else None,
                "ask": float(quote.ask_price) if quote and quote.ask_price is not None else None,
                "bid_size": float(quote.bid_size) if quote and quote.bid_size is not None else None,
                "ask_size": float(quote.ask_size) if quote and quote.ask_size is not None else None,
                "volume": float(daily.volume) if daily and daily.volume is not None else None,
                "open_interest": None,  # not provided by the snapshot model
            }
        return out
    except Exception as e:
        logger.warning(f"Option snapshot fetch failed: {e}")
        return {}


def find_best_option_contract(symbol: str, strike: float, expiry: str, option_type: str) -> dict:
    """Select the most liquid near-the-money contract for the trade plan.

    Fetches the candidate contracts (same strike/expiry window as
    find_option_contract_symbol), pulls their liquidity snapshots, and ranks
    them by liquidity_score (tight spread + OI + volume + strike proximity).
    Returns {symbol, strike, expiry, liquidity} for the best candidate, or None
    if no contract is found.

    If snapshot data is unavailable for all candidates, falls back to the
    closest-strike contract with an empty liquidity dict so the executor's
    liquidity gate can apply the configured fail-open/closed policy.
    """
    try:
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d")
        contract_type = ContractType.CALL if option_type == "CALL" else ContractType.PUT
        request = GetOptionContractsRequest(
            underlying_symbols=[symbol],
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

        # Safety guard: never trade a contract on a different underlying than the
        # one requested. The broker (notably the paper sandbox) can return sample
        # contracts that ignore the underlying_symbol filter — placing one of those
        # would be a wrong-ticker order. Reject any candidate whose underlying does
        # not match before ranking or ordering.
        requested = symbol.strip().upper()
        matched = [
            c for c in contract_list
            if str(getattr(c, "underlying_symbol", "") or "").strip().upper() == requested
        ]
        if not matched:
            returned = sorted({
                str(getattr(c, "underlying_symbol", "") or "?").upper() for c in contract_list
            })
            logger.error(
                f"Option contract underlying mismatch for {symbol}: broker returned "
                f"contracts for {returned} — refusing to trade a wrong-underlying contract"
            )
            return None
        contract_list = matched

        symbols = [c.symbol for c in contract_list]
        snapshots = get_option_snapshots(symbols)

        # Rank by liquidity (preference). Contracts that fail the liquidity gate
        # score -inf, so a tradeable contract is always chosen when one exists.
        def _score(contract) -> float:
            quote = snapshots.get(contract.symbol, {})
            return liquidity_score(quote, float(contract.strike_price), strike)

        best = max(contract_list, key=_score)
        best_quote = snapshots.get(best.symbol, {})

        # If nothing scored (no usable snapshots at all), fall back to closest strike.
        if not snapshots:
            best = min(contract_list, key=lambda c: abs(float(c.strike_price) - strike))
            best_quote = {}

        logger.info(
            f"Selected option contract {best.symbol} | Strike {best.strike_price} | "
            f"Expiry {best.expiration_date}"
        )
        return {
            "symbol": best.symbol,
            "strike": float(best.strike_price),
            "expiry": str(best.expiration_date),
            "liquidity": best_quote,
        }
    except Exception as e:
        logger.error(f"Best option contract lookup failed: {e}")
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
