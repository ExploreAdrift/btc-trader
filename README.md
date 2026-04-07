# BTC Trader

Automated BTC 15-minute binary contract trading on Kalshi.

## Features
- 9-gate consensus signal system with confidence scoring
- Volatility regime detection (high/normal/low/dead)
- Session-aware thresholds (Asian/European/US/dead zone)
- Tiered entry/exit: 30c cap, take-profit, stop-loss, underwater exits
- Window lock: prevents opposing trades in same 15-min window
- SQLite trade journal with signal recording
- Trade analysis: P&L by entry price, hour, direction, exit reason

## Usage

```bash
pip install -e .
btc-trader --dry-run
```
