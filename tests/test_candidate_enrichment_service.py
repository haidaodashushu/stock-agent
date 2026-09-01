import tempfile
import unittest
from datetime import datetime

from data.contracts import FinancialFactor, NewsEvent
from data.logic_change import get_logic_change_evidence
from data.services.candidate_enrichment_service import CandidateEnrichmentService
from data.store.sqlite_store import StockStore


class _Adapter:
    def query_stock_profiles(self, codes):
        return [
            {
                "code": code,
                "name": f"测试{code}",
                "concepts": ["人工智能"] if code == "000001" else ["储能"],
                "industries": ["软件开发"] if code == "000001" else ["电力设备"],
            }
            for code in codes
        ]

    def search_news(self, query, limit=20):
        if "000001" not in query:
            return []
        return [
            NewsEvent(
                code="000001",
                name="测试股票",
                title="测试股票中标重大项目",
                publish_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                score=2.0,
                sentiment="positive",
                tags=["订单"],
            ),
        ]

    def query_financials(self, query, limit=20):
        return [
            FinancialFactor(
                code="000002",
                period="2026Q1",
                revenue_yoy=35,
                profit_yoy=60,
                source="test",
            )
        ]


class CandidateEnrichmentServiceTests(unittest.TestCase):
    def test_refresh_is_bounded_to_requested_codes_and_persists_evidence(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            store = StockStore(handle.name)
            conn = store._get_conn()
            conn.executemany(
                "INSERT INTO stocks(code,name,is_active) VALUES (?,?,1)",
                [("000001", "测试一"), ("000002", "测试二")],
            )
            conn.commit()
            conn.close()

            result = CandidateEnrichmentService(
                store=store,
                adapter=_Adapter(),
                batch_size=10,
            ).refresh(["000001", "000002"])

            self.assertEqual(result["batches"], 1)
            self.assertEqual(result["events_seen"], 1)
            self.assertEqual(result["financials_seen"], 1)
            self.assertEqual(result["profiles_seen"], 2)
            conn = store._get_conn()
            event_codes = {
                row["code"]
                for row in conn.execute("SELECT DISTINCT code FROM news_events")
            }
            financial_codes = {
                row["code"]
                for row in conn.execute("SELECT DISTINCT code FROM financial_factors")
            }
            concepts = {
                row["name"]: row["stocks"]
                for row in conn.execute("SELECT name,stocks FROM concepts")
            }
            current_name = conn.execute(
                "SELECT name FROM stocks WHERE code='000001'"
            ).fetchone()["name"]
            normalized_memberships = {
                (row["code"], row["sector_name"], row["sector_type"])
                for row in conn.execute(
                    "SELECT code,sector_name,sector_type FROM stock_sector_membership"
                )
            }
            conn.close()
            self.assertEqual(event_codes, {"000001"})
            self.assertEqual(financial_codes, {"000002"})
            self.assertIn("000001", concepts["人工智能"])
            self.assertEqual(current_name, "测试000001")
            self.assertIn(("000001", "人工智能", "concept"), normalized_memberships)
            self.assertIn(("000001", "软件开发", "industry"), normalized_memberships)

            logic = get_logic_change_evidence(["000001"], store=store)
            self.assertEqual(logic["000001"].level, "strong")

    def test_compact_historical_date_does_not_remain_recent_forever(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            store = StockStore(handle.name)
            conn = store._get_conn()
            conn.execute(
                """INSERT INTO news_events
                   (code,title,publish_at,category,sentiment,score)
                   VALUES ('000001','旧事件','20200101','news','positive',3)"""
            )
            conn.commit()
            conn.close()

            evidence = get_logic_change_evidence(["000001"], store=store)
            self.assertEqual(evidence, {})


if __name__ == "__main__":
    unittest.main()
