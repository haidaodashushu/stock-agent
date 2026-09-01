import json
import tempfile
import unittest

import pandas as pd

from data.fundamental_llm import (
    get_fundamental_llm_scores,
    score_from_mapping,
    upsert_fundamental_llm_score,
)
from data.store.sqlite_store import StockStore
from scripts.prepare_fundamental_llm_reports import extract_mda_text, infer_period, infer_report_type
from strategy.selector.technical_scoring import TechnicalScoringSelector


class FundamentalLLMTests(unittest.TestCase):
    def test_default_composite_and_positive_boost(self):
        score = score_from_mapping({
            "code": "600460",
            "industry_demand_score": 5,
            "future_demand_score": 4,
            "product_penetration_score": 4,
            "strategy_score": 2.5,
            "candor_score": 1,
        })
        self.assertGreaterEqual(score.composite_score, 4.0)
        self.assertGreater(score.boost, 0)
        self.assertIn("LLM", "|".join(score.tags))

    def test_future_down_and_low_candor_penalize(self):
        score = score_from_mapping({
            "code": "000001",
            "composite_score": 3.8,
            "future_demand_score": 1.5,
            "candor_score": 0,
        })
        self.assertLess(score.boost, 0)
        self.assertIn("未来需求转弱", score.tags)
        self.assertIn("坦诚度低", score.tags)

    def test_upsert_and_get_latest_score(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = StockStore(db_path=f.name)
            upsert_fundamental_llm_score({
                "code": "600460",
                "name": "士兰微",
                "period": "2024A",
                "report_date": "2025-04-20",
                "composite_score": 3.2,
                "source": "test",
                "model": "unit",
            }, store=store)
            upsert_fundamental_llm_score({
                "code": "600460",
                "name": "士兰微",
                "period": "2025H1",
                "report_date": "2025-08-20",
                "composite_score": 4.6,
                "summary": "行业需求上行",
                "source": "test",
                "model": "unit",
            }, store=store)

            scores = get_fundamental_llm_scores(["600460"], store=store)
            self.assertEqual(scores["600460"].period, "2025H1")
            self.assertEqual(scores["600460"].summary, "行业需求上行")

    def test_selector_adds_fundamental_extra(self):
        dates = pd.date_range("2026-01-01", periods=40, freq="D")
        close = [10 + i * 0.1 for i in range(40)]
        df = pd.DataFrame({
            "date": dates,
            "open": close,
            "close": close,
            "high": [x + 0.2 for x in close],
            "low": [x - 0.2 for x in close],
            "volume": [100000 + i * 1000 for i in range(40)],
            "name": ["测试股"] * 40,
        })
        fundamental = score_from_mapping({
            "code": "000001",
            "period": "2025A",
            "industry_demand_score": 5,
            "future_demand_score": 4,
            "strategy_score": 2.5,
            "candor_score": 1,
            "summary": "行业需求上行",
        })
        selector = TechnicalScoringSelector()
        row = selector._score_one("000001", df, fundamental=fundamental)

        self.assertIsNotNone(row)
        self.assertIn("LLM", row["signal_tags"])
        extra = json.loads(row["extra"])
        self.assertEqual(extra["fundamental_llm"]["period"], "2025A")
        self.assertGreater(extra["fundamental_llm"]["boost"], 0)

    def test_report_helpers(self):
        self.assertEqual(infer_period("杭州士兰微电子股份有限公司2025年年度报告"), "2025A")
        self.assertEqual(infer_period("某公司2025年半年度报告"), "2025H1")
        self.assertEqual(infer_report_type("某公司2025年半年度报告"), "semiannual")
        text = "前文\n管理层讨论与分析\n行业需求明显上行，订单增长。\n公司治理\n后文"
        self.assertIn("行业需求明显上行", extract_mda_text(text, 1000))
        self.assertNotIn("后文", extract_mda_text(text, 1000))


if __name__ == "__main__":
    unittest.main()
