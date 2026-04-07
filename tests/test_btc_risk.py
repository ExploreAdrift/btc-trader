import unittest
from btc_trader.risk import lock_window_direction, _window_locks

class TestWindowLock(unittest.TestCase):
    def setUp(self):
        _window_locks.clear()

    def test_first_lock_succeeds(self):
        self.assertTrue(lock_window_direction("W1", "BULL"))

    def test_same_direction_succeeds(self):
        lock_window_direction("W1", "BULL")
        self.assertTrue(lock_window_direction("W1", "BULL"))

    def test_opposite_direction_blocked(self):
        lock_window_direction("W1", "BULL")
        self.assertFalse(lock_window_direction("W1", "BEAR"))

    def test_different_window_independent(self):
        lock_window_direction("W1", "BULL")
        self.assertTrue(lock_window_direction("W2", "BEAR"))

if __name__ == "__main__":
    unittest.main()
