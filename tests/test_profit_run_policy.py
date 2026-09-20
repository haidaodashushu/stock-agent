from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProfitRunPolicyContractTests(unittest.TestCase):
    def test_web_strategy_summary_does_not_advertise_fixed_take_profit(self):
        template = (ROOT / "web" / "templates" / "app.html").read_text(encoding="utf-8")

        self.assertNotIn("止盈+15%", template)
        self.assertIn("浮盈不封顶", template)


if __name__ == "__main__":
    unittest.main()
