"""Factual live-intent deduplication and numeric helpers; no model/network."""
import json
import math


def finite(value):
    try:
        value = float(value) if value is not None and not isinstance(value, bool) else None
        return value if value is not None and math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def intent_evidence(stock, context):
    return {'as_of':context['as_of'],
            'research_facts_version':(stock.get('research') or {}).get('facts_version'),
            'events':[{k:event.get(k) for k in ('kind','created_at')} for event in
                      (stock.get('opportunity') or {}).get('events', [])]}


def account_facts(snapshot):
    return {'cash':(snapshot.get('summary') or {}).get('available_cash'),
            'positions':sorted((str(p['code']), int(p.get('volume') or 0),
                                int(p.get('available_to_sell') or 0)) for p in snapshot.get('positions',[]))}


def ensure_intent_evidence(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS live_intent_evidence (intent_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')


def unchanged_intent(conn, decision, price, volume, snapshot, now, price_threshold=1.0):
    """A current-day unchanged intent is a no-op, not an execution failure.

    Fill/opposite action/day boundary or material price/size/account/evidence
    changes permit a new proposal. Rewording the rationale is not new evidence.
    """
    old = conn.execute('SELECT * FROM live_trade_intents WHERE code=? ORDER BY created_at DESC,id DESC LIMIT 1',
                       (decision['code'],)).fetchone()
    if not old or old['action'] != decision['action'] or old['status'] not in {'proposed','expired','cancelled','rejected'}:
        return None
    if str(old['created_at'])[:10] != str(now.date()):
        return None
    old_price = finite(old['suggested_price'])
    if not old_price or abs(price/old_price-1)*100 >= price_threshold or int(old['suggested_volume']) != volume:
        return None
    previous = conn.execute('SELECT payload FROM live_intent_evidence WHERE intent_id=?', (old['intent_id'],)).fetchone()
    if not previous:
        return None  # Cannot prove legacy proposal's account/evidence unchanged.
    previous = json.loads(previous['payload'])
    if previous.get('account') != json.loads(json.dumps(account_facts(snapshot))):
        return None
    current = (decision.get('raw') or {}).get('notification_evidence') or {}
    before = previous.get('evidence') or {}
    if current.get('research_facts_version') != before.get('research_facts_version'):
        return None
    urgent = {'structure_risk','holding_fast_drop','logic_risk','news_changed',
              'research_changed','position_changed','new_opportunity','price_recovery','price_pullback'}
    if any(event.get('kind') in urgent and str(event.get('created_at') or '') > str(old['created_at'])
           for event in current.get('events', [])):
        return None
    return {'intent_id':old['intent_id'], 'status':old['status'], 'expires_at':old['expires_at']}
