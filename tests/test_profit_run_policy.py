from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProfitRunPolicyContractTests(unittest.TestCase):
    def test_shared_trading_policy_has_no_fixed_profit_cap(self):
        policy = (ROOT / "config" / "agent_trading_policy.md").read_text(encoding="utf-8")

        self.assertIn("不设固定浮盈百分比上限", policy)
        self.assertIn("不得仅因达到某一盈利率机械卖出", policy)
        self.assertIn("趋势、量价承接和基本逻辑未破坏", policy)

    def test_web_strategy_summary_does_not_advertise_fixed_take_profit(self):
        template = (ROOT / "web" / "templates" / "app.html").read_text(encoding="utf-8")

        self.assertNotIn("止盈+15%", template)
        self.assertIn("浮盈不封顶", template)


if __name__ == "__main__":
    unittest.main()
