import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from data.adapters.fuyao_adapter import FuyaoAdapter
from data.research_financials import refresh_financials, latest_financial, FUYAO_FIELDS
from data.services.fuyao_sector_service import FuyaoSectorService
from data.services.sector_rotation_service import SectorRotationService
from data.services.stock_sector_membership_service import load_stock_memberships, replace_stock_memberships
from data.store.sqlite_store import StockStore

NOW = datetime(2026, 9, 22, 18)
TZ = ZoneInfo('Asia/Shanghai')


def ms(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp()*1000)


def statement(**changes):
    return {'thscode':'002957.SZ', 'report_date_ms':ms('2026-08-28'),
            'period_end_ms':ms('2026-06-30'), 'currency':'CNY',
            'basic_eps':0, 'act_cash_flow_net':100, **changes}


class FinancePriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = StockStore(str(Path(self.temp.name)/'test.db'))
        self.primary = {'source':'fuyao','report':'2026-2', 'indicators': {names[0]:2 for names in FUYAO_FIELDS.values()}}

    def refresh(self, primary=None, fallback=None, statements=None):
        with patch.object(FuyaoAdapter,'financials',return_value=primary or self.primary), \
             patch.object(FuyaoAdapter,'statement',side_effect=statements or (lambda code, kind:[statement()])), \
             patch('data.adapters.iwencai_client.IwenCaiClient.query2data',return_value=fallback or {}) as query:
            result = refresh_financials(self.store,['002957'],NOW)
            calls = query.call_count
        with self.store._get_conn() as conn:
            data = latest_financial(conn,'002957',NOW.isoformat(sep=' '))
        return result, data, calls

    def test_complete_fuyao_skips_iwencai_and_reuses_across_callers(self):
        result,data,calls = self.refresh()
        self.assertEqual(calls,0); self.assertEqual(data['eps'],0)
        self.assertEqual(data['operating_cash_flow'],100)
        self.assertEqual(data['source'],'fuyao')
        with patch.object(FuyaoAdapter,'financials',side_effect=AssertionError('must use cache')), \
             patch('data.adapters.iwencai_client.IwenCaiClient.query2data',side_effect=AssertionError('must use cache')):
            self.assertEqual(refresh_financials(self.store,['002957'],NOW+timedelta(minutes=30))['requested'],0)
        with self.store._get_conn() as conn:
            self.assertEqual(conn.execute("SELECT source FROM financial_factors WHERE code='002957'").fetchone()[0],'fuyao')

    def test_only_missing_fields_are_filled_from_same_period(self):
        self.primary['indicators'].pop(FUYAO_FIELDS['roe'][0])
        _,data,calls=self.refresh(fallback={'datas':[{'股票代码':'002957.SZ','净资产收益率[20260630]':9,'销售毛利率[20260630]':99}]})
        self.assertEqual(calls,1); self.assertEqual(data['roe'],9)
        self.assertEqual(data['gross_margin'],2); self.assertEqual(data['field_sources']['roe'],'iwencai')

    def test_old_fallback_cannot_fill_new_report(self):
        self.primary['indicators'].pop(FUYAO_FIELDS['roe'][0])
        _,data,_=self.refresh(fallback={'datas':[{'股票代码':'002957.SZ','净资产收益率[20260331]':9}]})
        self.assertIsNone(data.get('roe')); self.assertEqual(data['period'],'20260630')

    def test_newer_fallback_does_not_mix_old_fuyao_values(self):
        self.primary['indicators'].pop(FUYAO_FIELDS['roe'][0])
        # Test the merge independently of actual quarter-end schedule.
        with patch('data.services.finance_service.FinanceService.recent_periods', return_value=[('2026',1)]):
            _,data,_=self.refresh(statements=lambda code,kind:[], fallback={'datas':[{'股票代码':'002957.SZ','净资产收益率[20260630]':9}]})
        self.assertEqual(data['period'],'20260630'); self.assertEqual(data['roe'],9)
        self.assertIsNone(data.get('gross_margin')); self.assertNotIn('supplement',data)

    def test_future_publication_and_wrong_period_statement_not_used(self):
        _,data,calls=self.refresh(statements=lambda code,kind:[statement(report_date_ms=ms('2026-09-23'))])
        self.assertIsNone(data.get('eps')); self.assertIsNone(data.get('operating_cash_flow'))
        self.assertEqual(calls,1)

    def test_business_rate_limit_switches_and_persists_cooldown(self):
        def response(payload):
            r=Mock();r.__enter__=Mock(return_value=io.BytesIO(json.dumps(payload).encode()));r.__exit__=Mock(return_value=False);return r
        adapter=FuyaoAdapter(api_keys=['key-a','key-b'],state_path=Path(self.temp.name)/'keys.json')
        with patch('data.adapters.fuyao_adapter.urllib.request.urlopen',side_effect=[response({'code':4001}),response({'code':0,'data':{}})]) as fetch:
            adapter._request('https://fuyao.aicubes.cn/api/test')
            self.assertEqual(fetch.call_count,2)
        state=json.loads((Path(self.temp.name)/'keys.json').read_text())
        self.assertEqual(len(state['cooldowns']),1)
        self.assertNotIn('key-a',json.dumps(state))


class SectorPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=StockStore(str(Path(self.temp.name)/'test.db'))
        self.adapter=Mock()
        self.service=FuyaoSectorService(store=self.store,adapter=self.adapter)
        self.sector={'thscode':'885001.TI','name':'测试板块','kind':'concept'}

    def test_cached_constituents_replace_sector_without_erasing_other_sectors(self):
        with self.store._get_conn() as conn:
            replace_stock_memberships(conn,'000001',industries=[],concepts=['测试板块','其他板块'])
        self.adapter.get.return_value={'item':[{'thscode':'000002.SZ','name':'测试'}]}
        self.service.members(self.sector,[1]);self.service.members(self.sector,[0])
        self.assertEqual(self.adapter.get.call_count,1)
        facts=load_stock_memberships(self.store,['000001','000002'])
        self.assertEqual({r['sector_name'] for r in facts['000001']},{'其他板块'})
        self.assertEqual(facts['000002'][0]['source'],'fuyao_constituents')

    def test_empty_constituents_do_not_delete_old_facts(self):
        self.adapter.get.return_value={'item':[]}
        with self.store._get_conn() as conn:
            replace_stock_memberships(conn,'000001',industries=[],concepts=['测试板块'],source='fuyao_constituents')
        with self.assertRaises(ValueError):self.service.members(self.sector,[1])
        self.assertEqual(len(load_stock_memberships(self.store,['000001'])['000001']),1)
        with self.assertRaises(RuntimeError):self.service.members(self.sector,[1])
        self.assertEqual(self.adapter.get.call_count,1)

    def test_history_uses_documented_close_price_and_excludes_quote_day(self):
        today=datetime.now(TZ).replace(hour=12,minute=0,second=0,microsecond=0)
        bars=[{'date_ms':int((today-timedelta(days=i)).timestamp()*1000),'close_price':100} for i in range(1,130)]
        bars.append({'date_ms':int(today.timestamp()*1000),'close_price':1})
        with patch.object(self.service,'catalog',return_value=[self.sector]), \
             patch.object(self.service,'quotes',return_value={'885001.TI':{'price_change_ratio_pct':1,'last_price':110,'source_time':today.strftime('%Y-%m-%d %H:%M:%S')}}), \
             patch.object(self.service,'history',return_value={'item':bars}), \
             patch.object(self.service,'members',return_value={}):
            row=self.service.snapshot_rows()['rows'][0]
        self.assertEqual(row['pct_5d'],10);self.assertEqual(row['pct_3m'],10)
        self.assertIsNone(row['fund_inflow'])

    def test_quote_wrong_identity_or_old_timestamp_rejected(self):
        for payload in ({'timestamp':ms('2020-01-01'),'item':[]},
                        {'timestamp':datetime.now(TZ).timestamp()*1000,'item':[{'thscode':'wrong'}]}):
            with tempfile.TemporaryDirectory() as cache:
                svc=FuyaoSectorService(store=self.store,adapter=self.adapter,cache_dir=cache)
                self.adapter.get.return_value=payload
                with self.assertRaises(ValueError):svc.quotes([self.sector])

    def test_partial_history_gives_no_bonus_or_fund_flow_confirmation(self):
        row={'name':'测试板块','code':'885001.TI','pct_1d':9,'turnover':100000000,
             'pct_5d':None,'pct_1m':None,'pct_3m':None,'pct_6m':None,'fund_inflow':None}
        svc=SectorRotationService(store=self.store)
        with patch.object(svc,'_fetch_raw',return_value={'fuyao':{'rows':[row],'errors':['history unavailable']}}),patch.object(svc,'_write_cache'):
            snapshot=svc.get_snapshot(refresh=True)
        signal=snapshot['signals'][0]
        self.assertEqual(signal['score'],0);self.assertEqual(signal['stage'],'neutral')
        self.assertIsNone(signal['fund_inflow']);self.assertEqual(signal['evidence_status'],'partial')
        self.assertEqual(snapshot['fund_flow_status'],'unavailable')


if __name__=='__main__':unittest.main()
