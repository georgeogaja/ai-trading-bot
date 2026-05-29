"""State manager for trading bot modes: NORMAL, WATCH, ACTIVE.

Logs state transitions and exposes API for other modules to query/update state.
"""
from datetime import datetime
from pathlib import Path
from loguru import logger

JOURNAL = Path("journal")
JOURNAL.mkdir(parents=True, exist_ok=True)
TRANSITION_FILE = JOURNAL / "state_transitions.log"


class StateManager:
    def __init__(self):
        self.state = "NORMAL"
        self.active_symbols = []
        self.watch_expires = None

    def _log_transition(self, from_state: str, to_state: str, reason: str = ""):
        ts = datetime.now().isoformat()
        line = f"{ts} | {from_state} -> {to_state} | {reason}\n"
        try:
            TRANSITION_FILE.write_text(TRANSITION_FILE.read_text() + line)
        except Exception:
            with TRANSITION_FILE.open("a", encoding="utf-8") as f:
                f.write(line)
        logger.info(f"State transition: {from_state} -> {to_state} | {reason}")

    def set_normal(self):
        if self.state != "NORMAL":
            self._log_transition(self.state, "NORMAL", "returning to idle")
            self.state = "NORMAL"
            self.active_symbols = []
            self.watch_expires = None

    def set_watch(self, symbols: list, expires_at, reason: str = ""):
        prev = self.state
        self.state = "WATCH"
        self.active_symbols = list(dict.fromkeys(symbols))
        self.watch_expires = expires_at
        self._log_transition(prev, "WATCH", reason or f"symbols={self.active_symbols}")

    def set_active(self, symbols: list, reason: str = ""):
        prev = self.state
        self.state = "ACTIVE"
        self.active_symbols = list(dict.fromkeys(symbols))
        self.watch_expires = None
        self._log_transition(prev, "ACTIVE", reason or f"symbols={self.active_symbols}")

    def set_exited(self, reason: str = ""):
        prev = self.state
        self.state = "EXITED"
        self.active_symbols = []
        self.watch_expires = None
        self._log_transition(prev, "EXITED", reason)

    def get_state(self):
        return {
            "state": self.state,
            "symbols": self.active_symbols,
            "watch_expires": self.watch_expires,
        }


state_manager = StateManager()
