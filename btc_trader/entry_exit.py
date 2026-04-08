"""Tiered entry/exit rules for BTC 15-minute contracts.

Entry cap: 45c (loosened from 30c — confidence scoring + stop-loss protect against bad entries)
Take-profit: tiered by entry price, universal ceiling at 50c
Stop-loss: tiered by entry price
Time-based exits: progressive based on hold duration and P&L
Underwater exits: immediate exit on large adverse moves
"""

from __future__ import annotations

# ── Entry Rules ────────────────────────────────────────
MAX_ENTRY_PRICE = 45    # Never buy above 45c
MIN_TIME_REMAINING = 300  # 5 minutes minimum in window
MAX_SPREAD = 15         # Don't trade if spread > 15c

# ── Take-Profit Tiers ─────────────────────────────────
CEILING = 50            # Never hold above 50c — market says coin flip

# ── Stop-Loss Tiers ───────────────────────────────────
LOTTERY_MAX = 10        # 1-10c = lottery tier
VALUE_MAX = 20          # 11-20c = value tier
MODERATE_MAX = 30       # 21-30c = moderate tier
# 31-45c = expensive tier

# ── Time Rules ─────────────────────────────────────────
PROFIT_TIME_SEC = 480       # 8 min: exit if profit > 5c
LATE_PROFIT_TIME_SEC = 600  # 10 min: exit if any profit
FLAT_EXIT_SEC = 660         # 11 min (4 min left): exit if flat
FORCE_EXIT_SEC = 780        # 13 min (2 min left): force exit

# ── Underwater Rules ───────────────────────────────────
UNDERWATER_HARD = 15    # Down 15c at any time = immediate exit
UNDERWATER_LATE = 10    # Down 10c with <7 min remaining = exit
UNDERWATER_LATE_SEC = 480  # 8 min into trade = "late"


def should_enter(ask_price: int, spread: int, time_remaining_sec: int) -> tuple[bool, str]:
    """Check if entry conditions are met.

    Returns (allowed, reason).
    """
    if ask_price > MAX_ENTRY_PRICE:
        return False, f"entry_cap: {ask_price}c > {MAX_ENTRY_PRICE}c"
    if spread > MAX_SPREAD:
        return False, f"spread_cap: {spread}c > {MAX_SPREAD}c"
    if time_remaining_sec < MIN_TIME_REMAINING:
        return False, f"time_remaining: {time_remaining_sec}s < {MIN_TIME_REMAINING}s"
    return True, "ok"


def take_profit_target(entry_price: int) -> int:
    """Return take-profit exit price based on entry tier."""
    if entry_price <= LOTTERY_MAX:
        return min(entry_price * 3, CEILING)
    elif entry_price <= VALUE_MAX:
        return min(entry_price * 2, CEILING)
    elif entry_price <= MODERATE_MAX:
        return min(entry_price + 15, CEILING)
    else:
        # Expensive tier (31-45c): take 10c profit, tight because entry is high
        return min(entry_price + 10, CEILING)


def stop_loss_target(entry_price: int) -> int | None:
    """Return stop-loss price, or None for lottery tier."""
    if entry_price <= LOTTERY_MAX:
        return None  # Max loss is 10c, not worth stopping
    elif entry_price <= VALUE_MAX:
        return max(1, entry_price // 2)  # 50% of entry
    elif entry_price <= MODERATE_MAX:
        return max(1, entry_price - 10)  # Fixed 10c risk
    else:
        # Expensive tier (31-45c): tight 8c stop — protect capital
        return max(1, entry_price - 8)


def should_exit(
    *,
    entry_price: int,
    current_bid: int,
    hold_duration_sec: int,
    time_remaining_sec: int,
) -> tuple[bool, str]:
    """Check all exit conditions in priority order.

    Returns (should_exit, reason).
    """
    pnl = current_bid - entry_price

    # Priority 1: Force exit — less than 2 min remaining
    if time_remaining_sec <= 120:
        return True, "force_exit_2min"

    # Priority 2: Underwater hard stop — down >15c at any time
    if pnl <= -UNDERWATER_HARD:
        return True, f"underwater_hard_{UNDERWATER_HARD}c"

    # Priority 3: Underwater late — down >10c with <7 min left
    if pnl <= -UNDERWATER_LATE and time_remaining_sec < 420:
        return True, f"underwater_late_{UNDERWATER_LATE}c"

    # Priority 4: Universal ceiling — bid above 50c, take profit
    if current_bid >= CEILING:
        return True, f"ceiling_{CEILING}c"

    # Priority 5: Tiered stop-loss
    stop = stop_loss_target(entry_price)
    if stop is not None and current_bid <= stop:
        return True, f"stop_loss_{stop}c"

    # Priority 6: Tiered take-profit
    target = take_profit_target(entry_price)
    if current_bid >= target:
        return True, f"take_profit_{target}c"

    # Priority 7: Time-based profit exits
    if hold_duration_sec >= PROFIT_TIME_SEC and pnl >= 5:
        return True, "time_profit_8min"
    if hold_duration_sec >= LATE_PROFIT_TIME_SEC and pnl > 0:
        return True, "time_profit_10min"

    # Priority 8: Flat exit — near settlement and no movement
    if time_remaining_sec <= 240 and abs(pnl) <= 3:
        return True, "flat_exit_4min"

    return False, ""
