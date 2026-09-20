import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from data import agent_submission_service as service
from data.agent_submissions import get_submission
from data.adapters.fuyao_adapter import FuyaoAdapter
from data.research_financials import parse_financial, period_date, latest_financial, refresh_financials
from data.store.sqlite_store import StockStore
from data import opportunity_trial as trial
from tests.test_opportunity_trial import NOW, candidate, plan


class FinalWorkflowReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = StockStore(str(self.path/'test.db'))

    def submit(self, receipt, dry=False):
        context = {"stage":"1000", "as_of":trial.stamp(NOW), "opportunity_trial":True}
        def execute(*args, **kwargs):
            (self.path/'decision.json').write_text(json.dumps({"status":"ok"}))
            (self.path/'execution.json').write_text(json.dumps({"execution":receipt}))
            return SimpleNamespace(returncode=0,stdout='report',stderr='')
        with patch.object(service,'build_execution_context',return_value=context), \
             patch.object(service,'validate_simulated_decision',return_value={"signals":[]}), \
             patch.object(service.subprocess,'run',side_effect=execute), \
             patch('data.opportunity_trial.record_decision') as record, \
             patch('data.stock_research.record_updates'), patch('data.stock_research.record_observations'):
            result = service.submit_trading_decision(mode='simulated',stage='1000',as_of=context['as_of'],
                decision={},run_dir=self.path,provider='test',model='test',store=self.store,dry_run=dry)
            return result, record.call_count

    def test_executor_exception_does_not_complete_plans_or_replay(self):
        result, count = self.submit({"error":"write failed", "results":[]})
        self.assertEqual(result['status'],'failed')
        self.assertEqual(count,0)
        again,_ = self.submit({"results":[]})
        self.assertEqual(again['status'],'blocked')

    def test_after_close_skipped_execution_is_not_success(self):
        result,count = self.submit({"skipped":True,"results":[]})
        self.assertEqual((result['status'],count),('failed',0))

    def test_hold_and_individual_risk_rejections_are_completed_decisions(self):
        result,count = self.submit({"results":[{"executed":False,"errors":["T+1"]}]})
        self.assertEqual((result['status'],count),('submitted',1))

    def test_dry_run_does_not_enqueue_external_report_or_update_research(self):
        result,count = self.submit({"results":[]},dry=True)
        self.assertEqual((result['status'],count),('submitted',0))
        with self.store._get_conn() as conn:
            self.assertEqual(conn.execute('select count(*) from agent_message_outbox').fetchone()[0],0)

    def test_model_latency_does_not_skip_the_next_scheduled_review(self):
        trial.ingest(self.store,[candidate()],NOW)
        trial.record_decision(self.store,'simulated',{'signals':[{'code':'002185','action':'watch','watch_plan':plan()}]},
            {'as_of':trial.stamp(NOW),'positions':[]},{},NOW+timedelta(minutes=8))
        rows,*_ = trial.candidate_scope(self.store,'simulated',[],[],now=NOW+timedelta(minutes=30))
        self.assertEqual([r['code'] for r in rows],['002185'])

    def test_directory_refresh_does_not_generate_news_or_risk_event(self):
        from tests.test_opportunity_trial import quote
        trial.ingest(self.store,[candidate()],NOW)
        trial.record_decision(self.store,'simulated',{'signals':[{'code':'002185','action':'watch','watch_plan':plan()}]},
            {'as_of':trial.stamp(NOW-timedelta(minutes=10)),'positions':[]},{},NOW-timedelta(minutes=10))
        with self.store._get_conn() as conn:
            conn.execute("INSERT INTO news_events(code,title,content,url,score,risk_level,created_at) VALUES(?,?,?,?,?,?,?)",
                ('002185','公司动态','去年减持','https://basic.10jqka.com.cn/002185/event.html',5,'high',trial.stamp(NOW)))
        trial.observe(self.store,'simulated',{'002185':quote(16)}, {},NOW)
        with self.store._get_conn() as conn:
            self.assertEqual(conn.execute('select count(*) from opportunity_events').fetchone()[0],0)

    def test_validated_entry_fill_holding_monitor_t1_and_exit(self):
        from tests.test_trading_assessment import fixture
        from tests.test_opportunity_trial import quote
        from scripts.execute_trading_cycle import validate_simulated_decision
        from scripts.execute_trade_signal import execute
        row,context = fixture()
        trial.ingest(self.store,[candidate()],NOW)
        validated = validate_simulated_decision({'signals':[row]},context)
        with patch('account.trader.StockStore',return_value=self.store), \
             patch('scripts.execute_trade_signal.fetch_live_prices',return_value={'002185':10}):
            receipt = execute(validated)
            self.assertTrue(receipt['results'][0]['executed'])
            trial.record_decision(self.store,'simulated',validated,context,receipt,NOW)
            positions = {p['code']:p['volume'] for p in self.store.get_positions()}
            trial.observe(self.store,'simulated',{'002185':quote(10)},positions,NOW)
            exit_order = {'signals':[{'code':'002185','action':'clear','reason':'结构失效'}]}
            rejected = execute(exit_order)
            self.assertFalse(rejected['results'][0]['executed'])
            self.assertTrue(any('T+1' in e for e in rejected['results'][0]['errors']))
            # Roll the synthetic fill into a prior day to exercise sellability.
            with self.store._get_conn() as conn:
                conn.execute("UPDATE orders SET created_at=datetime('now','localtime','-1 day') WHERE direction='buy'")
            sold = execute(exit_order)
            self.assertTrue(sold['results'][0]['executed'])
            self.assertEqual(self.store.get_positions(),[])
            later=NOW+timedelta(minutes=3)
            trial.observe(self.store,'simulated',{'002185':quote(10,later)}, {},later)
            with self.store._get_conn() as conn:
                self.assertEqual(conn.execute("SELECT position_volume FROM opportunity_plans WHERE mode='simulated'").fetchone()[0],0)
                self.assertEqual(conn.execute("SELECT count(*) FROM orders WHERE status='filled'").fetchone()[0],2)

    def test_financial_zero_missing_mixed_periods_and_foreign_stock(self):
        value = parse_financial({'股票代码':'002185.SZ','净资产收益率[20260630]':0,
            '销售毛利率[20260331]':10,'资产负债率[20260630]':'NaN','每股收益':3},'002185')
        self.assertEqual(value['roe'],0)
        self.assertIsNone(value['gross_margin'])
        self.assertIsNone(value['debt_ratio'])
        self.assertIsNone(value['eps'])
        with self.assertRaises(ValueError):parse_financial({'股票代码':'000001.SZ'},'002185')

    def test_period_sorting_is_chronological_across_storage_formats(self):
        self.assertGreater(period_date('2026A'),period_date('2026Q3'))
        self.assertEqual(period_date('20260630'),period_date('2026Q2'))
        with self.store._get_conn() as conn:
            for period in ('2026Q1','20260630','2026A'):
                conn.execute("INSERT INTO financial_factors(code,period,source,updated_at) VALUES('002185',?,'test','2026-09-18 08:00:00')",(period,))
            self.assertEqual(latest_financial(conn,'002185',trial.stamp(NOW))['period'],'20260630')

    def test_financial_cache_reuses_data_and_preserves_observation_on_failure(self):
        raw={'datas':[{'股票代码':'002185.SZ','净资产收益率[20260630]':0}]}
        with patch('data.adapters.iwencai_client.IwenCaiClient.query2data',return_value=raw) as fetch, \
             patch.object(FuyaoAdapter,'financials',return_value=None):
            self.assertEqual(refresh_financials(self.store,['002185'],NOW)['available'],1)
            self.assertEqual(refresh_financials(self.store,['002185'],NOW+timedelta(minutes=30))['requested'],0)
            self.assertEqual(fetch.call_count,1)
        with patch('data.adapters.iwencai_client.IwenCaiClient.query2data',side_effect=TimeoutError), \
             patch.object(FuyaoAdapter,'financials',side_effect=RuntimeError('rate limited')):
            refresh_financials(self.store,['002185'],NOW+timedelta(days=1))
            with self.store._get_conn() as conn:
                data=latest_financial(conn,'002185',trial.stamp(NOW+timedelta(days=1,minutes=1)))
                self.assertEqual(data['updated_at'],trial.stamp(NOW))
                self.assertTrue(data['evidence_stale'])
                self.assertEqual(data['roe'],0)

    def test_fallback_covers_empty_primary_and_throttles_failed_attempts(self):
        with patch('data.adapters.iwencai_client.IwenCaiClient.query2data',return_value={}), \
             patch.object(FuyaoAdapter,'financials',return_value={'report':'2026-2','source':'fuyao','indicators':{'sale_gross_margin':14}}):
            self.assertEqual(refresh_financials(self.store,['002185'],NOW)['available'],1)
        with self.store._get_conn() as conn:
            data=latest_financial(conn,'002185',trial.stamp(NOW))
            self.assertEqual(data['period'],'20260630')
            self.assertEqual(data['supplement']['indicators']['sale_gross_margin'],14)

    def test_fuyao_business_error_and_identity_checked_without_retry(self):
        for payload in ({'code':4001}, {'code':0,'data':{'thscode':'000001.SZ','report':'2026-2'}}):
            with patch('data.adapters.fuyao_adapter.urllib.request.urlopen') as request:
                request.return_value.__enter__.return_value.read.return_value=json.dumps(payload).encode()
                with self.assertRaises((RuntimeError,ValueError)):FuyaoAdapter('test').financials('002185','2026-2')
                self.assertEqual(request.call_count,1)


if __name__ == '__main__':unittest.main()
