import base64
import os
import struct
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from data.store.sqlite_store import StockStore
from data.wecom_client import WeComCrypto, WeComSettings
from data.wecom_inbound import (
    WeComMessageAgent,
    handle_wecom_message,
    message_time,
    parse_wecom_xml,
)


AES_KEY = base64.b64encode(bytes(range(32))).decode().rstrip("=")


def _encrypt(xml: str, corp_id: str) -> str:
    key = base64.b64decode(AES_KEY + "=")
    payload = b"0123456789abcdef" + struct.pack("!I", len(xml.encode()))
    payload += xml.encode() + corp_id.encode()
    padding = 32 - len(payload) % 32
    payload += bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(payload) + encryptor.finalize()).decode()


def _settings() -> WeComSettings:
    return WeComSettings(
        corp_id="ww_test",
        agent_id=1000002,
        secret="secret",
        token="callback-token",
        encoding_aes_key=AES_KEY,
        allowed_user_ids=frozenset({"WangZhengKui"}),
        admin_user_ids=frozenset({"WangZhengKui"}),
        allow_all_users=True,
    )


class _FakeClient:
    def __init__(self):
        self.sent = []

    def send_markdown(self, user_id, content):
        self.sent.append((user_id, content))
        return {"errcode": 0}


class _FakeAgent:
    def __init__(self, returned_session_id=""):
        self.calls = []
        self.returned_session_id = returned_session_id

    def process(
        self, message_id, content, *, sender_id, can_write, chat_type, chat_id, session_id,
        image_paths=(),
    ):
        self.calls.append((
            message_id, content, sender_id, can_write, chat_type, chat_id, session_id,
            image_paths,
        ))
        if self.returned_session_id:
            return "处理完成", self.returned_session_id
        return "处理完成"


def _event(sender="WangZhengKui"):
    return {
        "ToUserName": "ww_test",
        "FromUserName": sender,
        "CreateTime": "1788242670",
        "MsgType": "text",
        "Content": "现在状态怎么样？",
        "MsgId": "123456789",
        "AgentID": "1000002",
    }


class WeComCryptoTests(unittest.TestCase):
    def test_verifies_and_decrypts_callback(self):
        crypto = WeComCrypto("callback-token", AES_KEY, "ww_test")
        xml = "<xml><Content><![CDATA[你好]]></Content></xml>"
        encrypted = _encrypt(xml, "ww_test")
        signature = crypto.signature("123", "nonce", encrypted)
        crypto.verify(signature, "123", "nonce", encrypted)
        self.assertEqual(crypto.decrypt(encrypted), xml)
        self.assertEqual(parse_wecom_xml(xml)["Content"], "你好")

    def test_rejects_invalid_signature_and_receiver(self):
        crypto = WeComCrypto("callback-token", AES_KEY, "ww_test")
        encrypted = _encrypt("<xml />", "another-corp")
        with self.assertRaisesRegex(ValueError, "signature"):
            crypto.verify("bad", "123", "nonce", encrypted)
        with self.assertRaisesRegex(ValueError, "corp_id"):
            crypto.decrypt(encrypted)

    def test_message_time_accepts_aibot_millisecond_timestamp(self):
        seconds = message_time("1788242670")
        milliseconds = message_time("1788242670000")
        self.assertEqual(seconds, milliseconds)


class WeComInboundTests(unittest.TestCase):
    def test_trusted_message_is_processed_replied_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            client = _FakeClient()
            agent = _FakeAgent()
            self.assertEqual(handle_wecom_message(
                _event(), settings=_settings(), store=store, client=client, agent=agent,
            ), "processed")
            self.assertEqual(client.sent, [("WangZhengKui", "处理完成")])
            self.assertEqual(agent.calls, [(
                "wecom:123456789", "现在状态怎么样？", "WangZhengKui", True,
                "single", "", "", (),
            )])
            self.assertEqual(handle_wecom_message(
                _event(), settings=_settings(), store=store, client=client, agent=agent,
            ), "duplicate")
            conn = store._get_conn()
            try:
                row = conn.execute(
                    "SELECT status,handler,result,replied_at FROM bot_inbound_messages"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual((row["status"], row["handler"], row["result"]), (
                "succeeded", "codex", "处理完成",
            ))
            self.assertTrue(row["replied_at"])

    def test_any_visible_sender_is_processed_as_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            client = _FakeClient()
            agent = _FakeAgent()
            event = _event("SomeoneElse")
            event["MsgId"] = "any-visible-user-1"
            self.assertEqual(handle_wecom_message(
                event, settings=_settings(), store=store, client=client, agent=agent,
            ), "processed")
            self.assertEqual(agent.calls, [(
                "wecom:any-visible-user-1", "现在状态怎么样？", "SomeoneElse", False,
                "single", "", "", (),
            )])
            self.assertEqual(client.sent, [("SomeoneElse", "处理完成")])

    def test_allowed_non_admin_is_processed_as_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            client = _FakeClient()
            agent = _FakeAgent()
            event = _event("HongBo")
            event["MsgId"] = "read-only-1"

            self.assertEqual(handle_wecom_message(
                event, settings=_settings(), store=store, client=client, agent=agent,
            ), "processed")
            self.assertEqual(agent.calls, [(
                "wecom:read-only-1", "现在状态怎么样？", "HongBo", False,
                "single", "", "", (),
            )])
            self.assertEqual(client.sent, [("HongBo", "处理完成")])

    def test_allowed_non_admin_cannot_submit_live_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            client = _FakeClient()
            agent = _FakeAgent()
            event = _event("nullpointerexception")
            event["MsgId"] = "denied-fill-1"
            event["Content"] = "成交：买入 600519，价格 1500，数量 100"

            self.assertEqual(handle_wecom_message(
                event, settings=_settings(), store=store, client=client, agent=agent,
            ), "processed")
            self.assertEqual(agent.calls, [])
            self.assertIn("只有管理员 WangZhengKui", client.sent[0][1])
            conn = store._get_conn()
            try:
                row = conn.execute(
                    "SELECT status,handler FROM bot_inbound_messages WHERE message_id=?",
                    ("wecom:denied-fill-1",),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual((row["status"], row["handler"]), (
                "succeeded", "permission-denied",
            ))

    def test_private_sessions_resume_per_user_and_new_resets_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            client = _FakeClient()
            first_agent = _FakeAgent("session-hongbo-1")
            first = _event("HongBo")
            first["MsgId"] = "session-first"
            handle_wecom_message(
                first, settings=_settings(), store=store, client=client, agent=first_agent,
            )

            second_agent = _FakeAgent("session-hongbo-1")
            second = _event("HongBo")
            second["MsgId"] = "session-second"
            handle_wecom_message(
                second, settings=_settings(), store=store, client=client, agent=second_agent,
            )
            self.assertEqual(second_agent.calls[0][-2], "session-hongbo-1")

            other_agent = _FakeAgent("session-other-1")
            other = _event("SomeoneElse")
            other["MsgId"] = "session-other"
            handle_wecom_message(
                other, settings=_settings(), store=store, client=client, agent=other_agent,
            )
            self.assertEqual(other_agent.calls[0][-2], "")

            reset = _event("HongBo")
            reset["MsgId"] = "session-reset"
            reset["Content"] = "/new"
            handle_wecom_message(
                reset, settings=_settings(), store=store, client=client, agent=second_agent,
            )
            self.assertIn("已开启新会话", client.sent[-1][1])
            conn = store._get_conn()
            try:
                row = conn.execute(
                    "SELECT session_id,generation FROM wecom_agent_sessions WHERE conversation_key=?",
                    ("single:HongBo",),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual((row["session_id"], row["generation"]), ("", 2))

    def test_group_mention_is_removed_and_session_isolated_by_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            client = _FakeClient()
            agent = _FakeAgent("group-session-1")
            event = _event("HongBo")
            event.update({
                "MsgId": "group-session",
                "Content": "@搅市的棍 讲个笑话",
                "ChatType": "group",
                "ChatId": "group-123",
                "ImagePaths": ["/tmp/quoted-news.png"],
            })
            handle_wecom_message(
                event, settings=_settings(), store=store, client=client, agent=agent,
            )
            self.assertEqual(agent.calls[0][1], "讲个笑话")
            self.assertEqual(agent.calls[0][-1], (Path("/tmp/quoted-news.png"),))
            conn = store._get_conn()
            try:
                row = conn.execute(
                    "SELECT conversation_key,session_id FROM wecom_agent_sessions"
                ).fetchone()
                inbound = conn.execute(
                    "SELECT chat_id FROM bot_inbound_messages WHERE message_id=?",
                    ("wecom:group-session",),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual((row["conversation_key"], row["session_id"]), (
                "group:group-123:HongBo", "group-session-1",
            ))
            self.assertEqual(inbound["chat_id"], "group-123")
            shared = WeComMessageAgent(_settings(), store)._recent_group_context(
                "wecom:another-message", "WangZhengKui", "group-123",
            )
            self.assertIn("HongBo", shared)
            self.assertIn("处理完成", shared)

    def test_group_mention_without_space_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            client = _FakeClient()
            agent = _FakeAgent()
            event = _event("HongBo")
            event.update({
                "MsgId": "group-no-space",
                "Content": "@搅市的棍请介绍一下自己",
                "ChatType": "group",
                "ChatId": "group-123",
            })
            handle_wecom_message(
                event, settings=_settings(), store=store, client=client, agent=agent,
            )
            self.assertEqual(agent.calls[0][1], "请介绍一下自己")

    def test_latest_night_selection_request_uses_durable_report(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            conn = store._get_conn()
            try:
                report = {
                    "profile": "screening_report",
                    "title": "夜间预选股 2026-09-01",
                    "summary": "结构性行情。",
                    "run": {"run_date": "2026-09-01"},
                    "candidates": [{
                        "code": "002541", "name": "鸿路钢构", "score": 18.8,
                        "ai_confidence": "strong", "ai_reason": "量价确认。",
                        "ai_risk": "位置偏高。",
                    }],
                }
                import json
                conn.execute(
                    """INSERT INTO agent_decision_submissions
                       (submission_key,task,mode,as_of,provider,model,status,decision,result,created_at)
                       VALUES ('selection:test','selection','','snapshot','codex','test','ready','{}',?,
                               '2026-09-01 00:20:04')""",
                    (json.dumps(report, ensure_ascii=False),),
                )
                conn.commit()
            finally:
                conn.close()
            client = _FakeClient()
            agent = _FakeAgent()
            event = _event("nullpointerexception")
            event.update({
                "MsgId": "latest-selection",
                "Content": "@搅市的棍请展示本轮夜间预选股 TOP10，并概括入选依据和主要风险",
                "ChatType": "group",
                "ChatId": "group-123",
            })
            handle_wecom_message(
                event, settings=_settings(), store=store, client=client, agent=agent,
            )
            self.assertEqual(agent.calls, [])
            self.assertIn("002541 鸿路钢构", client.sent[0][1])
            self.assertIn("量价确认", client.sent[0][1])
            self.assertIn("位置偏高", client.sent[0][1])


if __name__ == "__main__":
    unittest.main()
