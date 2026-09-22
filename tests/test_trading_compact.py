import asyncio
import copy
import json
import runpy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from data import trading_compact as compact
from data import trading_decision_repository as repository
from data import agent_submissions
from data.store.sqlite_store import StockStore
from data.trading_state import persist_trading_state
from scripts.execute_trading_cycle import validate_simulated_decision, validate_live_decision
from tests.test_trading_assessment import fixture
from tests.test_opportunity_trial import NOW, plan
from tests.test_trading_state import _item


def facts():
    _, ctx = fixture()
    stock = ctx['candidates'][0]
    stock['name']='华天科技'
    old=(NOW-timedelta(minutes=30)).isoformat(sep=' ')
    grades={'research':'moderate','timing':'wait','evidence':'partial','portfolio':'fit'}
    stock['research']['profile'].update(thesis='核心经营改善论据',risks='现金转化不足，不得忽略',
        refresh_condition='业绩或结构证伪时更新',company_view='公司详细讨论',trend_view='历史详细讨论')
    stock['opportunity']={'previous_plan':{'plan':plan(review_above=11,invalidation_below=9),
        'reviewed_at':old,'version':'old-price-version','last_action':'buy'}, 'events':[]}
    stock['decision_context']={'previous':{'as_of':old,'action':'buy','assessment_grades':grades,
        'reason':'旧决策重复叙述','risk':'旧风险'}, 'entry_thesis':{'reason':'实际入场依据'}}
    return stock, ctx


def short(stock, ctx):
    offer=compact.reuse_offer(stock,ctx['mode'],ctx['as_of'])
    return {'code':stock['code'],'action':'watch','confidence':'medium',
            'reason':'本轮价格与风险已核对，继续等原确认条件','risk':'现金转化风险仍在',
            'reuse_plan':offer['ref'],'review_grades':copy.deepcopy(offer['grades'])}


class CompactReviewTests(unittest.TestCase):
    def test_both_accounts_expand_nontrade_without_inheriting_buy_or_old_confirmations(self):
        for mode,validator,key in [('simulated',validate_simulated_decision,'signals'),('live',validate_live_decision,'decisions')]:
            stock,ctx=facts();ctx['mode']=mode
            row=short(stock,ctx)
            result=validator({key:[row]},ctx)[key][0]
            self.assertEqual(result['action'],'watch')
            self.assertEqual(result['name'],stock['name'])
            self.assertEqual(result['watch_plan']['thesis'],stock['opportunity']['previous_plan']['plan']['thesis'])
            self.assertEqual(result['watch_plan']['wait_reason'],row['reason'])
            self.assertFalse(result['watch_plan']['requalified'])
            self.assertEqual(result['assessment']['confirmations'],[])
            self.assertEqual(result['assessment']['timing']['reason'],row['reason'])
            self.assertIn('plan_reuse',result)
            self.assertNotIn('reuse_plan',result)
            # The executor validates an already-expanded persisted submission again.
            twice=validator({key:[result]},ctx)[key][0]
            self.assertEqual(twice['assessment'],result['assessment'])

    def test_holdings_still_require_explicit_current_hold_and_all_codes(self):
        stock,ctx=facts();row=short(stock,ctx)
        ctx['positions']=[stock];ctx['candidates']=[]
        with self.assertRaisesRegex(ValueError,'hold for holdings'):
            validate_simulated_decision({'signals':[row]},ctx)
        row['action']='hold'
        self.assertEqual(validate_simulated_decision({'signals':[row]},ctx)['signals'][0]['action'],'hold')
        with self.assertRaisesRegex(ValueError,'omitted current positions'):
            validate_simulated_decision({'signals':[]},ctx)

    def test_trades_and_mixed_full_fields_cannot_use_shortcut(self):
        for action in ('buy','add','sell','reduce','clear'):
            stock,ctx=facts();row=short(stock,ctx);row['action']=action
            with self.assertRaisesRegex(ValueError,'trades require full'):
                validate_simulated_decision({'signals':[row]},ctx)
        for field in ('watch_plan','assessment','research_update'):
            stock,ctx=facts();row=short(stock,ctx);row[field]={}
            with self.assertRaisesRegex(ValueError,'cannot be combined'):
                validate_simulated_decision({'signals':[row]},ctx)

    def test_refs_bind_account_snapshot_research_and_entire_plan(self):
        for change in ('mode','as_of','facts','plan','code'):
            stock,ctx=facts();row=short(stock,ctx)
            if change=='mode':ctx['mode']='live'
            elif change=='as_of':ctx['as_of']='2026-09-18 10:13:00'
            elif change=='facts':stock['research']['facts_version']='changed'
            elif change=='plan':stock['opportunity']['previous_plan']['plan']['thesis']='changed thesis'
            else:stock['code']='002186';row['code']='002186'
            with self.assertRaisesRegex(ValueError,'unavailable or stale'):
                validate_simulated_decision({'signals':[row]},ctx)

    def test_new_events_research_or_structural_failure_require_full_review(self):
        for kind in ('structure_risk','holding_fast_drop','logic_risk','news_changed',
                     'research_changed','position_changed','new_opportunity','price_recovery','price_pullback'):
            stock,ctx=facts();row=short(stock,ctx)
            stock['opportunity']['events']=[{'kind':kind}]
            self.assertFalse(compact.reuse_offer(stock,ctx['mode'],ctx['as_of'])['available'])
            with self.assertRaises(ValueError):validate_simulated_decision({'signals':[row]},ctx)
        for change in ('expired','grade','invalidation','research_inputs'):
            stock,ctx=facts()
            if change=='expired':stock['research']['status']='refresh_required'
            elif change=='grade':stock['research']['profile']['quality']['grade']='weak'
            elif change=='invalidation':stock['quote']['price']=8
            else:stock['changes_since_last_decision']={'research_inputs_changed':True}
            self.assertFalse(compact.reuse_offer(stock,ctx['mode'],ctx['as_of'])['available'])

    def test_changed_grades_and_empty_current_reason_are_not_accepted(self):
        stock,ctx=facts();row=short(stock,ctx);row['review_grades']['timing']='ready'
        with self.assertRaisesRegex(ValueError,'changed grades'):
            validate_simulated_decision({'signals':[row]},ctx)
        for field in ('reason','risk'):
            row=short(stock,ctx);row[field]=''
            with self.assertRaisesRegex(ValueError,'compact review'):
                validate_simulated_decision({'signals':[row]},ctx)

    def test_brief_keeps_risks_facts_and_paths_without_mutating_full_evidence(self):
        stock,ctx=facts()
        stock['fund_flow']={'status':'cached','source_date_verified':False,'main_net':0}
        stock['selection']['ai_selection']={'reason':'重复的选股历史'}
        stock['opportunity']['events']=[{'kind':'news_changed','facts':{'title':'重要风险',
            'quote':{'price':10,'source_time':'20260918101200','high':11},'plan':{'thesis':'重复计划'}}}]
        original=copy.deepcopy(stock)
        brief=compact.brief_stock(stock,ctx['mode'],ctx['as_of'])
        self.assertEqual(stock,original)
        for field in ('quote','technical','intraday','fund_flow'):
            self.assertEqual(brief[field],stock[field])
        self.assertEqual(brief['research']['profile']['risks'],stock['research']['profile']['risks'])
        self.assertEqual(brief['decision_context']['entry_thesis'],stock['decision_context']['entry_thesis'])
        self.assertEqual(brief['opportunity']['events'][0]['facts']['title'],'重要风险')
        self.assertNotIn('company_view',brief['research']['profile'])
        self.assertNotIn('ai_selection',brief['selection'])
        stock['research']['status']='refresh_required'
        self.assertIn('company_view',compact.brief_stock(stock,ctx['mode'],ctx['as_of'])['research']['profile'])

    def test_repository_full_financial_only_and_shortcut_use_same_snapshot(self):
        stock,ctx=facts();as_of=ctx['as_of'];old=stock['opportunity']['previous_plan']['reviewed_at']
        item=_item(stock['code'],candidate=True)
        item.update(research=stock['research'],opportunity=stock['opportunity'],
                    fundamental={'period':'20260630','revenue_yoy':12},quote=stock['quote'])
        with tempfile.TemporaryDirectory() as folder:
            store=StockStore(str(Path(folder)/'test.db'))
            claim=agent_submissions.claim_submission(store=store,task='trading',mode='simulated',as_of=old,
                stage='0942',provider='test',model='test',decision={'signals':[{'code':stock['code'],
                'action':'watch','assessment':{k:{'grade':v,'reason':'旧判断'} for k,v in stock['decision_context']['previous']['assessment_grades'].items()}}]})
            agent_submissions.complete_submission(store=store,key=claim.submission_key,result={},report='')
            persist_trading_state({'as_of':as_of,'stage':'1012','market':{},'account':ctx['account'],
                'account_policy':{},'positions':[],'candidates':[item],'tracked':[],
                'refresh':{'opportunity_trial':True,'decision_assessment_required':True}},'simulated',store)
            with patch.object(repository,'DB_PATH',Path(store.db_path)):
                brief=repository.get_stock_evidence([stock['code']],as_of,'simulated')['stocks'][0]
                full=repository.get_stock_evidence([stock['code']],as_of,'simulated',view='full')['stocks'][0]
                finance=repository.get_stock_evidence([stock['code']],as_of,'simulated',view='financials')['stocks'][0]
                self.assertNotIn('company_view',brief['research']['profile'])
                self.assertEqual(full['research']['profile']['company_view'],'公司详细讨论')
                self.assertEqual(finance['selection']['fundamental']['revenue_yoy'],12)
                self.assertNotIn('quote',finance)
                self.assertNotIn('profile',finance['research'])
                actual_context=repository.build_execution_context(as_of,'simulated')
                row=short(stock,ctx);row['reuse_plan']=brief['review_reuse']['ref']
                self.assertEqual(validate_simulated_decision({'signals':[row],'reviewed_codes':[stock['code']]},actual_context)['signals'][0]['action'],'watch')
                with self.assertRaisesRegex(ValueError,'state changed'):
                    repository.get_stock_evidence([stock['code']],old,'simulated')
                with self.assertRaisesRegex(ValueError,'outside current'):
                    repository.get_stock_evidence(['600001'],as_of,'simulated')

    def test_mcp_wire_is_json_text_without_pretty_print_or_double_encoding(self):
        with tempfile.TemporaryDirectory() as folder,patch('sys.argv',['stock_trading_mcp.py','--mode','simulated',
                '--stage','1012','--run-dir',folder]):
            server=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/stock_trading_mcp.py'))
        value={'风险':'不得忽略','zero':0,'flag':False}
        encoded=server['wire'](value)
        self.assertEqual(json.loads(encoded),value)
        self.assertNotIn('\n',encoded)
        # Exercise FastMCP conversion, not only our encoder.
        fn=server['trading_overview']
        with patch.dict(fn.__globals__, {'get_trading_overview': lambda mode: value}):
            blocks=asyncio.run(server['mcp'].call_tool('trading_overview',{}))
        self.assertEqual(len(blocks),1)
        self.assertEqual(blocks[0].type,'text')
        self.assertEqual(json.loads(blocks[0].text),value)
        self.assertEqual(blocks[0].text,encoded)


if __name__=='__main__':unittest.main()
