import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from data.live_fill_service import LiveFillError, parse_fill_commands, process_fill_report
from data.store.sqlite_store import StockStore
from scripts.feishu_message_listener import ListenerConfig, handle_event


def _store(directory: str) -> StockStore:
    return StockStore(str(Path(directory) / "stock.db"))


def _insert_intent(store: StockStore, intent_id: str, code: str, action: str, name: str):
    conn = store._get_conn()
    try:
        conn.execute(
            """INSERT INTO live_trade_intents
               (intent_id,code,name,action,suggested_price,suggested_volume,
                suggested_amount,limit_price,status,created_at,expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                intent_id, code, name, action, 20.0, 300, 6000.0, 25.0,
                "proposed", "2026-09-01 14:00:00", "2026-09-01 14:15:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _config(directory: str) -> ListenerConfig:
    return ListenerConfig(
        config_path=Path(directory) / "runtime.json",
        binary="lark-cli",
        profile="stock",
        chat_id="oc_allowed",
        allowed_sender_ids=frozenset({"ou_owner"}),
        ack_emoji="OnIt",
        agent_enabled=True,
        agent_model="test-model",
        agent_timeout_seconds=60,
        max_workers=1,
    )


class _FakeLark:
    def __init__(self, calls):
        self.calls = calls

    def react(self, message_id):
        self.calls.append(("react", message_id))

    def reply(self, message_id, content):
        self.calls.append(("reply", message_id, content))


class _FakeAgent:
    def __init__(self, calls):
        self.calls = calls

    def process(self, event):
        self.calls.append(("agent", event["message_id"]))
        return "处理完成"


def _event(message_id="om_event", content="现在状态怎么样？"):
    return {
        "message_id": message_id,
        "event_id": "evt_1",
        "chat_id": "oc_allowed",
        "sender_id": "ou_owner",
        "sender_type": "user",
        "message_type": "text",
        "content": content,
        "create_time": "1788242670000",
    }


class LiveFillServiceTests(unittest.TestCase):
    def test_parse_multiple_natural_feishu_fill_lines(self):
        commands = parse_fill_commands(
            "买入 000768 中航西飞：300 股，参考价 22.73\n"
            "买入 002541 鸿路钢构：300 股，参考价 21.28"
        )
        self.assertEqual(
            [(c.action, c.code, c.price, c.volume) for c in commands],
            [
                ("buy", "000768", 22.73, 300),
                ("buy", "002541", 21.28, 300),
            ],
        )

    def test_process_multiple_fills_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            _insert_intent(store, "L20260901140001-AAAAAA", "000768", "buy", "中航西飞")
            _insert_intent(store, "L20260901140002-BBBBBB", "002541", "buy", "鸿路钢构")
            result = process_fill_report(
                "买入 000768 中航西飞：300 股，成交价 22.73\n"
                "买入 002541 鸿路钢构：300 股，成交价 21.28",
                message_id="om_test",
                message_at=datetime(2026, 9, 1, 14, 4, 30),
                store=store,
            )
            self.assertEqual(len(result["fills"]), 2)
            conn = store._get_conn()
            try:
                rows = conn.execute(
                    "SELECT code,status,filled_price,filled_volume,filled_at "
                    "FROM live_trade_intents ORDER BY code"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    ("000768", "filled", 22.73, 300, "2026-09-01 14:04:30"),
                    ("002541", "filled", 21.28, 300, "2026-09-01 14:04:30"),
                ],
            )

    def test_fill_batch_rolls_back_when_any_line_cannot_match(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            _insert_intent(store, "L20260901140001-AAAAAA", "000768", "buy", "中航西飞")
            with self.assertRaisesRegex(LiveFillError, "未找到 002541"):
                process_fill_report(
                    "买入 000768 中航西飞：300 股，成交价 22.73\n"
                    "买入 002541 鸿路钢构：300 股，成交价 21.28",
                    message_id="om_test",
                    message_at=datetime(2026, 9, 1, 14, 4, 30),
                    store=store,
                )
            conn = store._get_conn()
            try:
                status = conn.execute(
                    "SELECT status FROM live_trade_intents WHERE code='000768'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(status, "proposed")


class FeishuInboundListenerTests(unittest.TestCase):
    def test_listener_reacts_before_agent_and_replies_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            calls = []
            event = _event()
            self.assertEqual(handle_event(
                event,
                config=_config(directory),
                store=store,
                lark=_FakeLark(calls),
                agent=_FakeAgent(calls),
            ), "processed")
            self.assertEqual([call[0] for call in calls], ["react", "agent", "reply"])
            self.assertEqual(handle_event(
                event,
                config=_config(directory),
                store=store,
                lark=_FakeLark(calls),
                agent=_FakeAgent(calls),
            ), "duplicate")
            self.assertEqual([call[0] for call in calls], ["react", "agent", "reply"])
            conn = store._get_conn()
            try:
                row = conn.execute(
                    "SELECT status,handler,ack_status,result,replied_at "
                    "FROM feishu_inbound_messages"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(row["handler"], "codex")
            self.assertEqual(row["ack_status"], "sent")
            self.assertEqual(row["result"], "处理完成")
            self.assertTrue(row["replied_at"])

    def test_listener_silently_ignores_untrusted_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            calls = []
            event = _event()
            event["sender_id"] = "ou_someone_else"
            self.assertEqual(handle_event(
                event,
                config=_config(directory),
                store=store,
                lark=_FakeLark(calls),
                agent=_FakeAgent(calls),
            ), "ignored_untrusted")
            self.assertEqual(calls, [])
            conn = store._get_conn()
            try:
                count = conn.execute("SELECT COUNT(*) FROM feishu_inbound_messages").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
