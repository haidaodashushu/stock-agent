import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from data import opportunity_trial as trial
from data.store.sqlite_store import StockStore
from data.trading_review_policy import due_change, unchanged_intent, account_facts
from scripts.execute_live_trade_decision import execute
from scripts.execute_trading_cycle import validate_simulated_decision, validate_live_decision
from tests.test_opportunity_trial import NOW, candidate, plan, quote
from tests.test_trading_assessment import fixture


class ReviewGateTests(unittest.TestCase):
    def gate(self, current=None, baseline=None, now=NOW):
        at=NOW-timedelta(minutes=15)
        baseline=baseline or {'as_of':trial.stamp(at), 'quote':{'price':10,'amount':27000,'volume':2700}}
        return due_change({'reviewed_at':baseline['as_of']}, baseline,
                          current or {'price':10,'amount':42000,'volume':4200}, now, {})

    def test_timer_alone_does_not_wake_but_price_or_pace_does(self):
        self.assertFalse(self.gate()['material'])
        self.assertEqual(self.gate({'price':10.2})['reason'],'price_change')
        self.assertEqual(self.gate({'price':10,'amount':60000})['reason'],'amount_pace')
        self.assertEqual(self.gate({'price':10,'volume':6000})['reason'],'volume_pace')

    def test_missing_baseline_fails_open_and_new_day_wakes(self):
        self.assertTrue(due_change({'reviewed_at':'new'}, {}, {'price':10},NOW,{})['material'])
        self.assertEqual(self.gate(now=NOW+timedelta(days=3))['reason'],'new_session')

    def test_intraday_excursion_and_lunch_adjusted_pace(self):
        at=NOW.replace(hour=11,minute=25)
        baseline={'as_of':trial.stamp(at),'quote':{'price':10,'high':10,'low':10,'amount':115000}}
        self.assertEqual(self.gate({'price':10,'high':10.2},baseline)['reason'],'new_high')
        self.assertEqual(self.gate({'price':10,'low':9.8},baseline)['reason'],'new_low')
        self.assertEqual(self.gate({'price':10,'amount':137000},baseline,NOW.replace(hour=13,minute=5))['reason'],'amount_pace')

    def test_due_gate_keeps_risk_and_scheduled_candidate_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StockStore(str(Path(tmp)/'test.db'));trial.ensure_tables(store)
            trial.ingest(store,[candidate()],NOW)
            at=NOW-timedelta(minutes=20)
            trial.record_decision(store,'simulated',{'signals':[{'code':'002185','action':'watch',
                'watch_plan':plan(review_above=None,invalidation_below=9.95,review_after_minutes=15)}]},
                {'as_of':trial.stamp(at),'positions':[]},{'results':[]},at)
            trial.write_cache(store,'decision_observation_simulated',{'002185':{'as_of':trial.stamp(at),'quote':quote(10,at)}},at)
            trial.observe(store,'simulated',{'002185':quote(10)}, {}, NOW)
            with store._get_conn() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM opportunity_events').fetchone()[0],0)
                p=conn.execute('SELECT reviewed_at FROM opportunity_plans').fetchone()[0]
                self.assertEqual(p,trial.stamp(at))  # no fabricated completed review
            selected,*_=trial.candidate_scope(store,'simulated',[],[],now=NOW)
            self.assertEqual([r['code'] for r in selected],['002185'])
            trial.observe(store,'simulated',{'002185':quote(9.94)}, {}, NOW)
            with store._get_conn() as conn:
                self.assertEqual([r[0] for r in conn.execute('SELECT kind FROM opportunity_events')],['structure_risk'])

    def test_unchanged_holding_timer_does_not_hide_a_new_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StockStore(str(Path(tmp)/'test.db'));trial.ensure_tables(store)
            at=NOW-timedelta(minutes=20)
            trial.record_decision(store,'simulated',{'signals':[{'code':'600001','action':'hold',
                'watch_plan':plan(state='holding',review_above=None,invalidation_below=9.95,review_after_minutes=15)}]},
                {'as_of':trial.stamp(at),'positions':[{'code':'600001'}]},{'results':[]},at)
            with store._get_conn() as conn:
                conn.execute("UPDATE opportunity_plans SET position_volume=100")
            trial.write_cache(store,'decision_observation_simulated',{'600001':{'as_of':trial.stamp(at),'quote':quote(10,at)}},at)
            trial.observe(store,'simulated',{'600001':quote(10)}, {'600001':100}, NOW)
            with store._get_conn() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM opportunity_events').fetchone()[0],0)
            trial.observe(store,'simulated',{'600001':quote(9.94)}, {'600001':100}, NOW)
            with store._get_conn() as conn:
                self.assertEqual([r[0] for r in conn.execute('SELECT kind FROM opportunity_events')],['structure_risk'])

    def test_pending_timer_is_rechecked_before_claim_and_can_revive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StockStore(str(Path(tmp)/'test.db'));trial.ensure_tables(store)
            trial.ingest(store,[candidate()],NOW)
            at=NOW-timedelta(minutes=20)
            trial.record_decision(store,'simulated',{'signals':[{'code':'002185','action':'watch',
                'watch_plan':plan(review_above=None,invalidation_below=None,review_after_minutes=15)}]},
                {'as_of':trial.stamp(at),'positions':[]},{'results':[]},at)
            stored=trial.load_plans(store,'simulated')['002185']
            with store._get_conn() as conn:
                trial.queue_event(conn,'simulated','002185',stored['setup_id'],'review_due',trial.stamp(at),{'reviewed_at':trial.stamp(at)},NOW)
            trial.write_cache(store,'decision_observation_simulated',{'002185':{'as_of':trial.stamp(at),'quote':quote(10,at)}},at)
            trial.write_cache(store,'monitor_quote',{'002185':quote(10)},NOW)
            self.assertEqual(trial.claim_events(store,'simulated',NOW),[])
            later=NOW+timedelta(minutes=3)
            trial.write_cache(store,'monitor_quote',{'002185':quote(10.2,later)},later)
            trial.observe(store,'simulated',{'002185':quote(10.2,later)}, {}, later)
            self.assertEqual([r['kind'] for r in trial.claim_events(store,'simulated',later)],['review_due'])


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

    def overnight(self):
        row,context=fixture();row['target_amount']=8000
        context['entry_risk_policy']={'stress_floor_pct':5,'gap_buffer_pct':2,'max_loss_equity_pct':1}
        row['position_plan']['overnight']={'acknowledge_t1':True,'requires_intraday_exit':False,
            'entry_basis':'结构改善但仍控制追价','thesis_horizon':'承受到下一交易日',
            'early_failure_response':'暂停加仓，新增股份不能当日卖出','next_session_review':'开盘核验缺口及失效条件',
            'stress_price':8.8,'max_loss_equity_pct':1}
        return row,context

    def test_t1_scenario_sizes_increment_and_uses_trading_calendar(self):
        row,ctx=self.overnight()
        result=validate_simulated_decision({'signals':[row]},ctx)['signals'][0]['position_plan']['overnight']
        self.assertEqual(result['estimated_loss'],960)
        self.assertEqual(result['budget_amount'],1000)
        self.assertEqual(result['first_sellable_session'],'2026-09-21')

    def test_t1_rejects_intraday_dependency_weak_stress_and_excess_budget(self):
        for field,value,match in [('requires_intraday_exit',True,'same-day exit'),('stress_price',9,'stress_price'),
                                  ('max_loss_equity_pct',2,'max_loss_equity_pct')]:
            row,ctx=self.overnight();row['position_plan']['overnight'][field]=value
            with self.assertRaisesRegex(ValueError,match): validate_simulated_decision({'signals':[row]},ctx)
        row,ctx=self.overnight();row['target_amount']=10000
        with self.assertRaisesRegex(ValueError,'exceeds declared budget'): validate_simulated_decision({'signals':[row]},ctx)
        row,ctx=self.overnight();del row['position_plan']['overnight']
        with self.assertRaisesRegex(ValueError,'overnight requires'): validate_simulated_decision({'signals':[row]},ctx)

    def test_t1_policy_also_applies_to_add_but_not_risk_reducing_exit(self):
        row,ctx=self.overnight();row['action']='add'
        ctx['positions']=ctx.pop('candidates');ctx['candidates']=[]
        del row['position_plan']['overnight']
        with self.assertRaisesRegex(ValueError,'overnight requires'):
            validate_simulated_decision({'signals':[row]},ctx)
        row['action']='reduce';row['sell_pct']=0.5
        row['exit_plan']={'trigger':'risk_reduction','reason':'风险恶化','why_now':'旧仓可卖'}
        self.assertEqual(validate_simulated_decision({'signals':[row]},ctx)['signals'][0]['action'],'reduce')

    def test_t1_without_structural_level_still_uses_drawdown_floor(self):
        row,ctx=self.overnight();row['position_plan']['invalidation_price']=None
        row['position_plan']['overnight']['stress_price']=9.6
        with self.assertRaisesRegex(ValueError,'stress_price'): validate_simulated_decision({'signals':[row]},ctx)
        row['position_plan']['overnight']['stress_price']=9.5
        result=validate_simulated_decision({'signals':[row]},ctx)['signals'][0]
        self.assertFalse(result['position_plan']['scenario']['available'])
        self.assertEqual(result['position_plan']['overnight']['estimated_loss'],400)

    def test_live_explicit_volume_and_server_evidence(self):
        row,ctx=self.overnight();ctx['mode']='live';row['volume']=200
        row['notification_evidence']={'research_facts_version':'spoof'}
        result=validate_live_decision({'decisions':[row]},ctx)['decisions'][0]
        self.assertEqual(result['position_plan']['overnight']['requested_notional'],2000)
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
