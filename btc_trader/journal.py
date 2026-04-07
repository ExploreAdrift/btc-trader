"""BTC trade journal — records trades, signals, and P&L."""

from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from btc_trader.db import get_connection, init_db, DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_trade(
    *,
    window_id: str,
    direction: str,
    entry_price_cents: int,
    contracts: int,
    status: str = "open",
    strike_price: float | None = None,
    kalshi_order_id: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Insert a trade and return its id."""
    conn = get_connection(db_path or DB_PATH)
    cursor = conn.execute(
        """INSERT INTO trades
           (window_id, direction, strike_price, entry_price_cents, contracts,
            status, entry_time, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (window_id, direction, strike_price, entry_price_cents, contracts,
         status, _now_iso(), _now_iso()),
    )
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def record_signal(
    *,
    trade_id: int,
    momentum_delta: float | None = None,
    volatility_sigma: float | None = None,
    z_score: float | None = None,
    rsi_14: float | None = None,
    vwap_side: str | None = None,
    volume_ratio: float | None = None,
    hour_trend: str | None = None,
    prev_window_dir: str | None = None,
    spread_cents: int | None = None,
    time_remaining_sec: int | None = None,
    btc_price: float | None = None,
    db_path: Path | None = None,
) -> None:
    conn = get_connection(db_path or DB_PATH)
    conn.execute(
        """INSERT INTO signals
           (trade_id, momentum_delta, volatility_sigma, z_score, rsi_14,
            vwap_side, volume_ratio, hour_trend, prev_window_dir,
            spread_cents, time_remaining_sec, btc_price)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (trade_id, momentum_delta, volatility_sigma, z_score, rsi_14,
         vwap_side, volume_ratio, hour_trend, prev_window_dir,
         spread_cents, time_remaining_sec, btc_price),
    )
    conn.commit()
    conn.close()


def close_trade(
    *,
    trade_id: int,
    exit_price_cents: int,
    pnl_cents: float,
    exit_reason: str,
    hold_duration_sec: int,
    db_path: Path | None = None,
) -> None:
    conn = get_connection(db_path or DB_PATH)
    conn.execute(
        """UPDATE trades SET
             exit_price_cents = ?, pnl_cents = ?, exit_reason = ?,
             exit_time = ?, hold_duration_sec = ?, status = 'closed'
           WHERE id = ?""",
        (exit_price_cents, pnl_cents, exit_reason, _now_iso(),
         hold_duration_sec, trade_id),
    )
    conn.commit()
    conn.close()


def has_trade_in_window(window_id: str, direction: str, db_path: Path | None = None) -> bool:
    """Check if a trade already exists for this window+direction. Prevents duplicates."""
    conn = get_connection(db_path or DB_PATH)
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM trades WHERE window_id = ? AND direction = ?",
        (window_id, direction),
    ).fetchone()
    conn.close()
    return row["cnt"] > 0


def get_daily_pnl(date_str: str | None = None, db_path: Path | None = None) -> dict:
    """Get daily P&L summary."""
    conn = get_connection(db_path or DB_PATH)
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT pnl_cents, exit_reason FROM trades WHERE status = 'closed' AND created_at LIKE ?",
        (f"{date_str}%",),
    ).fetchall()
    conn.close()

    total_pnl = sum(r["pnl_cents"] or 0 for r in rows)
    wins = sum(1 for r in rows if (r["pnl_cents"] or 0) > 0)
    losses = sum(1 for r in rows if (r["pnl_cents"] or 0) < 0)

    return {
        "total_pnl_cents": total_pnl,
        "trade_count": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(rows) if rows else 0.0,
    }
