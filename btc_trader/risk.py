"""Daily risk manager.

Tracks realised P&L and enforces hard stops so a single bad session
cannot wipe meaningful capital.

Rules
─────
  • Max loss per day  : DAILY_LOSS_CAP_CENTS   (default 500¢ = $5)
  • Max trades per day: MAX_TRADES_PER_DAY      (default 20)
  • Re-entry limit    : MAX_REENTRIES_PER_WINDOW (default 1)
  • After a stop-loss : no re-entry for the rest of that window
"""
from __future__ import annotations

import csv
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path


# ── window direction lock ────────────────────────────────────────────────────
# Prevents opposing trades within the same 15-min window.
_window_locks: dict[str, str] = {}

# Cooldown tracking: list of (window_id, timestamp) for recent stop-losses
_stop_loss_history: list[tuple[str, datetime]] = []

_COOLDOWN_WINDOWS = 2  # sit out for 2 windows (30 min) after a stop-loss


def lock_window_direction(window_id: str, direction: str) -> bool:
    """Lock a window to a direction. Returns False if already locked to opposite."""
    existing = _window_locks.get(window_id)
    if existing is None:
        _window_locks[window_id] = direction
        return True
    return existing == direction


def cooldown_active(current_window_id: str | None = None) -> bool:
    """Return True if a stop-loss happened within the last 2 windows (30 min).

    Uses timestamp-based check: any stop-loss in the last 1800 seconds triggers cooldown.
    """
    if not _stop_loss_history:
        return False
    now = datetime.now(timezone.utc)
    cooldown_seconds = _COOLDOWN_WINDOWS * 15 * 60  # 30 minutes
    for _wid, ts in reversed(_stop_loss_history):
        if (now - ts).total_seconds() < cooldown_seconds:
            return True
    return False


def record_stop_loss(window_id: str) -> None:
    """Record a stop-loss event for cooldown tracking."""
    _stop_loss_history.append((window_id, datetime.now(timezone.utc)))
    # Keep only last 10 entries to avoid unbounded growth
    if len(_stop_loss_history) > 10:
        _stop_loss_history.pop(0)


# ── tuneable limits ───────────────────────────────────────────────────────────
DAILY_LOSS_CAP_CENTS    = 500   # $5.00
MAX_TRADES_PER_DAY      = 20
MAX_REENTRIES_PER_WINDOW = 0   # no re-entries — one trade per 15-min window

# ── trade journal CSV ─────────────────────────────────────────────────────────
_CSV_PATH = Path(__file__).resolve().parent.parent / "btc_trades.csv"
_CSV_COLUMNS = [
    "timestamp_utc", "ticker", "direction", "entry_cents", "exit_cents",
    "pnl_cents", "contracts", "exit_reason", "momentum_delta", "volatility_z",
    "trend_1h", "vwap_delta", "btc_price", "bid_at_entry", "ask_at_entry",
    "spread", "seconds_remaining", "hour_utc",
]


@dataclass
class TradeRecord:
    ticker:        str
    entry_cents:   int
    exit_cents:    int
    pnl_cents:     int          # positive = profit
    exit_reason:   str
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class RiskManager:
    """Thread-safe daily risk guard.

    One shared instance lives for the lifetime of the process.
    Resets automatically when the UTC date rolls over.
    """

    def __init__(self) -> None:
        self._lock     = threading.Lock()
        self._date     = date.today()
        self._trades:  list[TradeRecord] = []
        self._loss_cents = 0
        # per-window state
        self._current_window_ticker:  str  = ""
        self._current_window_losses:  int  = 0
        self._current_window_reentries: int = 0

    # ── daily reset ───────────────────────────────────────────────────────────

    def _check_rollover(self) -> None:
        today = date.today()
        if today != self._date:
            self._date        = today
            self._trades      = []
            self._loss_cents  = 0
            self._reset_window("")

    def _reset_window(self, ticker: str) -> None:
        self._current_window_ticker   = ticker
        self._current_window_losses   = 0
        self._current_window_reentries = 0

    # ── queries ───────────────────────────────────────────────────────────────

    def can_enter(self, ticker: str) -> tuple[bool, str]:
        """Return (allowed, reason). Call before every entry attempt."""
        with self._lock:
            self._check_rollover()

            if self._loss_cents >= DAILY_LOSS_CAP_CENTS:
                return False, f"daily loss cap hit ({self._loss_cents}¢ ≥ {DAILY_LOSS_CAP_CENTS}¢)"

            if len(self._trades) >= MAX_TRADES_PER_DAY:
                return False, f"max trades/day reached ({MAX_TRADES_PER_DAY})"

            # window-specific checks
            if ticker == self._current_window_ticker:
                if self._current_window_losses > 0:
                    return False, "stop-loss hit this window — sitting out remainder"
                if self._current_window_reentries >= MAX_REENTRIES_PER_WINDOW:
                    return False, f"re-entry limit reached ({MAX_REENTRIES_PER_WINDOW}/window)"

            return True, ""

    def is_daily_cap_hit(self) -> bool:
        with self._lock:
            self._check_rollover()
            return self._loss_cents >= DAILY_LOSS_CAP_CENTS

    # ── recording ─────────────────────────────────────────────────────────────

    def record_entry(self, ticker: str) -> None:
        """Call when a position is opened."""
        with self._lock:
            self._check_rollover()
            if ticker != self._current_window_ticker:
                self._reset_window(ticker)
            else:
                self._current_window_reentries += 1

    def record_exit(
        self,
        ticker:      str,
        entry_cents: int,
        exit_cents:  int,
        exit_reason: str,
        count:       int = 1,
        signal_ctx:  dict | None = None,
    ) -> TradeRecord:
        """Call immediately after a position is closed. Returns the logged record.

        Parameters
        ----------
        signal_ctx : optional dict with keys matching Signal enrichment fields
                     (momentum_delta, volatility_z, trend_1h, vwap_delta,
                      btc_price, bid_at_entry, spread, seconds_left, direction).
                     Logged to btc_trades.csv for post-trade analysis.
        """
        pnl = (exit_cents - entry_cents) * count
        now = datetime.now(timezone.utc)
        rec = TradeRecord(
            ticker=ticker,
            entry_cents=entry_cents,
            exit_cents=exit_cents,
            pnl_cents=pnl,
            exit_reason=exit_reason,
            timestamp_utc=now,
        )
        with self._lock:
            self._trades.append(rec)
            if pnl < 0:
                self._loss_cents += abs(pnl)
                if ticker == self._current_window_ticker:
                    self._current_window_losses += 1

        # ── append to CSV journal ─────────────────────────────────────────────
        ctx = signal_ctx or {}
        direction_str = {1: "BULL", -1: "BEAR"}.get(ctx.get("direction", 0), "NONE")
        row = {
            "timestamp_utc":     now.isoformat(),
            "ticker":            ticker,
            "direction":         direction_str,
            "entry_cents":       entry_cents,
            "exit_cents":        exit_cents,
            "pnl_cents":         pnl,
            "contracts":         count,
            "exit_reason":       exit_reason,
            "momentum_delta":    round(ctx.get("momentum_delta", 0.0), 2),
            "volatility_z":      round(ctx.get("volatility_z", 0.0), 2),
            "trend_1h":          ctx.get("trend_1h", 0),
            "vwap_delta":        round(ctx.get("vwap_delta", 0.0), 2),
            "btc_price":         round(ctx.get("btc_price", 0.0), 2),
            "bid_at_entry":      ctx.get("bid_at_entry", 0),
            "ask_at_entry":      ctx.get("entry_price", entry_cents),
            "spread":            ctx.get("spread", 0),
            "seconds_remaining": round(ctx.get("seconds_left", 0.0), 0),
            "hour_utc":          now.hour,
        }
        try:
            write_header = not _CSV_PATH.exists() or os.path.getsize(_CSV_PATH) == 0
            with open(_CSV_PATH, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
                if write_header:
                    writer.writeheader()
                writer.writerow(row)
        except Exception:
            pass  # never let journaling break the trading loop

        return rec

    # ── summary ───────────────────────────────────────────────────────────────

    def summary(self) -> str:
        with self._lock:
            wins  = sum(1 for t in self._trades if t.pnl_cents > 0)
            loss  = sum(1 for t in self._trades if t.pnl_cents < 0)
            total = len(self._trades)
            pnl   = sum(t.pnl_cents for t in self._trades)
            return (
                f"trades={total}  W={wins} L={loss}  "
                f"pnl={pnl:+d}¢  loss_used={self._loss_cents}¢/{DAILY_LOSS_CAP_CENTS}¢"
            )
