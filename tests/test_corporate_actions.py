import unittest
from unittest.mock import patch

from data.corporate_actions import get_next_day_corporate_action_risks


class CorporateActionTests(unittest.TestCase):
    def test_next_day_ex_dividend_penalty(self):
        row = {
            "SECURITY_CODE": "000768",
            "EX_DIVIDEND_DATE": "2026-06-23 00:00:00",
            "EQUITY_RECORD_DATE": "2026-06-22 00:00:00",
            "PRETAX_BONUS_RMB": 1.5,
            "BONUS_RATIO": None,
            "IT_RATIO": None,
            "IMPL_PLAN_PROFILE": "10派1.50元(含税,扣税后1.35元)",
        }
        with patch("data.corporate_actions.is_market_open", return_value=True), \
             patch("data.corporate_actions._fetch_share_bonus_by_ex_date", return_value=[row]) as fetch:
            risks = get_next_day_corporate_action_risks(["000768"], as_of="2026-06-22")

        fetch.assert_called_once_with("2026-06-23", timeout=6.0)
        self.assertIn("000768", risks)
        self.assertEqual(risks["000768"].ex_date, "2026-06-23")
        self.assertEqual(risks["000768"].cash_per_share, 0.15)
        self.assertEqual(risks["000768"].penalty, -0.5)
        self.assertIn("次日除权除息", risks["000768"].tag)

    def test_batch_response_is_filtered_to_requested_codes(self):
        rows = [
            {
                "SECURITY_CODE": code,
                "EX_DIVIDEND_DATE": "2026-06-23 00:00:00",
                "PRETAX_BONUS_RMB": 1,
            }
            for code in ("000001", "000002")
        ]
        with patch("data.corporate_actions.is_market_open", return_value=True), \
             patch("data.corporate_actions._fetch_share_bonus_by_ex_date", return_value=rows):
            risks = get_next_day_corporate_action_risks(["000002"], as_of="2026-06-22")

        self.assertEqual(set(risks), {"000002"})


if __name__ == "__main__":
    unittest.main()
