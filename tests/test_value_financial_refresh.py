import tempfile
import unittest
from datetime import datetime

from data.contracts import FinancialFactor
from data.store.sqlite_store import StockStore
from scripts.refresh_value_financials import (
    default_period_candidates,
    is_informative_factor,
    normalize_factor,
    normalize_period,
    upsert_financial_factor,
)


class ValueFinancialRefreshTests(unittest.TestCase):
    def test_default_period_candidates(self):
        self.assertEqual(default_period_candidates(datetime(2026, 3, 1))[0], ("2025", 4))
        self.assertEqual(default_period_candidates(datetime(2026, 7, 1))[0], ("2026", 1))
        self.assertEqual(default_period_candidates(datetime(2026, 9, 1))[0], ("2026", 2))
        self.assertEqual(default_period_candidates(datetime(2026, 11, 1))[0], ("2026", 3))

    def test_informative_factor(self):
        self.assertFalse(is_informative_factor(FinancialFactor(code="000001", period="2026Q1")))
        self.assertTrue(is_informative_factor(FinancialFactor(code="000001", period="2026Q1", roe=12.3)))

    def test_normalize_period(self):
        self.assertEqual(normalize_period("20260331"), "2026Q1")
        self.assertEqual(normalize_period("2026-06-30"), "2026Q2")
        self.assertEqual(normalize_period("20260930"), "2026Q3")
        self.assertEqual(normalize_period("20261231"), "2026A")
        self.assertEqual(normalize_period("2026Q1"), "2026Q1")

    def test_normalize_factor(self):
        factor = normalize_factor(FinancialFactor(code="938", period="20260331", roe=5.2, source="unit"))
        self.assertEqual(factor.code, "000938")
        self.assertEqual(factor.period, "2026Q1")
        self.assertEqual(factor.roe, 5.2)

    def test_upsert_financial_factor(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = StockStore(db_path=f.name)
            factor = FinancialFactor(
                code="002594",
                period="2026Q1",
                roe=18.2,
                gross_margin=21.5,
                net_margin=6.8,
                revenue_yoy=12.0,
                profit_yoy=15.5,
                debt_ratio=68.0,
                source="unit",
            )
            upsert_financial_factor(factor, store=store)
            updated = FinancialFactor(
                code="002594",
                period="2026Q1",
                roe=19.0,
                gross_margin=22.0,
                source="unit",
            )
            upsert_financial_factor(updated, store=store)
            conn = store._get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM financial_factors WHERE code='002594' AND period='2026Q1' AND source='unit'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row["roe"], 19.0)
            self.assertEqual(row["gross_margin"], 22.0)


if __name__ == "__main__":
    unittest.main()
