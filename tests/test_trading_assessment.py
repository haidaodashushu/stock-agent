import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data import trading_assessment as assessment
from data.market_regime import classify_market_regime
from data.store.sqlite_store import StockStore
from scripts.execute_trading_cycle import validate_simulated_decision,validate_live_decision
from tests.test_opportunity_trial import NOW,plan,quote
from tests.test_stock_research import update
from data import trading_decision_repository as repository


def fixture():
    stock={"code":"002185","quote":quote(10),
           "technical":{"date":"2026-09-17","quality":"verified_qfq","ma5":9.5},
           "intraday":{"half_hour":{"volume_ratio":1.6}},
           "selection":{"entry_route":"early_start","setup_stage":"actionable","buy_eligible":True},
           "research":{"status":"ready","revision":"r1","facts_version":"v1",
                       "profile":{"quality":{"grade":"moderate","reason":"原研究依据"}}}}
    context={"mode":"simulated","as_of":NOW.isoformat(sep=" "),"opportunity_trial":True,
             "decision_assessment_required":True,"account":{"total_equity":100000},"positions":[],"candidates":[stock]}
    row={"code":"002185","action":"buy","confidence":"strong","target_amount":10000,
         "watch_plan":plan(invalidation_below=9),
         "assessment":{
             "research":{"grade":"moderate","reason":"原研究依据"},
             "timing":{"grade":"ready","reason":"结构确认"},
             "evidence":{"grade":"reliable","reason":"本轮行情与独立量能可核对"},
             "portfolio":{"grade":"fit","reason":"现金与行业暴露允许"},
             "route_reason":"萌芽结构改善后有承接，失效则重评",
             "confidence_reason":"两类证据支持当前动作，非盈利概率",
             "confirmations":[
                 {"family":"price_structure","direction":"support","source_path":"technical.ma5","basis":"站上改善的短期结构"},
                 {"family":"volume","direction":"support","source_path":"intraday.half_hour.volume_ratio","basis":"独立量能确认"},
             ]},
         "position_plan":{"amount_reason":"增量一万元符合账户规模","invalidation_price":9,
                          "invalidation_basis":"原结构参考位","risk_budget_reason":"需承受隔夜与跳空风险","concentration_reason":"不增加过度行业暴露"}}
    return row,context


class TradingAssessmentTests(unittest.TestCase):
    def test_all_four_dimensions_are_required_and_no_weighted_offset(self):
        for dimension,blocked in [("research","weak"),("timing","wait"),("evidence","insufficient"),("evidence","conflicting"),("portfolio","blocked")]:
            with self.subTest(dimension=dimension,blocked=blocked):
                row,context=fixture()
                row["assessment"][dimension]["grade"]=blocked
                if dimension=="research": context["candidates"][0]["research"]["profile"]["quality"]["grade"]=blocked
                with self.assertRaises(ValueError): validate_simulated_decision({"signals":[row]},context)
        row,context=fixture()
        del row["assessment"]["timing"]
        with self.assertRaisesRegex(ValueError,"timing.grade"):
            validate_simulated_decision({"signals":[row]},context)

    def test_buy_returns_a_reproducible_incremental_loss_scenario(self):
        row,context=fixture()
        result=validate_simulated_decision({"signals":[row]},context)["signals"][0]
        scenario=result["position_plan"]["scenario"]
        self.assertEqual(scenario["loss_to_invalidation"],1000)
        self.assertEqual(scenario["equity_pct"],1)
        self.assertEqual(result["assessment"]["research"]["grade"],"moderate")

    def test_live_volume_precedes_amount_in_scenario_like_existing_executor(self):
        row,context=fixture();context["mode"]="live"
        row["volume"]=200
        result=validate_live_decision({"decisions":[row]},context)["decisions"][0]
        self.assertEqual(result["position_plan"]["scenario"]["requested_notional"],2000)

    def test_uncertain_invalidation_is_explicit_and_no_scenario_is_invented(self):
        row,context=fixture();row["position_plan"]["invalidation_price"]=None
        result=validate_simulated_decision({"signals":[row]},context)["signals"][0]
        self.assertFalse(result["position_plan"]["scenario"]["available"])
        for bad in (0,10,11,float("nan"),True):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                row["position_plan"]["invalidation_price"]=bad
                validate_simulated_decision({"signals":[row]},context)
        row["position_plan"]["invalidation_price"]=None
        row["target_amount"]=float("nan")
        with self.assertRaisesRegex(ValueError,"finite positive"):
            validate_simulated_decision({"signals":[row]},context)

    def test_strong_confidence_cannot_count_correlated_price_indicators_twice(self):
        row,context=fixture()
        row["assessment"]["confirmations"][1]=copy.deepcopy(row["assessment"]["confirmations"][0])
        with self.assertRaisesRegex(ValueError,"family invalid or repeated"):
            validate_simulated_decision({"signals":[row]},context)
        row["assessment"]["confirmations"].pop()
        with self.assertRaisesRegex(ValueError,"independent evidence family"):
            validate_simulated_decision({"signals":[row]},context)

    def test_confirmation_must_reference_real_nonempty_evidence_of_that_family(self):
        for path in ("intraday.half_hour.nonexistent","technical.ma5","intraday.half_hour.volume_ratio"):
            with self.subTest(path=path):
                row,context=fixture()
                row["assessment"]["confirmations"][1]["source_path"]=path
                if path.endswith("volume_ratio"): context["candidates"][0]["intraday"]["half_hour"]["volume_ratio"]=None
                with self.assertRaises(ValueError): validate_simulated_decision({"signals":[row]},context)

    def test_partial_auxiliary_evidence_allows_medium_confidence_entry(self):
        row,context=fixture();row["confidence"]="medium"
        row["assessment"]["evidence"]["grade"]="partial"
        row["assessment"]["confirmations"].pop()
        self.assertEqual(validate_simulated_decision({"signals":[row]},context)["signals"][0]["action"],"buy")

    def test_cached_flow_cannot_be_presented_as_current_confirmation(self):
        row,context=fixture()
        context["candidates"][0]["fund_flow"]={"status":"cached","main_net":12345}
        row["assessment"]["confirmations"][1]["source_path"]="fund_flow.main_net"
        with self.assertRaisesRegex(ValueError,"background"):
            validate_simulated_decision({"signals":[row]},context)

    def test_fund_date_verification_survives_model_and_executor_views(self):
        for status, verified, allowed in [("available", True, True), ("cached", True, False),
                                           ("available", False, False), ("available", None, False)]:
            with self.subTest(status=status, verified=verified):
                row, context = fixture()
                item = {"code": row["code"], "is_candidate": True, "fund_flow": {
                    "status": status, "source_date_verified": verified,
                    "detail": {"date": "20260917", "main_net_inflow": 12345}}}
                as_of = context["as_of"]
                snapshot = ({"stage": "1012"}, [{"payload": json.dumps(item), "updated_at": as_of}], as_of)
                model_flow = repository._compact_stock(item, as_of)["fund_flow"]
                with patch.object(repository, "_connect"), patch.object(repository, "_snapshot", return_value=snapshot):
                    executor_flow = repository.build_execution_context(as_of, "simulated")["candidates"][0]["fund_flow"]
                self.assertEqual(model_flow, executor_flow)
                context["candidates"][0]["fund_flow"] = executor_flow
                row["assessment"]["confirmations"][1]["source_path"] = "fund_flow.main_net"
                if allowed:
                    self.assertEqual(validate_simulated_decision({"signals": [row]}, context)["signals"][0]["action"], "buy")
                else:
                    with self.assertRaisesRegex(ValueError, "002185: fund_flow.main_net.*source_date_verified"):
                        validate_simulated_decision({"signals": [row]}, context)
                    row["action"] = "watch"
                    row["assessment"]["confirmations"].pop()
                    self.assertEqual(validate_simulated_decision({"signals": [row]}, context)["signals"][0]["action"], "watch")

    def test_research_grade_changes_require_a_versioned_update(self):
        row,context=fixture();row["assessment"]["research"]["grade"]="strong"
        with self.assertRaisesRegex(ValueError,"versioned research_update"):
            validate_simulated_decision({"signals":[row]},context)
        row["research_update"]=update(context["candidates"][0]["research"])
        result=validate_simulated_decision({"signals":[row]},context)["signals"][0]
        self.assertEqual(result["research_update"]["quality"]["grade"],"strong")

    def test_risk_exit_can_proceed_with_insufficient_data_but_requires_trigger(self):
        row,context=fixture();context["positions"]=context.pop("candidates");context["candidates"]=[]
        row["action"]="clear";row["assessment"]["evidence"]["grade"]="insufficient"
        with self.assertRaisesRegex(ValueError,"exit_plan"):
            validate_simulated_decision({"signals":[row]},context)
        row["exit_plan"]={"trigger":"risk_reduction","reason":"组合暴露需要降低","why_now":"本轮风险变化且当前可卖"}
        self.assertEqual(validate_simulated_decision({"signals":[row]},context)["signals"][0]["action"],"clear")

    def test_assessments_are_idempotent_account_scoped_and_keep_research_version(self):
        row,context=fixture()
        decision=validate_simulated_decision({"signals":[row]},context)
        with tempfile.TemporaryDirectory() as folder:
            store=StockStore(str(Path(folder)/"test.db"))
            for mode in ("simulated","simulated","live"):
                assessment.record(store,mode,decision,context)
            with store._get_conn() as conn:
                rows=conn.execute("SELECT * FROM trading_assessments ORDER BY mode").fetchall()
                self.assertEqual(len(rows),2)
                self.assertEqual(json.loads(rows[0]["payload"])["research_revision"],"r1")


class MarketLabelBoundaryTests(unittest.TestCase):
    def test_missing_and_invalid_indices_are_unknown_not_neutral_evidence(self):
        for inputs in ({},{"sh000001":{"name":"上证","change_pct":float("nan")}},
                       {"sh000001":{"name":"上证","change_pct":True}}):
            result=classify_market_regime(inputs)
            self.assertEqual(result["data_status"],"unavailable")
            self.assertFalse(result["classification_usable"])
            self.assertIn("占位",result["summary"])

    def test_narrow_indices_do_not_imply_broad_market_or_bull_cycle(self):
        result=classify_market_regime({c:{"name":c,"change_pct":2} for c in ("sz399006","sh000688","sh000016")})
        self.assertEqual(result["data_status"],"partial")
        self.assertFalse(result["classification_usable"])
        self.assertFalse(result["stock_breadth_available"])
        self.assertIn("not bull/bear",result["interpretation_scope"])
