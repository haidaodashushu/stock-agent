import unittest
from unittest.mock import patch

import pandas as pd

from data.logic_change import LogicChangeEvidence
from engine.screener import StockScreener
from strategy.selector.technical_scoring import TechnicalScoringSelector


def _rising_frame(*, sell_news: bool = False) -> pd.DataFrame:
    dates = pd.date_range("2026-04-01", periods=60, freq="B")
    close = [10 + i * 0.15 for i in range(60)]
    open_ = list(close)
    high = [x + 0.2 for x in close]
    low = [x - 0.2 for x in close]
    volume = [100_000.0] * 60
    if sell_news:
        previous = close[-2]
        open_[-1] = previous * 1.05
        close[-1] = previous * 1.01
        high[-1] = previous * 1.075
        low[-1] = previous * 0.995
        volume[-1] = 300_000.0
    return pd.DataFrame({
        "date": dates,
        "open": open_,
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "name": ["测试股"] * 60,
    })


class ScreeningRiskModelTests(unittest.TestCase):
    def test_related_theme_bonuses_are_capped(self):
        row = TechnicalScoringSelector()._score_one("000977", _rising_frame())
        self.assertIsNotNone(row)
        self.assertLessEqual(row["theme_bonus"], 2.2)

    def test_high_zone_sell_news_blocks_buy(self):
        logic = LogicChangeEvidence(
            code="000001",
            level="strong",
            boost=2.5,
            tags=["强逻辑变化"],
            reasons=["业绩大幅预增"],
            event_count=1,
            max_score=4.0,
        )
        row = TechnicalScoringSelector()._score_one(
            "000001",
            _rising_frame(sell_news=True),
            logic=logic,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["entry_route"], "strong_continuation")
        self.assertNotIn("route", row)
        self.assertFalse(row["buy_eligible"])
        self.assertIn("高位利好兑现", row["risk_tags"])

    def test_final_signal_recomputed_after_penalty(self):
        buy = pd.Series({"final_score": 8.0, "buy_eligible": True})
        watch_for_risk = pd.Series({"final_score": 8.0, "buy_eligible": False})
        passed = pd.Series({"final_score": 3.9, "buy_eligible": True})
        self.assertEqual(StockScreener._classify_final_signal(buy), "buy")
        self.assertEqual(StockScreener._classify_final_signal(watch_for_risk), "watch")
        self.assertEqual(StockScreener._classify_final_signal(passed), "pass")

    def test_theme_concentration_is_softly_penalized_after_two_names(self):
        frame = pd.DataFrame([
            {"code": "1", "final_score": 10.0, "theme_group": "AI算力"},
            {"code": "2", "final_score": 9.0, "theme_group": "AI算力"},
            {"code": "3", "final_score": 8.0, "theme_group": "AI算力"},
            {"code": "4", "final_score": 7.0, "theme_group": "机器人"},
        ])
        ranked = StockScreener._apply_theme_concentration_penalty(frame)
        penalties = dict(zip(ranked["code"], ranked["theme_concentration_penalty"]))
        self.assertEqual(penalties["1"], 0.0)
        self.assertEqual(penalties["2"], 0.0)
        self.assertEqual(penalties["3"], -0.8)
        self.assertEqual(penalties["4"], 0.0)

    def test_enrichment_factors_can_promote_a_stock_into_final_top_n(self):
        primary = pd.DataFrame([
            {
                "code": f"{index:06d}",
                "score": float(11 - index),
                "buy_eligible": True,
                "theme_group": "",
                "signal_tags": "",
            }
            for index in range(1, 11)
        ])
        screener = StockScreener()

        with (
            patch.object(screener, "_load_daily_data", return_value={"000001": pd.DataFrame([{}])}),
            patch.object(screener, "_run_primary", side_effect=[primary, primary]) as run_primary,
            patch.object(
                screener,
                "_compute_fund_flow_boost",
                return_value={"000010": 20.0},
            ),
            patch.object(screener, "_compute_sector_rotation_boost", return_value={}),
            patch.object(screener, "_compute_corporate_action_risks", return_value={}),
        ):
            result = screener.screen(top_n=3, enrichment_pool_size=10)

        self.assertEqual(run_primary.call_count, 2)
        self.assertFalse(run_primary.call_args_list[0].kwargs["include_enrichment"])
        self.assertTrue(run_primary.call_args_list[1].kwargs["include_enrichment"])
        self.assertEqual(run_primary.call_args_list[0].args[1], 10)
        self.assertEqual(result.iloc[0]["code"], "000010")
        self.assertEqual(result.iloc[0]["base_score"], 1.0)
        self.assertEqual(result.iloc[0]["fund_flow_score"], 20.0)

    def test_technical_pool_does_not_apply_static_theme_or_logic(self):
        logic = LogicChangeEvidence(
            code="600536",
            level="strong",
            boost=2.5,
            tags=["强逻辑变化"],
            tradeable_positive=True,
        )
        selector = TechnicalScoringSelector()
        technical = selector._score_one(
            "600536",
            _rising_frame(),
            logic=logic,
            include_enrichment=False,
        )
        enriched = selector._score_one(
            "600536",
            _rising_frame(),
            logic=logic,
            include_enrichment=True,
        )

        self.assertEqual(technical["theme_bonus"], 0.0)
        self.assertEqual(technical["logic_score"], 0.0)
        self.assertNotIn("十五五主线", technical["signal_tags"])
        self.assertGreater(enriched["score"], technical["score"])
        self.assertIn("十五五主线", enriched["signal_tags"])


if __name__ == "__main__":
    unittest.main()
