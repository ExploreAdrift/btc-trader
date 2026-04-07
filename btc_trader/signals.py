"""Entry signal logic — CONSENSUS strategy.

Entry fires only when ALL conditions are met:
  1. Enough time remaining in window
  2. Contract price not lopsided
  3. Bid-ask spread acceptable
  4. 60-second BTC momentum exceeds volatility-adjusted threshold
  5. Previous 15-min window resolved in the same direction
  6. 1-hour trend agrees with momentum direction
  7. VWAP momentum confirms direction

Additional layers (v2):
  - Volatility regime detection (high/normal/low/dead)
  - Session-aware confidence thresholds (Asian/EU/US/dead zone)
  - Weighted signal confidence scoring (0.0–1.0)

Direction convention:  +1 = bullish (buy YES)  |  -1 = bearish (buy NO)
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from enum import IntEnum

from .binance import (
    PriceFeed,
    fetch_klines,
    previous_window_direction,
    rolling_volatility,
    rsi,
    trend_direction,
    volume_ratio,
    vwap_momentum,
)

# ── tuneable parameters ───────────────────────────────────────────────────────
MAX_ENTRY_CENTS      = 40    # max entry — wins cluster at 22-29¢, losses at 50-60¢
MAX_SPREAD_CENTS     = 30    # skip if bid-ask spread is wider than this
MIN_SECONDS_LEFT     = 300   # 5 minutes — no late entries
MOMENTUM_LOOKBACK    = 60.0  # seconds of BTC price history to consider
VOLATILITY_Z_THRESH  = 1.2   # momentum must exceed this — winning trades were 1.3σ+
MIN_MOMENTUM_FLOOR   = 3.0   # absolute floor — even in low-vol, need at least $3
MIN_SIGNAL_CONFIDENCE = 0.55  # don't trade below 55% confidence


class Direction(IntEnum):
    BEAR  = -1
    NONE  =  0
    BULL  =  1


@dataclass(frozen=True, slots=True)
class Signal:
    direction:      Direction
    momentum_delta: float   # raw BTC price change over lookback window
    prev_direction: int     # previous window outcome (+1/-1/0)
    entry_price:    int     # ask in cents at signal time
    reason:         str     # human-readable explanation for logs
    # ── enriched fields for journal / diagnostics ─────────────────────────────
    volatility_z:   float = 0.0   # momentum / rolling_stdev (how many σ)
    trend_1h:       int   = 0     # 1-hour trend direction at signal time
    vwap_delta:     float = 0.0   # current_price − VWAP (volume-weighted momentum)
    btc_price:      float = 0.0   # BTC spot price at signal time
    bid_at_entry:   int   = 0     # Kalshi bid at signal time
    spread:         int   = 0     # ask − bid at signal time
    seconds_left:   float = 0.0   # time remaining at signal time
    # ── v2: confidence scoring & regime awareness ────────────────────────────
    confidence:     float = 0.0   # weighted signal confidence (0.0–1.0)
    regime:         str   = "normal"  # volatility regime at signal time


# ── v2: volatility regime, session awareness, confidence scoring ─────────────

def detect_volatility_regime(feed: PriceFeed) -> dict:
    """Classify current BTC volatility into a regime.

    Uses 1-hour realized volatility (average high-low range) to determine
    if conditions are favorable for trading.

    Returns
    -------
    dict with keys: regime, hourly_vol, recommended_z_thresh, trade_allowed
    """
    try:
        candles = fetch_klines(interval="1h", limit=4)
    except Exception:
        candles = []

    if not candles or len(candles) < 2:
        return {"regime": "normal", "hourly_vol": 0.0, "recommended_z_thresh": 1.5, "trade_allowed": True}

    # Realized vol = average of (high - low) across recent completed 1h candles
    ranges = [c["high"] - c["low"] for c in candles[:-1]]  # exclude current incomplete
    avg_range = sum(ranges) / len(ranges)

    if avg_range >= 500:  # $500+ hourly range = very volatile
        return {
            "regime": "high",
            "hourly_vol": avg_range,
            "recommended_z_thresh": 1.8,  # need stronger signal in high vol
            "trade_allowed": True,
        }
    elif avg_range >= 150:  # $150-500 = normal
        return {
            "regime": "normal",
            "hourly_vol": avg_range,
            "recommended_z_thresh": 1.5,
            "trade_allowed": True,
        }
    elif avg_range >= 50:  # $50-150 = low vol
        return {
            "regime": "low",
            "hourly_vol": avg_range,
            "recommended_z_thresh": 2.0,  # need very strong signal in low vol
            "trade_allowed": True,
        }
    else:  # <$50 = dead market
        return {
            "regime": "dead",
            "hourly_vol": avg_range,
            "recommended_z_thresh": 999.0,  # effectively no trading
            "trade_allowed": False,
        }


def get_trading_session(hour_utc: int) -> dict:
    """Classify current hour into a trading session with adjusted parameters.

    BTC markets have different characteristics across sessions:
    - Asian session (00-08 UTC): Lower volume, wider spreads, more choppy
    - European session (08-13 UTC): Volume picks up, trends form
    - US session (13-21 UTC): Highest volume, strongest trends
    - Dead zone (21-00 UTC): Low volume overlap
    """
    if 0 <= hour_utc < 8:
        return {"session": "asian", "vol_multiplier": 0.8, "min_confidence": 0.65}
    elif 8 <= hour_utc < 13:
        return {"session": "european", "vol_multiplier": 1.0, "min_confidence": 0.55}
    elif 13 <= hour_utc < 21:
        return {"session": "us", "vol_multiplier": 1.0, "min_confidence": 0.50}
    else:
        return {"session": "dead_zone", "vol_multiplier": 0.6, "min_confidence": 0.70}


def compute_signal_confidence(
    z_score: float,
    rsi_14: float,
    volume_ratio: float,
    hour_trend_agrees: bool,
    vwap_agrees: bool,
    prev_window_agrees: bool,
    regime: dict,
) -> float:
    """Compute overall signal confidence from 0.0 to 1.0.

    Each factor contributes a weighted score.  Higher = more confident.
    """
    score = 0.0
    total_weight = 0.0

    # Z-score: most important -- how strong is momentum vs volatility
    # Weight: 30%
    z_thresh = regime.get("recommended_z_thresh", 1.5)
    if z_score >= z_thresh * 1.5:
        score += 0.30
    elif z_score >= z_thresh:
        score += 0.20
    elif z_score >= z_thresh * 0.8:
        score += 0.10
    total_weight += 0.30

    # Volume: second most important -- is the move real?
    # Weight: 25%
    if volume_ratio >= 1.5:
        score += 0.25  # very high volume
    elif volume_ratio >= 1.0:
        score += 0.20
    elif volume_ratio >= 0.7:
        score += 0.10
    total_weight += 0.25

    # RSI: is momentum exhausted?
    # Weight: 15%
    if 30 <= rsi_14 <= 70:
        score += 0.15  # healthy range -- not exhausted
    elif 25 <= rsi_14 <= 75:
        score += 0.08  # borderline
    # Outside 25-75: score stays 0 (exhausted)
    total_weight += 0.15

    # Trend agreement: 1h trend matches signal direction
    # Weight: 15%
    if hour_trend_agrees:
        score += 0.15
    total_weight += 0.15

    # VWAP agreement
    # Weight: 10%
    if vwap_agrees:
        score += 0.10
    total_weight += 0.10

    # Previous window agreement
    # Weight: 5%
    if prev_window_agrees:
        score += 0.05
    total_weight += 0.05

    return round(score / total_weight, 3) if total_weight > 0 else 0.0


def evaluate(feed: PriceFeed, ask_cents: int, bid_cents: int,
             seconds_left: float, bearish: bool = False) -> Signal:
    """Return a Signal. direction == NONE means do not trade.

    Parameters
    ----------
    feed         : live BTC price feed (shared instance)
    ask_cents    : current contract ask price in cents (YES ask or NO ask)
    bid_cents    : current contract bid price in cents (YES bid or NO bid)
    seconds_left : seconds until window closes
    bearish      : if True, look for bearish consensus instead of bullish
    """
    # ── pre-gate: volatility regime detection ────────────────────────────────
    vol_regime = detect_volatility_regime(feed)
    if not vol_regime["trade_allowed"]:
        return _no(f"dead market — hourly vol ${vol_regime['hourly_vol']:.0f}, no trade")

    # Use regime-adjusted z-threshold instead of hardcoded constant
    z_thresh = vol_regime["recommended_z_thresh"]

    # Session awareness
    hour_utc = _dt.datetime.utcnow().hour
    session = get_trading_session(hour_utc)

    # ── gate 1: time remaining ────────────────────────────────────────────────
    if seconds_left < MIN_SECONDS_LEFT:
        return _no("window closing soon — no entry")

    # ── gate 2: contract price not lopsided ───────────────────────────────────
    if ask_cents > MAX_ENTRY_CENTS:
        return _no(f"ask={ask_cents}¢ > {MAX_ENTRY_CENTS}¢ threshold — market too lopsided")

    # ── gate 3: spread check ─────────────────────────────────────────────────
    spread = ask_cents - bid_cents
    if spread > MAX_SPREAD_CENTS:
        return _no(f"spread={spread}¢ > {MAX_SPREAD_CENTS}¢ — too wide")

    # ── gate 4: volatility-adjusted BTC momentum ─────────────────────────────
    delta = feed.delta(MOMENTUM_LOOKBACK)
    vol = rolling_volatility(lookback_minutes=60)
    if vol > 0:
        z_score = abs(delta) / vol
    else:
        # fallback: if vol calc fails, use raw delta against floor
        z_score = abs(delta) / MIN_MOMENTUM_FLOOR if MIN_MOMENTUM_FLOOR else 0.0

    if abs(delta) < MIN_MOMENTUM_FLOOR:
        return _no(f"momentum Δ=${delta:+.1f} below absolute floor (${MIN_MOMENTUM_FLOOR})")

    if vol > 0 and z_score < z_thresh:
        return _no(
            f"momentum Δ=${delta:+.1f} only {z_score:.1f}σ "
            f"(need ≥{z_thresh}σ [{vol_regime['regime']}], vol=${vol:.1f})"
        )

    # ── gate 5: direction check (momentum must match side) ─────────────────
    if bearish:
        if delta >= 0:
            return _no(f"momentum bullish (Δ=${delta:+.1f}) — bearish-only strategy")
    else:
        if delta <= 0:
            return _no(f"momentum bearish (Δ=${delta:+.1f}) — bullish-only strategy")
    prev = previous_window_direction()  # still logged for CSV journal

    # ── gate 6: 1-hour trend confirmation ────────────────────────────────────
    trend_1h = trend_direction(interval="1h")
    expected_trend = -1 if bearish else 1
    if trend_1h != 0 and trend_1h != expected_trend:
        label = "BEAR" if bearish else "BULL"
        return _no(
            f"1h trend disagrees ({trend_1h:+d}) with {label} signal — "
            f"Δ=${delta:+.1f} z={z_score:.1f}σ"
        )

    # ── gate 7: VWAP confirmation ────────────────────────────────────────────
    vwap_d = vwap_momentum(lookback_minutes=15)
    if bearish and vwap_d > 0:
        return _no(f"VWAP bullish ({vwap_d:+.1f}) contradicts bearish signal")
    if not bearish and vwap_d < 0:
        return _no(f"VWAP bearish ({vwap_d:+.1f}) contradicts bullish signal")

    # ── gate 8: RSI — don't buy into exhausted momentum ─────────────────────
    current_rsi = rsi(lookback_minutes=14)
    if not bearish and current_rsi > 75:
        return _no(f"RSI overbought ({current_rsi:.0f}) — momentum exhaustion, likely to reverse")
    if bearish and current_rsi < 25:
        return _no(f"RSI oversold ({current_rsi:.0f}) — momentum exhaustion, likely to reverse")

    # ── gate 9: volume confirmation — move must be on real volume ────────────
    vol_ratio = volume_ratio(lookback_minutes=5, baseline_minutes=30)
    if vol_ratio < 0.7:
        return _no(f"volume ratio {vol_ratio:.1f}x — thin volume, move likely to fade")

    # ── all gates passed — compute confidence score ────────────────────────────
    conf = compute_signal_confidence(
        z_score=z_score,
        rsi_14=current_rsi,
        volume_ratio=vol_ratio,
        hour_trend_agrees=(trend_1h == expected_trend),
        vwap_agrees=(vwap_d < 0 if bearish else vwap_d > 0),
        prev_window_agrees=(prev == expected_trend),
        regime=vol_regime,
    )

    # Session-aware minimum confidence
    effective_min = max(MIN_SIGNAL_CONFIDENCE, session["min_confidence"])
    if conf < effective_min:
        return _no(
            f"confidence {conf:.1%} < {effective_min:.0%} threshold "
            f"({session['session']} session, {vol_regime['regime']} regime) — "
            f"Δ=${delta:+.1f} z={z_score:.1f}σ"
        )

    # ── build enriched signal ────────────────────────────────────────────────
    try:
        btc_price = feed.latest()
    except Exception:
        btc_price = 0.0

    direction = Direction.BEAR if bearish else Direction.BULL
    label = "BEAR" if bearish else "BULL"
    return Signal(
        direction=direction,
        momentum_delta=delta,
        prev_direction=prev,
        entry_price=ask_cents,
        reason=(
            f"CONSENSUS {label}: "
            f"Δ=${delta:+.1f}  z={z_score:.1f}σ  "
            f"1h={'AGREE' if trend_1h == expected_trend else 'FLAT'}  "
            f"vwap={vwap_d:+.1f}  RSI={current_rsi:.0f}  vol={vol_ratio:.1f}x  "
            f"conf={conf:.0%}  {vol_regime['regime']}/{session['session']}  "
            f"ask={ask_cents}¢"
        ),
        volatility_z=z_score,
        trend_1h=trend_1h,
        vwap_delta=vwap_d,
        btc_price=btc_price,
        bid_at_entry=bid_cents,
        spread=spread,
        seconds_left=seconds_left,
        confidence=conf,
        regime=vol_regime["regime"],
    )


def _no(reason: str) -> Signal:
    return Signal(
        direction=Direction.NONE,
        momentum_delta=0.0,
        prev_direction=0,
        entry_price=0,
        reason=reason,
    )
