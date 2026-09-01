import unittest
from collections import Counter
from unittest.mock import patch

import pandas as pd

from data.trading_state import _minute_scope, _minute_states, minute_state


class FakeMinuteFetcher:
    def __init__(self, frame):
        self.frame = frame

    def fetch_minute(self, code):
        return self.frame


class IntradayMinuteSummaryTest(unittest.TestCase):
    def test_half_hour_volume_price_up(self):
        rows = []
        cumulative_volume = 0.0
        cumulative_amount = 0.0
        for i in range(61):
            price = 10.0 if i < 31 else 10.0 + (i - 30) * 0.05
            volume = 100.0 if i < 31 else 250.0
            cumulative_volume += volume
            cumulative_amount += price * volume * 100
            rows.append(
                {
                    "time": f"{930 + i:04d}",
                    "price": price,
                    "volume": cumulative_volume,
                    "amount": cumulative_amount,
                }
            )
        summary = minute_state("000001", FakeMinuteFetcher(pd.DataFrame(rows)))

        half_hour = summary["half_hour"]
        self.assertTrue(half_hour["available"])
        self.assertEqual(half_hour["lookback"], "30_trading_minutes")
        self.assertGreater(half_hour["price_change_pct"], 1.0)
        self.assertGreaterEqual(half_hour["volume_last30_vs_prev30"], 1.5)
        self.assertEqual(half_hour["volume_price_signal"], "volume_price_up")

    def test_half_hour_unavailable_when_sample_too_short(self):
        frame = pd.DataFrame(
            [
                {"time": "0930", "price": 10.0, "volume": 100.0, "amount": 100000.0},
                {"time": "0931", "price": 10.1, "volume": 120.0, "amount": 121200.0},
            ]
        )
        summary = minute_state("000001", FakeMinuteFetcher(frame))

        self.assertFalse(summary["half_hour"]["available"])

    def test_default_scope_keeps_every_holding_and_candidate(self):
        codes = [f"{index:06d}" for index in range(1, 21)]

        self.assertEqual(_minute_scope(codes), codes)
        self.assertEqual(_minute_scope(codes, 12), codes[:12])

    def test_failed_code_is_retried_without_dropping_other_codes(self):
        calls = Counter()

        def fake_minute_state(code, fetcher):
            calls[code] += 1
            if code == "000001" and calls[code] == 1:
                return {"half_hour": {"available": False}}
            return {"last_time": "1030", "half_hour": {"available": True}}

        with patch("data.trading_state.minute_state", side_effect=fake_minute_state):
            result = _minute_states(["000001", "000002", "000003"])

        self.assertEqual(set(result), {"000001", "000002", "000003"})
        self.assertEqual(calls["000001"], 2)
        self.assertEqual(calls["000002"], 1)
        self.assertEqual(calls["000003"], 1)
        self.assertTrue(all(row.get("last_time") for row in result.values()))


if __name__ == "__main__":
    unittest.main()
