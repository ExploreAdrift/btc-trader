"""Binance public REST feed — live BTC/USDT price and recent klines.

No API key required. All endpoints are public.
Thread-safe price cache with configurable TTL.
"""
from __future__ import annotations

import time
import threading
from collections import deque
from typing import NamedTuple

import requests

# ── constants ─────────────────────────────────────────────────────────────────
_BASE          = "https://api.binance.us/api/v3"
_SYMBOL        = "BTCUSDT"
_TIMEOUT       = 5          # seconds per request
_CACHE_TTL     = 2.0        # seconds before re-fetching price
_PRICE_HISTORY = 120        # ticks to retain (~2 min at 1s resolution)

_session = requests.Session()
_session.headers.update({"Accept": "application/json"})


class Tick(NamedTuple):
    price: float
    ts: float   # epoch seconds (local clock)


# ── thread-safe price ring buffer ─────────────────────────────────────────────
class PriceFeed:
    """Rolling BTC price history with sub-second cache.

    Usage
    -----
    feed = PriceFeed()
    price = feed.latest()
    delta_60s = feed.delta(60)   # price change over last 60 seconds
    """

    def __init__(self, maxlen: int = _PRICE_HISTORY) -> None:
        self._buf:  deque[Tick] = deque(maxlen=maxlen)
        self._lock: threading.Lock = threading.Lock()
        self._last_fetch: float = 0.0

    # ── public API ────────────────────────────────────────────────────────────

    def latest(self) -> float:
        """Return the most recent BTC/USDT price, fetching if cache is stale."""
        self._maybe_refresh()
        with self._lock:
            if not self._buf:
                raise RuntimeError("Price feed empty — no data received yet.")
            return self._buf[-1].price

    def delta(self, lookback_seconds: float) -> float:
        """Return price change (current - lookback_seconds ago).

        Positive  → price moved up.
        Negative  → price moved down.
        Returns 0.0 if insufficient history.
        """
        self._maybe_refresh()
        cutoff = time.monotonic() - lookback_seconds
        with self._lock:
            if len(self._buf) < 2:
                return 0.0
            current = self._buf[-1].price
            # Walk back to find the oldest tick within the lookback window
            baseline = next(
                (t.price for t in reversed(self._buf) if t.ts <= cutoff),
                self._buf[0].price,
            )
            return current - baseline

    def record(self, price: float) -> None:
        """Manually push a price tick (used by background poller)."""
        with self._lock:
            self._buf.append(Tick(price=price, ts=time.monotonic()))

    # ── internals ─────────────────────────────────────────────────────────────

    def _maybe_refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_fetch < _CACHE_TTL:
            return
        try:
            price = _fetch_price()
            self.record(price)
            self._last_fetch = now
        except Exception:
            pass  # stale cache is acceptable; caller handles empty feed


# ── Binance REST helpers ───────────────────────────────────────────────────────

def _fetch_price() -> float:
    """Fetch the current BTC/USDT best bid price (fastest endpoint)."""
    r = _session.get(f"{_BASE}/ticker/bookTicker", params={"symbol": _SYMBOL}, timeout=_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["bidPrice"])


def fetch_klines(interval: str = "1m", limit: int = 3) -> list[dict]:
    """Return recent OHLCV klines.

    interval — Binance interval string: "1m", "3m", "5m", "15m"
    limit    — number of candles (max 1000)
    """
    r = _session.get(
        f"{_BASE}/klines",
        params={"symbol": _SYMBOL, "interval": interval, "limit": limit},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    raw = r.json()
    return [
        {
            "open_time": c[0],
            "open":      float(c[1]),
            "high":      float(c[2]),
            "low":       float(c[3]),
            "close":     float(c[4]),
            "volume":    float(c[5]),
            "close_time": c[6],
        }
        for c in raw
    ]


def previous_window_direction() -> int:
    """Return +1 if the last completed 15-min candle closed higher, -1 if lower, 0 if flat."""
    candles = fetch_klines(interval="15m", limit=2)
    if len(candles) < 2:
        return 0
    prev = candles[0]
    return 1 if prev["close"] > prev["open"] else (-1 if prev["close"] < prev["open"] else 0)


def rolling_volatility(lookback_minutes: int = 60) -> float:
    """Standard deviation of 1-minute BTC returns over the last *lookback_minutes*.

    Returns the stdev in dollar terms (e.g. 12.3 means typical 1-min moves are ~$12.3).
    Returns 0.0 if insufficient data.
    """
    candles = fetch_klines(interval="1m", limit=min(lookback_minutes + 1, 1000))
    if len(candles) < 3:
        return 0.0
    # Use close-to-close returns (in dollars)
    closes = [c["close"] for c in candles[:-1]]  # exclude current (incomplete) candle
    returns = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return variance ** 0.5


def trend_direction(interval: str = "1h") -> int:
    """Return +1 if the last completed candle at *interval* closed higher, -1 if lower, 0 if flat.

    Uses the same logic as previous_window_direction but for any interval.
    """
    candles = fetch_klines(interval=interval, limit=2)
    if len(candles) < 2:
        return 0
    prev = candles[0]  # last *completed* candle (index -1 is still open)
    return 1 if prev["close"] > prev["open"] else (-1 if prev["close"] < prev["open"] else 0)


def vwap_momentum(lookback_minutes: int = 15) -> float:
    """Return (current_price − VWAP) over recent 1-minute candles.

    Positive → price is above VWAP (bullish volume-weighted momentum).
    Negative → price is below VWAP (bearish volume-weighted momentum).
    Returns 0.0 if insufficient data.
    """
    candles = fetch_klines(interval="1m", limit=min(lookback_minutes + 1, 1000))
    if len(candles) < 2:
        return 0.0
    # VWAP = Σ(typical_price × volume) / Σ(volume) over completed candles
    completed = candles[:-1]  # exclude current incomplete candle
    total_vp = 0.0
    total_vol = 0.0
    for c in completed:
        typical = (c["high"] + c["low"] + c["close"]) / 3.0
        total_vp += typical * c["volume"]
        total_vol += c["volume"]
    if total_vol == 0:
        return 0.0
    vwap = total_vp / total_vol
    # Current price is the latest candle's close (still in-progress, best estimate)
    current = candles[-1]["close"]
    return current - vwap


def rsi(lookback_minutes: int = 14) -> float:
    """Relative Strength Index over the last *lookback_minutes* of 1-min candles.

    RSI > 70 → overbought (momentum exhaustion, likely to reverse down)
    RSI < 30 → oversold (momentum exhaustion, likely to reverse up)
    RSI 40-60 → neutral, momentum can continue

    Returns 50.0 (neutral) if insufficient data.
    """
    candles = fetch_klines(interval="1m", limit=min(lookback_minutes + 2, 1000))
    if len(candles) < 4:
        return 50.0
    closes = [c["close"] for c in candles[:-1]]  # exclude incomplete candle
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))
    if not gains:
        return 50.0
    avg_gain = sum(gains) / len(gains)
    avg_loss = sum(losses) / len(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def volume_ratio(lookback_minutes: int = 5, baseline_minutes: int = 30) -> float:
    """Ratio of recent volume to average volume.

    > 1.5 → heavy volume (move is real, likely to follow through)
    < 0.5 → thin volume (move is noise, likely to fade)

    Returns 1.0 if insufficient data.
    """
    candles = fetch_klines(interval="1m", limit=min(baseline_minutes + 2, 1000))
    if len(candles) < lookback_minutes + 2:
        return 1.0
    completed = candles[:-1]  # exclude incomplete candle
    recent = completed[-lookback_minutes:]
    baseline = completed[:-lookback_minutes] if len(completed) > lookback_minutes else completed

    recent_vol = sum(c["volume"] for c in recent) / len(recent)
    baseline_vol = sum(c["volume"] for c in baseline) / len(baseline)
    if baseline_vol == 0:
        return 1.0
    return recent_vol / baseline_vol
