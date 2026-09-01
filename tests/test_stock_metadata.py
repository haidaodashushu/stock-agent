import tempfile
import unittest

from data.fifteen_five_pool import FIFTEEN_FIVE_STOCKS, get_all_concepts
from data.store.sqlite_store import StockStore
from scripts.sync_baostock_basic import stock_health, sync_concepts


class StockMetadataTests(unittest.TestCase):
    def test_sync_concepts_uses_the_versioned_theme_pool(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = StockStore(db_path=f.name)
            result = sync_concepts(store)
            self.assertEqual(result["concepts"], len(get_all_concepts()))
            self.assertEqual(result["theme_stocks"], len(FIFTEEN_FIVE_STOCKS))

            conn = store._get_conn()
            try:
                rows = conn.execute("SELECT name, stocks FROM concepts").fetchall()
            finally:
                conn.close()
            self.assertEqual(len(rows), len(get_all_concepts()))
            self.assertTrue(all(row["stocks"] for row in rows))

    def test_health_reports_missing_stock_metadata(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = StockStore(db_path=f.name)
            conn = store._get_conn()
            try:
                conn.execute(
                    """INSERT INTO daily_prices
                       (code,date,open,close,high,low,volume,amount,adjust_flag)
                       VALUES ('000001','2026-07-14',10,10,10,10,1,10,'qfq')"""
                )
                conn.commit()
            finally:
                conn.close()

            health = stock_health(store)
            self.assertEqual(health["daily_price_codes"], 1)
            self.assertEqual(health["daily_codes_missing_metadata"], 1)


if __name__ == "__main__":
    unittest.main()
