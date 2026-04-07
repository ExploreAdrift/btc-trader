import unittest
from btc_trader.entry_exit import should_enter, take_profit_target, stop_loss_target, should_exit

class TestEntryGate(unittest.TestCase):
    def test_rejects_above_30(self):
        ok, _ = should_enter(35, 10, 600)
        self.assertFalse(ok)

    def test_accepts_at_30(self):
        ok, _ = should_enter(30, 10, 600)
        self.assertTrue(ok)

    def test_rejects_wide_spread(self):
        ok, reason = should_enter(20, 20, 600)
        self.assertFalse(ok)
        self.assertIn("spread", reason)

    def test_rejects_late_entry(self):
        ok, reason = should_enter(20, 10, 200)
        self.assertFalse(ok)
        self.assertIn("time", reason)

class TestTakeProfit(unittest.TestCase):
    def test_lottery_3x(self):
        self.assertEqual(take_profit_target(8), 24)

    def test_lottery_capped_50(self):
        self.assertEqual(take_profit_target(10), 30)

    def test_value_2x(self):
        self.assertEqual(take_profit_target(15), 30)

    def test_value_capped_50(self):
        self.assertEqual(take_profit_target(20), 40)

    def test_moderate_plus_15(self):
        self.assertEqual(take_profit_target(25), 40)

    def test_moderate_capped_50(self):
        self.assertEqual(take_profit_target(30), 45)

class TestStopLoss(unittest.TestCase):
    def test_lottery_no_stop(self):
        self.assertIsNone(stop_loss_target(5))

    def test_value_half(self):
        self.assertEqual(stop_loss_target(16), 8)

    def test_moderate_minus_10(self):
        self.assertEqual(stop_loss_target(28), 18)

class TestShouldExit(unittest.TestCase):
    def test_force_exit_2min(self):
        ok, reason = should_exit(entry_price=20, current_bid=22, hold_duration_sec=700, time_remaining_sec=90)
        self.assertTrue(ok)
        self.assertIn("force_exit", reason)

    def test_underwater_hard(self):
        ok, reason = should_exit(entry_price=25, current_bid=9, hold_duration_sec=60, time_remaining_sec=800)
        self.assertTrue(ok)
        self.assertIn("underwater_hard", reason)

    def test_underwater_late(self):
        ok, reason = should_exit(entry_price=25, current_bid=14, hold_duration_sec=500, time_remaining_sec=300)
        self.assertTrue(ok)
        self.assertIn("underwater_late", reason)

    def test_ceiling_exit(self):
        ok, reason = should_exit(entry_price=20, current_bid=52, hold_duration_sec=300, time_remaining_sec=600)
        self.assertTrue(ok)
        self.assertIn("ceiling", reason)

    def test_take_profit(self):
        ok, reason = should_exit(entry_price=15, current_bid=31, hold_duration_sec=200, time_remaining_sec=600)
        self.assertTrue(ok)
        self.assertIn("take_profit", reason)

    def test_time_profit_8min(self):
        ok, reason = should_exit(entry_price=20, current_bid=26, hold_duration_sec=500, time_remaining_sec=400)
        self.assertTrue(ok)
        self.assertIn("time_profit_8min", reason)

    def test_flat_exit(self):
        # pnl=0 (flat), <4 min remaining, hold < 10min so time_profit doesn't trigger
        ok, reason = should_exit(entry_price=20, current_bid=20, hold_duration_sec=400, time_remaining_sec=200)
        self.assertTrue(ok)
        self.assertIn("flat_exit", reason)

    def test_no_exit_normal(self):
        ok, _ = should_exit(entry_price=20, current_bid=24, hold_duration_sec=200, time_remaining_sec=600)
        self.assertFalse(ok)

    def test_lottery_no_stop(self):
        ok, _ = should_exit(entry_price=5, current_bid=2, hold_duration_sec=200, time_remaining_sec=600)
        self.assertFalse(ok)  # No stop for lottery tier, not underwater_hard yet

if __name__ == "__main__":
    unittest.main()
