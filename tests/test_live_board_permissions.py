import unittest

from data.live_manual_account import blocked_prefixes, is_live_buy_allowed


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


if __name__ == "__main__":
    unittest.main()
