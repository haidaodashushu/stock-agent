import json
import tempfile
import unittest

from data.store.sqlite_store import StockStore
from data.value_investing import (
    ValueSnapshot,
    ValueUniverseEntry,
    classify_company_type,
    get_due_value_universe,
    label_value,
    mark_value_freshness,
    score_value_facts,
    sync_value_universe,
    upsert_value_universe_entry,
    upsert_value_snapshot,
)


class ValueInvestingTests(unittest.TestCase):
    def test_classifies_tech_growth_from_concepts(self):
        company_type = classify_company_type(
            {"code": "000001", "name": "测试科技", "industry": ""},
            ["人工智能", "半导体"],
        )
        self.assertEqual(company_type, "tech_growth")

    def test_growth_stock_can_accept_higher_pe_with_evidence(self):
        facts = {
            "valuation": {"pe": 32, "pb": 4.2},
            "financial": {"roe": 18, "gross_margin": 46, "net_margin": 16, "revenue_yoy": 38, "profit_yoy": 45},
            "fundamental_llm": {
                "composite_score": 4.2,
                "confidence": 0.8,
                "future_demand_score": 4.2,
                "product_penetration_score": 4.0,
            },
            "technical": {"position_60d_pct": 35},
            "latest_news": [],
        }
        scores = score_value_facts("tech_growth", facts)
        self.assertGreaterEqual(scores["growth_credibility_score"], 70)
        self.assertGreater(scores["composite_score"], 60)

    def test_value_trap_risk_label(self):
        self.assertEqual(label_value(76, 70), "deep_value_trap_risk")

    def test_upsert_value_snapshot(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = StockStore(db_path=f.name)
            snapshot = ValueSnapshot(
                code="002594",
                name="比亚迪",
                as_of="2026-07-01",
                company_type="tech_growth",
                value_label="reasonable_low",
                watch_pool=True,
                business_quality_score=70,
                financial_quality_score=65,
                growth_credibility_score=78,
                valuation_margin_score=64,
                trap_risk_score=25,
                composite_score=68,
                confidence=0.8,
                facts={"valuation": {"pe": 26.7}},
                rule_summary="测试",
                ai_prompt_path="data/reports/value_ai/002594.json",
            )
            upsert_value_snapshot(snapshot, store=store)
            conn = store._get_conn()
            try:
                row = conn.execute("SELECT * FROM value_snapshots WHERE code='002594'").fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["value_label"], "reasonable_low")
            self.assertEqual(json.loads(row["facts"])["valuation"]["pe"], 26.7)

    def test_universe_keeps_strongest_tier_and_reasons(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = StockStore(db_path=f.name)
            upsert_value_universe_entry({
                "code": "002594",
                "name": "比亚迪",
                "tier": "candidate",
                "priority": 70,
                "reasons": ["最新盘前候选"],
                "sources": ["screen_records"],
            }, store=store)
            upsert_value_universe_entry({
                "code": "002594",
                "tier": "core",
                "priority": 100,
                "reasons": ["实盘建议单"],
                "sources": ["live_trade_intents"],
            }, store=store)

            conn = store._get_conn()
            try:
                row = conn.execute("SELECT * FROM value_universe WHERE code='002594'").fetchone()
            finally:
                conn.close()
            self.assertEqual(row["tier"], "core")
            self.assertEqual(row["priority"], 100)
            self.assertIn("最新盘前候选", json.loads(row["reasons"]))
            self.assertIn("实盘建议单", json.loads(row["reasons"]))

    def test_freshness_due_query(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = StockStore(db_path=f.name)
            sync_value_universe([
                ValueUniverseEntry(code="002594", name="比亚迪", tier="core", priority=100),
                ValueUniverseEntry(code="002138", name="顺络电子", tier="candidate", priority=70),
            ], store=store)
            mark_value_freshness("002594", "value_snapshot", status="ok", source="unit", store=store)

            due = get_due_value_universe(max_age_hours=24, limit=10, store=store)
            due_codes = {x.code for x in due}
            self.assertNotIn("002594", due_codes)
            self.assertIn("002138", due_codes)


if __name__ == "__main__":
    unittest.main()
