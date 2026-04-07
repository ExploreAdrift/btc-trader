"""Tiered trailing stop — high-water mark that tightens as price climbs.

Trail distances by contract price
──────────────────────────────────
  50¢ – 65¢  →  10¢ trail
  66¢ – 79¢  →   7¢ trail
  80¢ – 89¢  →   5¢ trail
  90¢+       →  immediate exit (don't risk giving back near-certain win)

The stop ONLY moves up. Once armed it never moves down.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import auto, Enum


# ── exit reason enum ──────────────────────────────────────────────────────────
class ExitReason(Enum):
    TRAILING_STOP   = auto()   # bid fell below high-water trail
    TAKE_PROFIT     = auto()   # bid hit 90¢+ — harvest immediately
    TIME_EXPIRY     = auto()   # <2 min left — force exit
    LOSS_CAP        = auto()   # daily loss cap hit — risk manager triggered


# ── trail schedule ────────────────────────────────────────────────────────────
_TRAIL_SCHEDULE: tuple[tuple[int, int, int], ...] = (
    # (price_low_inclusive, price_high_inclusive, trail_cents)
    (90,  99, 0),    # 90¢+ → exit immediately (trail=0 signals instant exit)
    (80,  89, 5),
    (66,  79, 7),
    (50,  65, 10),
)

INSTANT_EXIT_THRESHOLD = 90   # cents — harvest immediately above this
HARD_STOP_LOSS_CENTS   = 20   # exit if bid drops this far below entry, regardless of arming


def _trail_for(bid: int) -> int:
    """Return the trail distance in cents for the given bid price."""
    for lo, hi, trail in _TRAIL_SCHEDULE:
        if lo <= bid <= hi:
            return trail
    return 10   # default for any price below 50¢ (unlikely but safe)


# ── position state ────────────────────────────────────────────────────────────
@dataclass
class TrailingStop:
    """Mutable stop state for a single open position.

    Parameters
    ----------
    entry_cents : price paid (yes_ask at fill time)
    """
    entry_cents: int
    peak_bid:    int = field(init=False)
    stop_price:  int = field(init=False)
    armed:       bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.peak_bid   = self.entry_cents
        self.stop_price = self.entry_cents - _trail_for(self.entry_cents)

    # ── public interface ──────────────────────────────────────────────────────

    def update(self, bid: int) -> tuple[bool, ExitReason | None]:
        """Feed the latest bid price.

        Returns (should_exit, reason).  Mutates internal peak / stop state.
        """
        # ── instant harvest ───────────────────────────────────────────────────
        if bid >= INSTANT_EXIT_THRESHOLD:
            return True, ExitReason.TAKE_PROFIT

        # ── update high-water mark ────────────────────────────────────────────
        if bid > self.peak_bid:
            self.peak_bid   = bid
            trail           = _trail_for(bid)
            new_stop        = bid - trail
            # stop only moves up, never down
            if new_stop > self.stop_price:
                self.stop_price = new_stop
            self.armed = True

        # ── hard stop-loss (always active, even if not armed) ────────────────
        if bid <= self.entry_cents - HARD_STOP_LOSS_CENTS:
            return True, ExitReason.TRAILING_STOP

        # ── trailing stop (only when armed) ──────────────────────────────────
        if self.armed and bid <= self.stop_price:
            return True, ExitReason.TRAILING_STOP

        return False, None

    @property
    def profit_cents(self) -> int:
        """Unrealised profit vs entry based on current peak (optimistic)."""
        return self.peak_bid - self.entry_cents

    def __repr__(self) -> str:
        return (
            f"TrailingStop(entry={self.entry_cents}¢  peak={self.peak_bid}¢  "
            f"stop={self.stop_price}¢  armed={self.armed})"
        )
