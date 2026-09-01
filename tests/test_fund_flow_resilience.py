import unittest
import tempfile
from unittest.mock import patch

from data.adapters.iwencai_adapter import IwenCaiAdapter
from data.contracts import FundFlow
from data.services.fund_flow_service import FundFlowService
from data.trading_state import _fund_flows
from data.store.sqlite_store import StockStore


def _raw_flow(code: str, main_net: float) -> dict:
    return {
        "股票代码": code,
        "主力资金流向": main_net,
        "dde大单净额": main_net / 2,
        "小单净买入额": -main_net / 3,
        "资金流入": 10_000_000,
        "资金流出": 8_000_000,
    }


class IwenCaiFundFlowResilienceTests(unittest.TestCase):
    def test_failed_batch_does_not_discard_successful_batch(self):
        adapter = IwenCaiAdapter(
            api_key="test-key",
            retries=0,
            fill_missing=False,
            batch_size=2,
        )
        with patch.object(
            adapter,
            "_call_api",
            side_effect=[
                RuntimeError("temporary timeout"),
                {"datas": [_raw_flow("000003", 30), _raw_flow("000004", 40)]},
            ],
        ):
            flows = adapter.get_fund_flow(["000001", "000002", "000003", "000004"])

        self.assertEqual([flow.code for flow in flows], ["000003", "000004"])
        self.assertEqual(
            adapter.last_code_errors,
            {"000001": "temporary timeout", "000002": "temporary timeout"},
        )

    def test_missing_rows_are_reported_per_code(self):
        adapter = IwenCaiAdapter(
            api_key="test-key",
            retries=0,
            fill_missing=False,
            batch_size=5,
        )
        with patch.object(
            adapter,
            "_call_api",
            return_value={"datas": [_raw_flow("000001", 10)]},
        ):
            flows = adapter.get_fund_flow(["000001", "000002"])

        self.assertEqual([flow.code for flow in flows], ["000001"])
        self.assertEqual(adapter.last_code_errors["000002"], "问财未返回该标的资金流")

    def test_service_preserves_structured_partial_errors(self):
        adapter = IwenCaiAdapter(
            api_key="test-key",
            retries=0,
            fill_missing=False,
            batch_size=2,
        )
        service = FundFlowService()
        service.register("iwencai", adapter)
        with patch.object(
            adapter,
            "_call_api",
            side_effect=[
                RuntimeError("temporary timeout"),
                {"datas": [_raw_flow("000003", 30)]},
            ],
        ):
            flows = service.get_fund_flow(["000001", "000002", "000003"])

        self.assertEqual([flow.code for flow in flows], ["000003"])
        self.assertEqual(
            service.last_code_errors,
            {
                "000001": "iwencai: temporary timeout",
                "000002": "iwencai: temporary timeout",
            },
        )

    def test_fallback_provider_clears_errors_for_recovered_codes(self):
        class EmptyProvider:
            name = "primary"
            last_code_errors = {"000001": "timeout"}

            def get_fund_flow(self, codes, date=""):
                return []

        class WorkingProvider:
            name = "fallback"
            last_code_errors = {}

            def get_fund_flow(self, codes, date=""):
                return [
                    FundFlow(
                        code="000001",
                        date="20260715",
                        main_net_inflow=1,
                        source="fallback",
                    )
                ]

        service = FundFlowService()
        service.register("primary", EmptyProvider())
        service.register("fallback", WorkingProvider())

        flows = service.get_fund_flow(["000001"])

        self.assertEqual([flow.code for flow in flows], ["000001"])
        self.assertEqual(service.last_code_errors, {})

    def test_trading_state_exposes_availability_and_error(self):
        class PartialAdapter:
            name = "iwencai"

            def __init__(self):
                self.last_code_errors = {"000002": "read timeout"}

            def get_fund_flow(self, codes, date=""):
                return [
                    FundFlow(
                        code="000001",
                        date="20260715",
                        main_net_inflow=12_000_000,
                        source="iwencai",
                    )
                ]

        adapter = PartialAdapter()
        with patch("data.adapters.iwencai_adapter.IwenCaiAdapter", return_value=adapter):
            result = _fund_flows(["000001", "000002"])

        self.assertEqual(result["000001"]["status"], "available")
        self.assertIsNone(result["000001"]["error"])
        self.assertEqual(result["000002"]["status"], "unavailable")
        self.assertEqual(result["000002"]["error"], "iwencai: read timeout")
        self.assertTrue(result["000001"]["observed_at"])

    def test_retry_recovers_only_missing_codes(self):
        first = FundFlow(
            code="000001", date="20260729", main_net_inflow=1, source="iwencai",
        )
        recovered = FundFlow(
            code="000002", date="20260729", main_net_inflow=2, source="iwencai",
        )
        with patch(
            "data.trading_state._query_fund_flows",
            side_effect=[
                ({"000001": (first, "主力净入1")}, {"000002": "timeout"}),
                ({"000002": (recovered, "主力净入2")}, {}),
            ],
        ) as query:
            result = _fund_flows(["000001", "000002"], retry_missing=True)

        self.assertEqual(query.call_count, 2)
        self.assertEqual(result["000001"]["status"], "available")
        self.assertEqual(result["000002"]["status"], "available")
        self.assertIsNone(result["000002"]["error"])

    def test_recent_success_is_used_as_explicit_cached_fallback(self):
        flow = FundFlow(
            code="000001", date="20260729", main_net_inflow=12_000_000,
            source="iwencai",
        )
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            with patch(
                "data.trading_state._query_fund_flows",
                return_value=({"000001": (flow, "主力净入1200万")}, {}),
            ):
                fresh = _fund_flows(["000001"], store=store)
            with patch(
                "data.trading_state._query_fund_flows",
                return_value=({}, {"000001": "provider timeout"}),
            ):
                cached = _fund_flows(["000001"], store=store)

        self.assertEqual(fresh["000001"]["status"], "available")
        self.assertEqual(cached["000001"]["status"], "cached")
        self.assertEqual(cached["000001"]["freshness"], "cached")
        self.assertEqual(cached["000001"]["error"], "provider timeout")
        self.assertEqual(cached["000001"]["detail"]["main_net_inflow"], 12_000_000)
        self.assertGreaterEqual(cached["000001"]["cache_age_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
