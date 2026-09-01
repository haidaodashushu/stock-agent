import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data.wecom_aibot import load_wecom_aibot_settings, send_wecom_aibot_message


class WeComAiBotTests(unittest.TestCase):
    def _config(self, directory: str, **overrides) -> Path:
        bot = {
            "bot_id": "aib-test",
            "secret": "secret",
            "allow_all_users": True,
            "allowed_user_ids": [],
            "admin_user_ids": ["WangZhengKui"],
        }
        bot.update(overrides)
        path = Path(directory) / "runtime.json"
        path.write_text(json.dumps({"wecom_aibot": bot, "agent": {"model": "test-model"}}))
        return path

    def test_all_visible_users_mode_keeps_one_admin(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_wecom_aibot_settings(self._config(directory))
        self.assertTrue(settings.allow_all_users)
        self.assertEqual(settings.allowed_user_ids, frozenset())
        self.assertEqual(settings.admin_user_ids, frozenset({"WangZhengKui"}))
        self.assertEqual(settings.agent_model, "test-model")

    def test_restricted_mode_requires_admin_to_be_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._config(
                directory,
                allow_all_users=False,
                allowed_user_ids=["HongBo"],
            )
            with self.assertRaisesRegex(RuntimeError, "admins must be allowed"):
                load_wecom_aibot_settings(path)

    def test_loopback_bridge_sends_content(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_wecom_aibot_settings(self._config(directory))

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b'{"ok":true}'

        with patch("urllib.request.urlopen", return_value=Response()) as request:
            send_wecom_aibot_message(settings, "hello", idempotency_key="key")
        sent = json.loads(request.call_args.args[0].data.decode())
        self.assertEqual(sent, {
            "content": "hello", "idempotency_key": "key", "mention_all": False,
        })

    def test_loopback_bridge_can_request_mention_all(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_wecom_aibot_settings(self._config(directory))

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b'{"ok":true}'

        with patch("urllib.request.urlopen", return_value=Response()) as request:
            send_wecom_aibot_message(settings, "report", mention_all=True)
        sent = json.loads(request.call_args.args[0].data.decode())
        self.assertTrue(sent["mention_all"])


if __name__ == "__main__":
    unittest.main()
