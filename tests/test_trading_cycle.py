import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from account.models import Portfolio
from data.store.sqlite_store import StockStore
from data.live_manual_account import execution_deviation_warnings
from data.trading_decision_repository import _compact_stock
from scripts.execute_live_trade_decision import (
    execute as execute_live_intent,
)
from scripts.execute_trading_cycle import (
    extract_json,
    render_report,
    validate_live_decision,
    validate_simulated_decision,
)


class TradingCycleTests(unittest.TestCase):
    def setUp(self):
        self.simulated_context = {
            "status": "ok",
            "mode": "simulated",
            "stage": "1030",
            "as_of": "2026-07-14 10:30:10",
            "account": {"total_equity": 1_000_000},
            "positions": [{"code": "000001", "name": "模拟持仓股"}],
            "candidates": [{"code": "000002", "name": "候选股"}],
        }
        self.live_context = {
            "status": "ok",
            "mode": "live",
            "stage": "1032",
            "as_of": "2026-07-14 10:32:10",
            "account": {"total_equity": 20_000, "position_count": 1},
            "positions": [{"code": "000003", "name": "实盘持仓股", "volume": 100}],
            "candidates": [{"code": "000002", "name": "候选股"}],
        }

    def test_extracts_fenced_json(self):
        self.assertEqual(extract_json("```json\n{\"a\": 1}\n```"), {"a": 1})

    def test_validates_simulated_decision(self):
        payload = {
            "market_view": {"regime": "neutral", "summary": "震荡，控制仓位"},
            "signals": [{
                "code": "1", "name": "模拟持仓股", "action": "hold", "confidence": "medium",
            }],
            "report": {"focus": ["持仓观察"], "risk": "震荡放大"},
        }
        result = validate_simulated_decision(payload, self.simulated_context)
        self.assertEqual(result["signals"][0]["code"], "000001")
        self.assertNotIn("decisions", result)

    def test_validates_live_decision(self):
        payload = {
            "market_view": {"regime": "weak", "summary": "实盘保护本金"},
            "decisions": [{
                "code": "000003", "name": "实盘持仓股", "action": "sell",
                "confidence": "strong", "sell_pct": 1, "reason": "跌破关键位",
            }],
        }
        result = validate_live_decision(payload, self.live_context)
        self.assertEqual(result["decisions"][0]["action"], "sell")
        self.assertNotIn("signals", result)

    def test_decision_requires_complete_reviewed_codes_in_production_context(self):
        context = dict(self.simulated_context)
        context["required_evidence_codes"] = ["000001", "000002"]
        payload = {
            "signals": [{"code": "000001", "action": "hold"}],
        }
        with self.assertRaisesRegex(ValueError, "reviewed_codes"):
            validate_simulated_decision(payload, context)
        payload["reviewed_codes"] = ["000001"]
        with self.assertRaisesRegex(ValueError, "missing=000002"):
            validate_simulated_decision(payload, context)
        payload["reviewed_codes"] = ["000002", "000001"]
        with self.assertRaisesRegex(ValueError, "signals.*missing=000002"):
            validate_simulated_decision(payload, context)
        payload["signals"].append({"code": "000002", "action": "watch"})
        decision = validate_simulated_decision(payload, context)
        self.assertEqual(decision["reviewed_codes"], ["000001", "000002"])

    def test_live_decision_requires_one_row_for_every_required_code(self):
        context = dict(self.live_context)
        context["required_evidence_codes"] = ["000003", "000002"]
        payload = {
            "reviewed_codes": ["000003", "000002"],
            "decisions": [{"code": "000003", "action": "hold"}],
        }
        with self.assertRaisesRegex(ValueError, "decisions.*missing=000002"):
            validate_live_decision(payload, context)
        payload["decisions"].append({"code": "000002", "action": "watch"})
        self.assertEqual(
            len(validate_live_decision(payload, context)["decisions"]), 2,
        )

    def test_deterministic_market_regime_overrides_model_reclassification(self):
        context = dict(self.simulated_context)
        context["market_regime"] = {
            "regime": "strong", "source": "deterministic_indices.v1",
        }
        payload = {
            "market_view": {"regime": "weak", "summary": "模型仍负责账户解释"},
            "signals": [{"code": "000001", "action": "hold"}],
        }
        decision = validate_simulated_decision(payload, context)
        self.assertEqual(decision["market_view"]["regime"], "strong")
        self.assertEqual(
            decision["market_view"]["source"], "deterministic_indices.v1",
        )

    def test_executor_hard_rejects_non_actionable_candidate_buy(self):
        context = dict(self.simulated_context)
        context["candidates"] = [{
            "code": "000002", "name": "候选股",
            "selection": {
                "entry_route": "early_start", "setup_stage": "warming",
                "buy_eligible": False,
            },
        }]
        payload = {"signals": [
            {"code": "000001", "action": "hold"},
            {"code": "000002", "action": "buy", "target_amount": 10_000},
        ]}
        with self.assertRaisesRegex(ValueError, "not actionable/buy_eligible"):
            validate_simulated_decision(payload, context)

    def test_simulated_rejects_live_only_code(self):
        payload = {"signals": [{"code": "000003", "action": "watch"}]}
        with self.assertRaisesRegex(ValueError, "not present"):
            validate_simulated_decision(payload, self.simulated_context)

    def test_live_rejects_simulated_only_code(self):
        payload = {"decisions": [{"code": "000001", "action": "watch"}]}
        with self.assertRaisesRegex(ValueError, "not present"):
            validate_live_decision(payload, self.live_context)

    def test_rejects_conflicting_simulated_actions(self):
        payload = {"signals": [
            {"code": "000001", "action": "hold"},
            {"code": "000001", "action": "sell"},
        ]}
        with self.assertRaisesRegex(ValueError, "multiple actions"):
            validate_simulated_decision(payload, self.simulated_context)

    def test_simulated_buy_requires_explicit_target_amount(self):
        payload = {"signals": [
            {"code": "000001", "action": "hold"},
            {"code": "000002", "action": "buy"},
        ]}
        with self.assertRaisesRegex(ValueError, "requires target_amount"):
            validate_simulated_decision(payload, self.simulated_context)

    @staticmethod
    def _capacity_context(position_count: int) -> dict:
        return {
            "status": "ok",
            "mode": "simulated",
            "stage": "1030",
            "as_of": "2026-07-31 10:30:00",
            "account": {"total_equity": 1_000_000, "position_count": position_count},
            "positions": [
                {"code": f"600{index:03d}", "name": f"持仓{index}"}
                for index in range(position_count)
            ],
            "candidates": [{"code": "000002", "name": "候选股"}],
        }

    @staticmethod
    def _capacity_signals(position_count: int, replacement_action: str = "hold") -> list[dict]:
        signals = [
            {
                "code": f"600{index:03d}",
                "action": replacement_action if index == 0 else "hold",
                "confidence": "medium",
                "sell_pct": 0.5,
            }
            for index in range(position_count)
        ]
        signals.append({
            "code": "000002",
            "action": "buy",
            "confidence": "strong",
            "target_amount": 50_000,
        })
        return signals

    def test_target_range_buy_requires_explicit_strong_replacement(self):
        context = self._capacity_context(12)
        payload = {
            "portfolio_review": {
                "current_count": 12,
                "capacity_state": "within_target",
                "weakest_holdings": [{"code": "600000", "reason": "相对强度落后"}],
            },
            "signals": self._capacity_signals(12),
        }
        with self.assertRaisesRegex(ValueError, "replacement"):
            validate_simulated_decision(payload, context)

        payload["signals"][0]["action"] = "reduce"
        payload["signals"][-1].update({
            "replacement_code": "600000",
            "replacement_edge": "strong",
            "replacement_reason": "候选趋势和逻辑证据均强于旧仓",
        })
        decision = validate_simulated_decision(payload, context)
        self.assertEqual(decision["signals"][-1]["replacement_code"], "600000")

    def test_hard_cap_requires_full_exit_before_replacement_entry(self):
        context = self._capacity_context(15)
        payload = {
            "portfolio_review": {
                "current_count": 15,
                "capacity_state": "above_target",
                "weakest_holdings": [{"code": "600000", "reason": "逻辑衰减"}],
            },
            "signals": self._capacity_signals(15, replacement_action="reduce"),
        }
        payload["signals"][-1].update({
            "replacement_code": "600000",
            "replacement_edge": "strong",
            "replacement_reason": "候选明显更强",
        })
        with self.assertRaisesRegex(ValueError, "exceeds hard max"):
            validate_simulated_decision(payload, context)

        payload["signals"][0]["action"] = "clear"
        decision = validate_simulated_decision(payload, context)
        self.assertEqual(decision["portfolio_review"]["current_count"], 15)

    def test_legacy_hard_breach_can_execute_cleanup_without_new_buys(self):
        context = self._capacity_context(17)
        signals = self._capacity_signals(17)[:-1]
        signals[0].update({"action": "reduce", "sell_pct": 0.5})
        payload = {
            "portfolio_review": {
                "current_count": 17,
                "capacity_state": "hard_breach",
                "weakest_holdings": [{"code": "600000", "reason": "重复减仓待清理"}],
            },
            "signals": signals,
        }
        decision = validate_simulated_decision(payload, context)
        self.assertEqual(decision["signals"][0]["action"], "reduce")

    def test_live_buy_requires_explicit_size(self):
        payload = {"decisions": [
            {"code": "000003", "action": "hold"},
            {"code": "000002", "action": "buy"},
        ]}
        with self.assertRaisesRegex(ValueError, "requires target_amount or volume"):
            validate_live_decision(payload, self.live_context)

    def test_rejects_decision_that_omits_its_own_position(self):
        with self.assertRaisesRegex(ValueError, "simulated decision omitted"):
            validate_simulated_decision(
                {"signals": [{"code": "000002", "action": "watch"}]},
                self.simulated_context,
            )
        with self.assertRaisesRegex(ValueError, "live decision omitted"):
            validate_live_decision(
                {"decisions": [{"code": "000002", "action": "watch"}]},
                self.live_context,
            )

    def test_compacts_database_state_shape(self):
        compact = _compact_stock({
            "code": "1",
            "name": "持仓股",
            "is_sim_holding": True,
            "position": {"volume": 100, "cost_price": 10, "profit_pct": 2},
            "quote": {"price": 10.2, "source": "tencent"},
            "screen": {"score": 8, "signal_type": "buy", "tags": "金叉|放量"},
            "intraday": {"half_hour": {"available": True, "volume_price_signal": "volume_price_up"}},
            "news": [{
                "title": "公司发布公告",
                "summary": "正文显示公司中标重大合同。",
                "evidence_snippets": ["公司中标重大合同。"],
                "content_available": True,
                "analysis_basis": "title_content",
                "content_digest": "abc123",
                "source": "交易所",
                "published_at": "2026-07-14 09:00:00",
                "sentiment": "positive",
                "score": 3,
                "risk": "high",
                "tags": ["重大合同"],
            }],
        }, "2026-07-14 10:30:10")
        self.assertEqual(compact["code"], "000001")
        self.assertEqual(compact["sim_position"]["volume"], 100)
        self.assertEqual(compact["selection"]["tags"], ["金叉", "放量"])
        self.assertEqual(compact["news"][0]["summary"], "正文显示公司中标重大合同。")
        self.assertEqual(compact["news"][0]["source"], "交易所")
        self.assertEqual(compact["news"][0]["analysis_basis"], "title_content")

    def test_compact_stock_exposes_only_canonical_entry_route(self):
        compact = _compact_stock({
            "code": "000001",
            "screen": {"extra": {"selector": {
                "route": "low_logic", "entry_route": "strong_continuation",
            }}},
        }, "2026-08-27 10:00:00")
        self.assertEqual(
            compact["selection"]["entry_route"], "strong_continuation",
        )
        self.assertNotIn("route", compact["selection"])

    def test_report_is_account_specific(self):
        decision = {
            "market_view": {"summary": "000001继续观察"},
            "signals": [
                {"code": "000001", "name": "模拟持仓股", "action": "hold"},
                {"code": "000002", "name": "候选股", "action": "watch"},
            ],
            "report": {
                "focus": ["000001走强，000002等待确认"],
                "risk": "000002存在追高风险",
            },
        }
        sim = {
            "account": {"total_equity": 1_000_000, "available_cash": 900_000, "position_count": 1},
            "results": [],
        }
        text = render_report(self.simulated_context, "simulated", decision, sim)
        self.assertIn("模拟盘半小时操盘", text)
        self.assertIn("模拟持仓股（000001）继续观察", text)
        self.assertIn("模拟持仓股（000001）走强，候选股（000002）等待确认", text)
        self.assertIn("候选股（000002）存在追高风险", text)
        self.assertIn("关注", text)
        self.assertIn("本轮无成交", text)

    def test_report_does_not_duplicate_an_existing_name_and_code_label(self):
        decision = {
            "market_view": {"summary": "模拟持仓股（000001）继续观察"},
            "report": {"focus": ["模拟持仓股（000001）等待确认"]},
        }
        text = render_report(self.simulated_context, "simulated", decision, {"results": []})
        self.assertNotIn("模拟持仓股（模拟持仓股", text)
        self.assertIn("模拟持仓股（000001）等待确认", text)

    def test_preserves_all_concise_focus_items(self):
        payload = {
            "signals": [{"code": "000001", "action": "hold"}],
            "report": {"focus": ["变化一", "变化二", "变化三", "变化四"]},
        }
        decision = validate_simulated_decision(payload, self.simulated_context)
        self.assertEqual(
            decision["report"]["focus"],
            ["变化一", "变化二", "变化三", "变化四"],
        )

    def test_portfolio_summary_uses_persisted_total_order_count(self):
        portfolio = Portfolio(total_order_count=96)
        self.assertEqual(portfolio.summary()["total_orders"], 96)

    def test_live_executor_keeps_limit_price_advisory(self):
        config = {
            "initial_cash": 20_000,
            "max_positions": None,
            "min_lot": 100,
            "blocked_boards": ["300", "301", "688", "8", "4"],
        }
        payload = {"decisions": [{
            "code": "600001",
            "name": "测试股票",
            "action": "buy",
            "confidence": "medium",
            "price": 10,
            "limit_price": 10,
            "volume": 100,
            "expire_minutes": 15,
        }]}
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            with (
                patch("scripts.execute_live_trade_decision.StockStore", return_value=store),
                patch("scripts.execute_live_trade_decision.fetch_live_prices", return_value={"600001": 10.01}),
                patch("scripts.execute_live_trade_decision.load_config", return_value=config),
                patch("data.live_manual_account.load_config", return_value=config),
            ):
                result = execute_live_intent(payload)
            self.assertEqual(result["summary"]["created_intents"], 1)
            self.assertEqual(result["summary"]["rejected"], 0)
            self.assertEqual(result["results"][0]["price"], 10.01)
            conn = store._get_conn()
            try:
                row = conn.execute(
                    "SELECT suggested_price, limit_price FROM live_trade_intents"
                ).fetchone()
                self.assertEqual(float(row["suggested_price"]), 10.01)
                self.assertEqual(float(row["limit_price"]), 10.0)
            finally:
                conn.close()

    def test_live_executor_rejects_material_price_drift_before_intent_creation(self):
        config = {
            "initial_cash": 20_000, "capital_flows": [], "max_positions": None,
            "min_lot": 100, "blocked_boards": ["688", "8", "4"],
            "max_decision_price_drift_pct": 2.0,
            "default_buy_price_buffer_pct": 1.5,
        }
        payload = {"decisions": [{
            "code": "600001", "name": "测试股票", "action": "buy",
            "confidence": "strong", "price": 10.0, "limit_price": 0,
            "volume": 100, "expire_minutes": 15,
        }]}
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            with (
                patch("scripts.execute_live_trade_decision.StockStore", return_value=store),
                patch("scripts.execute_live_trade_decision.fetch_live_prices", return_value={"600001": 9.7}),
                patch("scripts.execute_live_trade_decision.load_config", return_value=config),
                patch("data.live_manual_account.load_config", return_value=config),
            ):
                result = execute_live_intent(payload)
            self.assertEqual(result["summary"]["created_intents"], 0)
            self.assertIn("需重新分析", result["results"][0]["errors"][0])

    def test_real_fill_outside_advice_is_recordable_but_warned(self):
        class Intent(dict):
            def keys(self):
                return super().keys()

        warnings = execution_deviation_warnings(Intent({
            "action": "buy", "suggested_price": 16.83,
            "limit_price": 16.83, "suggested_volume": 300,
        }), 18.17, 300, {"max_decision_price_drift_pct": 2.0})
        self.assertTrue(any("高于建议最高价" in item for item in warnings))
        self.assertTrue(any("偏离建议价7.96%" in item for item in warnings))

    def test_live_executor_rejects_same_day_a_share_sell(self):
        config = {
            "initial_cash": 20_000,
            "capital_flows": [],
            "max_positions": None,
            "min_lot": 100,
            "blocked_boards": ["688", "8", "4"],
        }
        payload = {"decisions": [{
            "code": "600001", "name": "测试股票", "action": "sell",
            "confidence": "strong", "price": 9.5, "volume": 100,
            "expire_minutes": 15,
        }]}
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            conn = store._get_conn()
            conn.execute(
                """INSERT INTO live_trade_intents
                   (intent_id,code,name,action,suggested_price,suggested_volume,
                    suggested_amount,status,filled_price,filled_volume,filled_amount,filled_at)
                   VALUES ('same-day-buy','600001','测试股票','buy',10,300,3000,
                           'filled',10,300,3000,?)""",
                (f"{date.today().isoformat()} 09:50:00",),
            )
            conn.commit()
            conn.close()
            with (
                patch("scripts.execute_live_trade_decision.StockStore", return_value=store),
                patch("scripts.execute_live_trade_decision.fetch_live_prices", return_value={"600001": 9.5}),
                patch("scripts.execute_live_trade_decision.load_config", return_value=config),
                patch("data.live_manual_account.load_config", return_value=config),
            ):
                result = execute_live_intent(payload)

            self.assertEqual(result["summary"]["created_intents"], 0)
            self.assertEqual(result["summary"]["rejected"], 1)
            self.assertIn("A股T+1可卖数量不足", result["results"][0]["errors"][0])
            conn = store._get_conn()
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM live_trade_intents WHERE action='sell'").fetchone()[0],
                    0,
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
