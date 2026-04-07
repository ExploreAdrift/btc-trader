# BTC Trader Upgrade — Bug Fixes, Trade Journal, Optimized Entry/Exit

**Date:** 2026-04-07
**Status:** Approved
**Scope:** Sub-project 1 — Fix bugs, add measurement, optimize entry/exit rules

## Context

The BTC trader trades 15-minute binary contracts on Kalshi (series: KXBTC15M). YES = BTC finishes above the strike, NO = below. Entry is via Binance price data feeding a 9-gate consensus signal system.

### Problems

1. **Same-window opposing trades** — Risk manager doesn't prevent long AND short on the same 15-minute window. Can bet against itself.
2. **Duplicate order logging** — CSV shows identical rows with same timestamps.
3. **Catastrophic TIME_EXPIRY exits** — Holds underwater positions until 2 min left, exiting at 1¢ for massive losses.
4. **Entry price too high** — Wins cluster at 22-29¢ entry, losses at 50-60¢. Current cap of 40¢ is too loose.
5. **No measurement infrastructure** — Can't analyze which signals work, optimal entry/exit, or time-of-day patterns.
6. **Trailing stop thresholds unvalidated** — Hardcoded without backtesting.

### Trading profile

- $5/day max loss (experimental phase)
- 24/7 operation
- Priority: high win rate over high profit per trade
- ~20 trades/day max

---

## Component A: Fix Critical Bugs

### 1. Same-window direction lock

In `btc_trader/risk.py`, add a window lock mechanism:

```python
_window_locks: dict[str, str] = {}  # window_id -> "BULL" or "BEAR"

def lock_window_direction(window_id: str, direction: str) -> bool:
    """Lock a window to one direction. Returns False if already locked to opposite."""
    existing = _window_locks.get(window_id)
    if existing is not None and existing != direction:
        return False  # Already locked to opposite direction
    _window_locks[window_id] = direction
    return True
```

Call this before any order placement. If it returns False, skip the trade.

In `btc_trader/auto_trader_btc.py`, the main loop tries both BULL and BEAR directions (lines 311-327). Change to: evaluate signal direction first, lock that direction, only try that one.

### 2. Duplicate order dedup

In `btc_trader/risk.py`, the `record_trade()` function appends to CSV without checking for duplicates. Add:
- Check if a row with same window_id + direction + entry_time already exists
- Skip write if duplicate detected
- Log warning when duplicate prevented

### 3. Early exit for underwater positions

Replace the current hold logic in `btc_trader/trailing_stop.py` with progressive exits:

| Condition | Action |
|-----------|--------|
| Down >15¢ from entry, any time | Exit immediately |
| Down >10¢, <7 min remaining | Exit |
| Flat (within ±3¢), <4 min remaining | Exit (avoid settlement risk) |
| Up any amount, <2 min remaining | Exit (take what you have) |

This replaces the current FORCE_EXIT_SEC=120s which waits too long.

---

## Component B: Trade Journal (SQLite)

### Database: `btc_trades.db`

#### `trades` table

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PRIMARY KEY | Auto-increment |
| window_id | TEXT NOT NULL | 15-min window identifier |
| direction | TEXT NOT NULL | "BULL" or "BEAR" |
| strike_price | REAL | Kalshi strike price |
| entry_price_cents | REAL | Price paid |
| exit_price_cents | REAL | Price received on exit |
| contracts | INTEGER | Number of contracts |
| pnl_cents | REAL | Realized P&L |
| exit_reason | TEXT | "take_profit", "stop_loss", "time_exit", "trailing_stop", "underwater_exit", "settlement" |
| entry_time | TEXT | ISO timestamp |
| exit_time | TEXT | ISO timestamp |
| hold_duration_sec | INTEGER | Seconds held |
| kalshi_order_id | TEXT | Order ID |
| status | TEXT | "open", "closed", "settled" |
| created_at | TEXT | ISO timestamp |

#### `signals` table

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PRIMARY KEY | Auto-increment |
| trade_id | INTEGER | FK to trades |
| momentum_delta | REAL | BTC price delta (60s lookback) |
| volatility_sigma | REAL | Rolling volatility |
| z_score | REAL | momentum / volatility |
| rsi_14 | REAL | 14-period RSI |
| vwap_side | TEXT | "above" or "below" |
| volume_ratio | REAL | Recent vs baseline volume |
| hour_trend | TEXT | "bullish", "bearish", "neutral" |
| prev_window_dir | TEXT | Previous window's direction |
| spread_cents | REAL | Bid-ask spread at entry |
| time_remaining_sec | INTEGER | Seconds left in window at entry |
| btc_price_at_entry | REAL | BTC/USDT price |

### P&L query capabilities

- Win rate by entry price bucket (1-10¢, 11-20¢, 21-30¢)
- Win rate by time of day (4-hour buckets)
- Win rate by exit reason
- Average P&L by signal z_score bucket
- Which gates correlate with wins vs losses

---

## Component C: Tiered Entry/Exit Rules

### Entry rules

**Hard cap: 30¢** (reduced from 40¢)

Rationale: Trade history shows wins cluster at 22-29¢, losses at 50-60¢. Lower entry = more room to profit, less to lose.

**Minimum time in window: 5 minutes remaining** (keep current)

**Spread cap: 15¢** (tightened from 30¢ — wide spreads eat profit)

### Take-profit (tiered by entry price)

| Entry Price | Exit When | Rationale |
|-------------|-----------|-----------|
| 1-10¢ | 3x entry OR 50¢, whichever lower | Cheap — let it run |
| 11-20¢ | 2x entry OR 50¢, whichever lower | Take a double |
| 21-30¢ | Entry + 15¢ OR 50¢, whichever lower | Take 15¢ profit |

**Universal ceiling: 50¢** — never hold above 50¢. At 50¢ the market is saying it's a coin flip. Take your profit and leave.

### Stop-loss (tiered by entry price)

| Entry Price | Stop When | Rationale |
|-------------|-----------|-----------|
| 1-10¢ | No stop — ride to 0 | Max loss is 10¢ |
| 11-20¢ | 50% of entry (e.g., 16¢ → stop at 8¢) | Cut at half |
| 21-30¢ | Entry - 10¢ (e.g., 28¢ → stop at 18¢) | Fixed 10¢ risk |

### Time-based exits

| Condition | Action |
|-----------|--------|
| Holding >8 min AND profit >5¢ | Exit — take profit, don't gamble on settlement |
| Holding >10 min AND any profit | Exit — too close to settlement |
| <4 min remaining AND flat (±3¢) | Exit — avoid coin-flip settlement |
| <2 min remaining | Force exit regardless |

### Underwater position rules

| Condition | Action |
|-----------|--------|
| Down >15¢ at any time | Exit immediately — position is wrong |
| Down >10¢ with <7 min left | Exit — not enough time to recover |

---

## Component D: Risk Manager Hardening

### Daily limits (keep existing)
- Max daily loss: $5 (500¢)
- Max trades per day: 20

### New rules
- **One direction per window** — once BULL is chosen for a window, BEAR is blocked (and vice versa)
- **Cooldown after loss** — wait 2 windows (30 min) after a stop-loss before re-entering
- **No trading in first/last 2 min of window** — entry in final 2 min is gambling, not trading

---

## File Changes

### Modified files
- `btc_trader/risk.py` — Window lock, dedup, cooldown, new trade journal writes
- `btc_trader/trailing_stop.py` — Replace trailing stop with tiered take-profit/stop-loss + underwater exits
- `btc_trader/auto_trader_btc.py` — Single direction per scan, use new risk controls
- `btc_trader/signals.py` — Record all gate values for journal

### New files
- `btc_trader/db.py` — SQLite schema and helpers (same pattern as weather_market/db.py)
- `btc_trader/journal.py` — Trade journal CRUD
- `btc_trader/entry_exit.py` — Tiered entry/exit/stop-loss rules (equivalent to weather_market/execution/risk.py)
- `tests/test_btc_entry_exit.py` — Tests for entry/exit rules
- `tests/test_btc_journal.py` — Tests for trade journal
- `tests/test_btc_risk.py` — Tests for window lock, dedup, cooldown

## Out of Scope (Sub-project 2+)

- Signal gate optimization (which gates actually predict wins)
- Time-of-day threshold adjustments
- Volatility regime detection
- Backtesting harness for BTC
- Signal strength weighting (replace binary gates with scored confidence)
