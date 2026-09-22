import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from data import opportunity_trial as trial
from data.store.sqlite_store import StockStore
from data.trading_review_policy import unchanged_intent, account_facts
from scripts.execute_live_trade_decision import execute
from scripts.execute_trading_cycle import validate_simulated_decision, validate_live_decision
from tests.test_opportunity_trial import NOW, candidate, plan, quote
from tests.test_trading_assessment import fixture


class ScheduledAndEventTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.store=StockStore(str(Path(folder.name)/'test.db'))
        trial.ensure_tables(self.store)
        trial.ingest(self.store,[candidate()],NOW)

    def legacy_plan(self, *, mode='simulated', holding=False, minutes=15, at=NOW):
        # Write the old field directly, as a database retained across deployment.
        old=plan(review_above=None,invalidation_below=None)
        old['review_after_minutes']=minutes
        trial.record_decision(self.store,mode,{'signals' if mode=='simulated' else 'decisions':[
            {'code':'002185','action':'hold' if holding else 'watch','watch_plan':old}]},
            {'as_of':trial.stamp(at),'positions':[{'code':'002185'}] if holding else []},{},at)
        with self.store._get_conn() as conn:
            conn.execute('UPDATE opportunity_plans SET position_volume=?',(100 if holding else 0,))

    def test_no_timer_for_candidates_or_holdings_even_without_baseline_or_with_price_change(self):
        for mode in ('simulated','live'):
            for holding in (False,True):
                with self.subTest(mode=mode,holding=holding):
                    self.legacy_plan(mode=mode,holding=holding)
                    for minutes,price in ((14,10),(15,10),(30,10.5),(90,11)):
                        at=NOW+timedelta(minutes=minutes)
                        trial.observe(self.store,mode,{'002185':quote(price,at)},
                                      {'002185':100} if holding else {},at)
                    with self.store._get_conn() as conn:
                        self.assertEqual(conn.execute('SELECT COUNT(*) FROM opportunity_events').fetchone()[0],0)

    def test_scheduled_scope_ignores_legacy_future_timer_and_keeps_rotation(self):
        self.legacy_plan(minutes=240)
        plans=trial.load_plans(self.store,'simulated')
        self.assertNotIn('next_review_at',plans['002185'])
        self.assertNotIn('review_after_minutes',plans['002185']['plan'])
        selected,*_=trial.candidate_scope(self.store,'simulated',[],[],now=NOW+timedelta(minutes=30))
        self.assertEqual([r['code'] for r in selected],['002185'])
        trial.ingest(self.store,[candidate(f'00218{i}') for i in range(6)],NOW)
        selected,*_=trial.candidate_scope(self.store,'simulated',[],[],now=NOW)
        self.assertEqual(len(selected),3)  # Deep-research capacity and oldest-first remain.
        self.assertNotIn('002185',[r['code'] for r in selected])

    def test_old_timer_fields_are_ignored_on_new_submissions(self):
        for old in (15,240,'legacy value'):
            self.assertNotIn('review_after_minutes',plan(review_after_minutes=old))

    def test_price_crossing_and_structure_risk_still_wake_without_timer(self):
        p=plan(review_above=10.1,invalidation_below=9.5)
        trial.record_decision(self.store,'simulated',{'signals':[{'code':'002185','action':'watch','watch_plan':p}]},
                              {'as_of':trial.stamp(NOW),'positions':[]},{},NOW)
        for price,kind in ((10.2,'price_recovery'),(9.4,'structure_risk')):
            at=NOW+timedelta(minutes=3)
            trial.observe(self.store,'simulated',{'002185':quote(price,at)}, {},at)
            rows=trial.claim_events(self.store,'simulated',at)
            self.assertEqual([r['kind'] for r in rows],[kind])
            trial.finish_events(self.store,rows,True)

    def test_pending_legacy_timers_expire_without_blocking_new_events(self):
        self.legacy_plan()
        with self.store._get_conn() as conn:
            for mode in ('simulated','live'):
                for kind in ('review_due','holding_review_due'):
                    trial.queue_event(conn,mode,'002185','legacy',kind,'old',{},NOW)
                trial.queue_event(conn,mode,'002185','legacy','news_changed','new',{},NOW)
        for mode in ('simulated','live'):
            rows=trial.claim_events(self.store,mode,NOW)
            self.assertEqual([r['kind'] for r in rows],['news_changed'])
        with self.store._get_conn() as conn:
            rows=conn.execute("SELECT status,error FROM opportunity_events WHERE kind IN ('review_due','holding_review_due')").fetchall()
            self.assertEqual([(r[0],r[1]) for r in rows],[('expired','timer trigger retired')]*4)

    def test_actual_holdings_events_do_not_consume_candidate_slots(self):
        codes=[f'00218{i}' for i in range(5)]
        trial.ingest(self.store,[candidate(c) for c in codes],NOW)
        with self.store._get_conn() as conn:
            for code in codes:
                trial.queue_event(conn,'live',code,'fixture','news_changed','new',{},NOW)
        config=trial.settings()|{'event_batch_size':1}
        with patch.object(trial,'settings',return_value=config), \
             patch('data.live_manual_account.account_snapshot',return_value={'positions':[{'code':c} for c in codes[:4]]}):
            rows=trial.claim_events(self.store,'live',NOW)
        self.assertEqual({r['code'] for r in rows},set(codes))


class AssessmentFixTests(unittest.TestCase):
    def test_nontrade_conflict_normalizes_conservatively_in_both_modes(self):
        for mode,validator,key in [('simulated',validate_simulated_decision,'signals'),('live',validate_live_decision,'decisions')]:
            row,context=fixture();context['mode']=mode
            row['action']='watch';row['watch_plan']['state']='account_blocked'
            row['assessment']['portfolio']['grade']='conditional'
            result=validator({key:[row]},context)[key][0]
            self.assertEqual(result['action'],'watch')
            self.assertEqual(result['assessment']['portfolio']['grade'],'blocked')
            self.assertEqual(result['assessment_adjustments'][0]['submitted'],'conditional')

    def test_conflict_does_not_grant_permission_or_silence_other_errors(self):
        row,context=fixture();row['watch_plan']['state']='account_blocked'
        row['assessment']['portfolio']['grade']='conditional'
        with self.assertRaisesRegex(ValueError,'002185:.*account_blocked'):
            validate_simulated_decision({'signals':[row]},context)
        row['action']='watch';row['assessment']['portfolio']['grade']='fit'
        with self.assertRaisesRegex(ValueError,'002185:.*account_blocked'):
            validate_simulated_decision({'signals':[row]},context)

    def test_buy_and_add_no_longer_require_overnight_or_apply_fixed_budget(self):
        for mode,validator,key in [('simulated',validate_simulated_decision,'signals'),('live',validate_live_decision,'decisions')]:
            for action in ('buy','add') if mode=='simulated' else ('buy',):
                row,ctx=fixture();ctx['mode']=mode;row['action']=action
                row['target_amount']=20000
                # A saved context from before rollback must not reactivate the gate.
                ctx['entry_risk_policy']={'stress_floor_pct':5,'gap_buffer_pct':2,'max_loss_equity_pct':0.1}
                if action=='add':
                    ctx['positions']=ctx.pop('candidates');ctx['candidates']=[]
                result=validator({key:[row]},ctx)[key][0]
                self.assertNotIn('overnight',result['position_plan'])
                self.assertEqual(result['position_plan']['scenario']['requested_notional'],20000)
                self.assertEqual(result['position_plan']['scenario']['equity_pct'],2)

    def test_old_overnight_fields_do_not_reactivate_gate_and_live_evidence_remains_trusted(self):
        row,ctx=fixture();ctx['mode']='live';row['volume']=200
        row['position_plan']['overnight']={'stress_price':0,'max_loss_equity_pct':0}
        row['notification_evidence']={'research_facts_version':'spoof'}
        result=validate_live_decision({'decisions':[row]},ctx)['decisions'][0]
        self.assertNotIn('overnight',result['position_plan'])
        self.assertEqual(result['position_plan']['scenario']['requested_notional'],2000)
        self.assertEqual(result['notification_evidence']['research_facts_version'],'v1')


class LiveIntentDedupTests(unittest.TestCase):
    def test_repeat_expiry_is_noop_new_facts_allow_new_proposal(self):
        config={'initial_cash':20000,'max_positions':None,'min_lot':100,'blocked_boards':['300','688']}
        payload={'decisions':[{'code':'600001','name':'测试','action':'buy','confidence':'medium','price':10,
                              'volume':100,'notification_evidence':{'research_facts_version':'v1','events':[]}}]}
        with tempfile.TemporaryDirectory() as tmp:
            store=StockStore(str(Path(tmp)/'test.db'))
            with patch('scripts.execute_live_trade_decision.StockStore',return_value=store), \
                 patch('scripts.execute_live_trade_decision.fetch_live_prices',return_value={'600001':10}), \
                 patch('scripts.execute_live_trade_decision.load_config',return_value=config), \
                 patch('data.live_manual_account.load_config',return_value=config):
                self.assertEqual(execute(payload)['summary']['created_intents'],1)
                self.assertEqual(execute(payload)['results'][0]['message'],'unchanged_live_intent_suppressed')
                with store._get_conn() as conn:
                    conn.execute("UPDATE live_trade_intents SET status='expired'")
                repeated=execute(payload)
                self.assertEqual(repeated['summary']['created_intents'],0)
                self.assertEqual(repeated['summary']['rejected'],0)
                snap={'summary':{'available_cash':20000},'positions':[]}
                decision={'code':'600001','action':'buy','raw':payload['decisions'][0]}
                with store._get_conn() as conn:
                    # Factual price/size/cash/availability and newer risk events must escape dedup.
                    self.assertIsNone(unchanged_intent(conn,decision,10.2,100,snap,datetime.now()))
                    self.assertIsNone(unchanged_intent(conn,decision,10,200,snap,datetime.now()))
                    changed=copy.deepcopy(snap);changed['summary']['available_cash']=19000
                    self.assertIsNone(unchanged_intent(conn,decision,10,100,changed,datetime.now()))
                    changed=copy.deepcopy(decision)
                    changed['raw']['notification_evidence']['events']=[{'kind':'structure_risk','created_at':(datetime.now()+timedelta(minutes=1)).isoformat(sep=' ')}]
                    self.assertIsNone(unchanged_intent(conn,changed,10,100,snap,datetime.now()))
                    self.assertIsNone(unchanged_intent(conn,decision,10,100,snap,datetime.now()+timedelta(days=1)))
                payload['decisions'][0]['notification_evidence']['research_facts_version']='v2'
                self.assertEqual(execute(payload,dry_run=True)['results'][0]['message'],'dry_run_intent_validated')
                self.assertEqual(execute(payload)['summary']['created_intents'],1)

    def test_available_to_sell_is_part_of_actual_account_facts(self):
        snap={'positions':[{'code':'600001','volume':100,'available_to_sell':0}]}
        old=account_facts(snap)
        snap['positions'][0]['available_to_sell']=100
        self.assertNotEqual(old,account_facts(snap))


if __name__ == '__main__':
    unittest.main()
