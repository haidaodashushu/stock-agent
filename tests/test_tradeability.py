import unittest

import pandas as pd

from data.tradeability import assess_tradeability


def bars(*, periods: int = 120, volume: float = 100_000, amount: float = 50_000_000):
    dates = pd.date_range("2026-01-01", periods=periods, freq="B")
    close = [10 + index * 0.01 for index in range(periods)]
    return pd.DataFrame({
        "date": dates,
        "open": close,
        "close": close,
        "high": [value + 0.1 for value in close],
        "low": [value - 0.1 for value in close],
        "volume": [volume] * periods,
        "amount": [amount] * periods,
    })


class TradeabilityTests(unittest.TestCase):
    def test_old_liquid_active_stock_is_eligible(self):
        frame = bars()
        decision = assess_tradeability(
            "000001",
            "测试股份",
            frame,
            expected_date=str(frame.iloc[-1]["date"])[:10],
            list_date="2000-01-01",
        )
        self.assertTrue(decision.eligible)

    def test_st_is_a_hard_rejection_but_recent_listing_is_not(self):
        frame = bars(periods=30)
        decision = assess_tradeability(
            "000001",
            "*ST测试",
            frame,
            expected_date=str(frame.iloc[-1]["date"])[:10],
            list_date=str(frame.iloc[0]["date"])[:10],
        )
        self.assertFalse(decision.eligible)
        self.assertIn("st", decision.reasons)
        self.assertNotIn("recent_listing", decision.reasons)

    def test_short_but_liquid_history_is_not_a_hard_rejection(self):
        frame = bars(periods=10)
        decision = assess_tradeability(
            "000001",
            "测试股份",
            frame,
            expected_date=str(frame.iloc[-1]["date"])[:10],
            list_date=str(frame.iloc[0]["date"])[:10],
        )
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.metrics["liquidity_bars"], 10)

    def test_inactive_and_legacy_st_names_are_rejected(self):
        frame = bars()
        decision = assess_tradeability(
            "000001",
            "S*ST测试",
            frame,
            expected_date=str(frame.iloc[-1]["date"])[:10],
            list_date="2000-01-01",
            is_active=False,
        )
        self.assertFalse(decision.eligible)
        self.assertIn("st", decision.reasons)
        self.assertIn("inactive", decision.reasons)

    def test_suspended_stale_and_illiquid_stocks_are_rejected(self):
        frame = bars(amount=1_000_000)
        frame.loc[frame.index[-1], "volume"] = 0
        expected = (frame.iloc[-1]["date"] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        decision = assess_tradeability(
            "000001",
            "测试股份",
            frame,
            expected_date=expected,
            list_date="2000-01-01",
        )
        self.assertFalse(decision.eligible)
        self.assertIn("suspended_or_stale", decision.reasons)
        self.assertIn("suspended_or_unpriced", decision.reasons)
        self.assertIn("illiquid", decision.reasons)

    def test_one_price_limit_is_not_treated_as_buyable(self):
        frame = bars()
        previous = float(frame.iloc[-2]["close"])
        limit_price = previous * 1.1
        for column in ("open", "close", "high", "low"):
            frame.loc[frame.index[-1], column] = limit_price
        decision = assess_tradeability(
            "000001",
            "测试股份",
            frame,
            expected_date=str(frame.iloc[-1]["date"])[:10],
            list_date="2000-01-01",
        )
        self.assertFalse(decision.eligible)
        self.assertIn("one_price_limit", decision.reasons)

    def test_chinext_uses_twenty_percent_limit(self):
        frame = bars()
        previous = float(frame.iloc[-2]["close"])
        for column in ("open", "close", "high", "low"):
            frame.loc[frame.index[-1], column] = previous * 1.1
        decision = assess_tradeability(
            "300001",
            "测试股份",
            frame,
            expected_date=str(frame.iloc[-1]["date"])[:10],
            list_date="2000-01-01",
        )
        self.assertTrue(decision.eligible)


if __name__ == "__main__":
    unittest.main()
