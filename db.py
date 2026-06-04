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

    # ── RUNNERS ───────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS runners (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id                INTEGER,
        symbol                  TEXT NOT NULL,
        option_symbol           TEXT,
        option_type             TEXT,
        entry_date              TEXT NOT NULL,
        original_contracts      INTEGER NOT NULL,
        contracts_sold          INTEGER NOT NULL,
        contracts_remaining     INTEGER NOT NULL,
        entry_option_price      REAL,
        current_option_price    REAL,
        realized_pnl            REAL,
        unrealized_pnl          REAL,
        runner_stop_price       REAL,
        runner_target_price     REAL,
        expiration_date         TEXT,
        days_to_expiration      INTEGER,
        status                  TEXT DEFAULT 'active_runner', -- 'active_runner' | 'closed_runner'
        reason_still_holding    TEXT,
        notes                   TEXT,
        FOREIGN KEY(trade_id) REFERENCES trades(id)
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

    # ── RESEARCH CACHE ─────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS research_cache (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT NOT NULL,
        query           TEXT,
        source          TEXT,
        data            TEXT,
        created_at      TEXT NOT NULL,
        expires_at      TEXT NOT NULL
    )""")

    # ── SUPPLY / DEMAND ZONES (Phase 4A — TradingView webhook) ──
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS supply_demand_zones (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT NOT NULL,
        zone_type       TEXT NOT NULL,        -- SUPPLY | DEMAND
        direction_bias  TEXT,                 -- BULLISH | BEARISH | N/A (from TradingView alert)
        price_low       REAL NOT NULL,
        price_high      REAL NOT NULL,
        timeframe       TEXT,
        source          TEXT,                 -- e.g. TRADINGVIEW
        note            TEXT,
        raw             TEXT,                 -- original payload JSON
        created_at      TEXT NOT NULL,
        expires_at      TEXT NOT NULL
    )""")

    # Migration: add direction_bias to supply_demand_zones tables created before
    # the column existed (CREATE TABLE IF NOT EXISTS won't alter an old table).
    cursor.execute("PRAGMA table_info(supply_demand_zones)")
    if "direction_bias" not in {row[1] for row in cursor.fetchall()}:
        cursor.execute("ALTER TABLE supply_demand_zones ADD COLUMN direction_bias TEXT")

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


def update_trade_fields(trade_id: int, fields: dict):
    """Update arbitrary columns on an OPEN trade (e.g. trailing the stop).

    Unlike update_trade_exit this does not compute P/L or set an exit date — it
    is for in-flight adjustments to a still-open position.
    """
    if not fields:
        return
    conn = get_connection()
    cursor = conn.cursor()
    set_clause = ', '.join([f"{k} = ?" for k in fields.keys()])
    cursor.execute(
        f"UPDATE trades SET {set_clause} WHERE id = ?",
        list(fields.values()) + [trade_id],
    )
    conn.commit()
    conn.close()


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
        contracts = int(exit_data.get('contracts', trade['contracts']) or trade['contracts'] or 1)
        pnl = (exit_price - entry_price) * 100 * contracts
        pnl_pct = 0.0
        if entry_price:
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


def get_active_runners() -> list:
    """Return all active runner records."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM runners WHERE status = 'active_runner'")
    runners = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return runners


def show_active_runners() -> list:
    """Return a summary of active runners for review."""
    active = get_active_runners()
    summary = []
    for runner in active:
        entry = runner.get('entry_option_price') or 0.0
        current = runner.get('current_option_price') or 0.0
        percent = 0.0
        if entry:
            percent = ((current - entry) / entry) * 100
        summary.append({
            'id': runner.get('id'),
            'symbol': runner.get('symbol'),
            'contracts_remaining': runner.get('contracts_remaining'),
            'entry_premium': entry,
            'current_premium': current,
            'percent_gain_loss': round(percent, 1),
            'stop_level': runner.get('runner_stop_price'),
            'target': runner.get('runner_target_price'),
            'expiration_date': runner.get('expiration_date'),
            'days_to_expiration': runner.get('days_to_expiration'),
            'reason_still_holding': runner.get('reason_still_holding'),
            'status': runner.get('status'),
        })
    return summary


def log_runner_entry(runner_data: dict) -> int:
    """Create a new runner record after partial profit is taken."""
    conn = get_connection()
    cursor = conn.cursor()
    runner_data['entry_date'] = datetime.now().isoformat()
    runner_data['status'] = 'active_runner'

    columns = ', '.join(runner_data.keys())
    placeholders = ', '.join(['?' for _ in runner_data])
    cursor.execute(
        f"INSERT INTO runners ({columns}) VALUES ({placeholders})",
        list(runner_data.values())
    )
    runner_id = cursor.lastrowid
    conn.commit()
    conn.close()
    logger.info(f"🏃 Runner logged: {runner_data.get('symbol')} | Runner ID: {runner_id}")
    return runner_id


def update_runner(runner_id: int, update_data: dict):
    """Update an active runner record."""
    conn = get_connection()
    cursor = conn.cursor()
    set_clause = ', '.join([f"{k} = ?" for k in update_data.keys()])
    cursor.execute(
        f"UPDATE runners SET {set_clause} WHERE id = ?",
        list(update_data.values()) + [runner_id]
    )
    conn.commit()
    conn.close()
    logger.info(f"🔄 Runner {runner_id} updated")


def close_runner(runner_id: int, close_data: dict = None):
    """Close a runner and mark it as closed_runner."""
    if close_data is None:
        close_data = {}
    close_data['status'] = 'closed_runner'
    update_runner(runner_id, close_data)


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


def get_today_trade_count() -> int:
    """Return the number of trades opened today."""
    conn = get_connection()
    cursor = conn.cursor()
    today = datetime.now().date().isoformat()
    cursor.execute(
        "SELECT COUNT(*) as total FROM trades WHERE DATE(entry_date) = ?",
        (today,)
    )
    result = cursor.fetchone()
    conn.close()
    return int(result[0] if result else 0)


def get_today_realized_pnl() -> float:
    """Return today's realized P/L from closed trades."""
    conn = get_connection()
    cursor = conn.cursor()
    today = datetime.now().date().isoformat()
    cursor.execute(
        "SELECT SUM(pnl_dollars) as total FROM trades WHERE DATE(exit_date) = ? AND status != 'OPEN'",
        (today,)
    )
    result = cursor.fetchone()
    conn.close()
    return float(result[0] or 0.0)


def get_total_realized_pnl() -> float:
    """Return all-time realized P/L from every closed trade."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT SUM(pnl_dollars) as total FROM trades WHERE status != 'OPEN'"
    )
    result = cursor.fetchone()
    conn.close()
    return float(result[0] or 0.0)


def get_trades_entered_today() -> list:
    """Return all trades opened today (any status)."""
    conn = get_connection()
    cursor = conn.cursor()
    today = datetime.now().date().isoformat()
    cursor.execute("SELECT * FROM trades WHERE DATE(entry_date) = ?", (today,))
    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades


def get_trades_exited_today() -> list:
    """Return all trades closed today (status != OPEN)."""
    conn = get_connection()
    cursor = conn.cursor()
    today = datetime.now().date().isoformat()
    cursor.execute(
        "SELECT * FROM trades WHERE DATE(exit_date) = ? AND status != 'OPEN'",
        (today,),
    )
    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades


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


def get_research_cache(symbol: str) -> dict:
    """Return cached research for a symbol if still valid."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM research_cache WHERE symbol = ? ORDER BY expires_at DESC LIMIT 1",
        (symbol,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {}

    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.now() >= expires_at:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM research_cache WHERE id = ?", (row["id"],))
        conn.commit()
        conn.close()
        return {}

    return {
        "symbol": row["symbol"],
        "query": row["query"],
        "source": row["source"],
        "data": json.loads(row["data"]),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


def save_research_cache(symbol: str, query: str, source: str, data: dict, ttl_hours: int = 6):
    """Save research results to the local cache database."""
    expires_at = (datetime.now() + timedelta(hours=ttl_hours)).isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO research_cache (symbol, query, source, data, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            symbol,
            query,
            source,
            json.dumps(data),
            datetime.now().isoformat(),
            expires_at,
        )
    )
    conn.commit()
    conn.close()
    logger.info(f"🧠 Research cache saved for {symbol} (expires {expires_at})")


def delete_expired_research_cache():
    """Delete expired research cache entries."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM research_cache WHERE expires_at <= ?", (datetime.now().isoformat(),))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────
# SUPPLY / DEMAND ZONES (Phase 4A — TradingView webhook receiver)
# ─────────────────────────────────────────────────────────────

def save_supply_demand_zone(zone: dict, ttl_hours: int = 72) -> int:
    """Persist a supply/demand zone from a TradingView alert. Returns row ID.

    Expected keys: symbol, zone_type (SUPPLY|DEMAND), direction_bias, price_low,
    price_high, timeframe, source, note, raw.
    """
    created_at = datetime.now().isoformat()
    expires_at = (datetime.now() + timedelta(hours=ttl_hours)).isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO supply_demand_zones
           (symbol, zone_type, direction_bias, price_low, price_high, timeframe, source, note, raw, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            zone.get("symbol"),
            zone.get("zone_type"),
            zone.get("direction_bias"),
            zone.get("price_low"),
            zone.get("price_high"),
            zone.get("timeframe"),
            zone.get("source", "TRADINGVIEW"),
            zone.get("note"),
            zone.get("raw"),
            created_at,
            expires_at,
        ),
    )
    zone_id = cursor.lastrowid
    conn.commit()
    conn.close()
    logger.info(
        f"📥 Supply/demand zone saved: {zone.get('symbol')} {zone.get('zone_type')} "
        f"{zone.get('price_low')}–{zone.get('price_high')} (expires {expires_at})"
    )
    return zone_id


def get_active_zones(symbol: str = None) -> list:
    """Return non-expired supply/demand zones, optionally filtered by symbol.

    Most recently received first. Exposed for a later phase to consume; the
    Phase 4A receiver only writes zones.
    """
    now = datetime.now().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    if symbol:
        cursor.execute(
            "SELECT * FROM supply_demand_zones WHERE expires_at > ? AND symbol = ? ORDER BY created_at DESC",
            (now, symbol.upper()),
        )
    else:
        cursor.execute(
            "SELECT * FROM supply_demand_zones WHERE expires_at > ? ORDER BY created_at DESC",
            (now,),
        )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def delete_expired_zones() -> int:
    """Delete expired supply/demand zones. Returns the number removed."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM supply_demand_zones WHERE expires_at <= ?", (datetime.now().isoformat(),)
    )
    removed = cursor.rowcount
    conn.commit()
    conn.close()
    return removed


def add_to_watchlist(symbol: str, source: str, sector: str, reason: str, headline: str = None):
    """Add a stock to the dynamic watchlist."""
    conn = get_connection()
    cursor = conn.cursor()

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


def log_signal(signal_data: dict):
    """Log scan signal details for later review and analytics."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO signals_log (date, symbol, signal, bull_score, bear_score, rsi, adx, bias, acted_on, reason_skipped) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            signal_data.get('date', datetime.now().isoformat()),
            signal_data.get('symbol'),
            signal_data.get('signal'),
            signal_data.get('bull_score'),
            signal_data.get('bear_score'),
            signal_data.get('rsi'),
            signal_data.get('adx'),
            signal_data.get('bias'),
            int(signal_data.get('acted_on', 0)),
            signal_data.get('reason_skipped'),
        )
    )
    conn.commit()
    conn.close()
    logger.info(f"🧭 Logged signal: {signal_data.get('symbol')} | {signal_data.get('signal')}")


def log_macro(data: dict):
    """Log macro market conditions used by the bot."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO macro_log (date, oil_price, vix, ten_yr_yield, sp500, macro_signal, iran_status, hormuz_status, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data.get('date', datetime.now().isoformat()),
            data.get('oil_price'),
            data.get('vix'),
            data.get('ten_yr_yield'),
            data.get('sp500'),
            data.get('macro_signal'),
            data.get('iran_status'),
            data.get('hormuz_status'),
            data.get('summary'),
        )
    )
    conn.commit()
    conn.close()
    logger.info(f"📈 Logged macro conditions: {data.get('macro_signal', 'N/A')}")


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
