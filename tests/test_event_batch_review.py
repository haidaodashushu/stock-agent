import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from data import opportunity_trial as trial
from data import stock_research
from data.store.sqlite_store import StockStore
from scripts.execute_trading_cycle import render_report
from tests.test_opportunity_trial import NOW, candidate, plan, quote


class EventBatchReviewTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = StockStore(str(Path(folder.name) / 'test.db'))
        trial.ensure_tables(self.store)

    def queue_candidates(self, codes):
        trial.ingest(self.store, [candidate(c, str(NOW.date())) for c in codes], NOW)
        trial.observe(self.store, 'simulated', {c: quote() for c in codes}, {}, NOW)

    def test_thirty_ready_candidates_are_claimed_and_reach_snapshot_scope(self):
        codes = [f'002{i:03d}' for i in range(40)]
        ready = {c: {'status': 'ready'} for c in codes}
        with patch.object(stock_research, 'contexts', return_value=ready):
            self.queue_candidates(codes)
            rows = trial.claim_events(self.store, 'simulated', NOW)
            self.assertEqual(len(rows), 30)
            scope, *_ = trial.candidate_scope(self.store, 'simulated', [], [],
                                             {r['code'] for r in rows}, NOW)
            self.assertEqual({r['code'] for r in scope}, {r['code'] for r in rows})
            trial.finish_events(self.store, rows, True, reviewed_codes={r['code'] for r in scope})
            remaining = trial.claim_events(self.store, 'simulated', NOW + timedelta(minutes=1))
            self.assertEqual(len(remaining), 10)

    def test_deep_research_limit_does_not_block_ready_candidates_behind_it(self):
        codes = [f'002{i:03d}' for i in range(40)]
        states = {c: {'status': 'refresh_required' if i < 10 else 'ready'} for i,c in enumerate(codes)}
        with patch.object(stock_research, 'contexts', return_value=states):
            self.queue_candidates(codes)
            rows = trial.claim_events(self.store, 'simulated', NOW)
            self.assertEqual(len(rows), 30)
            self.assertEqual(sum(states[r['code']]['status'] != 'ready' for r in rows), 3)
            scope, *_ = trial.candidate_scope(self.store, 'simulated', [], [], set(codes), NOW)
            self.assertEqual(len(scope), 30)
            self.assertEqual(sum(states[r['code']]['status'] != 'ready' for r in scope), 3)

    def test_ordinary_research_alone_waits_for_scheduled_scope(self):
        trial.ingest(self.store, [candidate()], NOW)
        trial.observe(self.store, 'simulated', {'002185': quote()}, {}, NOW)
        self.assertEqual(trial.claim_events(self.store, 'simulated', NOW), [])
        scope, *_ = trial.candidate_scope(self.store, 'simulated', [], [], now=NOW)
        self.assertEqual([r['code'] for r in scope], ['002185'])
        with self.store._get_conn() as conn:
            self.assertEqual(conn.execute('SELECT status FROM opportunity_events').fetchone()[0], 'pending')

    def test_unreviewed_claim_is_returned_to_queue_not_acknowledged(self):
        codes = ['002180', '002181']
        self.queue_candidates(codes)
        rows = trial.claim_events(self.store, 'simulated', NOW)
        trial.finish_events(self.store, rows, True, reviewed_codes={codes[0]})
        with self.store._get_conn() as conn:
            results = {r['code']:dict(r) for r in conn.execute('SELECT code,status,batch_id FROM opportunity_events')}
        self.assertEqual(results[codes[0]]['status'], 'done')
        self.assertEqual(results[codes[1]]['status'], 'pending')
        self.assertEqual(results[codes[1]]['batch_id'], '')

    def record_plan(self, p, at, price=None):
        context = {'as_of':trial.stamp(at), 'positions':[], 'candidates':[]}
        if price is not None:
            context['candidates'] = [{'code':'002185', 'quote':quote(price,at)}]
        trial.record_decision(self.store, 'simulated', {'signals':[
            {'code':'002185', 'action':'watch', 'watch_plan':p}]}, context, {}, at)
        if price is not None:
            stock_research.record_observations(self.store, 'simulated', context)

    def test_plan_revision_does_not_repeat_acknowledged_price_but_new_crossing_can_wake(self):
        trial.ingest(self.store, [candidate()], NOW)
        self.record_plan(plan(review_after_minutes=60), NOW-timedelta(minutes=5))
        trial.observe(self.store, 'simulated', {'002185':quote(16.3)}, {}, NOW)
        self.record_plan(plan(review_above=16.1, review_after_minutes=60), NOW, 16.3)
        later = NOW+timedelta(minutes=3)
        trial.observe(self.store, 'simulated', {'002185':quote(16.4,later)}, {}, later)
        self.assertEqual(trial.claim_events(self.store, 'simulated', later), [])
        trial.write_cache(self.store, 'monitor_quote', {'002185':quote(16.0,later)}, later)
        later += timedelta(minutes=3)
        trial.observe(self.store, 'simulated', {'002185':quote(16.4,later)}, {}, later)
        rows = trial.claim_events(self.store, 'simulated', later)
        self.assertEqual([r['kind'] for r in rows], ['price_recovery'])

    def test_pending_price_from_superseded_plan_is_not_executed(self):
        trial.ingest(self.store, [candidate()], NOW)
        self.record_plan(plan(review_after_minutes=60), NOW-timedelta(minutes=5))
        trial.observe(self.store, 'simulated', {'002185':quote(16.3)}, {}, NOW)
        # Completion uses an earlier snapshot: the old event arrived in flight.
        self.record_plan(plan(review_above=17,review_after_minutes=60), NOW-timedelta(minutes=1))
        self.assertEqual(trial.claim_events(self.store, 'simulated', NOW), [])
        with self.store._get_conn() as conn:
            self.assertEqual(conn.execute('SELECT error FROM opportunity_events').fetchone()[0], 'price plan superseded')

    def test_event_report_names_its_trigger_and_keeps_risk_visible(self):
        context = {'stage':'1012', 'decision_trigger':'event', 'positions':[], 'candidates':[
            {'code':'002185','opportunity':{'events':[{'kind':'review_due'},{'kind':'price_recovery'}]}}]}
        text = render_report(context, 'live', {'report':{'risk':'结构未确认'}}, {})
        self.assertIn('实盘事件复核', text)
        self.assertIn('候选到期复核 1只', text)
        self.assertIn('价格走强 1只', text)
        self.assertIn('结构未确认', text)
        self.assertNotIn('半小时操盘', text)


if __name__ == '__main__':
    unittest.main()
