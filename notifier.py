"""Discord webhook notifier for George's trading bot.

Uses Discord embeds for clean, professional swing options alerts.
"""
from dotenv import load_dotenv
from pathlib import Path
import os
from datetime import datetime, timezone

import requests
from loguru import logger

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

COLOR_GREEN = 0x2ecc71
COLOR_RED = 0xe74c3c
COLOR_BLUE = 0x3498db
COLOR_GREY = 0x95a5a6


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_string(value) -> str:
    if value is None:
        return "N/A"
    cleaned = str(value).strip()
    return cleaned if cleaned else "N/A"


def _sanitize_fields(fields: list[dict]) -> list[dict]:
    sanitized = []
    for field in fields or []:
        name = _clean_string(field.get("name"))
        value = _clean_string(field.get("value"))
        inline = bool(field.get("inline", False))
        sanitized.append({"name": name, "value": value, "inline": inline})
    return sanitized


def _build_embed(title: str, description: str = None, fields: list[dict] = None, color: int = COLOR_BLUE) -> dict:
    embed = {
        "title": _clean_string(title),
        "color": color,
        "footer": {"text": "Paper trading only"},
    }
    if description is not None:
        embed["description"] = _clean_string(description)
    sanitized_fields = _sanitize_fields(fields)
    if sanitized_fields:
        embed["fields"] = sanitized_fields
    return embed


def _send_discord(payload: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        logger.warning("Discord webhook not configured, skipping notification")
        return False

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Discord notification sent")
        return True
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        response_text = exc.response.text if exc.response is not None else "No response body"
        logger.warning(f"Discord webhook HTTP error: {status_code} - {response_text}")
        return False
    except Exception:
        logger.warning("Discord webhook failed due to network or configuration issue")
        return False


def send_discord_message(message: str) -> bool:
    return _send_discord({"content": _clean_string(message)})


def notify_watch_mode(
    symbol: str,
    direction: str,
    confidence: float | int,
    expiration_window: str,
    strike_preference: str,
    catalyst: str,
    technical_setup: str,
    invalidation_level: str,
    frequency_minutes: int,
    note: str = "No trade entered yet. Watching for confirmation.",
) -> bool:
    fields = [
        {"name": "Ticker", "value": symbol, "inline": True},
        {"name": "Setup Type", "value": "Swing Options", "inline": True},
        {"name": "Direction", "value": direction, "inline": True},
        {"name": "Confidence", "value": f"{confidence}/10", "inline": True},
        {"name": "Expiration Window", "value": expiration_window, "inline": True},
        {"name": "Strike Preference", "value": strike_preference, "inline": True},
        {"name": "Catalyst", "value": catalyst or "Not specified", "inline": False},
        {"name": "Technical Setup", "value": technical_setup or "Trend and momentum confirmation", "inline": False},
        {"name": "Invalidation Level", "value": invalidation_level or "Set by setup invalidation", "inline": False},
        {"name": "Monitoring Frequency", "value": f"{frequency_minutes} minutes", "inline": True},
        {"name": "Status", "value": note, "inline": False},
    ]
    embed = _build_embed(
        f"Watch Mode Activated — {symbol}",
        description="Swing options watch setup identified. No trade entered yet.",
        fields=fields,
        color=COLOR_BLUE,
    )
    return _send_discord({"embeds": [embed]})


def notify_trade_entered(
    symbol: str,
    option_type: str,
    strike: float,
    expiry: str,
    entry_premium: float,
    stop_loss_premium: float,
    profit_target_premium: float,
    max_risk: float,
    position_size: str,
    expected_hold: str,
    reason: str,
    exit_plan: str,
) -> bool:
    fields = [
        {"name": "Ticker", "value": symbol, "inline": True},
        {"name": "Option Type", "value": option_type, "inline": True},
        {"name": "Strike", "value": f"{strike}", "inline": True},
        {"name": "Expiration", "value": expiry or "Unknown", "inline": True},
        {"name": "Entry Premium", "value": f"${entry_premium:.2f}", "inline": True},
        {"name": "Stop Loss Premium", "value": f"${stop_loss_premium:.2f}", "inline": True},
        {"name": "Profit Target Premium", "value": f"${profit_target_premium:.2f}", "inline": True},
        {"name": "Max Risk", "value": f"${max_risk:,.2f}", "inline": True},
        {"name": "Position Size", "value": position_size, "inline": True},
        {"name": "Expected Hold", "value": expected_hold, "inline": True},
        {"name": "Reason for Entry", "value": reason or "Confirmed swing setup", "inline": False},
        {"name": "Exit Plan", "value": exit_plan or "Hold to target or invalidation level", "inline": False},
    ]
    embed = _build_embed(
        f"Trade Entered — {symbol}",
        description="Paper trade executed for a swing options setup.",
        fields=fields,
        color=COLOR_GREEN,
    )
    return _send_discord({"embeds": [embed]})


def notify_trade_exited(
    symbol: str,
    option_type: str,
    exit_reason: str,
    entry_premium: float,
    exit_premium: float,
    percent_return: float,
    dollar_pnl: float,
    lesson: str,
) -> bool:
    color = COLOR_GREEN if dollar_pnl >= 0 else COLOR_RED
    footer_text = "Paper trade closed" if dollar_pnl >= 0 else "Paper trade closed with loss"
    fields = [
        {"name": "Ticker", "value": symbol, "inline": True},
        {"name": "Option Type", "value": option_type, "inline": True},
        {"name": "Exit Reason", "value": exit_reason, "inline": True},
        {"name": "Entry Premium", "value": f"${entry_premium:.2f}", "inline": True},
        {"name": "Exit Premium", "value": f"${exit_premium:.2f}", "inline": True},
        {"name": "Return", "value": f"{percent_return:.1f}%", "inline": True},
        {"name": "Dollar P/L", "value": f"${dollar_pnl:,.2f}", "inline": True},
        {"name": "What Was Learned", "value": lesson or "Review setup and execution details.", "inline": False},
    ]
    embed = _build_embed(
        f"Trade Exited — {symbol}",
        description="Swing options paper position closed.",
        fields=fields,
        color=color,
    )
    embed["footer"]["text"] = footer_text
    return _send_discord({"embeds": [embed]})


def notify_risk_blocked(
    symbol: str,
    direction: str,
    reason: str,
    risk_rule: str,
    improvement: str,
) -> bool:
    fields = [
        {"name": "Ticker", "value": symbol, "inline": True},
        {"name": "Setup Direction", "value": direction, "inline": True},
        {"name": "Blocked Reason", "value": reason, "inline": False},
        {"name": "Risk Rule Triggered", "value": risk_rule, "inline": False},
        {"name": "Improvement Needed", "value": improvement or "Tighten setup or reduce risk exposure.", "inline": False},
    ]
    embed = _build_embed(
        f"Risk Blocked — {symbol}",
        description="Swing options trade blocked by risk controls.",
        fields=fields,
        color=COLOR_RED,
    )
    return _send_discord({"embeds": [embed]})


def notify_runner_created(
    symbol: str,
    option_type: str,
    contracts_remaining: int,
    entry_premium: float,
    current_premium: float,
    runner_stop: float,
    runner_target: float,
    days_to_expiration: int,
    reason_still_holding: str,
) -> bool:
    fields = [
        {"name": "Ticker", "value": symbol, "inline": True},
        {"name": "Contract", "value": option_type, "inline": True},
        {"name": "Contracts Remaining", "value": str(contracts_remaining), "inline": True},
        {"name": "Entry", "value": f"${entry_premium:.2f}", "inline": True},
        {"name": "Current", "value": f"${current_premium:.2f}", "inline": True},
        {"name": "Unrealized P/L", "value": f"${current_premium - entry_premium:.2f}", "inline": True},
        {"name": "Runner Stop", "value": f"${runner_stop:.2f}", "inline": True},
        {"name": "Runner Target", "value": f"${runner_target:.2f}", "inline": True},
        {"name": "Days to Expiration", "value": str(days_to_expiration), "inline": True},
        {"name": "Reason Still Holding", "value": reason_still_holding or "Maintaining runner toward target.", "inline": False},
    ]
    embed = _build_embed(
        "🏃 SWING OPTIONS RUNNER ACTIVE",
        description="Runner created after partial profit taking.",
        fields=fields,
        color=COLOR_BLUE,
    )
    return _send_discord({"embeds": [embed]})


def notify_runner_stop_moved(
    symbol: str,
    option_type: str,
    runner_stop: float,
    days_to_expiration: int,
    reason_still_holding: str,
) -> bool:
    fields = [
        {"name": "Ticker", "value": symbol, "inline": True},
        {"name": "Contract", "value": option_type, "inline": True},
        {"name": "Runner Stop", "value": f"${runner_stop:.2f}", "inline": True},
        {"name": "Days to Expiration", "value": str(days_to_expiration), "inline": True},
        {"name": "Reason Still Holding", "value": reason_still_holding or "Runner stop moved to protect gains.", "inline": False},
    ]
    embed = _build_embed(
        "🏃 SWING OPTIONS RUNNER STOP MOVED",
        description="Runner stop level updated to protect the remaining position.",
        fields=fields,
        color=COLOR_BLUE,
    )
    return _send_discord({"embeds": [embed]})


def notify_runner_target_hit(
    symbol: str,
    option_type: str,
    entry_premium: float,
    exit_premium: float,
    percent_return: float,
    dollar_pnl: float,
    days_to_expiration: int,
) -> bool:
    fields = [
        {"name": "Ticker", "value": symbol, "inline": True},
        {"name": "Contract", "value": option_type, "inline": True},
        {"name": "Entry", "value": f"${entry_premium:.2f}", "inline": True},
        {"name": "Exit", "value": f"${exit_premium:.2f}", "inline": True},
        {"name": "Percent Gain/Loss", "value": f"{percent_return:.1f}%", "inline": True},
        {"name": "Dollar P/L", "value": f"${dollar_pnl:,.2f}", "inline": True},
        {"name": "Days to Expiration", "value": str(days_to_expiration), "inline": True},
    ]
    embed = _build_embed(
        "🏃 SWING OPTIONS RUNNER TARGET HIT",
        description="Runner target achieved and position is being closed.",
        fields=fields,
        color=COLOR_GREEN,
    )
    return _send_discord({"embeds": [embed]})


def notify_runner_closed(
    symbol: str,
    option_type: str,
    entry_premium: float,
    exit_premium: float,
    percent_return: float,
    dollar_pnl: float,
    days_to_expiration: int,
    reason: str,
) -> bool:
    fields = [
        {"name": "Ticker", "value": symbol, "inline": True},
        {"name": "Contract", "value": option_type, "inline": True},
        {"name": "Entry", "value": f"${entry_premium:.2f}", "inline": True},
        {"name": "Exit", "value": f"${exit_premium:.2f}", "inline": True},
        {"name": "Percent Gain/Loss", "value": f"{percent_return:.1f}%", "inline": True},
        {"name": "Dollar P/L", "value": f"${dollar_pnl:,.2f}", "inline": True},
        {"name": "Days to Expiration", "value": str(days_to_expiration), "inline": True},
        {"name": "Reason", "value": reason or "Runner position closed based on risk management.", "inline": False},
    ]
    embed = _build_embed(
        "🏃 SWING OPTIONS RUNNER CLOSED",
        description="Runner position has been closed.",
        fields=fields,
        color=COLOR_RED if dollar_pnl < 0 else COLOR_GREEN,
    )
    return _send_discord({"embeds": [embed]})


def notify_liquidity_blocked(
    symbol: str,
    option_type: str,
    contract_symbol: str,
    reason: str,
    details: str,
) -> bool:
    """Post an options-liquidity rejection to Discord."""
    fields = [
        {"name": "Ticker", "value": symbol, "inline": True},
        {"name": "Option Type", "value": option_type, "inline": True},
        {"name": "Contract", "value": contract_symbol or "N/A", "inline": False},
        {"name": "Reason", "value": reason, "inline": False},
        {"name": "Liquidity", "value": details or "N/A", "inline": False},
    ]
    embed = _build_embed(
        f"Liquidity Blocked — {symbol}",
        description="Option contract rejected by the liquidity filter before execution.",
        fields=fields,
        color=COLOR_RED,
    )
    return _send_discord({"embeds": [embed]})


def notify_runner_summary(runners: list[dict]) -> bool:
    lines = []
    for runner in runners[:5]:
        lines.append(
            f"{runner['symbol']} | {runner['contracts_remaining']} contracts | "
            f"${runner['current_option_price']:.2f} / ${runner['entry_option_price']:.2f} "
            f"({runner.get('percent_gain_loss', 0):+.1f}%) | stop ${runner['runner_stop_price']:.2f} | "
            f"target ${runner['runner_target_price']:.2f} | {runner['days_to_expiration']}d"
        )
    description = "\n".join(lines) if lines else "No active runners at this time."
    embed = _build_embed(
        "🏃 SWING OPTIONS RUNNER SUMMARY",
        description=description,
        color=COLOR_BLUE,
    )
    return _send_discord({"embeds": [embed]})


def notify_daily_summary(summary: str) -> bool:
    embed = _build_embed("SWING OPTIONS SUMMARY", description=summary, color=COLOR_BLUE)
    return _send_discord({"embeds": [embed]})


def notify_market_regime(
    regime: str,
    score: int,
    spy_trend: str,
    qqq_trend: str,
    vix: float | None,
    vol_source: str,
    posture: str,
    reasons: list[str] | None = None,
) -> bool:
    """Post the current market regime summary to Discord.

    `regime` is BULLISH / NEUTRAL / BEARISH; `posture` is the plain-English
    description of what the regime allows (normal longs, reduced size, or
    longs/calls blocked).
    """
    regime = (regime or "NEUTRAL").upper()
    color = {
        "BULLISH": COLOR_GREEN,
        "NEUTRAL": COLOR_GREY,
        "BEARISH": COLOR_RED,
    }.get(regime, COLOR_BLUE)

    vix_value = f"{vix:.2f}" if vix is not None else "N/A"
    fields = [
        {"name": "Regime", "value": regime, "inline": True},
        {"name": "Trend Score", "value": f"{score}/6", "inline": True},
        {"name": vol_source if vol_source != "NONE" else "Volatility",
         "value": vix_value, "inline": True},
        {"name": "SPY Trend", "value": spy_trend, "inline": True},
        {"name": "QQQ Trend", "value": qqq_trend, "inline": True},
        {"name": "Posture", "value": posture, "inline": False},
    ]
    if reasons:
        fields.append(
            {"name": "Signals", "value": "\n".join(f"• {r}" for r in reasons), "inline": False}
        )
    embed = _build_embed(
        f"MARKET REGIME: {regime}",
        description="Overall market regime assessed before scanning individual stocks.",
        fields=fields,
        color=color,
    )
    return _send_discord({"embeds": [embed]})


def notify_sector_strength(
    strongest: list[tuple[str, dict]],
    weakest: list[tuple[str, dict]],
    benchmark: float | None,
    top_n: int = 3,
) -> bool:
    """Post the sector-strength ranking (strongest vs weakest) to Discord.

    `strongest` / `weakest` are lists of (group_name, detail) where detail holds
    `rank`, `rel` and `return`.
    """
    def _fmt(rows: list[tuple[str, dict]]) -> str:
        lines = []
        for name, d in rows[:top_n]:
            rel = d.get("rel")
            rank = d.get("rank", "NEUTRAL")
            rel_txt = f"{rel:+.1f}%" if rel is not None else "n/a"
            lines.append(f"{name} — {rank} ({rel_txt} vs SPY/QQQ)")
        return "\n".join(lines) if lines else "No ranked sectors."

    bench_txt = f"{benchmark:+.1f}%" if benchmark is not None else "N/A"
    fields = [
        {"name": "Strongest Sectors", "value": _fmt(strongest), "inline": False},
        {"name": "Weakest Sectors", "value": _fmt(weakest), "inline": False},
        {"name": "Benchmark (SPY/QQQ blend, ~21d)", "value": bench_txt, "inline": True},
    ]
    embed = _build_embed(
        "SECTOR STRENGTH",
        description="Sector/theme strength vs SPY/QQQ, assessed before ranking stocks.",
        fields=fields,
        color=COLOR_BLUE,
    )
    return _send_discord({"embeds": [embed]})
