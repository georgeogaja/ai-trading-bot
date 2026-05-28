"""
database.py — George's Trading Bot Database
SQLite database for trade tracking, mistake logging, and performance analytics.
This is the memory system — the bot learns from everything stored here.
"""

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger

DB_PATH = Path("database/trading_bot.db")

def get_connection():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def initialize_database():
    """Create all tables on first run."""
    conn = get_connection()
    cursor = conn.cursor()

    # ── WATCHLIST ─────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS watchlist (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT NOT NULL,
        added_date      TEXT NOT NULL,
        source          TEXT,           -- 'CORE' | 'NEWS' | 'SECTOR_TREND' | 'AI_AGENT'
        sector          TEXT,
        reason          TEXT,           -- Why added
        news_headline   TEXT,           -- Headline that triggered add
        is_active       INTEGER DEFAULT 1,
        removed_date    TEXT,
        removed_reason  TEXT
    )""")

    # ── TRADES ────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol              TEXT NOT NULL,
        option_symbol       TEXT,           -- Full option symbol
        trade_type          TEXT,           -- 'CALL' | 'PUT'
        strategy            TEXT,           -- 'SWING' | 'LEAP'
        entry_date          TEXT NOT NULL,
        exit_date           TEXT,
        entry_stock_price   REAL,
        exit_stock_price    REAL,
        strike              REAL,
        expiry              TEXT,
        contracts           INTEGER DEFAULT 1,
        entry_option_price  REAL,
        exit_option_price   REAL,
        stop_loss_level     REAL,           -- Stock price stop
        target_1            REAL,           -- Option price target 1
        target_2            REAL,           -- Option price target 2
        status              TEXT DEFAULT 'OPEN',  -- 'OPEN' | 'CLOSED' | 'STOPPED'
        pnl_dollars         REAL,
        pnl_pct             REAL,
        exit_reason         TEXT,           -- 'TARGET_1' | 'TARGET_2' | 'STOP' | 'MANUAL'
        signal_score        INTEGER,        -- Bull score at entry (1-10)
        rsi_at_entry        REAL,
        adx_at_entry        REAL,
        bias_at_entry       TEXT,
        rsi_divergence      INTEGER DEFAULT 0,
        macro_condition     TEXT,           -- 'GREEN' | 'YELLOW' | 'RED'
        sector              TEXT,
        catalyst            TEXT,
        thesis              TEXT,
        alpaca_order_id     TEXT,
        notes               TEXT
    )""")

    # ── MISTAKES ──────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS mistakes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id        INTEGER,
        date            TEXT NOT NULL,
        symbol          TEXT,
        category        TEXT NOT NULL,  -- From MISTAKE_CATEGORIES in config
        description     TEXT,
        what_went_wrong TEXT,
        what_to_do_instead TEXT,
        pnl_impact      REAL,           -- How much this mistake cost
        severity        TEXT,           -- 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'
        adjustment_made TEXT,           -- What the bot changed in response
        FOREIGN KEY (trade_id) REFERENCES trades(id)
    )""")

    # ── PERFORMANCE METRICS ───────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS performance (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        date            TEXT NOT NULL,
        period          TEXT,           -- 'DAILY' | 'WEEKLY' | 'MONTHLY'
        total_trades    INTEGER,
        winning_trades  INTEGER,
        losing_trades   INTEGER,
        win_rate        REAL,
        total_pnl       REAL,
        avg_win         REAL,
        avg_loss        REAL,
        largest_win     REAL,
        largest_loss    REAL,
        profit_factor   REAL,           -- Gross profit / Gross loss
        account_value   REAL,
        return_pct      REAL,
        sharpe_ratio    REAL,
        notes           TEXT
    )""")

    # ── MACRO LOG ─────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS macro_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        date        TEXT NOT NULL,
        oil_price   REAL,
        vix         REAL,
        ten_yr_yield REAL,
        sp500       REAL,
        macro_signal TEXT,      -- 'GREEN' | 'YELLOW' | 'RED'
        iran_status  TEXT,
        hormuz_status TEXT,
        summary     TEXT
    )""")

    # ── SIGNALS LOG ───────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signals_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        date        TEXT NOT NULL,
        symbol      TEXT NOT NULL,
        signal      TEXT,
        bull_score  INTEGER,
        bear_score  INTEGER,
        rsi         REAL,
        adx         REAL,
        bias        TEXT,
        acted_on    INTEGER DEFAULT 0,  -- 1 if trade was placed
        reason_skipped TEXT
    )""")

    # ── WEEKLY REPORTS ────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS weekly_reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        week_start  TEXT NOT NULL,
        week_end    TEXT NOT NULL,
        report_text TEXT,
        total_pnl   REAL,
        win_rate    REAL,
        created_at  TEXT
    )""")

    # ── ADJUSTMENTS ───────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS adjustments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        date        TEXT,
        adjustment  TEXT,
        reason      TEXT
    )""")

    conn.commit()
    conn.close()
    logger.info("✅ Database initialized successfully")


# ─────────────────────────────────────────────────────────────
# TRADE FUNCTIONS
# ─────────────────────────────────────────────────────────────

def log_trade_entry(trade_data: dict) -> int:
    """Record a new trade entry. Returns trade ID."""
    conn = get_connection()
    cursor = conn.cursor()
    trade_data['entry_date'] = datetime.now().isoformat()
    trade_data['status'] = 'OPEN'

    columns = ', '.join(trade_data.keys())
    placeholders = ', '.join(['?' for _ in trade_data])
    cursor.execute(
        f"INSERT INTO trades ({columns}) VALUES ({placeholders})",
        list(trade_data.values())
    )
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    logger.info(f"📝 Trade logged: {trade_data.get('symbol')} | ID: {trade_id}")
    return trade_id


def update_trade_exit(trade_id: int, exit_data: dict):
    """Update a trade when it closes."""
    conn = get_connection()
    cursor = conn.cursor()
    exit_data['exit_date'] = datetime.now().isoformat()

    # Calculate P/L
    cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
    trade = cursor.fetchone()
    if trade:
        entry_price = trade['entry_option_price']
        exit_price = exit_data.get('exit_option_price', 0)
        contracts = trade['contracts']
        pnl = (exit_price - entry_price) * 100 * contracts
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        exit_data['pnl_dollars'] = pnl
        exit_data['pnl_pct'] = pnl_pct

    set_clause = ', '.join([f"{k} = ?" for k in exit_data.keys()])
    cursor.execute(
        f"UPDATE trades SET {set_clause} WHERE id = ?",
        list(exit_data.values()) + [trade_id]
    )
    conn.commit()
    conn.close()
    logger.info(f"✅ Trade {trade_id} closed | P/L: ${exit_data.get('pnl_dollars', 0):.2f}")


def get_open_trades():
    """Get all currently open positions."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades WHERE status = 'OPEN'")
    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades


def get_trade_by_id(trade_id: int) -> dict:
    """Fetch a single trade by its database ID."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else {}


def get_adjustments(days_back: int = 365) -> list:
    """Fetch historical strategy adjustments from the learning engine."""
    conn = get_connection()
    cursor = conn.cursor()
    since = (datetime.now() - timedelta(days=days_back)).isoformat()
    cursor.execute(
        "SELECT * FROM adjustments WHERE date >= ? ORDER BY date ASC",
        (since,)
    )
    adjustments = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return adjustments


# ─────────────────────────────────────────────────────────────
# MISTAKE TRACKING
# ─────────────────────────────────────────────────────────────

def log_mistake(trade_id: int, mistake_data: dict):
    """Record a mistake for the learning engine."""
    conn = get_connection()
    cursor = conn.cursor()
    mistake_data['date'] = datetime.now().isoformat()
    mistake_data['trade_id'] = trade_id

    columns = ', '.join(mistake_data.keys())
    placeholders = ', '.join(['?' for _ in mistake_data])
    cursor.execute(
        f"INSERT INTO mistakes ({columns}) VALUES ({placeholders})",
        list(mistake_data.values())
    )
    conn.commit()
    conn.close()
    logger.warning(f"⚠️ Mistake logged: {mistake_data.get('category')} | {mistake_data.get('symbol')}")


def get_mistake_patterns(days_back: int = 30) -> dict:
    """Analyze recent mistakes to find patterns."""
    conn = get_connection()
    cursor = conn.cursor()
    since = (datetime.now() - timedelta(days=days_back)).isoformat()

    cursor.execute("""
        SELECT category, COUNT(*) as count, SUM(pnl_impact) as total_impact
        FROM mistakes
        WHERE date >= ?
        GROUP BY category
        ORDER BY count DESC
    """, (since,))

    patterns = {row['category']: {
        'count': row['count'],
        'total_impact': row['total_impact']
    } for row in cursor.fetchall()}
    conn.close()
    return patterns


# ─────────────────────────────────────────────────────────────
# PERFORMANCE ANALYTICS
# ─────────────────────────────────────────────────────────────

def calculate_weekly_performance(week_start: str, week_end: str) -> dict:
    """Calculate full performance metrics for a week."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM trades
        WHERE exit_date >= ? AND exit_date <= ? AND status != 'OPEN'
    """, (week_start, week_end))

    closed_trades = [dict(row) for row in cursor.fetchall()]
    conn.close()

    if not closed_trades:
        return {"message": "No closed trades this week"}

    total_pnl = sum(t['pnl_dollars'] for t in closed_trades if t['pnl_dollars'])
    winners = [t for t in closed_trades if (t['pnl_dollars'] or 0) > 0]
    losers = [t for t in closed_trades if (t['pnl_dollars'] or 0) <= 0]

    win_rate = (len(winners) / len(closed_trades)) * 100 if closed_trades else 0
    avg_win = sum(t['pnl_dollars'] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t['pnl_dollars'] for t in losers) / len(losers) if losers else 0
    largest_win = max((t['pnl_dollars'] for t in winners), default=0)
    largest_loss = min((t['pnl_dollars'] for t in losers), default=0)

    gross_profit = sum(t['pnl_dollars'] for t in winners)
    gross_loss = abs(sum(t['pnl_dollars'] for t in losers))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

    return {
        "week_start": week_start,
        "week_end": week_end,
        "total_trades": len(closed_trades),
        "winning_trades": len(winners),
        "losing_trades": len(losers),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "largest_win": round(largest_win, 2),
        "largest_loss": round(largest_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "trades": closed_trades,
    }


# ─────────────────────────────────────────────────────────────
# WATCHLIST MANAGEMENT
# ─────────────────────────────────────────────────────────────

def get_active_watchlist() -> list:
    """Return all currently active watchlist symbols."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM watchlist WHERE is_active = 1")
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items


def add_to_watchlist(symbol: str, source: str, sector: str, reason: str, headline: str = None):
    """Add a stock to the dynamic watchlist."""
    conn = get_connection()
    cursor = conn.cursor()

    # Check if already active
    cursor.execute(
        "SELECT id FROM watchlist WHERE symbol = ? AND is_active = 1",
        (symbol,)
    )
    if cursor.fetchone():
        conn.close()
        return

    cursor.execute("""
        INSERT INTO watchlist (symbol, added_date, source, sector, reason, news_headline)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (symbol, datetime.now().isoformat(), source, sector, reason, headline))
    conn.commit()
    conn.close()
    logger.info(f"📋 Added to watchlist: {symbol} | Source: {source} | Reason: {reason[:50]}")


def remove_from_watchlist(symbol: str, reason: str):
    """Remove a stock from active watchlist."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE watchlist
        SET is_active = 0, removed_date = ?, removed_reason = ?
        WHERE symbol = ? AND is_active = 1
    """, (datetime.now().isoformat(), reason, symbol))
    conn.commit()
    conn.close()
