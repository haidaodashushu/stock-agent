import tempfile
import unittest

from data.services.stock_sector_membership_service import (
    StockSectorMembershipService,
    load_stock_memberships,
    replace_stock_memberships,
)
from data.store.sqlite_store import StockStore


class _ProfileAdapter:
    def __init__(self):
        self.calls = 0

    def query_stock_profiles(self, codes):
        self.calls += 1
        return [
            {
                "code": code,
                "name": f"测试{code}",
                "industries": ["一级行业", "软件开发"],
                "concepts": ["人工智能", "数据要素"],
            }
            for code in codes
        ]


class StockSectorMembershipTests(unittest.TestCase):
    def test_provider_snapshot_replaces_stale_memberships(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            store = StockStore(handle.name)
            conn = store._get_conn()
            replace_stock_memberships(
                conn,
                "000001",
                industries=["旧行业"],
                concepts=["旧概念"],
                observed_at="2026-07-28 10:00:00",
            )
            replace_stock_memberships(
                conn,
                "000001",
                industries=["新行业"],
                concepts=["新概念"],
                observed_at="2026-07-29 10:00:00",
            )
            conn.commit()
            rows = {
                row["sector_name"]
                for row in conn.execute(
                    "SELECT sector_name FROM stock_sector_membership WHERE code='000001'"
                )
            }
            conn.close()

            self.assertEqual(rows, {"新行业", "新概念"})

    def test_daily_ensure_refreshes_once_and_loads_structured_memberships(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            store = StockStore(handle.name)
            conn = store._get_conn()
            conn.execute(
                "INSERT INTO stocks(code,name,is_active) VALUES ('000001','测试',1)"
            )
            conn.commit()
            conn.close()
            adapter = _ProfileAdapter()
            service = StockSectorMembershipService(store=store, adapter=adapter)

            first = service.ensure(["000001"])
            second = service.ensure(["000001"])
            facts = load_stock_memberships(store, ["000001"])["000001"]

            self.assertEqual(first["refreshed"], 1)
            self.assertEqual(second["refreshed"], 0)
            self.assertEqual(adapter.calls, 1)
            self.assertIn("软件开发", {row["sector_name"] for row in facts})
            self.assertIn("人工智能", {row["sector_name"] for row in facts})

    def test_batch_omission_is_retried_for_that_code_once(self):
        class OmitFromBatchAdapter:
            def __init__(self):
                self.calls = []

            def query_stock_profiles(self, codes):
                self.calls.append(list(codes))
                returned = codes[:1] if len(codes) > 1 else codes
                return [
                    {
                        "code": code,
                        "name": f"测试{code}",
                        "industries": ["软件开发"],
                        "concepts": ["人工智能"],
                    }
                    for code in returned
                ]

        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            store = StockStore(handle.name)
            conn = store._get_conn()
            conn.executemany(
                "INSERT INTO stocks(code,name,is_active) VALUES (?,?,1)",
                [("000001", "测试一"), ("000002", "测试二")],
            )
            conn.commit()
            conn.close()
            adapter = OmitFromBatchAdapter()
            result = StockSectorMembershipService(
                store=store, adapter=adapter,
            ).ensure(["000001", "000002"])

            self.assertEqual(result["refreshed"], 2)
            self.assertEqual(result["missing"], [])
            self.assertEqual(adapter.calls, [["000001", "000002"], ["000002"]])


if __name__ == "__main__":
    unittest.main()
