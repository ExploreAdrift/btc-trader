"""Tests for BTC signal confidence scoring, session awareness, and volatility regime."""
import unittest

from btc_trader.signals import (
    MIN_SIGNAL_CONFIDENCE,
    compute_signal_confidence,
    detect_volatility_regime,
    get_trading_session,
)


class TestSignalConfidence(unittest.TestCase):
    def _default_regime(self):
        return {"regime": "normal", "recommended_z_thresh": 1.5, "trade_allowed": True}

    def test_all_strong_signals(self):
        score = compute_signal_confidence(
            z_score=3.0, rsi_14=50.0, volume_ratio=1.5,
            hour_trend_agrees=True, vwap_agrees=True, prev_window_agrees=True,
            regime=self._default_regime(),
        )
        self.assertGreater(score, 0.85)

    def test_weak_signals(self):
        score = compute_signal_confidence(
            z_score=0.5, rsi_14=80.0, volume_ratio=0.3,
            hour_trend_agrees=False, vwap_agrees=False, prev_window_agrees=False,
            regime=self._default_regime(),
        )
        self.assertLess(score, 0.2)

    def test_medium_signals(self):
        score = compute_signal_confidence(
            z_score=1.5, rsi_14=55.0, volume_ratio=0.8,
            hour_trend_agrees=True, vwap_agrees=False, prev_window_agrees=True,
            regime=self._default_regime(),
        )
        self.assertTrue(0.4 < score < 0.8)

    def test_returns_float_between_0_and_1(self):
        score = compute_signal_confidence(
            z_score=1.0, rsi_14=50.0, volume_ratio=1.0,
            hour_trend_agrees=False, vwap_agrees=True, prev_window_agrees=False,
            regime=self._default_regime(),
        )
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_high_vol_regime_raises_z_bar(self):
        high_regime = {"regime": "high", "recommended_z_thresh": 1.8, "trade_allowed": True}
        # z=1.6 passes normal (1.5) but fails high (1.8) — score should be lower
        score_normal = compute_signal_confidence(
            z_score=1.6, rsi_14=50.0, volume_ratio=1.0,
            hour_trend_agrees=True, vwap_agrees=True, prev_window_agrees=True,
            regime=self._default_regime(),
        )
        score_high = compute_signal_confidence(
            z_score=1.6, rsi_14=50.0, volume_ratio=1.0,
            hour_trend_agrees=True, vwap_agrees=True, prev_window_agrees=True,
            regime=high_regime,
        )
        self.assertGreater(score_normal, score_high)


class TestTradingSession(unittest.TestCase):
    def test_us_session(self):
        s = get_trading_session(15)
        self.assertEqual(s["session"], "us")
        self.assertAlmostEqual(s["min_confidence"], 0.50)

    def test_asian_session_higher_confidence(self):
        s = get_trading_session(3)
        self.assertEqual(s["session"], "asian")
        self.assertGreater(s["min_confidence"], 0.55)

    def test_dead_zone_highest_confidence(self):
        s = get_trading_session(22)
        self.assertEqual(s["session"], "dead_zone")
        self.assertGreaterEqual(s["min_confidence"], 0.60)

    def test_european_session(self):
        s = get_trading_session(10)
        self.assertEqual(s["session"], "european")
        self.assertAlmostEqual(s["min_confidence"], 0.55)

    def test_all_hours_covered(self):
        for h in range(24):
            s = get_trading_session(h)
            self.assertIn("session", s)
            self.assertIn("min_confidence", s)
            self.assertIn("vol_multiplier", s)


class TestVolatilityRegime(unittest.TestCase):
    def test_returns_dict_with_required_keys(self):
        class MockFeed:
            pass

        # Use a mock that makes fetch_klines return controlled data
        import btc_trader.signals as sig
        original = sig.fetch_klines

        def fake_klines(interval="1h", limit=4):
            return [
                {"open": 84800.0, "high": 85000.0, "low": 84700.0, "close": 84900.0, "volume": 10.0, "open_time": 0, "close_time": 0},
                {"open": 84700.0, "high": 85100.0, "low": 84600.0, "close": 85000.0, "volume": 12.0, "open_time": 0, "close_time": 0},
                {"open": 85000.0, "high": 85200.0, "low": 84800.0, "close": 85100.0, "volume": 11.0, "open_time": 0, "close_time": 0},
            ]

        try:
            sig.fetch_klines = fake_klines
            regime = detect_volatility_regime(MockFeed())
            self.assertIn("regime", regime)
            self.assertIn("trade_allowed", regime)
            self.assertIn("recommended_z_thresh", regime)
            self.assertIn("hourly_vol", regime)
        finally:
            sig.fetch_klines = original

    def test_high_vol_regime(self):
        import btc_trader.signals as sig
        original = sig.fetch_klines

        def fake_klines(interval="1h", limit=4):
            return [
                {"open": 80000.0, "high": 80800.0, "low": 80000.0, "close": 80600.0, "volume": 10.0, "open_time": 0, "close_time": 0},
                {"open": 80600.0, "high": 81500.0, "low": 80500.0, "close": 81200.0, "volume": 12.0, "open_time": 0, "close_time": 0},
                {"open": 81200.0, "high": 81800.0, "low": 81200.0, "close": 81500.0, "volume": 11.0, "open_time": 0, "close_time": 0},
            ]

        try:
            sig.fetch_klines = fake_klines
            regime = detect_volatility_regime(object())
            self.assertEqual(regime["regime"], "high")
            self.assertGreater(regime["recommended_z_thresh"], 1.5)
        finally:
            sig.fetch_klines = original

    def test_dead_market_blocks_trading(self):
        import btc_trader.signals as sig
        original = sig.fetch_klines

        def fake_klines(interval="1h", limit=4):
            return [
                {"open": 85000.0, "high": 85010.0, "low": 84995.0, "close": 85005.0, "volume": 1.0, "open_time": 0, "close_time": 0},
                {"open": 85005.0, "high": 85020.0, "low": 84990.0, "close": 85010.0, "volume": 1.0, "open_time": 0, "close_time": 0},
                {"open": 85010.0, "high": 85015.0, "low": 85000.0, "close": 85012.0, "volume": 1.0, "open_time": 0, "close_time": 0},
            ]

        try:
            sig.fetch_klines = fake_klines
            regime = detect_volatility_regime(object())
            self.assertEqual(regime["regime"], "dead")
            self.assertFalse(regime["trade_allowed"])
        finally:
            sig.fetch_klines = original


class TestMinConfidenceConstant(unittest.TestCase):
    def test_min_confidence_is_reasonable(self):
        self.assertGreater(MIN_SIGNAL_CONFIDENCE, 0.0)
        self.assertLess(MIN_SIGNAL_CONFIDENCE, 1.0)
        self.assertAlmostEqual(MIN_SIGNAL_CONFIDENCE, 0.55)


if __name__ == "__main__":
    unittest.main()
