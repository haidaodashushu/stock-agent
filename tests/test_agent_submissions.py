from __future__ import annotations

import tempfile
import sqlite3
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
from data.trading_decision_repository import _entry_theses, _previous_decision_context


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
            prompt_version="abc123",
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
        self.assertEqual(ready.existing["prompt_version"], "abc123")
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

    def test_previous_decision_and_executed_entry_reason_are_queryable(self) -> None:
        claim = claim_submission(
            store=self.store, task="trading", mode="simulated",
            as_of="2026-08-31 10:00:00", stage="1000",
            provider="codex-cli", model="gpt-test", prompt_version="prompt123",
            decision={
                "signals": [{
                    "code": "000001", "action": "hold", "confidence": "medium",
                    "reason": "趋势保持", "risk": "跌破平台",
                }],
                "report": {"focus": ["验证平台承接"], "risk": "市场波动"},
            },
        )
        complete_submission(
            store=self.store, key=claim.submission_key, result={}, report="ok",
        )
        with self.store._get_conn() as conn:
            conn.execute(
                """INSERT INTO orders
                   (order_id,code,name,direction,volume,status,reason,created_at)
                   VALUES ('o1','000001','测试','buy',100,'filled','首次突破平台','2026-08-31 09:35:00')"""
            )
            conn.commit()
            by_code, previous_round = _previous_decision_context(
                conn, "simulated", "2026-08-31 10:30:00",
            )
            entries = _entry_theses(conn, "simulated")

        self.assertEqual(by_code["000001"]["reason"], "趋势保持")
        self.assertEqual(by_code["000001"]["prompt_version"], "prompt123")
        self.assertEqual(previous_round["focus"], ["验证平台承接"])
        self.assertEqual(entries["000001"]["original"]["reason"], "首次突破平台")

    def test_entry_thesis_resets_after_a_full_exit_and_reentry(self) -> None:
        with self.store._get_conn() as conn:
            conn.executemany(
                """INSERT INTO orders
                   (order_id,code,name,direction,volume,status,reason,created_at)
                   VALUES (?,?,?,?,?,'filled',?,?)""",
                [
                    ("o1", "000001", "测试", "buy", 100, "旧周期买入", "2026-08-30 09:35:00"),
                    ("o2", "000001", "测试", "sell", 100, "旧周期退出", "2026-08-30 14:00:00"),
                    ("o3", "000001", "测试", "buy", 200, "新周期买入", "2026-08-31 09:40:00"),
                    ("o4", "000001", "测试", "buy", 100, "新周期加仓", "2026-08-31 10:10:00"),
                ],
            )
            conn.commit()
            entries = _entry_theses(conn, "simulated")

        self.assertEqual(entries["000001"]["original"]["reason"], "新周期买入")
        self.assertEqual(entries["000001"]["latest"]["reason"], "新周期加仓")

    def test_legacy_submission_table_is_migrated_with_prompt_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE agent_decision_submissions (
                    submission_key TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT '',
                    as_of TEXT NOT NULL,
                    stage TEXT DEFAULT '',
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT '{}',
                    result TEXT NOT NULL DEFAULT '{}',
                    report TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                )"""
            )
            conn.commit()
            conn.close()

            store = StockStore(str(path))
            with store._get_conn() as migrated:
                columns = {
                    row[1]
                    for row in migrated.execute(
                        "PRAGMA table_info(agent_decision_submissions)"
                    )
                }
            self.assertIn("prompt_version", columns)


if __name__ == "__main__":
    unittest.main()
