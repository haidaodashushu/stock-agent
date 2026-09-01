import unittest

from account.portfolio_policy import simulated_account_policy
from data.trading_state import _annotate_relative_strength, _industry_exposure


class PortfolioPolicyTests(unittest.TestCase):
    def test_capacity_states_cover_target_and_hard_breach(self):
        self.assertEqual(simulated_account_policy(9)["capacity_state"], "below_target")
        self.assertEqual(simulated_account_policy(10)["capacity_state"], "within_target")
        self.assertEqual(simulated_account_policy(12)["capacity_state"], "within_target")
        self.assertEqual(simulated_account_policy(13)["capacity_state"], "above_target")
        self.assertEqual(simulated_account_policy(16)["capacity_state"], "hard_breach")
        self.assertEqual(simulated_account_policy(16)["max_positions"], 15)

    def test_industry_exposure_uses_portfolio_weight_not_name_count(self):
        items = [
            {
                "code": "600001", "is_sim_holding": True,
                "position": {"market_value": 120_000},
                "sector": {"primary_industry": "汽车"},
            },
            {
                "code": "600002", "is_sim_holding": True,
                "position": {"market_value": 80_000},
                "sector": {"primary_industry": "汽车"},
            },
            {
                "code": "600003", "is_sim_holding": True,
                "position": {"market_value": 50_000},
                "sector": {"primary_industry": "银行"},
            },
        ]
        exposure = _industry_exposure(items, 1_000_000, "simulated")
        self.assertEqual(exposure[0]["industry"], "汽车")
        self.assertEqual(exposure[0]["position_count"], 2)
        self.assertEqual(exposure[0]["weight_pct"], 20.0)

    def test_relative_strength_is_ranked_across_one_decision_universe(self):
        items = [
            {"technical": {"return_5d_pct": value}}
            for value in (-2.0, 1.0, 5.0)
        ]
        _annotate_relative_strength(items)
        self.assertEqual(items[0]["technical"]["rs_5d_percentile"], 33.3)
        self.assertEqual(items[-1]["technical"]["rs_5d_percentile"], 100.0)


if __name__ == "__main__":
    unittest.main()
