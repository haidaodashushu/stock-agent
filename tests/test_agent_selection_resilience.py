from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data.agent_submission_service import submit_stock_selection
from data.stock_selection_repository import stage_candidate_pool, update_selection_status
from data.store.sqlite_store import StockStore


class AgentSelectionResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StockStore(str(Path(self.tmp.name) / "stock.db"))
        self.as_of = "2026-08-31 22:45:02.654711"
        stage_candidate_pool(
            [{
                "code": "000001", "name": "测试", "price": 10,
                "score": 8, "final_score": 8, "signal_type": "buy",
                "route": "balanced", "entry_route": "strong_continuation",
            }],
            run_date="2026-09-01", run_time="22:45:02", run_label="夜间预选股",
            target="next-trading-day", expected_daily_date="2026-08-31",
            generated_at=self.as_of, store=self.store,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_non_ready_snapshot_is_rejected_before_submission_claim(self) -> None:
        update_selection_status(
            self.as_of, status="failed", error="legacy provider timeout", store=self.store,
        )
        response = submit_stock_selection(
            as_of=self.as_of,
            decision={
                "as_of": self.as_of,
                "reviewed_codes": ["000001"],
                "market_view": {"regime": "neutral"},
                "selections": [{
                    "code": "000001", "entry_route": "strong_continuation",
                    "reason": "量价确认", "risk": "持续性待验证",
                }],
                "report": {},
            },
            run_dir=Path(self.tmp.name) / "run", provider="codex-cli",
            model="gpt-test", store=self.store,
        )

        self.assertEqual(response["status"], "rejected")
        self.assertFalse(response["can_retry"])
        with self.store._get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_decision_submissions"
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
