import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from data.live_fill_service import LiveFillError
from data.store.sqlite_store import StockStore
from data.wecom_inbound import handle_wecom_message
from data.wecom_intent import execute_live_action, validate_decision
from tests.test_wecom_integration import _event, _settings, _FakeClient, _FakeAgent


def decision(action, **kwargs):
    return {"action": action, "reason": "用户当前请求", "clarification": "",
            "fills": [], "cancel_ids": [], **kwargs}


class WeComIntentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StockStore(str(Path(tmp.name) / "stock.db"))

    def count_fills(self):
        conn = self.store._get_conn()
        try:
            return conn.execute("SELECT count(*) FROM live_trade_intents WHERE status='filled'").fetchone()[0]
        finally:
            conn.close()

    def test_market_question_with_quoted_fill_example_reaches_answer_agent(self):
        event = _event()
        event["Content"] = ("触发了什么关键词吗？直接看下A股今天的市场数据，看看是不是止跌了\n"
                            "[企业微信引用消息]\n未识别成交回报。示例：买入 600460 士兰微：300 股，成交价 41.20")
        agent = _FakeAgent()
        with patch("data.wecom_inbound.route_message", return_value=decision("answer")) as router:
            handle_wecom_message(event, settings=_settings(), store=self.store,
                                 client=_FakeClient(), agent=agent)
        self.assertEqual(router.call_count, 1)
        self.assertEqual(agent.calls[0][1], event["Content"])
        self.assertEqual(self.count_fills(), 0)

    def test_ai_fill_uses_structured_parameters_not_quote_example(self):
        event = _event()
        event["Content"] = "实际我买的是中航西飞，两百股二十三元。\n[企业微信引用消息]\n买入 600460 41.20 300"
        plan = decision("fill", fills=[{"action": "buy", "code": "000768", "intent_id": "", "price": 23, "volume": 200}])
        client = _FakeClient()
        with patch("data.wecom_inbound.route_message", return_value=plan):
            handle_wecom_message(event, settings=_settings(), store=self.store, client=client)
            self.assertEqual(handle_wecom_message(event, settings=_settings(), store=self.store, client=client), "duplicate")
        conn = self.store._get_conn()
        try:
            rows = conn.execute("SELECT code,filled_price,filled_volume FROM live_trade_intents").fetchall()
            self.assertEqual([tuple(r) for r in rows], [("000768", 23, 200)])
        finally:
            conn.close()

    def seed_fill(self):
        execute_live_action(decision("fill", fills=[{
            "action": "buy", "code": "600460", "intent_id": "", "price": 41.2, "volume": 300,
        }]), can_write=True, message_id="seed", message_at=datetime(2026, 9, 14, 17, 16, 10), store=self.store)
        conn = self.store._get_conn()
        try:
            return conn.execute("SELECT intent_id FROM live_trade_intents").fetchone()[0]
        finally:
            conn.close()

    def test_ai_cancel_preserves_audit_record_and_is_idempotent(self):
        intent_id = self.seed_fill()
        event = _event()
        event["Content"] = f"没有买入600460，撤销这个成交\n[企业微信引用消息]\n已记录 {intent_id}"
        client = _FakeClient()
        with patch("data.wecom_inbound.route_message", return_value=decision("cancel", cancel_ids=[intent_id])):
            handle_wecom_message(event, settings=_settings(), store=self.store, client=client)
        self.assertIn("已撤销", client.sent[0][1])
        self.assertEqual(self.count_fills(), 0)
        conn = self.store._get_conn()
        try:
            r = conn.execute("SELECT status,filled_volume,user_note FROM live_trade_intents").fetchone()
            self.assertEqual((r["status"], r["filled_volume"]), ("cancelled", 300))
            self.assertIn("wecom:123456789", r["user_note"])
        finally:
            conn.close()
        execute_live_action(decision("cancel", cancel_ids=[intent_id]), can_write=True,
                            message_id="retry", message_at=datetime.now(), store=self.store)
        self.assertEqual(self.count_fills(), 0)

    def test_cancel_unknown_record_rolls_back_whole_batch(self):
        intent_id = self.seed_fill()
        with self.assertRaises(LiveFillError):
            execute_live_action(decision("cancel", cancel_ids=[intent_id, "L20260914000000-FFFFFF"]),
                                can_write=True, message_id="cancel", message_at=datetime.now(), store=self.store)
        self.assertEqual(self.count_fills(), 1)

    def test_non_admin_cannot_cancel(self):
        intent_id = self.seed_fill()
        reply = execute_live_action(decision("cancel", cancel_ids=[intent_id]), can_write=False,
                                    message_id="cancel", message_at=datetime.now(), store=self.store)
        self.assertIn("只有管理员", reply)
        self.assertEqual(self.count_fills(), 1)

    def test_invalid_or_failed_ai_output_never_falls_back_to_regex(self):
        event = _event()
        event["Content"] = "买入 600460 41.20 300"
        with patch("data.wecom_inbound.route_message", side_effect=RuntimeError("模型不可用")):
            handle_wecom_message(event, settings=_settings(), store=self.store, client=_FakeClient())
        self.assertEqual(self.count_fills(), 0)
        with self.assertRaises(ValueError):
            validate_decision(decision("answer", fills=[{"action": "buy", "code": "600460", "intent_id": "", "price": 41.2, "volume": 300}]))


if __name__ == "__main__":
    unittest.main()
