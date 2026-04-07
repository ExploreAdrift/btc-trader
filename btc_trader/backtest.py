"""BTC trade analysis and backtesting."""

from __future__ import annotations
import logging
from pathlib import Path
from btc_trader.db import get_connection, DB_PATH

logger = logging.getLogger(__name__)


def analyze_by_entry_price(db_path: Path | None = None) -> list[dict]:
    """Win rate and avg P&L bucketed by entry price.

    Buckets: 1-10c (lottery), 11-20c (value), 21-30c (moderate), 31+c (expensive)
    """
    conn = get_connection(db_path or DB_PATH)
    rows = conn.execute(
        "SELECT entry_price_cents, pnl_cents FROM trades WHERE status = 'closed'"
    ).fetchall()
    conn.close()

    buckets = {
        "1-10c": {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": []},
        "11-20c": {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": []},
        "21-30c": {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": []},
        "31c+": {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": []},
    }

    for row in rows:
        entry = row["entry_price_cents"] or 0
        pnl = row["pnl_cents"] or 0

        if entry <= 10:
            bucket = "1-10c"
        elif entry <= 20:
            bucket = "11-20c"
        elif entry <= 30:
            bucket = "21-30c"
        else:
            bucket = "31c+"

        buckets[bucket]["total_pnl"] += pnl
        if pnl > 0:
            buckets[bucket]["wins"] += 1
        elif pnl < 0:
            buckets[bucket]["losses"] += 1

    result = []
    for name, data in buckets.items():
        total = data["wins"] + data["losses"]
        result.append({
            "bucket": name,
            "trades": total,
            "wins": data["wins"],
            "losses": data["losses"],
            "win_rate": data["wins"] / total if total > 0 else 0.0,
            "total_pnl_cents": data["total_pnl"],
            "avg_pnl_cents": data["total_pnl"] / total if total > 0 else 0.0,
        })
    return result


def analyze_by_hour(db_path: Path | None = None) -> list[dict]:
    """Win rate by hour of day (UTC)."""
    conn = get_connection(db_path or DB_PATH)
    rows = conn.execute(
        "SELECT entry_time, pnl_cents FROM trades WHERE status = 'closed'"
    ).fetchall()
    conn.close()

    hours: dict[int, dict] = {}
    for row in rows:
        entry_time = row["entry_time"] or ""
        pnl = row["pnl_cents"] or 0
        try:
            hour = int(entry_time[11:13])
        except (ValueError, IndexError):
            continue

        if hour not in hours:
            hours[hour] = {"wins": 0, "losses": 0, "total_pnl": 0.0}

        hours[hour]["total_pnl"] += pnl
        if pnl > 0:
            hours[hour]["wins"] += 1
        elif pnl < 0:
            hours[hour]["losses"] += 1

    result = []
    for hour in sorted(hours.keys()):
        data = hours[hour]
        total = data["wins"] + data["losses"]
        result.append({
            "hour_utc": hour,
            "trades": total,
            "wins": data["wins"],
            "losses": data["losses"],
            "win_rate": data["wins"] / total if total > 0 else 0.0,
            "total_pnl_cents": data["total_pnl"],
        })
    return result


def analyze_by_exit_reason(db_path: Path | None = None) -> list[dict]:
    """P&L breakdown by exit reason."""
    conn = get_connection(db_path or DB_PATH)
    rows = conn.execute(
        "SELECT exit_reason, pnl_cents FROM trades WHERE status = 'closed'"
    ).fetchall()
    conn.close()

    reasons: dict[str, dict] = {}
    for row in rows:
        reason = row["exit_reason"] or "unknown"
        pnl = row["pnl_cents"] or 0

        if reason not in reasons:
            reasons[reason] = {"count": 0, "total_pnl": 0.0, "wins": 0, "losses": 0}

        reasons[reason]["count"] += 1
        reasons[reason]["total_pnl"] += pnl
        if pnl > 0:
            reasons[reason]["wins"] += 1
        elif pnl < 0:
            reasons[reason]["losses"] += 1

    return [
        {
            "exit_reason": reason,
            "count": data["count"],
            "total_pnl_cents": data["total_pnl"],
            "avg_pnl_cents": data["total_pnl"] / data["count"] if data["count"] > 0 else 0.0,
            "wins": data["wins"],
            "losses": data["losses"],
        }
        for reason, data in sorted(reasons.items())
    ]


def analyze_signals(db_path: Path | None = None) -> list[dict]:
    """Correlation between signal values and outcomes.

    For each signal field, compute average value for winning vs losing trades.
    """
    conn = get_connection(db_path or DB_PATH)
    rows = conn.execute(
        """SELECT s.*, t.pnl_cents
           FROM signals s JOIN trades t ON s.trade_id = t.id
           WHERE t.status = 'closed'"""
    ).fetchall()
    conn.close()

    if not rows:
        return []

    signal_fields = [
        "momentum_delta", "volatility_sigma", "z_score", "rsi_14",
        "volume_ratio", "spread_cents", "time_remaining_sec", "btc_price"
    ]

    result = []
    for field in signal_fields:
        win_vals = [row[field] for row in rows if (row["pnl_cents"] or 0) > 0 and row[field] is not None]
        loss_vals = [row[field] for row in rows if (row["pnl_cents"] or 0) < 0 and row[field] is not None]

        result.append({
            "signal": field,
            "win_avg": sum(win_vals) / len(win_vals) if win_vals else None,
            "loss_avg": sum(loss_vals) / len(loss_vals) if loss_vals else None,
            "win_count": len(win_vals),
            "loss_count": len(loss_vals),
        })
    return result


def analyze_by_direction(db_path: Path | None = None) -> dict:
    """Win rate by BULL vs BEAR."""
    conn = get_connection(db_path or DB_PATH)
    rows = conn.execute(
        "SELECT direction, pnl_cents FROM trades WHERE status = 'closed'"
    ).fetchall()
    conn.close()

    dirs: dict[str, dict] = {}
    for row in rows:
        d = row["direction"]
        pnl = row["pnl_cents"] or 0
        if d not in dirs:
            dirs[d] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
        dirs[d]["total_pnl"] += pnl
        if pnl > 0:
            dirs[d]["wins"] += 1
        elif pnl < 0:
            dirs[d]["losses"] += 1

    result = {}
    for d, data in dirs.items():
        total = data["wins"] + data["losses"]
        result[d] = {
            "trades": total,
            "wins": data["wins"],
            "losses": data["losses"],
            "win_rate": data["wins"] / total if total > 0 else 0.0,
            "total_pnl_cents": data["total_pnl"],
        }
    return result


def print_full_report(db_path: Path | None = None) -> None:
    """Print a comprehensive analysis report."""
    path = db_path or DB_PATH

    print("\n" + "=" * 60)
    print("  BTC TRADER — PERFORMANCE ANALYSIS")
    print("=" * 60)

    # By entry price
    print("\n--- By Entry Price ---")
    print(f"{'Bucket':<10} {'Trades':>7} {'Wins':>6} {'Losses':>7} {'Win%':>7} {'P&L':>10}")
    for row in analyze_by_entry_price(path):
        if row["trades"] == 0:
            continue
        print(f"{row['bucket']:<10} {row['trades']:>7} {row['wins']:>6} {row['losses']:>7} "
              f"{row['win_rate']:>6.0%} {row['total_pnl_cents']:>+9.0f}c")

    # By direction
    print("\n--- By Direction ---")
    for d, data in analyze_by_direction(path).items():
        print(f"  {d}: {data['trades']} trades, {data['win_rate']:.0%} win rate, {data['total_pnl_cents']:+.0f}c")

    # By exit reason
    print("\n--- By Exit Reason ---")
    print(f"{'Reason':<25} {'Count':>6} {'Avg P&L':>10} {'Total':>10}")
    for row in analyze_by_exit_reason(path):
        print(f"{row['exit_reason']:<25} {row['count']:>6} {row['avg_pnl_cents']:>+9.0f}c {row['total_pnl_cents']:>+9.0f}c")

    # By hour
    print("\n--- By Hour (UTC) ---")
    print(f"{'Hour':>6} {'Trades':>7} {'Win%':>7} {'P&L':>10}")
    for row in analyze_by_hour(path):
        print(f"{row['hour_utc']:>4}:00 {row['trades']:>7} {row['win_rate']:>6.0%} {row['total_pnl_cents']:>+9.0f}c")

    # Signal analysis
    print("\n--- Signal Analysis (Wins vs Losses) ---")
    print(f"{'Signal':<20} {'Win Avg':>10} {'Loss Avg':>10} {'Diff':>10}")
    for row in analyze_signals(path):
        if row["win_avg"] is None or row["loss_avg"] is None:
            continue
        diff = row["win_avg"] - row["loss_avg"]
        print(f"{row['signal']:<20} {row['win_avg']:>10.2f} {row['loss_avg']:>10.2f} {diff:>+10.2f}")

    print("\n" + "=" * 60)
