"""Compact model views and explicit, snapshot-bound reuse of unchanged plans.

Executors still validate complete records against full persisted evidence.
Only non-trading rows may reuse a plan; current decisions are never inferred.
"""
import copy
import hashlib
import json

from data.trading_assessment import GRADES, text


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def reuse_offer(stock, mode, as_of):
    research = stock.get('research') or {}
    opportunity = stock.get('opportunity') or {}
    previous = (stock.get('decision_context') or {}).get('previous') or {}
    stored = opportunity.get('previous_plan') or {}
    plan = stored.get('plan') or {}
    grades = previous.get('assessment_grades') or {}
    if research.get('status') != 'ready' or not research.get('revision'):
        return {'available': False, 'reason': 'research requires full review'}
    if (not plan or plan.get('state') not in {'watch','holding','account_blocked'}
            or not stored.get('reviewed_at') or previous.get('as_of') != stored.get('reviewed_at')
            or any(grades.get(k) not in values for k,values in GRADES.items())):
        return {'available': False, 'reason': 'no matching reusable plan and assessment'}
    if grades['research'] != ((research.get('profile') or {}).get('quality') or {}).get('grade'):
        return {'available': False, 'reason': 'research grade changed'}
    material = {'structure_risk','holding_fast_drop','logic_risk','news_changed',
                'research_changed','position_changed','new_opportunity','price_recovery','price_pullback'}
    if any(event.get('kind') in material for event in opportunity.get('events', [])):
        return {'available': False, 'reason': 'new event requires full review'}
    price = (stock.get('quote') or {}).get('price')
    invalidation = plan.get('invalidation_below')
    if price and invalidation and float(price) <= float(invalidation):
        return {'available': False, 'reason': 'structural risk requires full review'}
    if (stock.get('changes_since_last_decision') or {}).get('research_inputs_changed'):
        return {'available': False, 'reason': 'research changed since last decision'}
    basis = {'mode':mode, 'as_of':as_of, 'code':stock['code'], 'plan':stored,
             'grades':grades, 'research_revision':research['revision'],
             'facts_version':research.get('facts_version')}
    return {'available':True, 'ref':hashlib.sha256(encode(basis).encode()).hexdigest()[:24],
            'grades':grades}


def expand_review(row, stock, context):
    if 'reuse_plan' not in row:
        return
    if not context.get('decision_assessment_required'):
        raise ValueError('compact review requires decision assessment')
    holding = row['code'] in {s['code'] for s in context.get('positions', [])}
    if row['action'] != ('hold' if holding else 'watch'):
        raise ValueError('reuse_plan permits only explicit hold for holdings or watch for candidates; trades require full rows')
    if any(key in row for key in ('watch_plan','assessment','research_update','position_plan','exit_plan')):
        raise ValueError('reuse_plan cannot be combined with full plan/research/assessment fields')
    offer = reuse_offer(stock, context['mode'], context['as_of'])
    if not offer['available'] or row['reuse_plan'] != offer.get('ref'):
        raise ValueError('reuse_plan unavailable or stale; read current evidence and submit a full row')
    if row.get('review_grades') != offer['grades']:
        raise ValueError('changed grades require a full assessment and plan')
    reason = text(row.get('reason'), 'compact review reason')
    text(row.get('risk'), 'compact review risk')
    stored = stock['opportunity']['previous_plan']
    plan = copy.deepcopy(stored['plan'])
    plan.update(wait_reason=reason, requalified=False, requalification_reason='')
    row['name'] = stock.get('name') or row.get('name') or row['code']
    row['watch_plan'] = plan
    # Reasons are this round's explicit confirmation, never stale copied
    # evidence/confirmations. All normal assessment validation still runs.
    row['assessment'] = {k:{'grade':grade, 'reason':reason} for k,grade in offer['grades'].items()}
    row['assessment'].update(route_reason=reason, confidence_reason=reason, confirmations=[])
    row['plan_reuse'] = {'ref':row.pop('reuse_plan'), 'from_as_of':stored['reviewed_at'],
                         'research_revision':(stock.get('research') or {}).get('revision'),
                         'assessment_basis':'current explicit unchanged-grade review; no old confirmations inherited'}
    row.pop('review_grades')


def brief_stock(stock, mode, as_of):
    """Keep actionable facts and full risk text; move duplicate history to details.

    Do not invent summaries or truncate risks. Retain original paths for all
    emitted evidence so the full-evidence validator can resolve confirmations.
    """
    out = copy.deepcopy(stock)
    ready = (out.get('research') or {}).get('status') == 'ready'
    selection = out.get('selection') or {}
    keep = {'date','zone','entry_route','setup_stage','buy_eligible','risk_tags',
            'setup_triggers','opportunity','fundamental','logic_change'}
    if not ready:
        keep |= {'ai_selection','lifecycle','promotion'}
    out['selection'] = {k:v for k,v in selection.items() if k in keep}
    if ready and (out.get('research') or {}).get('profile'):
        profile = out['research']['profile']
        # The stored thesis/risks/refresh condition are the compact research
        # conclusion. Full company/trend discussions remain available on demand.
        out['research']['profile'] = {k:v for k,v in profile.items()
                                     if k in {'thesis','risks','refresh_condition','quality'}}
    previous = (out.get('decision_context') or {}).get('previous')
    if previous:
        out['decision_context']['previous'] = {k:v for k,v in previous.items()
                                               if k in {'as_of','action','confidence','assessment_grades'}}
    opportunity = out.get('opportunity') or {}
    stored = opportunity.get('previous_plan') or {}
    if stored:
        opportunity['previous_plan'] = {k:v for k,v in stored.items()
                                       if k in {'reviewed_at','last_action','plan'}}
        # Keep the original plan thesis: it can differ from the research thesis.
    for event in opportunity.get('events',[]):
        facts = event.get('facts') or {}
        facts.pop('plan',None)
        facts.pop('previous_quote',None)
        if isinstance(facts.get('quote'),dict):
            facts['quote'] = {k:v for k,v in facts['quote'].items() if k in {'price','source_time'}}
    out['review_reuse'] = reuse_offer(stock, mode, as_of)
    out['evidence_view'] = 'brief.v1; full evidence available via stock_evidence(view="full")'
    result = prune(out)
    result["position"] = out.get("position")
    return result


def prune(value):
    """Omit empty fields, preserving false/zero and list indices/source paths."""
    if isinstance(value,dict):
        return {k:prune(v) for k,v in value.items() if v is not None and v != '' and v != [] and v != {}}
    if isinstance(value,list):
        return [prune(v) for v in value]
    return value
