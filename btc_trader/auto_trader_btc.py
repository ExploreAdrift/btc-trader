"""BTC 15-minute Kalshi auto-trader — main loop.

Architecture
────────────
  SCAN  loop  (30s)  — watches for entry signals when flat
  HOLD  loop  (10s)  — monitors open position, fires exits
  FORCE exit  (120s) — hard kill when <2 min remain in window

Zero weather code. Zero shared state outside the Kalshi client.

Usage
─────
  python auto_trader_btc.py          # live trading
  python auto_trader_btc.py --once   # single scan cycle, no orders
  python auto_trader_btc.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── project path ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from weather_market.kalshi.client import KalshiClient

from btc_trader.binance       import PriceFeed
from btc_trader.kalshi_btc    import (
    BET_DOLLARS,
    MAX_CONTRACTS,
    Contract,
    cancel_resting_orders,
    get_active_contract,
    place_buy,
    place_market_sell,
    refresh_bid,
)
from btc_trader.db            import init_db
from btc_trader.entry_exit    import should_enter, should_exit
from btc_trader.journal       import record_trade, record_signal, close_trade, has_trade_in_window
from btc_trader.risk          import RiskManager, DAILY_LOSS_CAP_CENTS, lock_window_direction, cooldown_active
from btc_trader.signals       import Direction, Signal, evaluate
from btc_trader.trailing_stop import ExitReason, TrailingStop

# ── logging ───────────────────────────────────────────────────────────────────
LOG_FILE = ROOT / "auto_trader_btc.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── poll intervals ────────────────────────────────────────────────────────────
SCAN_INTERVAL  = 5     # seconds between entry scans (no position)
HOLD_INTERVAL  = 3     # seconds between exit checks (position held)
FORCE_EXIT_SEC = 120   # force-exit when this many seconds remain in window

# ── global singletons ─────────────────────────────────────────────────────────
_feed = PriceFeed()
_risk = RiskManager()


def _direction_label(bearish: bool) -> str:
    """Return 'BEAR' or 'BULL' for journal/lock APIs."""
    return "BEAR" if bearish else "BULL"


def _signal_ctx(sig: Signal) -> dict:
    """Extract signal fields as a plain dict for CSV journaling."""
    return {
        "direction":      int(sig.direction),
        "momentum_delta": sig.momentum_delta,
        "volatility_z":   sig.volatility_z,
        "trend_1h":       sig.trend_1h,
        "vwap_delta":     sig.vwap_delta,
        "btc_price":      sig.btc_price,
        "bid_at_entry":   sig.bid_at_entry,
        "entry_price":    sig.entry_price,
        "spread":         sig.spread,
        "seconds_left":   sig.seconds_left,
    }


# ══════════════════════════════════════════════════════════════════════════════
# POSITION LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

def _parse_fill_cents(result: dict, fallback: int, bearish: bool = False) -> int:
    """Extract fill price in cents from Kalshi order response.

    The API returns prices in dollars (e.g. "0.5100") with '_dollars' suffix.
    We also check legacy field names without the suffix as a fallback.
    """
    if bearish:
        keys = ("no_price_dollars", "no_price", "price")
    else:
        keys = ("yes_price_dollars", "yes_price", "price")
    for key in keys:
        raw = result.get(key)
        if raw is not None:
            raw_str = str(raw)
            if "." in raw_str:
                return int(round(float(raw_str) * 100))
            return int(raw)
    return fallback


def _open_position(
    client:   KalshiClient,
    contract: Contract,
    dry_run:  bool,
    bearish:  bool = False,
) -> tuple[int, int] | None:
    """Buy contracts sized to BET_DOLLARS. Returns (fill_price, count) or None."""
    allowed, reason = _risk.can_enter(contract.ticker)
    if not allowed:
        log.info(f"RISK BLOCK — {reason}")
        return None

    side_label = "NO" if bearish else "YES"
    ask = contract.no_ask if bearish else contract.yes_ask
    limit = min(ask + 1, 97)
    count = min(max(1, (BET_DOLLARS * 100) // limit), MAX_CONTRACTS)

    log.info(
        f"BUY {side_label}  {contract.ticker}  ask={ask}¢  "
        f"count={count}  (${count * limit / 100:.2f})  "
        f"seconds_left={contract.seconds_left:.0f}s"
    )
    if dry_run:
        log.info("[DRY RUN] would BUY — no order placed")
        _risk.record_entry(contract.ticker)
        return ask, count

    try:
        result = place_buy(client, contract.ticker, ask, bearish=bearish)
        fill = _parse_fill_cents(result, ask, bearish=bearish)

        # Check actual fill count from API
        fill_count_raw = result.get("fill_count_fp") or result.get("fill_count")
        actual_count = int(float(fill_count_raw)) if fill_count_raw else 0
        order_status = result.get("status", "unknown")

        # Sanity check: fill shouldn't be wildly different from ask
        if fill == 0 or abs(fill - ask) > 10:
            log.warning(
                f"Fill price {fill}¢ looks wrong (ask was {ask}¢) — using ask as entry"
            )
            fill = ask

        # If nothing filled, don't enter the hold loop
        if actual_count == 0 and order_status == "resting":
            log.warning(f"Order resting (0 filled) — cancelling")
            oid = result.get("order_id") or result.get("id")
            if oid:
                try:
                    cancel_resting_orders(client, contract.ticker)
                except Exception:
                    pass
            return None

        # Use actual fill count if available, otherwise assume full fill
        if actual_count > 0:
            count = actual_count

        log.info(
            f"ORDER PLACED — id={result.get('order_id')}  fill={fill}¢  "
            f"count={count}  status={order_status}  "
            f"cost=${result.get('taker_fill_cost_dollars', '?')}"
        )
        _risk.record_entry(contract.ticker)
        return fill, count
    except Exception as exc:
        log.error(f"BUY failed — {exc}")
        return None


def _close_position(
    client:      KalshiClient,
    ticker:      str,
    entry_cents: int,
    count:       int,
    reason:      ExitReason,
    dry_run:     bool,
    bearish:     bool = False,
    signal_ctx:  dict | None = None,
) -> None:
    """Sell immediately at bid. Cancel any resting orders first."""
    cancel_resting_orders(client, ticker)

    bid = refresh_bid(client, ticker, bearish=bearish)
    pnl = (bid - entry_cents) * count
    side_label = "NO" if bearish else "YES"
    log.info(
        f"EXIT {side_label} [{reason.name}]  {ticker}  "
        f"bid={bid}¢  entry={entry_cents}¢  count={count}  pnl={pnl:+d}¢"
    )

    if dry_run:
        log.info("[DRY RUN] would SELL — no order placed")
        _risk.record_exit(ticker, entry_cents, bid, reason.name, count, signal_ctx=signal_ctx)
        return

    try:
        result = place_market_sell(client, ticker, bid, count, bearish=bearish)
        actual_exit = _parse_fill_cents(result, bid, bearish=bearish)
        log.info(f"SOLD — id={result.get('order_id')}  exit={actual_exit}¢  count={count}")
        _risk.record_exit(ticker, entry_cents, actual_exit, reason.name, count, signal_ctx=signal_ctx)
    except Exception as exc:
        log.error(f"SELL failed — {exc}  (position may still be open)")
        _risk.record_exit(ticker, entry_cents, bid, f"SELL_ERROR:{exc}", count, signal_ctx=signal_ctx)


# ══════════════════════════════════════════════════════════════════════════════
# HOLD LOOP — monitors an open position every HOLD_INTERVAL seconds
# ══════════════════════════════════════════════════════════════════════════════

def _hold_loop(
    client:      KalshiClient,
    ticker:      str,
    entry_cents: int,
    count:       int,
    window_end:  datetime,
    dry_run:     bool,
    bearish:     bool = False,
    signal_ctx:  dict | None = None,
    trade_id:    int | None = None,
    window_id:   str | None = None,
) -> None:
    stop = TrailingStop(entry_cents=entry_cents)
    side_label = "NO" if bearish else "YES"
    entry_time = datetime.now(timezone.utc)
    log.info(f"HOLDING {side_label} {ticker} — entry={entry_cents}¢  count={count}  {stop}")

    def _do_close(bid: int, exit_reason_str: str, exit_enum: ExitReason) -> None:
        """Close position + record in journal."""
        _close_position(client, ticker, entry_cents, count, exit_enum, dry_run, bearish, signal_ctx=signal_ctx)
        if trade_id is not None:
            now = datetime.now(timezone.utc)
            hold_sec = int((now - entry_time).total_seconds())
            pnl = (bid - entry_cents) * count
            try:
                close_trade(
                    trade_id=trade_id,
                    exit_price_cents=bid,
                    pnl_cents=pnl,
                    exit_reason=exit_reason_str,
                    hold_duration_sec=hold_sec,
                )
            except Exception as exc:
                log.warning("Journal close_trade failed: %s", exc)

    while True:
        now = datetime.now(timezone.utc)
        seconds_left = (window_end - now).total_seconds()
        hold_duration = int((now - entry_time).total_seconds())

        # ── force exit near window close ──────────────────────────────────────
        if seconds_left <= FORCE_EXIT_SEC:
            bid = refresh_bid(client, ticker, bearish=bearish) if not dry_run else entry_cents
            _do_close(bid, "force_exit_time", ExitReason.TIME_EXPIRY)
            return

        # ── daily cap check ───────────────────────────────────────────────────
        if _risk.is_daily_cap_hit():
            bid = refresh_bid(client, ticker, bearish=bearish) if not dry_run else entry_cents
            _do_close(bid, "daily_loss_cap", ExitReason.LOSS_CAP)
            return

        # ── refresh bid ───────────────────────────────────────────────────────
        try:
            bid = refresh_bid(client, ticker, bearish=bearish)
        except Exception as exc:
            log.warning(f"bid refresh failed — {exc} — retrying")
            time.sleep(HOLD_INTERVAL)
            continue

        # ── new rules-based exit (primary) ────────────────────────────────────
        exit_now, exit_reason = should_exit(
            entry_price=entry_cents,
            current_bid=bid,
            hold_duration_sec=hold_duration,
            time_remaining_sec=int(seconds_left),
        )
        if exit_now:
            log.info(f"EXIT RULE triggered: {exit_reason}")
            # Map to the closest ExitReason enum for _close_position
            if "stop_loss" in exit_reason or "underwater" in exit_reason:
                exit_enum = ExitReason.TRAILING_STOP
            elif "profit" in exit_reason or "ceiling" in exit_reason:
                exit_enum = ExitReason.TAKE_PROFIT
            elif "time" in exit_reason or "force" in exit_reason or "flat" in exit_reason:
                exit_enum = ExitReason.TIME_EXPIRY
            else:
                exit_enum = ExitReason.TRAILING_STOP
            _do_close(bid, exit_reason, exit_enum)
            return

        # ── trailing stop fallback (belt and suspenders) ──────────────────────
        trailing_exit, trailing_reason = stop.update(bid)
        log.info(
            f"  {ticker}  bid={bid}¢  peak={stop.peak_bid}¢  "
            f"stop={stop.stop_price}¢  pnl={stop.profit_cents:+d}¢"
        )

        if trailing_exit and trailing_reason is not None:
            log.info(f"TRAILING STOP fallback triggered: {trailing_reason.name}")
            _do_close(bid, f"trailing_{trailing_reason.name}", trailing_reason)
            return

        time.sleep(HOLD_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
# SCAN LOOP — looks for entry signals when flat
# ══════════════════════════════════════════════════════════════════════════════

def run_cycle(client: KalshiClient, dry_run: bool, force_direction: int = 0) -> None:
    """One full scan: check for signal → enter if valid → monitor until exit.

    Parameters
    ----------
    force_direction : +1 = bullish only, -1 = bearish only, 0 = try both (default)
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info(f"── Scan {now_utc}  [{_risk.summary()}] ──")

    # ── prefetch BTC price to warm the cache ─────────────────────────────────
    try:
        _feed.latest()
    except Exception as exc:
        log.warning(f"BTC price prefetch failed — {exc}")

    # ── daily cap guard ───────────────────────────────────────────────────────
    if _risk.is_daily_cap_hit():
        log.info("Daily loss cap hit — no new trades today")
        return

    # ── find active Kalshi contract ───────────────────────────────────────────
    contract = get_active_contract(client)
    if contract is None:
        log.info("No active KXBTC15M contract found")
        return

    log.info(
        f"CONTRACT {contract.ticker}  "
        f"YES bid={contract.yes_bid}¢ ask={contract.yes_ask}¢  "
        f"NO bid={contract.no_bid}¢ ask={contract.no_ask}¢  "
        f"seconds_left={contract.seconds_left:.0f}s"
    )

    # ── evaluate entry signal — try both directions if not forced ─────────────
    signal = None
    bearish = False

    if force_direction >= 0:
        # try bullish (YES side)
        bull = evaluate(_feed, contract.yes_ask, contract.yes_bid,
                        contract.seconds_left, bearish=False)
        if bull.direction != Direction.NONE:
            signal, bearish = bull, False
        else:
            log.info(f"SIGNAL BULL  {bull.reason}")

    if signal is None and force_direction <= 0:
        # try bearish (NO side)
        bear = evaluate(_feed, contract.no_ask, contract.no_bid,
                        contract.seconds_left, bearish=True)
        if bear.direction != Direction.NONE:
            signal, bearish = bear, True
        else:
            log.info(f"SIGNAL BEAR  {bear.reason}")

    if signal is None:
        return

    log.info(f"SIGNAL  {signal.reason}")

    # ── derive identifiers for gating ─────────────────────────────────────────
    window_id = contract.ticker          # unique per 15-min window
    direction = _direction_label(bearish)
    ask_price = contract.no_ask if bearish else contract.yes_ask
    bid_price = contract.no_bid if bearish else contract.yes_bid
    spread = ask_price - bid_price

    # ── entry rules (entry_exit module) ───────────────────────────────────────
    allowed, reason = should_enter(ask_price, spread, int(contract.seconds_left))
    if not allowed:
        log.info("Entry blocked: %s", reason)
        return

    # ── window direction lock — only one direction per window ─────────────────
    if not lock_window_direction(window_id, direction):
        log.info("Window %s already locked to opposite direction", window_id)
        return

    # ── cooldown after stop-loss ──────────────────────────────────────────────
    if cooldown_active():
        log.info("Cooldown active — skipping")
        return

    # ── dedup — one trade per window+direction ────────────────────────────────
    try:
        if has_trade_in_window(window_id, direction):
            log.info("Already have trade in window %s %s", window_id, direction)
            return
    except Exception as exc:
        log.warning("Journal dedup check failed: %s — proceeding", exc)

    # ── open position ─────────────────────────────────────────────────────────
    result = _open_position(client, contract, dry_run, bearish=bearish)
    if result is None:
        return
    fill, count = result
    ctx = _signal_ctx(signal)

    # ── record trade + signal in journal ──────────────────────────────────────
    trade_id = None
    try:
        trade_id = record_trade(
            window_id=window_id,
            direction=direction,
            entry_price_cents=fill,
            contracts=count,
            strike_price=signal.btc_price,
        )
        record_signal(
            trade_id=trade_id,
            momentum_delta=signal.momentum_delta,
            z_score=signal.volatility_z,
            spread_cents=spread,
            time_remaining_sec=int(contract.seconds_left),
            btc_price=signal.btc_price,
        )
    except Exception as exc:
        log.warning("Journal record_trade/signal failed: %s", exc)

    # ── monitor until exit ────────────────────────────────────────────────────
    _hold_loop(
        client, contract.ticker, fill, count, contract.window_end,
        dry_run, bearish=bearish, signal_ctx=ctx, trade_id=trade_id,
        window_id=window_id,
    )
    log.info(f"── Position closed  [{_risk.summary()}] ──")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    init_db()
    parser = argparse.ArgumentParser(description="BTC 15-min Kalshi auto-trader")
    parser.add_argument("--dry-run",  action="store_true", help="No real orders placed")
    parser.add_argument("--once",     action="store_true", help="Single scan then exit")
    direction = parser.add_mutually_exclusive_group()
    direction.add_argument("--bearish", action="store_true", help="Force bearish only (buy NO)")
    direction.add_argument("--bullish", action="store_true", help="Force bullish only (buy YES)")
    args = parser.parse_args()

    dry_run = args.dry_run
    # 0 = dynamic (both), +1 = bull only, -1 = bear only
    force_direction = -1 if args.bearish else (1 if args.bullish else 0)
    strategy_label = {-1: "BEARISH (NO)", 1: "BULLISH (YES)", 0: "DYNAMIC (both)"}[force_direction]
    sep = "=" * 68

    log.info(sep)
    log.info("  BTC 15-Min Kalshi Auto-Trader")
    log.info(f"  Mode          : {'DRY RUN' if dry_run else 'LIVE 🔴'}")
    log.info(f"  Strategy      : {strategy_label}")
    log.info(f"  Daily cap     : ${DAILY_LOSS_CAP_CENTS / 100:.2f}")
    log.info(f"  Scan interval : {SCAN_INTERVAL}s  |  Hold interval: {HOLD_INTERVAL}s")
    log.info(f"  Force exit at : {FORCE_EXIT_SEC}s remaining")
    log.info(f"  Log file      : {LOG_FILE}")
    log.info(sep)

    try:
        client = KalshiClient(demo=False)
        log.info("Kalshi connection OK")
    except Exception as exc:
        log.error(f"Cannot connect to Kalshi: {exc}")
        sys.exit(1)

    if args.once:
        run_cycle(client, dry_run, force_direction)
        return

    while True:
        try:
            run_cycle(client, dry_run, force_direction)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as exc:
            log.error(f"Cycle error: {exc}", exc_info=True)

        log.info(f"Next scan in {SCAN_INTERVAL}s")
        try:
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break


if __name__ == "__main__":
    main()
