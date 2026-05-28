"""Simple Alpaca paper connection smoke test.

This script loads environment variables from .env, verifies that paper trading
is enabled, confirms the Alpaca endpoint is the paper endpoint, and prints the
current account status and buying power.
"""

import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient


def main():
    load_dotenv()

    paper_trading = os.getenv("PAPER_TRADING", "true").strip().lower()
    alpaca_base_url = os.getenv(
        "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
    ).strip()
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if paper_trading != "true":
        raise SystemExit(
            "ERROR: PAPER_TRADING must be true for this test. "
            "Set PAPER_TRADING=true in your .env file."
        )

    if "paper-api.alpaca.markets" not in alpaca_base_url:
        raise SystemExit(
            f"ERROR: Alpaca base URL must be the paper endpoint. "
            f"Current ALPACA_BASE_URL={alpaca_base_url}"
        )

    if not api_key or not secret_key:
        raise SystemExit(
            "ERROR: ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env."
        )

    print("Connecting to Alpaca paper endpoint:")
    print(f"  PAPER_TRADING={paper_trading}")
    print(f"  ALPACA_BASE_URL={alpaca_base_url}")

    client = TradingClient(api_key, secret_key, paper=True)
    account = client.get_account()

    print("\nAlpaca account status:")
    print(f"  Account status: {account.status}")
    print(f"  Cash: ${account.cash}")
    print(f"  Buying power: ${account.buying_power}")
    print(f"  Portfolio value: ${account.portfolio_value}")

    print("\nTest complete: connected to Alpaca paper endpoint successfully.")
    print("No trades were placed.")


if __name__ == "__main__":
    main()
