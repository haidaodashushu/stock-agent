from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "agent_trading_policy.md"
ENTRY_POLICY_PATH = ROOT / "config" / "agent_stock_entry_policy.md"
SIMULATED_PROMPT_PATH = ROOT / "config" / "agent_simulated_trading_prompt.md"
LIVE_PROMPT_PATH = ROOT / "config" / "agent_live_trading_prompt.md"


class TradingPolicyBalanceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = (
            ENTRY_POLICY_PATH.read_text(encoding="utf-8") + "\n" +
            POLICY_PATH.read_text(encoding="utf-8")
        )
        cls.simulated_prompt = SIMULATED_PROMPT_PATH.read_text(encoding="utf-8")
        cls.live_prompt = LIVE_PROMPT_PATH.read_text(encoding="utf-8")

    def test_single_stock_missing_data_does_not_freeze_other_stocks(self):
        self.assertIn("账户状态、市场状态、`as_of` 版本或整体决策范围失效时，整轮不交易", self.policy)
        self.assertIn("单只股票的关键行情缺失时，只禁止该股票交易", self.policy)
        self.assertIn("不得因此冻结其他证据完整的股票", self.policy)

    def test_recent_activity_is_context_not_an_automatic_veto(self):
        self.assertIn("近期同向动作只作为组合风险背景", self.policy)
        self.assertIn("不是自动保持或观察的理由", self.policy)
        self.assertIn("不得仅因账户此前买过另一只股票", self.policy)

    def test_positive_and_risk_evidence_are_assessed_by_strength(self):
        self.assertIn("分别评估正面证据和风险证据的强度、可靠性与持续性", self.policy)
        self.assertIn("一般性分歧不自动否决", self.policy)
        self.assertIn("不得只提高 `confidence` 而始终不改变实际动作", self.policy)

    def test_idle_cash_opportunity_cost_is_considered_without_forcing_deployment(self):
        self.assertIn("持续持有大量现金的机会成本", self.policy)
        self.assertIn("低仓位本身不是买入理由", self.policy)
        self.assertIn("不得为了提高仓位而凑单", self.policy)

    def test_position_size_follows_opportunity_instead_of_recent_small_trades(self):
        self.assertIn("不得把近期交易金额或股数当作固定模板", self.policy)
        self.assertIn("小仓不是谨慎的默认答案", self.policy)
        self.assertIn("不得默认使用最小一手试仓", self.live_prompt)
        self.assertIn("实盘以持续提高账户收益为目标", self.live_prompt)
        self.assertIn("脱离账户规模的固定小额试仓", self.simulated_prompt)

    def test_live_prompt_treats_external_funding_as_capital_not_profit(self):
        self.assertIn("`net_external_cash_flow`", self.live_prompt)
        self.assertIn("`net_contributed_capital`", self.live_prompt)
        self.assertIn("外部入金不是交易收益", self.live_prompt)
        self.assertIn("新增现金也不是必须立即买入的理由", self.live_prompt)

    def test_live_t1_sellable_volume_is_a_hard_constraint(self):
        self.assertIn("`available_to_sell` 是A股T+1", self.live_prompt)
        self.assertIn("`available_to_sell=0` 时不得输出 `sell`", self.live_prompt)
        self.assertIn("可卖数量为0时不得输出卖出或减仓动作", self.policy)

    def test_complete_candidate_review_and_continuation_guards_are_explicit(self):
        self.assertIn("`reviewed_codes` 原样列出全部", self.policy)
        self.assertIn("从日内高点回撤5%以上", self.policy)
        self.assertIn("tradeable_positive=false", self.policy)
        self.assertIn("实时价格相对模型决策价变化超过", self.live_prompt)
        self.assertIn("不是建议单生成的硬拦截", self.live_prompt)
        self.assertNotIn("高于买入限价时", self.live_prompt)

    def test_trading_and_promotion_share_one_entry_policy(self):
        trading_script = (ROOT / "scripts" / "hermes_trading_cycle.sh").read_text()
        promotion_script = (ROOT / "scripts" / "hermes_candidate_promotion.sh").read_text()
        self.assertIn("agent_stock_entry_policy.md", trading_script)
        self.assertIn("agent_stock_entry_policy.md", promotion_script)
        self.assertIn("stock-candidate-promotion", promotion_script)

    def test_afternoon_time_is_not_itself_a_trade_veto(self):
        self.assertIn("不得仅因时间较晚而否决交易", self.policy)
        self.assertIn("午后若出现动能衰减", self.policy)

    def test_profitable_intact_trend_defaults_to_holding(self):
        self.assertIn("盈利仓位以继续持有为默认选择", self.policy)
        self.assertIn("短暂跌破 MA5、短暂跌破 VWAP 或单时点资金流转弱", self.policy)
        self.assertIn("不足以单独触发减仓", self.policy)
        self.assertIn("明确负面逻辑、决定性趋势结构破位或严重量价恶化", self.policy)
        self.assertIn("应及时按证据强弱减仓或卖出", self.policy)

    def test_portfolio_competition_and_decay_exits_are_explicit(self):
        self.assertIn("将持仓和候选放入同一个机会集合比较", self.policy)
        self.assertIn("相对强度排名持续落后", self.policy)
        self.assertIn("被替换或减持的旧仓", self.policy)
        self.assertIn("低权重", self.policy)
        self.assertIn("零碎仓位", self.policy)
        self.assertIn("目标持仓为10—12只", self.simulated_prompt)
        self.assertIn("15只是执行层硬上限", self.simulated_prompt)
        self.assertIn("replacement_code", self.simulated_prompt)

    def test_policy_does_not_introduce_numeric_sizing_bands(self):
        forbidden_patterns = [
            r"强信号.{0,20}\d+%",
            r"中等信号.{0,20}\d+%",
            r"目标总仓位.{0,20}\d+%",
            r"初始买入.{0,20}\d+%",
            r"(?:买入|加仓|计划资金|计划金额).{0,20}\d+(?:\.\d+)?%",
            r"(?:买入|加仓|计划资金|计划金额).{0,20}\d+(?:\.\d+)?万?元",
            r"(?:目标|最低|最多|上限).{0,10}(?:仓位|持仓数量).{0,20}\d+",
        ]
        for pattern in forbidden_patterns:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, self.policy))


if __name__ == "__main__":
    unittest.main()
