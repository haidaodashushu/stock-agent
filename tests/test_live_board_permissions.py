import unittest

from fastapi.testclient import TestClient

from data.live_manual_account import blocked_prefixes, is_live_buy_allowed
from web.app import app


class LiveBoardPermissionTests(unittest.TestCase):
    def test_config_can_allow_chinext_while_blocking_other_boards(self):
        config = {"blocked_boards": ["688", "8", "4"]}

        self.assertEqual(blocked_prefixes(config), ("688", "8", "4"))
        self.assertTrue(is_live_buy_allowed("300996", config))
        self.assertTrue(is_live_buy_allowed("301001", config))
        self.assertFalse(is_live_buy_allowed("688001", config))
        self.assertFalse(is_live_buy_allowed("830001", config))
        self.assertFalse(is_live_buy_allowed("430001", config))

    def test_explicit_empty_block_list_allows_every_board(self):
        config = {"blocked_boards": []}

        self.assertEqual(blocked_prefixes(config), ())
        self.assertTrue(is_live_buy_allowed("688001", config))

    def test_public_web_cannot_write_live_fills_or_statuses(self):
        client = TestClient(app)

        fill = client.post(
            "/api/live-intents/not-real/fill",
            json={"price": 10.0, "volume": 100},
        )
        status = client.post(
            "/api/live-intents/not-real/status",
            json={"status": "cancelled"},
        )

        self.assertEqual(fill.status_code, 403)
        self.assertEqual(status.status_code, 403)
        self.assertIn("企业微信", fill.json()["detail"])
        self.assertIn("企业微信", status.json()["detail"])


if __name__ == "__main__":
    unittest.main()
