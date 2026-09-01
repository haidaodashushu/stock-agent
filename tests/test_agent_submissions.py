from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data.agent_submissions import (
    agent_runtime_health,
    claim_submission,
    complete_submission,
    enqueue_message,
    get_submission,
)
from data.store.sqlite_store import StockStore


class AgentSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StockStore(str(Path(self.tmp.name) / "stock.db"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_claim_is_idempotent_and_ready_result_is_reused(self) -> None:
        first = claim_submission(
            store=self.store, task="trading", mode="simulated",
            as_of="2026-08-31 10:00:00", stage="1000",
            provider="codex-cli", model="gpt-test", decision={"signals": []},
        )
        self.assertEqual(first.state, "claimed")
        duplicate = claim_submission(
            store=self.store, task="trading", mode="simulated",
            as_of="2026-08-31 10:00:00", stage="1000",
            provider="codex-cli", model="gpt-test", decision={"signals": [{"different": True}]},
        )
        self.assertEqual(duplicate.state, "processing")
        complete_submission(
            store=self.store, key=first.submission_key,
            result={"executed": 1}, report="done",
        )
        ready = claim_submission(
            store=self.store, task="trading", mode="simulated",
            as_of="2026-08-31 10:00:00", stage="1000",
            provider="codex-cli", model="gpt-test", decision={},
        )
        self.assertEqual(ready.state, "ready")
        self.assertEqual(ready.existing["result"], {"executed": 1})
        self.assertEqual(
            get_submission(
                store=self.store, task="trading", mode="simulated",
                as_of="2026-08-31 10:00:00",
            )["report"],
            "done",
        )

    def test_outbox_deduplicates_same_submission_report(self) -> None:
        claim = claim_submission(
            store=self.store, task="selection", mode="", as_of="v1", stage="night",
            provider="codex-cli", model="gpt-test", decision={},
        )
        complete_submission(store=self.store, key=claim.submission_key, result={}, report="ok")
        for _ in range(2):
            enqueue_message(
                store=self.store, submission_key=claim.submission_key,
                message_type="text", content="hello",
            )
        with self.store._get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM agent_message_outbox").fetchone()[0]
        self.assertEqual(count, 1)

    def test_health_reports_latest_ready_submission(self) -> None:
        claim = claim_submission(
            store=self.store, task="promotion", mode="", as_of="v2", stage="intraday",
            provider="codex-cli", model="gpt-test", decision={},
        )
        complete_submission(store=self.store, key=claim.submission_key, result={}, report="ok")

        health = agent_runtime_health(store=self.store)

        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["latest"][0]["provider"], "codex-cli")


if __name__ == "__main__":
    unittest.main()
