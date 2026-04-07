import unittest
import tempfile
from pathlib import Path
from btc_trader.db import init_db
from btc_trader.journal import record_trade, record_signal, close_trade
from btc_trader.backtest import analyze_by_entry_price, analyze_by_direction, analyze_by_exit_reason


class TestBtcBacktest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.tmp.name)
        self.tmp.close()
        init_db(self.db_path)

        # Create some test trades
        # Win: bought at 20c, sold at 40c
        tid1 = record_trade(db_path=self.db_path, window_id="W1", direction="BULL", entry_price_cents=20, contracts=5)
        close_trade(db_path=self.db_path, trade_id=tid1, exit_price_cents=40, pnl_cents=100, exit_reason="take_profit", hold_duration_sec=300)

        # Loss: bought at 25c, sold at 10c
        tid2 = record_trade(db_path=self.db_path, window_id="W2", direction="BEAR", entry_price_cents=25, contracts=5)
        close_trade(db_path=self.db_path, trade_id=tid2, exit_price_cents=10, pnl_cents=-75, exit_reason="stop_loss", hold_duration_sec=200)

        # Win: cheap lottery
        tid3 = record_trade(db_path=self.db_path, window_id="W3", direction="BULL", entry_price_cents=8, contracts=10)
        close_trade(db_path=self.db_path, trade_id=tid3, exit_price_cents=24, pnl_cents=160, exit_reason="take_profit", hold_duration_sec=400)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_by_entry_price(self):
        result = analyze_by_entry_price(self.db_path)
        lottery = next(r for r in result if r["bucket"] == "1-10c")
        self.assertEqual(lottery["wins"], 1)
        self.assertEqual(lottery["losses"], 0)
        self.assertAlmostEqual(lottery["total_pnl_cents"], 160.0)

    def test_by_direction(self):
        result = analyze_by_direction(self.db_path)
        self.assertEqual(result["BULL"]["wins"], 2)
        self.assertEqual(result["BEAR"]["losses"], 1)

    def test_by_exit_reason(self):
        result = analyze_by_exit_reason(self.db_path)
        tp = next(r for r in result if r["exit_reason"] == "take_profit")
        self.assertEqual(tp["count"], 2)
        sl = next(r for r in result if r["exit_reason"] == "stop_loss")
        self.assertEqual(sl["count"], 1)


if __name__ == "__main__":
    unittest.main()
