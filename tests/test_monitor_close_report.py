import tempfile
import unittest
from pathlib import Path

from data.store.sqlite_store import StockStore
from scripts.monitor_close import build_report, calculate_daily_return, previous_daily_equity


class MonitorCloseReportTests(unittest.TestCase):
    def test_daily_return_uses_previous_close_equity(self):
        result = calculate_daily_return(
            {"total_equity": 1184413.10},
            {"date": "2026-07-14", "total_equity": 1186374.50},
        )
        self.assertEqual(result["profit"], -1961.40)
        self.assertEqual(result["profit_pct"], -0.1653)
        self.assertEqual(result["previous_date"], "2026-07-14")

    def test_previous_equity_excludes_same_day_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StockStore(str(Path(tmp) / "stock.db"))
            conn = store._get_conn()
            conn.executemany(
                """INSERT INTO daily_equity
                (date, total_equity, available_cash, market_value, total_profit)
                VALUES (?, ?, 0, 0, 0)""",
                [("2026-07-14", 1001000), ("2026-07-15", 1002000)],
            )
            conn.commit()
            conn.close()
            previous = previous_daily_equity(store, "2026-07-15")
        self.assertEqual(previous, {"date": "2026-07-14", "total_equity": 1001000.0})

    def test_report_shows_daily_profit_and_uses_it_for_tone(self):
        summary = {
            "total_equity": 1184413.10,
            "available_cash": 940818.10,
            "position_market_value": 243595.00,
            "position_count": 4,
            "total_profit": 184413.10,
            "total_profit_pct": 18.44,
        }
        daily_return = {
            "profit": -1961.40,
            "profit_pct": -0.1653,
            "previous_date": "2026-07-14",
        }
        report = build_report("2026-07-15", summary, [], [], [], [], daily_return)
        self.assertEqual(report["tone"], "warning")
        self.assertIn("今日收益 -1,961 (-0.17%)", report["summary"])
        self.assertEqual(report["account"]["daily_profit"], -1961.40)
        self.assertEqual(report["account"]["daily_profit_basis_date"], "2026-07-14")


if __name__ == "__main__":
    unittest.main()
