import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from data import opportunity_trial as trial
from data.store.sqlite_store import StockStore
from data.trading_data_quality import summarize_minutes,valid_quote
from data.adjusted_daily import fetch_window,save_window
from data.adapters.iwencai_adapter import IwenCaiAdapter
from data.fund_flow_filter import FundFlowFilter
from scripts.execute_trading_cycle import validate_simulated_decision
from scripts.run_opportunity_monitor import monitor
from data import trading_decision_repository as repository

NOW=datetime(2026,9,18,10,12)


def candidate(code="002185",day="2026-09-14"):
    return {"code":code,"name":"示例","run_date":day,"extra":{
      "selector":{"entry_route":"early_start","buy_eligible":True,"setup_stage":"actionable"},
      "ai_selection":{"rank":1,"entry_route":"early_start","reason":"原研究"}}}


def quote(price=16.22,now=NOW):
    return {"price":price,"source_time":now.strftime("%Y%m%d%H%M%S"),"source":"test"}


def plan(**kwargs):
    return trial.validate_plan({"state":"watch","thesis":"原平台逻辑","wait_reason":"等待确认",
      "review_above":16.2,"review_below":None,"invalidation_below":15.5,**kwargs})


class OpportunityTrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=StockStore(str(Path(self.tmp.name)/"test.db"))
        trial.ensure_tables(self.store)

    def record(self,code="002185",p=None,mode="simulated",now=NOW-timedelta(minutes=30)):
        trial.record_decision(self.store,mode,{"signals" if mode=="simulated" else "decisions":[{
           "code":code,"action":"watch","watch_plan":p or plan()}]},
           {"as_of":trial.stamp(now),"positions":[]},{"results":[]},now)

    def test_missing_next_daily_list_retains_observation_not_buy_permission(self):
        trial.ingest(self.store,[candidate()],NOW)
        selected,setups,_,_=trial.candidate_scope(self.store,"simulated",[],[],now=NOW)
        self.assertEqual(selected[0]["code"],"002185")
        selector=selected[0]["extra"]["selector"]
        self.assertFalse(selector["buy_eligible"])
        self.assertTrue(selected[0]["extra"]["opportunity"]["requires_requalification"])
        self.assertEqual(setups["002185"]["first_seen"],"2026-09-14")

    def test_overflow_is_preserved_and_research_rotates(self):
        trial.ingest(self.store,[candidate(f"00218{i}") for i in range(6)],NOW)
        first,_,_,count=trial.candidate_scope(self.store,"simulated",[],[],now=NOW)
        self.assertEqual((len(first),count),(3,6))
        for row in first:
            self.record(row["code"])
        second,_,_,_=trial.candidate_scope(self.store,"simulated",[],[],now=NOW)
        self.assertFalse({r["code"] for r in first}&{r["code"] for r in second})

    def test_source_expiry_and_holding_retention(self):
        trial.ingest(self.store,[candidate(day="2026-09-01")],NOW)
        self.assertEqual(trial.load_setups(self.store,NOW),{})
        self.assertIn("002185",trial.load_setups(self.store,NOW,["002185"]))

    def test_stale_quotes_do_not_trigger_and_repeated_crossing_deduplicates(self):
        trial.ingest(self.store,[candidate()],NOW)
        self.record()
        trial.observe(self.store,"simulated",{"002185":quote(now=NOW-timedelta(days=1))},{},NOW)
        with self.store._get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunity_events").fetchone()[0],0)
        for _ in range(3):
            trial.observe(self.store,"simulated",{"002185":quote()},{},NOW)
        with self.store._get_conn() as conn:
            rows=conn.execute("SELECT kind FROM opportunity_events").fetchall()
        self.assertEqual([r[0] for r in rows],["price_recovery"])

    def test_claim_is_once_and_uncertain_execution_not_replayed(self):
        trial.ingest(self.store,[candidate()],NOW)
        self.record()
        trial.observe(self.store,"simulated",{"002185":quote()},{},NOW)
        claimed=trial.claim_events(self.store,"simulated",NOW)
        self.assertEqual(len(claimed),1)
        self.assertEqual(trial.claim_events(self.store,"simulated",NOW),[])
        trial.finish_events(self.store,claimed,False,"interrupted")
        trial.observe(self.store,"simulated",{"002185":quote()},{},NOW)
        self.assertEqual(trial.claim_events(self.store,"simulated",NOW+timedelta(minutes=30)),[])

    def test_risk_event_has_priority_over_position_change(self):
        trial.ingest(self.store,[candidate()],NOW)
        self.record()
        trial.observe(self.store,"simulated",{"002185":quote()},{},NOW)
        claimed=trial.claim_events(self.store,"simulated",NOW)
        trial.finish_events(self.store,claimed,True)
        later=NOW+timedelta(minutes=3)
        trial.observe(self.store,"simulated",{"002185":quote(15.4,later)},{"002185":100},later)
        risk=trial.claim_events(self.store,"simulated",later)
        self.assertEqual([r["kind"] for r in risk],["structure_risk", "position_changed"])

    def test_live_blocked_market_does_not_enter_research_queue(self):
        trial.ingest(self.store,[candidate("300236")],NOW)
        with patch("data.live_manual_account.is_live_buy_allowed",return_value=False):
            trial.observe(self.store,"live",{"300236":quote()},{},NOW)
            selected,_,_,_=trial.candidate_scope(self.store,"live",[],[],now=NOW)
        self.assertEqual(selected,[])
        self.assertEqual(trial.claim_events(self.store,"live",NOW),[])

    def test_pending_overflow_can_run_next_batch_without_account_delay(self):
        codes=[f"00218{i}" for i in range(5)]
        trial.ingest(self.store,[candidate(c,str(NOW.date())) for c in codes],NOW)
        trial.observe(self.store,"simulated",{c:quote() for c in codes},{},NOW)
        cfg=trial.settings()|{"event_batch_size":2}
        seen=[]
        with patch.object(trial,"settings",return_value=cfg):
            for minute,size in enumerate((2,2,1)):
                rows=trial.claim_events(self.store,"simulated",NOW+timedelta(minutes=minute))
                self.assertEqual(len(rows),size)
                seen.extend(r["code"] for r in rows)
                trial.finish_events(self.store,rows,True)
            self.assertEqual(trial.claim_events(self.store,"simulated",NOW+timedelta(minutes=3)),[])
        self.assertEqual(len(set(seen)),5)

    def test_stale_price_event_still_expires_without_cooldown(self):
        trial.ingest(self.store,[candidate()],NOW)
        self.record()
        trial.observe(self.store,"simulated",{"002185":quote()},{},NOW)
        self.assertEqual(trial.claim_events(self.store,"simulated",NOW+timedelta(minutes=16)),[])
        with self.store._get_conn() as conn:
            self.assertEqual(conn.execute("SELECT status FROM opportunity_events WHERE kind='price_recovery'").fetchone()[0],"expired")

    def test_new_news_is_deduplicated_and_trigger_evidence_is_account_scoped(self):
        trial.ingest(self.store,[candidate()],NOW)
        self.record()
        with self.store._get_conn() as conn:
            conn.execute("INSERT INTO news_events(code,title,score,risk_level,created_at) VALUES(?,?,?,?,?)",
                         ("002185","新增重要公告",3,"high",trial.stamp(NOW-timedelta(minutes=1))))
        for _ in range(2):
            trial.observe(self.store,"simulated",{"002185":quote(16)},{},NOW)
        rows=trial.claim_events(self.store,"simulated",NOW)
        self.assertEqual([r["kind"] for r in rows],["logic_risk"])
        self.assertEqual(trial.processing_evidence(self.store,"live"),{})
        evidence=trial.processing_evidence(self.store,"simulated")["002185"][0]
        self.assertEqual(evidence["facts"]["title"],"新增重要公告")

    def test_discovery_audit_preserves_original_reason(self):
        source=candidate()
        trial.ingest(self.store,[source],NOW)
        trial.ingest(self.store,[source],NOW)
        source["extra"]["ai_selection"]["reason"]="后续补充"
        trial.ingest(self.store,[source],NOW)
        with self.store._get_conn() as conn:
            rows=conn.execute("SELECT payload FROM opportunity_audit WHERE kind='selection_discovery' ORDER BY id").fetchall()
        self.assertEqual(len(rows),2)
        self.assertIn("原研究",rows[0][0])
        self.assertIn("后续补充",rows[1][0])

    def test_requalification_and_data_quality_are_required_for_retained_buy(self):
        trial.ingest(self.store,[candidate()],NOW)
        selected,_,_,_=trial.candidate_scope(self.store,"simulated",[],[],now=NOW)
        selection=selected[0]["extra"]["selector"]|{"opportunity":selected[0]["extra"]["opportunity"]}
        context={"as_of":trial.stamp(NOW),"opportunity_trial":True,"positions":[],"candidates":[{
          "code":"002185","selection":selection,"quote":quote(),
          "technical":{"date":"2026-09-17","quality":"verified_qfq"}}]}
        payload={"signals":[{"code":"002185","action":"buy","target_amount":20000,"watch_plan":plan()}]}
        with self.assertRaisesRegex(ValueError,"requalification"):
            validate_simulated_decision(payload,context)
        payload["signals"][0]["watch_plan"]=plan(requalified=True,requalification_reason="当轮结构与原论据仍成立")
        self.assertEqual(validate_simulated_decision(payload,context)["signals"][0]["action"],"buy")
        context["candidates"][0]["quote"]=quote(now=NOW-timedelta(days=1))
        with self.assertRaisesRegex(ValueError,"source-timestamped"):
            validate_simulated_decision(payload,context)

    def test_closed_market_never_fetches(self):
        with patch.object(trial,"enabled",return_value=True),patch("scripts.run_opportunity_monitor.is_actionable_trading_time",return_value=False):
            def fail(_): raise AssertionError("unexpected request")
            self.assertEqual(monitor(self.store,NOW,fail)["status"],"skipped")

    def test_plan_does_not_confuse_zero_nan_or_invalid_thesis(self):
        for value in [0,-1,float("nan"),True,"16.2"]:
            with self.assertRaises(ValueError): plan(review_above=value)
        with self.assertRaises(ValueError): plan(state="invalid")

    def test_refresh_persists_small_candidate_scope_and_all_holdings(self):
        from contextlib import ExitStack
        from data.trading_state import refresh_trading_state
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None): return cls(2026,9,18,10,12)
        positions=[{"code":"600487","name":"持仓","volume":200,"market_value":3000,"available_to_sell":200}]
        account={"positions":positions,"summary":{"total_equity":20000,"available_cash":17000}}
        with self.store._get_conn() as conn:
            conn.execute("INSERT INTO financial_factors(code,period,revenue_yoy,source,updated_at) VALUES(?,?,?,?,?)",("002180","20260630",20,"fixture","2026-09-17 08:00:00"))
        with ExitStack() as stack:
            replacements={
              "StockStore":lambda:self.store,"datetime":Clock,
              "_live_account_without_network":lambda *a:account,
              "account_snapshot":lambda *a,**k:account,
              "candidate_board_status":lambda *a,**k:{"status":"ready","active_count":6},
              "_screen_candidates":lambda *a:[candidate(f"00218{i}","2026-09-18") for i in range(6)],
              "fetch_quotes":lambda codes:{c:quote()|{"name":c} for c in codes},
              "_minute_states":lambda codes:{c:{"last_time":"1012","source_time":"2026-09-18 10:12:00","half_hour":{"available":False}} for c in codes},
              "_fund_flows":lambda *a,**k:{},"fetch_market_indices":lambda:{},
              "_sector_state":lambda:{},"_ensure_sector_memberships":lambda *a:{},
              "technical_state":lambda *a:{"daily_date":"2026-09-17","quality":"verified_qfq"},
            }
            for key,value in replacements.items(): stack.enter_context(patch("data.trading_state."+key,value))
            stack.enter_context(patch("data.trading_state.SectorRotationService.get_stock_contexts",return_value={}))
            stack.enter_context(patch.object(trial,"enabled",return_value=True))
            stack.enter_context(patch("data.adjusted_daily.ensure_windows",return_value={}))
            stack.enter_context(patch.dict("os.environ",{"STOCK_OPPORTUNITY_FOCUS":""}))
            state=refresh_trading_state("1012","live")
            with patch.object(repository,"DB_PATH",Path(self.store.db_path)):
                overview=repository.get_trading_overview("live")
                evidence=repository.get_stock_evidence(overview["required_evidence_codes"],state["as_of"],"live")
                context=repository.build_execution_context(state["as_of"],"live")
        self.assertTrue(context["opportunity_trial"])
        self.assertNotIn("entry_risk_policy", context)
        self.assertNotIn("entry_risk_policy", overview["refresh"])
        self.assertEqual(len(context["positions"]),1)
        self.assertEqual(len(context["candidates"]),3)
        self.assertEqual(len(overview["required_evidence_codes"]),4)
        fundamental=next(s for s in evidence["stocks"] if s["code"]=="002180")["selection"]["fundamental"]
        self.assertEqual(fundamental["period"],"20260630")
        self.assertEqual(len(trial.load_setups(self.store,NOW)),6)


class DataQualityTests(unittest.TestCase):
    def frame(self,n=61):
        frame=pd.DataFrame([{"time":(NOW.replace(hour=9,minute=30)+timedelta(minutes=i)).strftime("%H%M"),
            "price":10.0,"volume":(i+1)*100,"amount":(i+1)*100000} for i in range(n)])
        frame.attrs["trading_date"]="20260918"
        return frame

    def test_minute_date_duplicates_and_units(self):
        frame=self.frame()
        now=NOW.replace(hour=10,minute=30)
        self.assertEqual(summarize_minutes(frame,now)["vwap"],10)
        frame.attrs["trading_date"]="20200101"
        self.assertIn("error",summarize_minutes(frame,now))
        frame=self.frame();frame.loc[2,"time"]=frame.loc[1,"time"]
        self.assertIn("error",summarize_minutes(frame,now))
        frame=self.frame();frame["amount"]*=2
        self.assertIsNone(summarize_minutes(frame,now)["vwap"])

    def test_first_half_hour_price_is_available_without_volume_comparison(self):
        value=summarize_minutes(self.frame(31),NOW.replace(hour=10,minute=1))
        self.assertEqual(value["half_hour"]["price_change_pct"],0)
        self.assertFalse(value["half_hour"]["available"])

    def test_funds_reject_old_or_missing_fields_and_keep_true_zero(self):
        adapter=IwenCaiAdapter(api_key="test-key")
        today=datetime.now().strftime("%Y%m%d")
        self.assertIsNone(adapter._parse_fund_flow({"股票代码":"002185"},today))
        self.assertIsNone(adapter._parse_fund_flow({"股票代码":"002185","主力资金流向[20200101]":123},today))
        value=adapter._parse_fund_flow({"股票代码":"002185",f"主力资金流向[{today}]":0},today)
        self.assertEqual(value.main_net_inflow,0)
        self.assertIsNone(value.big_net_inflow)
        self.assertIsNone(value.main_net_pct)
        FundFlowFilter._summarize_from_flow(value)

    def test_raw_daily_response_never_marked_qfq(self):
        import io
        import json
        data={"data":{"sz002185":{"day":[["2026-09-17",10,10,10,10,100]]}}}
        with patch("data.adjusted_daily.urllib.request.urlopen",side_effect=lambda *a,**k:io.BytesIO(json.dumps(data).encode())),patch("data.adjusted_daily.time.sleep"):
            with self.assertRaisesRegex(RuntimeError,"qfqday missing"):
                fetch_window("002185",through="2026-09-17")


if __name__=="__main__":
    unittest.main()
