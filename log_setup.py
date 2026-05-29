"""Centralized logging configuration for the trading bot.

Call configure_logging() once at process startup (in main.py or any entry point).
All other modules use loguru's module-level `logger` directly — no additional setup needed.
"""
import sys
from pathlib import Path
from loguru import logger
from config import LOGGING


def configure_logging(log_prefix: str = "trading_bot") -> None:
    """Remove loguru defaults and set up stdout + rotating file logging."""
    logger.remove()

    Path(LOGGING["log_dir"]).mkdir(parents=True, exist_ok=True)

    # Human-readable stdout
    logger.add(
        sys.stdout,
        level=LOGGING["log_level"],
        format=LOGGING["format"],
        colorize=False,
    )

    # Rotating daily file log
    log_file = Path(LOGGING["log_dir"]) / f"{log_prefix}_{{time:YYYY-MM-DD}}.log"
    logger.add(
        str(log_file),
        level=LOGGING["log_level"],
        rotation=LOGGING["rotation"],
        retention=LOGGING["retention"],
        format=LOGGING["format"],
        encoding="utf-8",
    )

    logger.info(
        f"Logging configured | prefix={log_prefix} | "
        f"dir={LOGGING['log_dir']} | level={LOGGING['log_level']}"
    )
