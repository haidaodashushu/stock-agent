import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from data.message_delivery import send_configured_message


REPORT = {
    "profile": "screening_report",
    "title": "夜间预选股 2026-09-02",
    "summary": "结构性行情。",
    "run": {"run_date": "2026-09-02"},
    "candidates": [{
        "code": "301208", "name": "中亦科技", "score": 17.5,
        "ai_confidence": "strong", "ai_reason": "量价确认。", "ai_risk": "位置偏高。",
    }],
}


class MessageDeliveryAdapterTests(unittest.TestCase):
    def _config(self, directory: str, provider: str, **messaging) -> Path:
        path = Path(directory) / "runtime.json"
        path.write_text(json.dumps({
            "messaging": {"provider": provider, **messaging},
            "feishu": {}, "wecom_aibot": {},
        }))
        return path

    def test_wecom_adapter_renders_markdown_and_mentions_all(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory, "wecom_aibot", mention_all_on_push=True)
            with (
                patch("data.message_delivery.load_wecom_aibot_settings", return_value=object()),
                patch("data.message_delivery.send_wecom_aibot_message") as send,
            ):
                send_configured_message(
                    config_path=config, content=json.dumps(REPORT, ensure_ascii=False),
                    message_type="interactive", idempotency_key="selection:test",
                )
        content = send.call_args.args[1]
        self.assertIn("**1. 301208 中亦科技**", content)
        self.assertIn("> 入选：量价确认。", content)
        self.assertTrue(send.call_args.kwargs["mention_all"])

    def test_feishu_adapter_renders_native_card_from_same_report(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory, "feishu")
            with (
                patch("data.message_delivery.load_feishu_settings", return_value=object()),
                patch(
                    "data.message_delivery.send_feishu",
                    return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
                ) as send,
            ):
                send_configured_message(
                    config_path=config, content=json.dumps(REPORT, ensure_ascii=False),
                    message_type="interactive", idempotency_key="selection:test",
                )
        card = json.loads(send.call_args.kwargs["content"])
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["title"]["content"], "夜间预选股 2026-09-02")
        self.assertEqual(send.call_args.kwargs["message_type"], "interactive")


if __name__ == "__main__":
    unittest.main()
