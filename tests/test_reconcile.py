import json
import os
import tempfile
import unittest

from data.store.sqlite_store import StockStore
from account.reconcile import load_resolved_issue_order_ids, reconcile


class ReconcileTests(unittest.TestCase):
    def make_store(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
        return StockStore(db_path=tmp.name)

    def test_rebuilds_positions_and_cash_from_orders(self):
        store = self.make_store()
        conn = store._get_conn()
        conn.execute("""INSERT INTO orders
            (code,name,direction,price,volume,amount,commission,tax,created_at)
            VALUES ('600000','测试股','buy',10,1000,10000,3,0,'2026-06-16 09:30:00')""")
        conn.execute("""INSERT INTO orders
            (code,name,direction,price,volume,amount,commission,tax,created_at)
            VALUES ('600000','测试股','sell',11,300,3300,0.99,3.3,'2026-06-17 10:00:00')""")
        conn.commit(); conn.close()

        result = reconcile(store, apply=True, live_prices={"600000": 12.0})
        self.assertEqual(result.order_count, 2)
        self.assertEqual(result.cash, 993292.71)
        self.assertEqual(result.positions[0]["volume"], 700)
        self.assertEqual(result.positions[0]["market_value"], 8400)
        self.assertFalse(any(i.level == "error" for i in result.issues))

    def test_flags_t1_violation(self):
        store = self.make_store()
        conn = store._get_conn()
        conn.execute("""INSERT INTO orders
            (code,name,direction,price,volume,amount,created_at)
            VALUES ('600000','测试股','buy',10,1000,10000,'2026-06-17 09:30:00')""")
        conn.execute("""INSERT INTO orders
            (code,name,direction,price,volume,amount,created_at)
            VALUES ('600000','测试股','sell',11,100,1100,'2026-06-17 10:00:00')""")
        conn.commit(); conn.close()

        result = reconcile(store, live_prices={"600000": 10.0})
        self.assertTrue(any("T+1" in i.message for i in result.issues))

    def test_flags_forbidden_board_buy(self):
        store = self.make_store()
        conn = store._get_conn()
        conn.execute("""INSERT INTO orders
            (code,name,direction,price,volume,amount,created_at)
            VALUES ('688001','科创测试','buy',10,1000,10000,'2026-06-17 09:30:00')""")
        conn.commit(); conn.close()

        result = reconcile(store, live_prices={"688001": 10.0})
        self.assertTrue(any("禁止板块" in i.message for i in result.issues))

    def test_loads_resolved_order_ids_from_config(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8") as tmp:
            json.dump({"resolved_order_ids": [88, "93", "bad", None]}, tmp)
            path = tmp.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        self.assertEqual(load_resolved_issue_order_ids(path), {88, 93})

    def test_malformed_resolved_issue_schema_returns_empty_set(self):
        for payload in ([], {"resolved_order_ids": None}, {"resolved_order_ids": "88"}):
            with self.subTest(payload=payload):
                with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8") as tmp:
                    json.dump(payload, tmp)
                    path = tmp.name
                self.addCleanup(lambda path=path: os.path.exists(path) and os.unlink(path))

                self.assertEqual(load_resolved_issue_order_ids(path), set())

    def test_invalid_resolved_order_id_values_are_skipped(self):
        payload = {"resolved_order_ids": [88.9, True, False, float("inf"), -1, 0, "", "9.5", " 93 "]}
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8") as tmp:
            json.dump(payload, tmp)
            path = tmp.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        self.assertEqual(load_resolved_issue_order_ids(path), {93})

    def test_invalid_utf8_resolved_issue_config_returns_empty_set(self):
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".json") as tmp:
            tmp.write(b'\xff\xfe{"resolved_order_ids":[88]}')
            path = tmp.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        self.assertEqual(load_resolved_issue_order_ids(path), set())

    def test_oversized_numeric_values_do_not_crash_loader(self):
        oversized = "9" * 5000
        payloads = [
            '{"resolved_order_ids":[' + oversized + "]}",
            json.dumps({"resolved_order_ids": [oversized, 93]}),
        ]
        expected = [set(), {93}]
        for content, expected_ids in zip(payloads, expected):
            with self.subTest(raw_number=content.startswith('{"resolved_order_ids":[9')):
                with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8") as tmp:
                    tmp.write(content)
                    path = tmp.name
                self.addCleanup(lambda path=path: os.path.exists(path) and os.unlink(path))

                self.assertEqual(load_resolved_issue_order_ids(path), expected_ids)

    def test_hides_issues_for_resolved_order_ids(self):
        store = self.make_store()
        conn = store._get_conn()
        conn.execute("""INSERT INTO orders
            (code,name,direction,price,volume,amount,created_at)
            VALUES ('600000','测试股','buy',10,1000,10000,'2026-06-17 09:30:00')""")
        conn.execute("""INSERT INTO orders
            (code,name,direction,price,volume,amount,created_at)
            VALUES ('600000','测试股','sell',11,100,1100,'2026-06-17 10:00:00')""")
        resolved_order_id = conn.execute("SELECT MAX(id) FROM orders").fetchone()[0]
        conn.commit(); conn.close()

        result = reconcile(
            store,
            live_prices={"600000": 10.0},
            resolved_issue_order_ids={resolved_order_id},
        )

        self.assertFalse(any(i.order_id == resolved_order_id for i in result.issues))


if __name__ == "__main__":
    unittest.main()
