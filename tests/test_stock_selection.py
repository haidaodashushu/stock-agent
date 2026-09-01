import json
import tempfile
import unittest
from pathlib import Path

from data.stock_selection_repository import (
    get_candidate_evidence,
    get_selection_overview,
    stage_candidate_pool,
)
from data.store.sqlite_store import StockStore
from scripts.daily_screen import _save_screen_results
from scripts.execute_stock_selection import validate_selection


def candidate(code: str, score: float = 8.0) -> dict:
    return {
        "code": code,
        "name": f"测试{code}",
        "price": 10.0,
        "score": 6.0,
        "base_score": 6.0,
        "final_score": score,
        "enrichment_score": score - 6.0,
        "signal_type": "buy",
        "signal_tags": "多头排列|放量",
        "trend": "多头",
        "pct_change": 1.2,
        "vol_ratio": 1.5,
        "position_pct": 0.6,
        "zone": "中位",
        "route": "balanced",
        "theme_group": "人工智能",
        "buy_eligible": True,
        "risk_tags": "",
        "theme_bonus": 1.2,
        "logic_score": 0.0,
        "logic_available": False,
        "fundamental_score": 0.5,
        "fundamental_available": True,
        "fund_flow_score": 1.0,
        "sector_rotation_score": 0.3,
        "sector_rotation_tags": "轮动:人工智能:leader",
        "sector_context": {
            "membership_status": "available",
            "primary_industry": "软件开发",
            "concepts": ["人工智能", "软件"],
            "rotation_as_of": "2026-07-23 08:29:00",
            "rotation_score": 0.3,
            "alignment": "positive",
            "matches": [{"name": "人工智能", "stage": "leader"}],
            "tags": ["轮动:人工智能:leader"],
        },
        "corporate_action_penalty": 0.0,
        "theme_concentration_penalty": 0.0,
        "extra": json.dumps({"concepts": ["人工智能", "软件"]}, ensure_ascii=False),
    }


class StockSelectionRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.handle = tempfile.NamedTemporaryFile(suffix=".db")
        self.store = StockStore(self.handle.name)
        self.as_of = "2026-07-23 08:30:00.123456"
        stage_candidate_pool(
            [candidate("000001"), candidate("000002", 7.5)],
            run_date="2026-07-23",
            run_time="08:30:00",
            run_label="盘前选股",
            target="today",
            expected_daily_date="2026-07-22",
            generated_at=self.as_of,
            market_context={"leading_sectors": [{"name": "人工智能", "stage": "leader"}]},
            store=self.store,
        )

    def tearDown(self):
        self.handle.close()

    def test_overview_and_evidence_are_versioned_and_pool_scoped(self):
        db_path = Path(self.handle.name)
        overview = get_selection_overview(db_path=db_path)
        self.assertEqual(overview["as_of"], self.as_of)
        self.assertEqual(overview["required_evidence_codes"], ["000001", "000002"])
        self.assertEqual(overview["market"]["leading_sectors"][0]["name"], "人工智能")

        evidence = get_candidate_evidence(["000002"], self.as_of, db_path=db_path)
        self.assertEqual(evidence["stocks"][0]["code"], "000002")
        self.assertEqual(evidence["stocks"][0]["quantitative"]["final_score"], 7.5)
        self.assertEqual(evidence["stocks"][0]["theme"]["concepts"], ["人工智能", "软件"])
        self.assertEqual(evidence["stocks"][0]["sector"]["alignment"], "positive")

        with self.assertRaisesRegex(ValueError, "outside current candidate pool"):
            get_candidate_evidence(["600000"], self.as_of, db_path=db_path)
        with self.assertRaisesRegex(ValueError, "selection pool changed"):
            get_candidate_evidence(["000001"], "stale", db_path=db_path)

    def test_hundred_stock_overview_stays_a_compact_complete_index(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            store = StockStore(handle.name)
            rows = [candidate(f"{index:06d}", 8.0 - index / 1000) for index in range(100)]
            stage_candidate_pool(
                rows,
                run_date="2026-07-24",
                run_time="08:30:00",
                run_label="容量测试",
                target="today",
                expected_daily_date="2026-07-23",
                generated_at="2026-07-24 08:30:00.123456",
                store=store,
            )
            overview = get_selection_overview(db_path=Path(handle.name))
            encoded = json.dumps(overview, ensure_ascii=False).encode()
            self.assertEqual(len(overview["required_evidence_codes"]), 100)
            self.assertLess(len(encoded), 50_000)
            self.assertNotIn("setup_triggers", overview["candidates"][0])

    def test_evidence_reads_article_body_and_matches_global_policy(self):
        conn = self.store._get_conn()
        try:
            conn.executemany(
                """INSERT INTO news_events
                   (code,name,title,content,source,publish_at,category,
                    sentiment,score,risk_level,tags,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        "000002", "测试二", "公司发布公告",
                        "公告正文显示公司中标重大合同。", "交易所",
                        "2026-07-23 08:00:00", "news", "neutral", 0, "low", "[]",
                        "2026-07-23 08:01:00",
                    ),
                    (
                        "POLICY", "政策热点", "工信部发布通知",
                        "通知提出支持人工智能产业和算力基础设施建设。", "政府网站",
                        "2026-07-23 07:30:00", "policy_hotspot", "neutral", 0, "low", "[]",
                        "2026-07-23 07:31:00",
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        evidence = get_candidate_evidence(
            ["000002"], self.as_of, db_path=Path(self.handle.name),
        )

        news = evidence["stocks"][0]["recent_news"][0]
        self.assertEqual(news["analysis_basis"], "title_content")
        self.assertIn("重大合同", news["summary"])
        self.assertEqual(news["source"], "交易所")
        self.assertEqual(
            evidence["stocks"][0]["matched_policy_evidence"][0]["matched_topics"],
            ["人工智能"],
        )
        self.assertIn("人工智能", evidence["policy_context"][0]["summary"])

    def test_ai_publish_is_atomic_and_records_selection_metadata(self):
        selection = {
            "rank": 1,
            "code": "000002",
            "confidence": "strong",
            "reason": "趋势与板块证据同向",
            "risk": "放量持续性待验证",
        }
        row = candidate("000002", 7.5)
        saved = _save_screen_results(
            self.store,
            [row],
            run_date="2026-07-23",
            run_time="08:30:00",
            run_label="盘前选股",
            target="today",
            generated_at="2026-07-23 08:31:00",
            ai_selections={"000002": selection},
            expected_as_of=self.as_of,
        )
        self.assertEqual(saved, 1)
        conn = self.store._get_conn()
        state = conn.execute("SELECT status,selected_count FROM screen_candidate_state").fetchone()
        final = conn.execute("SELECT code,extra FROM screen_records").fetchone()
        conn.close()
        self.assertEqual((state["status"], state["selected_count"]), ("selected", 1))
        self.assertEqual(final["code"], "000002")
        self.assertEqual(json.loads(final["extra"])["ai_selection"]["reason"], "趋势与板块证据同向")

    def test_stale_ai_response_cannot_replace_existing_candidates(self):
        conn = self.store._get_conn()
        conn.execute(
            """INSERT INTO screen_records
               (run_date,run_time,code,name,price,score,signal_type)
               VALUES ('2026-07-23','08:00:00','600000','原结果',10,7,'watch')"""
        )
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            _save_screen_results(
                self.store,
                [candidate("000001")],
                run_date="2026-07-23",
                run_time="08:30:00",
                run_label="盘前选股",
                target="today",
                generated_at="2026-07-23 08:31:00",
                expected_as_of="stale",
            )
        conn = self.store._get_conn()
        codes = [row["code"] for row in conn.execute("SELECT code FROM screen_records")]
        conn.close()
        self.assertEqual(codes, ["600000"])


class StockSelectionValidationTests(unittest.TestCase):
    def test_valid_selection_keeps_ai_order_without_trade_actions(self):
        payload = {
            "as_of": "v1",
            "reviewed_codes": ["000001", "000002"],
            "market_view": {"regime": "neutral", "summary": "震荡"},
            "selections": [
                {"code": "2", "entry_route": "strong_continuation", "confidence": "strong", "reason": "证据A", "risk": "风险A"},
                {"code": "1", "entry_route": "early_start", "confidence": "medium", "reason": "证据B", "risk": "风险B"},
            ],
            "report": {"focus": ["观察量能"], "risk": "市场波动"},
        }
        result = validate_selection(payload, expected_as_of="v1", allowed_codes={"000001", "000002"})
        self.assertEqual([row["code"] for row in result["selections"]], ["000002", "000001"])
        self.assertFalse(any("action" in row for row in result["selections"]))

    def test_out_of_pool_or_duplicate_selection_is_rejected(self):
        base = {
            "as_of": "v1",
            "reviewed_codes": ["000001"],
            "market_view": {"regime": "weak"},
            "report": {},
        }
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_selection(
                base | {"selections": [{"code": "600000", "entry_route": "early_start", "reason": "x", "risk": "y"}]},
                expected_as_of="v1",
                allowed_codes={"000001"},
            )
        with self.assertRaisesRegex(ValueError, "duplicated"):
            validate_selection(
                (base | {"reviewed_codes": ["000001"]}) | {"selections": [
                    {"code": "000001", "entry_route": "early_start", "reason": "x", "risk": "y"},
                    {"code": "000001", "entry_route": "early_start", "reason": "x", "risk": "y"},
                ]},
                expected_as_of="v1",
                allowed_codes={"000001"},
            )

    def test_incomplete_review_cannot_publish_an_empty_selection(self):
        with self.assertRaisesRegex(ValueError, "complete candidate pool"):
            validate_selection(
                {
                    "as_of": "v1",
                    "reviewed_codes": ["000001"],
                    "market_view": {"regime": "neutral"},
                    "selections": [],
                    "report": {},
                },
                expected_as_of="v1",
                allowed_codes={"000001", "000002"},
            )


if __name__ == "__main__":
    unittest.main()
