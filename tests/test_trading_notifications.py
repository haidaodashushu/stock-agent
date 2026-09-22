import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from data import agent_submission_service as service
from data.agent_submissions import get_submission
from data.store.sqlite_store import StockStore


class TradingNotificationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.store = StockStore(str(self.path / 'test.db'))
        self.as_of = '2026-09-22 10:12:00'

    def submit(self, receipt, *, mode='simulated', event=True, action='watch', dry=False):
        context = {'stage':'1012', 'as_of':self.as_of, 'opportunity_trial':True,
                   'decision_trigger':'event' if event else 'scheduled_review'}
        validated = {'signals' if mode=='simulated' else 'decisions':[
            {'code':'002185','action':action}]}
        def execute(command, **kwargs):
            if any('render_live_trade_fortune.py' in part for part in command):
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            (self.path/'decision.json').write_text(json.dumps({'status':'ok'}))
            (self.path/'execution.json').write_text(json.dumps({'execution':receipt}))
            return SimpleNamespace(returncode=0, stdout='完整复核报告', stderr='')
        validator = 'validate_simulated_decision' if mode=='simulated' else 'validate_live_decision'
        with patch.object(service,'build_execution_context',return_value=context), \
             patch.object(service,validator,return_value=validated), \
             patch.object(service.subprocess,'run',side_effect=execute) as executor, \
             patch('data.opportunity_trial.record_decision') as plans, \
             patch('data.stock_research.record_updates') as research, \
             patch('data.stock_research.record_observations') as observations:
            result = service.submit_trading_decision(mode=mode, stage='1012', as_of=self.as_of,
                decision={}, run_dir=self.path, provider='test', model='test', store=self.store, dry_run=dry)
        self.assertEqual(result['status'], 'submitted')
        with self.store._get_conn() as conn:
            messages = conn.execute('SELECT COUNT(*) FROM agent_message_outbox').fetchone()[0]
        stored = get_submission(store=self.store, task='trading', mode=mode, as_of=self.as_of)
        self.assertEqual(stored['status'], 'ready')
        self.assertEqual(stored['report'], '完整复核报告')
        self.assertEqual(stored['result']['notification'], result['notification'])
        for update in (plans, research, observations):
            self.assertEqual(update.call_count, 0 if dry else 1)
        return result, messages, executor.call_count

    def test_event_hold_is_silent_but_decision_and_research_are_saved(self):
        result, messages, _ = self.submit({'results':[{'executed':False,'errors':[]}]}, action='hold')
        self.assertEqual(messages, 0)
        self.assertEqual(result['notification']['reason'], 'event_review_without_action')

    def test_live_event_without_new_intent_is_silent_and_skips_fortune(self):
        _, messages, calls = self.submit({'results':[{'created_intent':False}]}, mode='live')
        self.assertEqual((messages,calls), (0,1))

    def test_suppressed_repeated_live_buy_is_saved_without_notification(self):
        result, messages, calls = self.submit({'results':[{'created_intent':False,'executed':False,
            'errors':[], 'message':'unchanged_live_intent_suppressed'}]}, mode='live', action='buy')
        self.assertEqual((messages,calls), (0,1))
        self.assertEqual(result['notification']['reason'], 'event_review_without_action')

    def test_scheduled_report_is_sent_even_without_action(self):
        _, messages, _ = self.submit({'results':[]}, event=False)
        self.assertEqual(messages, 1)

    def test_simulated_fill_is_sent(self):
        result, messages, _ = self.submit({'results':[{'executed':True}]}, action='reduce')
        self.assertEqual(messages, 1)
        self.assertEqual(result['notification']['reason'], 'trade_or_new_intent')

    def test_new_live_intent_is_sent_before_manual_fill(self):
        _, messages, _ = self.submit({'results':[{'created_intent':True,'executed':False}]}, mode='live', action='buy')
        self.assertEqual(messages, 1)

    def test_individual_rejection_remains_visible_without_a_fill(self):
        result, messages, _ = self.submit({'results':[{'executed':False,'errors':['T+1不可卖']}]}, action='reduce')
        self.assertEqual(messages, 1)
        self.assertEqual(result['notification']['reason'], 'execution_issue')

    def test_model_action_alone_does_not_count_as_a_fill(self):
        _, messages, _ = self.submit({'results':[{'executed':False,'errors':[]}]}, action='buy')
        self.assertEqual(messages, 0)

    def test_dry_run_never_sends_or_updates_research(self):
        result, messages, _ = self.submit({'results':[{'executed':True}]}, dry=True)
        self.assertEqual(messages, 0)
        self.assertEqual(result['notification']['reason'], 'dry_run')


if __name__=='__main__':
    unittest.main()
