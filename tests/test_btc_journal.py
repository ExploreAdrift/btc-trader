import unittest
import tempfile
from pathlib import Path
from btc_trader.db import init_db
from btc_trader.journal import record_trade, record_signal, close_trade, has_trade_in_window, get_daily_pnl

class TestBtcJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.tmp.name)
        self.tmp.close()
        init_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_record_and_close_trade(self):
        tid = record_trade(
            db_path=self.db_path, window_id="W1", direction="BULL",
            entry_price_cents=25, contracts=5,
        )
        close_trade(
            db_path=self.db_path, trade_id=tid,
            exit_price_cents=40, pnl_cents=75, exit_reason="take_profit",
            hold_duration_sec=300,
        )
        pnl = get_daily_pnl(db_path=self.db_path)
        self.assertEqual(pnl["wins"], 1)
        self.assertAlmostEqual(pnl["total_pnl_cents"], 75.0)

    def test_dedup_prevents_duplicate(self):
        record_trade(
            db_path=self.db_path, window_id="W1", direction="BULL",
            entry_price_cents=25, contracts=5,
        )
        self.assertTrue(has_trade_in_window("W1", "BULL", self.db_path))
        self.assertFalse(has_trade_in_window("W1", "BEAR", self.db_path))
        self.assertFalse(has_trade_in_window("W2", "BULL", self.db_path))

    def test_signal_recording(self):
        tid = record_trade(
            db_path=self.db_path, window_id="W1", direction="BULL",
            entry_price_cents=20, contracts=3,
        )
        record_signal(
            db_path=self.db_path, trade_id=tid,
            momentum_delta=15.0, z_score=1.8, rsi_14=55.0,
            btc_price=84500.0,
        )
        from btc_trader.db import get_connection
        conn = get_connection(self.db_path)
        row = conn.execute("SELECT z_score, btc_price FROM signals WHERE trade_id = ?", (tid,)).fetchone()
        self.assertAlmostEqual(row["z_score"], 1.8)
        conn.close()

if __name__ == "__main__":
    unittest.main()
