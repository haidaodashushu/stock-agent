import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock,patch

from data import opportunity_trial as trial
from data import stock_research as research
from data.store.sqlite_store import StockStore
from data.trading_decision_repository import _compact_stock
from scripts.execute_trading_cycle import validate_simulated_decision
from tests.test_opportunity_trial import NOW,candidate,plan,quote


def update(context,**changes):
    return {"facts_version":context["facts_version"],"status":"ready",
            "thesis":"研究原始平台逻辑","company_view":"报告期20260630，盈利证据待核对",
            "trend_view":"日线平台结构","risks":"结构破坏风险",
            "refresh_condition":"新财报或趋势结构变化时重做研究",**changes}


class StockResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=StockStore(str(Path(self.tmp.name)/"test.db"))
        trial.ingest(self.store,[candidate()],NOW)

    def context(self,code="002185",now=NOW):
        return research.contexts(self.store,[code],trial.load_setups(self.store,now),now)[code]

    def record(self,code="002185",context=None,**changes):
        context=context or self.context(code)
        decision={"signals":[{"code":code,"research_update":update(context,**changes)}]}
        snapshot={"as_of":trial.stamp(NOW),"positions":[],"candidates":[{"code":code,"research":context}]}
        research.record_updates(self.store,decision,snapshot)

    def test_shared_research_reused_without_changing_account_plans(self):
        self.record()
        first=self.context()
        self.assertEqual(first["status"],"ready")
        for mode in ("live","simulated"):
            decision={"signals" if mode=="simulated" else "decisions":[{"code":"002185","action":"watch","watch_plan":plan(state="account_blocked" if mode=="live" else "watch")}]}
            trial.record_decision(self.store,mode,decision,{"as_of":trial.stamp(NOW),"positions":[]},{},NOW)
        self.assertEqual(self.context()["revision"],first["revision"])
        self.assertNotEqual(trial.load_plans(self.store,"live")["002185"]["plan"],trial.load_plans(self.store,"simulated")["002185"]["plan"])

    def test_financial_content_and_new_news_expire_research_not_fetch_timestamp(self):
        with self.store._get_conn() as conn:
            conn.execute("INSERT INTO financial_factors(code,period,revenue_yoy,updated_at) VALUES(?,?,?,?)",("002185","20260630",10,trial.stamp(NOW-timedelta(days=1))))
        self.record()
        with self.store._get_conn() as conn:
            conn.execute("UPDATE financial_factors SET updated_at=?",(trial.stamp(NOW),))
        self.assertEqual(self.context()["status"],"ready")
        with self.store._get_conn() as conn:
            conn.execute("UPDATE financial_factors SET revenue_yoy=20")
        self.assertIn("company_news_or_setup_changed",self.context()["reasons"])
        self.record()
        with self.store._get_conn() as conn:
            conn.execute("INSERT INTO news_events(code,title,score,created_at) VALUES(?,?,?,?)",("002185","重要新事实",3,trial.stamp(NOW)))
        self.assertEqual(self.context()["status"],"refresh_required")

    def test_expired_or_pending_research_is_not_ready(self):
        self.record()
        self.assertIn("research_expired",self.context(now=NOW+timedelta(days=12))["reasons"])
        self.record(status="data_pending")
        self.assertIn("research_data_pending",self.context()["reasons"])

    def test_other_account_cannot_overwrite_a_newer_revision(self):
        original=self.context()
        self.record(context=original)
        first=self.context()["revision"]
        self.record(context=original,thesis="另一个账户晚到的版本")
        self.assertEqual(self.context()["revision"],first)
        with self.store._get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM stock_research_history").fetchone()[0],1)

    def test_ready_candidates_expand_scope_but_new_research_stays_bounded(self):
        sources=[candidate(f"00218{i}") for i in range(10)]
        trial.ingest(self.store,sources,NOW)
        selected,*_=trial.candidate_scope(self.store,"simulated",[],[],now=NOW)
        self.assertEqual(len(selected),3)
        for row in sources[:6]:
            self.record(row["code"])
        selected,*_=trial.candidate_scope(self.store,"simulated",[],[],now=NOW)
        self.assertEqual(len(selected),8)
        self.assertLessEqual(sum(self.context(row["code"])["status"]!="ready" for row in selected),3)

    def test_research_validation_rejects_wrong_version_and_pending_buy(self):
        evidence={"code":"002185","research":self.context(),"quote":quote(),
                  "technical":{"date":"2026-09-17","quality":"verified_qfq"},
                  "selection":{"entry_route":"early_start","setup_stage":"actionable","buy_eligible":True}}
        context={"as_of":trial.stamp(NOW),"opportunity_trial":True,"positions":[],"candidates":[evidence]}
        row={"code":"002185","action":"buy","target_amount":20000,"watch_plan":plan(),"research_update":update(evidence["research"],facts_version="old")}
        with self.assertRaisesRegex(ValueError,"facts_version"):
            validate_simulated_decision({"signals":[row]},context)
        row["research_update"]=update(evidence["research"],status="data_pending")
        with self.assertRaisesRegex(ValueError,"research data pending"):
            validate_simulated_decision({"signals":[row]},context)
        row["research_update"]=update(evidence["research"])
        self.assertEqual(validate_simulated_decision({"signals":[row]},context)["signals"][0]["action"],"buy")

    def test_risk_exit_does_not_wait_for_research_completion(self):
        evidence={"code":"002185","research":self.context()}
        context={"as_of":trial.stamp(NOW),"opportunity_trial":True,"positions":[evidence],"candidates":[]}
        row={"code":"002185","action":"clear","watch_plan":plan(state="invalid",invalidation_reason="逻辑破坏")}
        self.assertEqual(validate_simulated_decision({"signals":[row]},context)["signals"][0]["action"],"clear")

    def test_compact_evidence_can_expand_original_financials_on_demand(self):
        self.record()
        item={"research":self.context(),"fundamental":{"period":"20260630","revenue_yoy":10}}
        compact=_compact_stock(item,trial.stamp(NOW))
        detailed=_compact_stock(item,trial.stamp(NOW),include_research_details=True)
        self.assertNotIn("revenue_yoy",compact["selection"]["fundamental"])
        self.assertEqual(detailed["selection"]["fundamental"]["revenue_yoy"],10)
        self.assertEqual(compact["research"],detailed["research"])

    def test_daily_computation_is_cached_until_verified_window_changes(self):
        from data.adjusted_daily import ensure_table
        with self.store._get_conn() as conn:
            ensure_table(conn)
            conn.execute("INSERT INTO adjusted_daily_windows VALUES(?,?,?,?,?)",("002185","2025-01-01","2026-09-17","qfq","2026-09-17 22:00:00"))
        compute=Mock(return_value={"daily_date":"2026-09-17","quality":"verified_qfq"})
        for _ in range(2): research.daily_technical(self.store,"002185",compute)
        self.assertEqual(compute.call_count,1)
        with self.store._get_conn() as conn:
            conn.execute("UPDATE adjusted_daily_windows SET end_date='2026-09-18'")
        research.daily_technical(self.store,"002185",compute)
        self.assertEqual(compute.call_count,2)

    def test_incremental_comparison_uses_completed_decision_of_same_account(self):
        research.record_observations(self.store,"simulated",{
            "as_of":trial.stamp(NOW),"positions":[{"code":"002185","quote":quote(16),
            "technical":{"date":"2026-09-17"},"research":{"facts_version":"v1"}}],"candidates":[]})
        later=NOW+timedelta(minutes=30)
        stock={"code":"002185","quote":quote(16.8,later),"technical":{"daily_date":"2026-09-17"},"research":{"facts_version":"v1"}}
        research.annotate_changes(self.store,"simulated",[stock],later)
        self.assertEqual(stock["changes_since_last_decision"]["price_change_pct"],5)
        self.assertFalse(stock["changes_since_last_decision"]["research_inputs_changed"])
        research.annotate_changes(self.store,"live",[stock],later)
        self.assertFalse(stock["changes_since_last_decision"]["available"])
        stock["quote"]=quote(16.8,NOW-timedelta(days=1))
        research.annotate_changes(self.store,"simulated",[stock],later)
        self.assertIsNone(stock["changes_since_last_decision"]["price_change_pct"])

    def test_changed_candidate_board_wakes_monitor_without_waiting_for_cron(self):
        from datetime import datetime
        from scripts import refresh_candidate_board as board
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None): return NOW
        with patch.object(board,"datetime",Clock),patch("sys.argv",["refresh_candidate_board.py"]),patch.object(trial,"enabled",return_value=True),patch.object(board,"refresh_candidate_board",return_value={"status":"ready"}) as refresh,patch.object(board.subprocess,"run") as run:
            self.assertEqual(board.main(),0)
            self.assertIn("--wake",run.call_args.args[0])
            run.reset_mock()
            refresh.return_value={"status":"unchanged"}
            self.assertEqual(board.main(),0)
            run.assert_not_called()

    def test_wake_starts_only_guarded_consumers_and_never_during_closed_market(self):
        from scripts import run_opportunity_monitor as monitor
        with patch.object(monitor,"ROOT",Path(self.tmp.name)),patch.object(trial,"enabled",return_value=True),patch.object(monitor,"is_actionable_trading_time",return_value=False) as actionable,patch.object(monitor.subprocess,"Popen") as launch:
            monitor.wake_workers()
            launch.assert_not_called()
            actionable.return_value=True
            monitor.wake_workers()
            self.assertEqual(launch.call_count,2)
            for call in launch.call_args_list:
                self.assertIn("--event",call.args[0])
                self.assertTrue(call.kwargs["start_new_session"])

    def test_new_opportunity_bypasses_cooldown_but_respects_daily_budget(self):
        trial.observe(self.store,"simulated",{"002185":quote()},{},NOW)
        trial.finish_events(self.store,trial.claim_events(self.store,"simulated",NOW),True)
        later=NOW+timedelta(minutes=1)
        trial.ingest(self.store,[candidate("002186",str(NOW.date()))],later)
        trial.observe(self.store,"simulated",{"002186":quote(now=later)},{},later)
        cfg=trial.settings()|{"max_event_runs_per_mode_per_day":1}
        with patch.object(trial,"settings",return_value=cfg):
            self.assertEqual(trial.claim_events(self.store,"simulated",later),[])
        rows=trial.claim_events(self.store,"simulated",later)
        self.assertEqual([r["kind"] for r in rows],["new_opportunity"])


if __name__=="__main__":
    unittest.main()
