"""Kalshi BTC-15m market interface.

Fetches the current open YES/NO contract for a given 15-minute window,
reads live bid/ask prices, and places market-sell exits.

Shares the KalshiClient credentials from the weather module but
contains zero weather-specific logic.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather_market.kalshi.client import KalshiClient

# ── Kalshi KXBTC15M series ────────────────────────────────────────────────────
_SERIES        = "KXBTC15M"
_TICKER_RE     = re.compile(r"KXBTC15M-(\d{2})([A-Z]{3})(\d{2})(\d{4})(?:-\d+)?")
_MAX_PRICE     = 99   # cents — treat as settled if at or above
_MIN_PRICE     = 1    # cents — treat as settled if at or below


class Contract(NamedTuple):
    """A live KXBTC15M contract."""
    ticker:       str
    yes_bid:      int   # cents
    yes_ask:      int   # cents
    no_bid:       int   # cents
    no_ask:       int   # cents
    window_start: datetime   # UTC
    window_end:   datetime   # UTC
    seconds_left: float


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_window(ticker: str) -> tuple[datetime, datetime] | None:
    """Parse window start/end from ticker e.g. KXBTC15M-26APR021600-00.

    The 4-digit time in the ticker is the window *close* in US Eastern time.
    We convert to UTC and derive the 15-min start from it.
    """
    m = _TICKER_RE.match(ticker.upper())
    if not m:
        return None
    yy, mon, dd, hhmm = m.groups()
    hh, mm = int(hhmm[:2]), int(hhmm[2:])
    eastern = ZoneInfo("America/New_York")
    try:
        end_et = datetime.strptime(
            f"20{yy} {mon} {dd} {hh:02d} {mm:02d}", "%Y %b %d %H %M"
        ).replace(tzinfo=eastern)
    except ValueError:
        return None
    end = end_et.astimezone(timezone.utc)
    start = end - timedelta(minutes=15)
    return start, end


def _cents(market: dict, key_dollars: str, key_cents: str, fallback: int) -> int:
    raw = market.get(key_dollars) or market.get(key_cents)
    if raw is None:
        return fallback
    return int(round(float(raw) * 100)) if "." in str(raw) else int(raw)


# ── public API ────────────────────────────────────────────────────────────────

def get_active_contract(client: KalshiClient) -> Contract | None:
    """Return the currently active BTC 15-min YES contract, or None."""
    now = datetime.now(timezone.utc)
    markets = client.get_markets(series_ticker=_SERIES, status=None, limit=200)

    for mkt in markets:
        if mkt.get("status") != "active":
            continue

        ticker = mkt.get("ticker", "")
        close_raw = mkt.get("close_time")
        open_raw = mkt.get("open_time")
        if not close_raw or not open_raw:
            continue

        start = datetime.fromisoformat(open_raw.replace("Z", "+00:00"))
        end = datetime.fromisoformat(close_raw.replace("Z", "+00:00"))
        remaining = (end - now).total_seconds()
        if remaining <= 0:
            continue

        yes_bid = _cents(mkt, "yes_bid_dollars", "yes_bid", 0)
        yes_ask = _cents(mkt, "yes_ask_dollars", "yes_ask", 99)
        no_bid  = _cents(mkt, "no_bid_dollars", "no_bid", 0)
        no_ask  = _cents(mkt, "no_ask_dollars", "no_ask", 99)

        return Contract(
            ticker=ticker,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            window_start=start,
            window_end=end,
            seconds_left=remaining,
        )

    return None


def refresh_bid(client: KalshiClient, ticker: str, bearish: bool = False) -> int:
    """Fetch the current bid price for a single ticker. Returns cents."""
    mkt = client.get_market(ticker)
    if bearish:
        return _cents(mkt, "no_bid_dollars", "no_bid", 0)
    return _cents(mkt, "yes_bid_dollars", "yes_bid", 0)


BET_DOLLARS = 1  # target bet size in dollars

MAX_CONTRACTS = 10  # hard cap to prevent runaway sizing

def place_buy(client: KalshiClient, ticker: str, ask_cents: int,
              bearish: bool = False) -> dict:
    """Place a taker buy sized to BET_DOLLARS at the current ask.

    Uses a limit order at ask + 1¢ to guarantee fill as a taker.
    Caps at 97¢ to avoid paying near-settled price.
    """
    if ask_cents < 5:
        raise ValueError(f"ask too low ({ask_cents}¢) — refusing to place order")
    limit = min(ask_cents + 1, 97)
    count = min(max(1, (BET_DOLLARS * 100) // limit), MAX_CONTRACTS)
    if bearish:
        return client.create_order(
            ticker=ticker,
            side="no",
            action="buy",
            count=count,
            order_type="limit",
            no_price=limit,
        )
    return client.create_order(
        ticker=ticker,
        side="yes",
        action="buy",
        count=count,
        order_type="limit",
        yes_price=limit,
    )


def place_market_sell(client: KalshiClient, ticker: str, bid_cents: int,
                      count: int = 1, bearish: bool = False) -> dict:
    """Exit immediately at the current bid — speed over price.

    Submits a limit sell at bid - 1¢ to cross the spread and guarantee
    an immediate fill rather than resting in the book.
    """
    limit = max(bid_cents - 1, 1)
    if bearish:
        return client.create_order(
            ticker=ticker,
            side="no",
            action="sell",
            count=count,
            order_type="limit",
            no_price=limit,
        )
    return client.create_order(
        ticker=ticker,
        side="yes",
        action="sell",
        count=count,
        order_type="limit",
        yes_price=limit,
    )


def cancel_resting_orders(client: KalshiClient, ticker: str) -> None:
    """Cancel any open orders on this ticker before re-submitting."""
    try:
        orders = client.get_orders(ticker=ticker, status="resting")
        for order in orders:
            oid = order.get("order_id") or order.get("id")
            if oid:
                client.cancel_order(oid)
    except Exception:
        pass   # best-effort; don't block exit logic
