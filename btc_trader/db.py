"""SQLite database for BTC trade journal."""

from __future__ import annotations
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "btc_trades.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id       TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    strike_price    REAL,
    entry_price_cents INTEGER,
    exit_price_cents  INTEGER,
    contracts       INTEGER NOT NULL,
    pnl_cents       REAL,
    exit_reason     TEXT,
    entry_time      TEXT    NOT NULL,
    exit_time       TEXT,
    hold_duration_sec INTEGER,
    kalshi_order_id TEXT,
    status          TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades(id),
    momentum_delta  REAL,
    volatility_sigma REAL,
    z_score         REAL,
    rsi_14          REAL,
    vwap_side       TEXT,
    volume_ratio    REAL,
    hour_trend      TEXT,
    prev_window_dir TEXT,
    spread_cents    INTEGER,
    time_remaining_sec INTEGER,
    btc_price       REAL
);

CREATE INDEX IF NOT EXISTS idx_trades_window ON trades(window_id);
CREATE INDEX IF NOT EXISTS idx_trades_direction ON trades(direction);
CREATE INDEX IF NOT EXISTS idx_signals_trade ON signals(trade_id);
"""

def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: Path | None = None) -> None:
    conn = get_connection(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
