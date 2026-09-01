import tempfile
import unittest

from data.financial_scoring import get_financial_scores
from data.store.sqlite_store import StockStore


class FinancialScoringTests(unittest.TestCase):
    def test_latest_structured_financial_factor_produces_bounded_boost(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            store = StockStore(handle.name)
            conn = store._get_conn()
            conn.execute(
                """INSERT INTO financial_factors
                   (code,period,roe,revenue_yoy,profit_yoy,debt_ratio,source)
                   VALUES ('000001','2026Q1',18,35,60,40,'test')"""
            )
            conn.commit()
            conn.close()

            score = get_financial_scores(["000001"], store=store)["000001"]

            self.assertEqual(score.boost, 1.5)
            self.assertIn("利润高增", score.tags)
            self.assertIn("营收高增", score.tags)
            self.assertIn("ROE较好", score.tags)


if __name__ == "__main__":
    unittest.main()
