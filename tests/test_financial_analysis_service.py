import tempfile
import unittest
from unittest.mock import patch

from data.contracts import FinancialFactor
from data.services.finance_service import FinanceService
from data.services.financial_analysis_service import (
    FinancialAnalysisService,
    normalize_codes,
)
from data.store.sqlite_store import StockStore
from data.value_investing import ValueSnapshot


class _EmptyIwenCai:
    def stock_financials(self, code):
        return []


class _BaoStockWithData:
    def get_financial_factors(self, code, year, quarter):
        return [
            FinancialFactor(
                code=code,
                period=f"{year}Q{quarter}",
                roe=12.5,
                gross_margin=34.0,
                source="baostock-unit",
            )
        ]


class _FinanceStub:
    def refresh_latest_factor(self, code):
        return FinancialFactor(code=code, period="2026Q1", roe=18.0), []


class FinancialAnalysisServiceTests(unittest.TestCase):
    def test_normalize_codes_accepts_common_separators_and_deduplicates(self):
        self.assertEqual(
            normalize_codes("977, 603501\n000977；938"),
            ["000977", "603501", "000938"],
        )

    def test_finance_service_falls_back_to_baostock_and_persists(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            service = FinanceService(
                store=store,
                iwencai=_EmptyIwenCai(),
                baostock=_BaoStockWithData(),
            )
            factor, warnings = service.refresh_latest_factor(
                "000977",
                periods=[("2026", 1)],
            )

            self.assertIsNotNone(factor)
            self.assertEqual(factor.source, "baostock-unit")
            self.assertIn("问财未返回有效财务数据", warnings)
            self.assertEqual(service.get_latest_factors("000977").roe, 12.5)

    @patch("data.services.financial_analysis_service.mark_value_freshness")
    @patch("data.services.financial_analysis_service.upsert_value_snapshot")
    @patch("data.services.financial_analysis_service.build_value_snapshot")
    def test_analysis_passes_company_type_to_ai_without_trading_side_effects(
        self,
        build_snapshot,
        upsert_snapshot,
        mark_freshness,
    ):
        snapshot = ValueSnapshot(
            code="603501",
            name="豪威集团",
            as_of="2026-07-10",
            company_type="tech_growth",
            value_label="reasonable_high",
            watch_pool=False,
            business_quality_score=70,
            financial_quality_score=65,
            growth_credibility_score=76,
            valuation_margin_score=45,
            trap_risk_score=30,
            composite_score=63,
            confidence=0.8,
            facts={
                "quote": {"price": 99.0},
                "valuation": {"pe": 42.0, "source": "unit"},
                "financial": {"period": "2026Q1", "roe": 18.0, "source": "unit"},
                "data_freshness": {"as_of": "2026-07-10"},
            },
            rule_summary="科技成长口径",
        )
        build_snapshot.return_value = snapshot
        ai_inputs = []

        def ai_runner(items):
            ai_inputs.extend(items)
            return {
                "items": [
                    {
                        "code": "603501",
                        "conclusion": "按科技成长口径观察",
                        "financial_view": "增长质量待跟踪",
                        "valuation_view": "静态PE不单独定性",
                        "strengths": [],
                        "risks": [],
                        "focus": ["收入增长"],
                        "missing_data": [],
                    }
                ]
            }

        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            result = FinancialAnalysisService(
                store=store,
                finance=_FinanceStub(),
                ai_runner=ai_runner,
            ).analyze(["603501"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["scope"], "web_query_only")
        self.assertEqual(ai_inputs[0]["company_type"], "tech_growth")
        self.assertIn("静态PE", result["items"][0]["ai_advice"]["valuation_view"])
        upsert_snapshot.assert_called_once()
        self.assertGreaterEqual(mark_freshness.call_count, 3)

    def test_extracts_gateway_llm_task_json(self):
        payload = {"details": {"json": {"items": [{"code": "000001"}]}}}
        parsed = FinancialAnalysisService._extract_llm_json(payload)
        self.assertEqual(parsed["items"][0]["code"], "000001")


if __name__ == "__main__":
    unittest.main()
