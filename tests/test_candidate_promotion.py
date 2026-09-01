import tempfile
import unittest
import json
from datetime import datetime, timedelta
from pathlib import Path

from data.candidate_board import refresh_candidate_board
from data.candidate_promotion import (
    apply_promotion_decision,
    build_promotion_snapshot,
    load_active_promotions,
    promotion_health,
    record_promotion_failure,
)
from data.intraday_radar import save_radar_result
from data.store.sqlite_store import StockStore


class CandidatePromotionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = StockStore(str(Path(self.directory.name) / "promotion.db"))
        self.now = datetime(2026, 8, 27, 10, 20)
        save_radar_result(
            self.store,
            as_of=self.now.strftime("%Y-%m-%d %H:%M:%S"),
            pool_size=100,
            quote_count=100,
            prefiltered_count=10,
            candidates=[{
                "code": "002371", "name": "北方华创",
                "theme_group": "半导体与国产替代", "score": 8.5,
                "price": 100.0, "change_pct": 4.0,
                "triggers": ["站上VWAP", "成交速度放大"], "risk_tags": [],
                "evidence": {
                    "source": "strategic_pool_intraday_radar",
                    "radar_actionable": True,
                    "confirmation_families": ["price", "volume", "intraday", "sector"],
                    "setup": {"available": True, "position_60d_pct": 72},
                    "metrics": {"volume_pace_20d": 1.8},
                    "intraday": {"above_vwap": True},
                    "fund_flow": {"status": "available"},
                    "sector": {"alignment": "positive"},
                },
            }],
            market_context={
                "indices": {
                    "sh000001": {"name": "上证指数", "change_pct": 0.8},
                    "sz399001": {"name": "深证成指", "change_pct": 0.9},
                    "sh000300": {"name": "沪深300", "change_pct": 0.7},
                },
            },
        )

    def tearDown(self):
        self.directory.cleanup()

    def _payload(self, decision="promote"):
        return {
            "reviewed_codes": ["002371"],
            "decisions": [{
                "code": "002371", "name": "北方华创", "decision": decision,
                "entry_route": "strong_continuation" if decision == "promote" else "unclassified",
                "confidence": "strong", "reason": "平台突破且量价承接确认",
                "risk": "跌回平台或持续失守VWAP",
            }],
        }

    def test_promotion_is_account_free_and_valid_for_the_trade_date(self):
        snapshot = build_promotion_snapshot(self.store, now=self.now)
        self.assertEqual(snapshot["required_evidence_codes"], ["002371"])
        self.assertEqual(snapshot["market"]["regime"]["regime"], "strong")

        result = apply_promotion_decision(
            self._payload(), snapshot["as_of"], self.store, now=self.now,
        )
        self.assertEqual(result["promoted_count"], 1)
        promoted = load_active_promotions(
            self.store, trade_date="2026-08-27", now=self.now + timedelta(hours=3),
        )
        self.assertEqual(promoted[0]["entry_route"], "strong_continuation")
        self.assertEqual(promoted[0]["expires_at"], "2026-08-27 15:05:00")

        board = refresh_candidate_board(
            self.store, now=datetime(2026, 8, 27, 13, 20),
        )
        conn = self.store._get_conn()
        try:
            member = conn.execute(
                """SELECT buy_eligible,payload FROM candidate_board_members
                    WHERE as_of=? AND code='002371'""",
                (board["as_of"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertTrue(member["buy_eligible"])
        self.assertIn('"entry_route": "strong_continuation"', member["payload"])
        self.assertIn('"candidate_promotion"', member["payload"])

    def test_exact_per_stock_decision_is_required(self):
        snapshot = build_promotion_snapshot(self.store, now=self.now)
        with self.assertRaisesRegex(ValueError, "exactly one row"):
            apply_promotion_decision(
                {"reviewed_codes": ["002371"], "decisions": []},
                snapshot["as_of"], self.store, now=self.now,
            )

    def test_failure_is_persisted_and_exposed_by_health_check(self):
        snapshot = build_promotion_snapshot(self.store, now=self.now)
        missing = promotion_health(self.store, trade_date="2026-08-27")
        self.assertEqual(missing["status"], "failed")
        self.assertIn("没有留下评估记录", missing["message"])

        result = record_promotion_failure(
            snapshot["as_of"], "MCP preflight: missing dependency",
            self.store, now=self.now,
        )
        self.assertEqual(result["candidate_count"], 1)
        health = promotion_health(self.store, trade_date="2026-08-27")
        self.assertFalse(health["healthy"])
        self.assertEqual(health["status"], "failed")
        self.assertIn("missing dependency", health["error"])

        conn = self.store._get_conn()
        try:
            row = conn.execute(
                "SELECT status,error FROM candidate_promotion_runs WHERE as_of=?",
                (snapshot["as_of"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "failed")

    def test_successful_run_is_reported_as_healthy(self):
        snapshot = build_promotion_snapshot(self.store, now=self.now)
        apply_promotion_decision(
            self._payload("watch"), snapshot["as_of"], self.store, now=self.now,
        )
        health = promotion_health(self.store, trade_date="2026-08-27")
        self.assertTrue(health["healthy"])
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["candidate_count"], 1)

    def test_promoted_stock_is_not_re_evaluated_that_day(self):
        snapshot = build_promotion_snapshot(self.store, now=self.now)
        apply_promotion_decision(
            self._payload(), snapshot["as_of"], self.store, now=self.now,
        )
        next_snapshot = build_promotion_snapshot(
            self.store, now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(next_snapshot["required_evidence_codes"], [])

    def test_promotion_replaces_weakest_daily_candidate_only_at_capacity(self):
        conn = self.store._get_conn()
        try:
            conn.executemany(
                """INSERT INTO screen_records
                   (run_date,run_time,code,name,price,score,signal_type,strategies,extra)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        "2026-08-27", "08:30:00", f"600{index:03d}",
                        f"候选{index}", 10, 20-index, "buy", "每日最终选股",
                        json.dumps({
                            "selector": {"entry_route": "early_start"},
                            "ai_selection": {"rank": index},
                        }, ensure_ascii=False),
                    )
                    for index in range(1, 16)
                ],
            )
            conn.commit()
        finally:
            conn.close()
        before = refresh_candidate_board(self.store, now=self.now)
        self.assertEqual(before["active"], 15)

        snapshot = build_promotion_snapshot(self.store, now=self.now)
        apply_promotion_decision(
            self._payload(), snapshot["as_of"], self.store, now=self.now,
        )
        after = refresh_candidate_board(
            self.store, now=self.now + timedelta(minutes=1),
        )
        conn = self.store._get_conn()
        try:
            members = conn.execute(
                """SELECT code,replaced_code FROM candidate_board_members
                     WHERE as_of=? AND state='active' ORDER BY rank""",
                (after["as_of"],),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(members), 15)
        by_code = {row["code"]: row for row in members}
        self.assertIn("002371", by_code)
        self.assertNotIn("600015", by_code)
        self.assertEqual(by_code["002371"]["replaced_code"], "600015")


if __name__ == "__main__":
    unittest.main()
