"""Test runner notifications for swing options flow.

This script simulates a NVDA swing options setup and exercises Discord
notification helpers without placing real or paper trades.
"""
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
from notifier import (
    notify_trade_entered,
    notify_runner_created,
    notify_runner_summary,
    notify_runner_stop_moved,
    notify_runner_target_hit,
    notify_runner_closed,
)

load_dotenv()


def print_status(message: str):
    print(message)


def main():
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print_status("DISCORD_WEBHOOK_URL is not configured. Notifications will not be sent.")

    symbol = "NVDA"
    option_type = "CALL"
    strike = 1200.0
    expiry = (datetime.now() + timedelta(days=45)).date().isoformat()
    entry_premium = 18.50
    current_premium = 18.50
    stop_level = 18.50
    target_level = 36.00
    contracts_remaining = 1

    print_status("1. Sending swing trade opened notification...")
    notify_trade_entered(
        symbol=symbol,
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        entry_premium=entry_premium,
        stop_loss_premium=stop_level,
        profit_target_premium=target_level,
        max_risk=1850.0,
        position_size=f"{contracts_remaining} contract(s)",
        expected_hold="A multiday swing through the next earnings cycle.",
        reason="NVDA call aligned with bullish momentum, support structure, and macro setup.",
        exit_plan="Take partial profit at 50% gain, hold runner with breakeven stop, exit on target or invalidation.",
    )

    print_status("2. Sending partial profit / runner created notification...")
    current_premium = 27.75
    notify_runner_created(
        symbol=symbol,
        option_type=option_type,
        contracts_remaining=1,
        entry_premium=entry_premium,
        current_premium=current_premium,
        runner_stop=entry_premium,
        runner_target=target_level,
        days_to_expiration=30,
        reason_still_holding="Runner retained after taking partial profits and moving stop to breakeven.",
    )

    print_status("3. Sending runner still active summary notification...")
    notify_runner_summary([
        {
            "symbol": symbol,
            "contracts_remaining": contracts_remaining,
            "entry_option_price": entry_premium,
            "current_option_price": current_premium,
            "percent_gain_loss": 50.0,
            "runner_stop_price": stop_level,
            "runner_target_price": target_level,
            "days_to_expiration": 30,
        }
    ])

    print_status("4. Sending runner stop-loss moved to breakeven notification...")
    notify_runner_stop_moved(
        symbol=symbol,
        option_type=option_type,
        runner_stop=entry_premium,
        days_to_expiration=28,
        reason_still_holding="Stop moved to breakeven to protect the remaining position.",
    )

    print_status("5. Sending runner over 100% notification...")
    current_premium = 38.00
    notify_runner_target_hit(
        symbol=symbol,
        option_type=option_type,
        entry_premium=entry_premium,
        exit_premium=current_premium,
        percent_return=105.4,
        dollar_pnl=(current_premium - entry_premium) * 100 * contracts_remaining,
        days_to_expiration=14,
    )

    print_status("6. Sending runner closed notification...")
    notify_runner_closed(
        symbol=symbol,
        option_type=option_type,
        entry_premium=entry_premium,
        exit_premium=current_premium,
        percent_return=105.4,
        dollar_pnl=(current_premium - entry_premium) * 100 * contracts_remaining,
        days_to_expiration=14,
        reason="Runner closed after target reached and momentum was intact.",
    )

    print_status("Test notifications sent. Check Discord for the messages.")


if __name__ == "__main__":
    main()
