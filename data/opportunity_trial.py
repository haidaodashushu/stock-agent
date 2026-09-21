"""Durable opportunity research and bounded event scheduling.

No model calls, orders or external messages in this module. Historical
qualification provides observation rights; a new buy needs explicit current
requalification at the normal decision validator.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

from data.market_calendar import market_day
from data.security_universe import is_supported_board_code
from data.trading_data_quality import valid_quote

ROOT = Path(__file__).resolve().parents[1]


def settings():
    default = ROOT / "config/opportunity_trial.json"
    values = json.loads(default.read_text())
    path = Path(os.environ.get("STOCK_OPPORTUNITY_CONFIG") or default)
    if path != default:
        values.update(json.loads(path.read_text()))
    return values


def enabled():
    return bool(settings().get("enabled"))


def stamp(now=None):
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def obj(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def ensure_tables(store):
    with store._get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS opportunity_setups (
          code TEXT PRIMARY KEY, setup_id TEXT NOT NULL, first_seen TEXT NOT NULL,
          last_seen TEXT NOT NULL, expires_on TEXT NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS opportunity_plans (
          mode TEXT NOT NULL, code TEXT NOT NULL, setup_id TEXT NOT NULL,
          plan TEXT NOT NULL, version TEXT NOT NULL, reviewed_at TEXT NOT NULL,
          last_action TEXT NOT NULL, position_volume INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(mode,code));
        CREATE TABLE IF NOT EXISTS opportunity_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, code TEXT NOT NULL,
          setup_id TEXT NOT NULL, kind TEXT NOT NULL, dedup TEXT UNIQUE NOT NULL,
          payload TEXT NOT NULL, created_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', batch_id TEXT NOT NULL DEFAULT '',
          finished_at TEXT, error TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS opportunity_event_queue ON opportunity_events(mode,status,created_at);
        CREATE TABLE IF NOT EXISTS opportunity_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, code TEXT,
          kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS opportunity_cache (
          kind TEXT NOT NULL, code TEXT NOT NULL, payload TEXT NOT NULL,
          fetched_at TEXT NOT NULL, PRIMARY KEY(kind,code));
        CREATE TABLE IF NOT EXISTS opportunity_monitor_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
          scope_count INTEGER NOT NULL, quote_ok INTEGER NOT NULL,
          elapsed_seconds REAL NOT NULL, payload TEXT NOT NULL);
        """)


def expiry(day, sessions):
    current = datetime.strptime(day, "%Y-%m-%d").date()
    for _ in range(sessions):
        current += timedelta(days=1)
        while not market_day(current).is_open:
            current += timedelta(days=1)
    return current.isoformat()


def ingest(store, candidates, now=None):
    now = now or datetime.now()
    ensure_tables(store)
    with store._get_conn() as conn:
        for candidate in candidates:
            code = str(candidate.get("code") or "").zfill(6)
            extra = obj(candidate.get("extra"))
            route = obj(extra.get("selector")).get("entry_route") or obj(extra.get("ai_selection")).get("entry_route")
            day = str(candidate.get("run_date") or now.date().isoformat())[:10]
            if not is_supported_board_code(code) or route not in {"early_start", "strong_continuation"} or day > now.date().isoformat():
                continue
            old = conn.execute("SELECT * FROM opportunity_setups WHERE code=?", (code,)).fetchone()
            if old and old["last_seen"] > day:
                continue
            setup_id = old["setup_id"] if old and old["expires_on"] >= day else f"{code}:{day}"
            end = expiry(day, settings()["observation_sessions"])
            if not old or old["source"] != encode(candidate):
                conn.execute("INSERT INTO opportunity_audit(code,kind,payload,created_at) VALUES(?,?,?,?)",
                             (code,"selection_discovery",encode({"setup_id":setup_id,"source":candidate}),stamp(now)))
            conn.execute("""INSERT INTO opportunity_setups VALUES (?,?,?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET setup_id=excluded.setup_id,
                first_seen=CASE WHEN opportunity_setups.setup_id=excluded.setup_id THEN opportunity_setups.first_seen ELSE excluded.first_seen END,
                last_seen=excluded.last_seen,expires_on=excluded.expires_on,source=excluded.source""",
                (code, setup_id, day, day, end, encode(candidate)))


def bootstrap(store, now=None):
    """Adopt only historical final buy selections, never raw screen scores."""
    now = now or datetime.now()
    ensure_tables(store)
    with store._get_conn() as conn:
        rows = conn.execute("""SELECT * FROM screen_records WHERE signal_type='buy'
          AND run_date BETWEEN ? AND ? ORDER BY run_date,created_at,id""",
          ((now-timedelta(days=14)).date().isoformat(), now.date().isoformat())).fetchall()
    qualified = []
    for stored in rows:
        row = dict(stored)
        row["extra"] = obj(row.get("extra"))
        if obj(row["extra"].get("ai_selection")) and expiry(row["run_date"], settings()["observation_sessions"]) >= now.date().isoformat():
            qualified.append(row)
    ingest(store, qualified, now)
    return len(qualified)


def load_setups(store, now=None, include_codes=()):
    now = now or datetime.now()
    ensure_tables(store)
    with store._get_conn() as conn:
        rows = conn.execute("SELECT * FROM opportunity_setups ORDER BY last_seen DESC,code").fetchall()
    return {r["code"]: {**dict(r), "source": obj(r["source"])} for r in rows
            if r["expires_on"] >= now.date().isoformat() or r["code"] in include_codes}


def load_plans(store, mode):
    ensure_tables(store)
    with store._get_conn() as conn:
        result = {r["code"]: {**dict(r), "plan": obj(r["plan"])} for r in
                  conn.execute("SELECT * FROM opportunity_plans WHERE mode=?", (mode,))}
    for row in result.values():
        row["next_review_at"] = next_review_at(row["reviewed_at"], row["plan"].get("review_after_minutes", 30))
    return result


def next_review_at(reviewed_at, minutes):
    """Add trading minutes, then align to a time the event worker can start."""
    current = datetime.fromisoformat(reviewed_at)
    remaining = timedelta(minutes=minutes)
    for _ in range(370):
        if market_day(current).is_open:
            for h1, m1, h2, m2 in ((9,30,11,30), (13,0,15,0)):
                start = current.replace(hour=h1, minute=m1, second=0, microsecond=0)
                end = current.replace(hour=h2, minute=m2, second=0, microsecond=0)
                if current >= end:
                    continue
                current = max(current, start)
                if remaining < end-current:
                    due = current + remaining
                    # Existing workers stop opening event runs five minutes
                    # before session end, allowing models time to finish.
                    if due < end-timedelta(minutes=5):
                        return stamp(due)
                    current, remaining = end, timedelta(0)
                else:
                    remaining -= end-current
                    current = end
        current = (current+timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError("no review trading session within one year")


def candidate_scope(store, mode, current, holding_codes, focus_codes=None, now=None):
    now = now or datetime.now()
    ingest(store, current, now)
    setups = load_setups(store, now, holding_codes)
    plans = load_plans(store, mode)
    from data.stock_research import contexts
    research = contexts(store, list(setups), setups, now)
    current_by_code = {r["code"]: r for r in current}
    from data.live_manual_account import is_live_buy_allowed

    eligible = []
    for code, setup in setups.items():
        plan = plans.get(code, {})
        if code in holding_codes or (mode == "live" and not is_live_buy_allowed(code)):
            continue
        if plan.get("setup_id") == setup["setup_id"] and plan.get("plan", {}).get("state") == "invalid" and setup["last_seen"] <= plan.get("reviewed_at", "")[:10]:
            continue
        if focus_codes is not None and code not in focus_codes:
            continue
        if focus_codes is None and plan.get("reviewed_at") and plan.get("setup_id") == setup["setup_id"]:
            if stamp(now) < plan["next_review_at"]:
                continue
        eligible.append(code)
    # Least recently researched first; never discard an overflow candidate.
    eligible.sort(key=lambda c: (plans.get(c, {}).get("reviewed_at", ""),
                                int(obj(obj(setups[c]["source"].get("extra")).get("ai_selection")).get("rank") or 999), c))
    selected = []
    deep_count = 0
    for code in eligible:
        deep = research[code]["status"] != "ready"
        if focus_codes is None and deep and deep_count >= settings()["deep_research_candidate_batch_size"]:
            continue
        selected.append(code)
        deep_count += int(deep)
        if focus_codes is None and len(selected) >= settings()["candidate_batch_size"]:
            break
    result = []
    for code in selected:
        source = json.loads(encode(current_by_code.get(code) or setups[code]["source"]))
        extra = source.setdefault("extra", {})
        selector = extra.setdefault("selector", {})
        if code not in current_by_code:
            selector.update(buy_eligible=False, setup_stage="observation")
            selector.setdefault("entry_route", obj(extra.get("ai_selection")).get("entry_route"))
        extra["opportunity"] = {k:v for k,v in setups[code].items() if k != "source"}
        extra["opportunity"]["requires_requalification"] = code not in current_by_code
        result.append(source)
    return result, setups, plans, len(eligible)


def validate_plan(raw):
    if not isinstance(raw, dict):
        raise ValueError("watch_plan must be an object")
    if raw.get("state") not in {"watch", "account_blocked", "data_pending", "invalid", "holding"}:
        raise ValueError("watch_plan.state invalid")
    plan = {"state": raw["state"], "thesis": str(raw.get("thesis") or "")[:300],
            "wait_reason": str(raw.get("wait_reason") or "")[:240],
            "invalidation_reason": str(raw.get("invalidation_reason") or "")[:240],
            "requalification_reason": str(raw.get("requalification_reason") or "")[:300],
            "requalified": raw.get("requalified") is True}
    minutes = raw.get("review_after_minutes", 30)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 15 <= minutes <= 240:
        raise ValueError("watch_plan.review_after_minutes must be an integer between 15 and 240")
    plan["review_after_minutes"] = minutes
    for field in ("review_above", "review_below", "invalidation_below"):
        value = raw.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value <= 0):
            raise ValueError(f"watch_plan.{field} must be a positive finite price or null")
        plan[field] = value
    if plan["state"] == "invalid" and not plan["invalidation_reason"]:
        raise ValueError("invalid opportunity requires invalidation_reason")
    if not plan["thesis"] or not plan["wait_reason"]:
        raise ValueError("watch_plan requires thesis and wait_reason")
    if plan["requalified"] and not plan["requalification_reason"]:
        raise ValueError("requalification requires current evidence reason")
    return plan


def record_decision(store, mode, decision, context, result, now=None):
    """Record validated decisions AFTER execution, without pretending intent=fill."""
    now = now or datetime.now()
    ensure_tables(store)
    setups = load_setups(store, now, [r["code"] for r in context.get("positions", [])])
    rows = decision.get("signals" if mode == "simulated" else "decisions", [])
    with store._get_conn() as conn:
        for row in rows:
            plan = row.get("watch_plan")
            if not plan:
                continue
            code = row["code"]
            setup_id = setups.get(code, {}).get("setup_id", f"holding:{code}")
            version = hashlib.sha256(encode({k:plan[k] for k in ("state","review_above","review_below","invalidation_below")}).encode()).hexdigest()[:16]
            conn.execute("""INSERT INTO opportunity_plans(mode,code,setup_id,plan,version,reviewed_at,last_action)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(mode,code) DO UPDATE SET
              setup_id=excluded.setup_id,plan=excluded.plan,version=excluded.version,
              reviewed_at=excluded.reviewed_at,last_action=excluded.last_action""",
              (mode,code,setup_id,encode(plan),version,context["as_of"],row["action"]))
            conn.execute("INSERT INTO opportunity_audit(mode,code,kind,payload,created_at) VALUES(?,?,?,?,?)",
                         (mode,code,"decision_and_execution",encode({"as_of":context["as_of"],"decision":row,"execution":result}),stamp(now)))
            # Complete only pre-existing unclaimed events; a separately claimed
            # event batch is acknowledged by its worker after successful return.
            conn.execute("UPDATE opportunity_events SET status='done',finished_at=? WHERE mode=? AND code=? AND status='pending' AND created_at<=?",
                         (stamp(now),mode,code,context["as_of"]))


def read_cache(store, kind, codes, max_age, now=None):
    now = now or datetime.now()
    ensure_tables(store)
    with store._get_conn() as conn:
        rows = conn.execute("SELECT * FROM opportunity_cache WHERE kind=? AND fetched_at>=?",
                            (kind,stamp(now-timedelta(seconds=max_age)))).fetchall()
    return {r["code"]:obj(r["payload"]) for r in rows if r["code"] in codes}


def write_cache(store, kind, data, now=None):
    ensure_tables(store)
    with store._get_conn() as conn:
        conn.executemany("INSERT OR REPLACE INTO opportunity_cache VALUES(?,?,?,?)",
                         [(kind,c,encode(v),stamp(now)) for c,v in data.items() if not v.get("error")])


def queue_event(conn, mode, code, setup_id, kind, version, payload, now):
    dedup = f"{now.date()}:{mode}:{code}:{setup_id}:{kind}:{version}"
    conn.execute("""INSERT INTO opportunity_events(mode,code,setup_id,kind,dedup,payload,created_at)
      VALUES(?,?,?,?,?,?,?) ON CONFLICT(dedup) DO UPDATE SET
      status='pending',payload=excluded.payload,created_at=excluded.created_at
      WHERE opportunity_events.status='expired'""",
      (mode,code,setup_id,kind,dedup,encode(payload),stamp(now)))


def observe(store, mode, quotes, positions, now=None):
    now = now or datetime.now()
    setups = load_setups(store, now, positions)
    plans = load_plans(store, mode)
    old_quotes = read_cache(store, "monitor_quote", list(quotes), 3600, now)
    prior_account = read_cache(store, "monitor_account", [mode], 86400, now).get(mode)
    from data.stock_research import contexts
    research = contexts(store, list(set(setups)|set(positions)), setups, now)
    with store._get_conn() as conn:
        from data.live_manual_account import is_live_buy_allowed
        for code in set(setups) | set(positions):
            if code not in positions and mode == "live" and not is_live_buy_allowed(code):
                continue
            q = quotes.get(code, {})
            if not valid_quote(q, now, settings()["quote_max_age_seconds"]):
                continue
            stored = plans.get(code, {})
            setup = setups.get(code, {})
            setup_id = setup.get("setup_id", f"holding:{code}")
            plan = stored.get("plan", {}) if stored.get("setup_id") == setup_id else {}
            if plan.get("state") == "invalid" and code not in positions:
                if setup.get("last_seen", "") <= stored.get("reviewed_at", "")[:10]:
                    continue
                plan = {}
            price = float(q["price"])
            payload = {"quote": q, "previous_quote": old_quotes.get(code), "plan": plan}
            version = stored.get("version", "initial")
            due = stored.get("next_review_at")
            if plan and due and stamp(now) >= due:
                kind = "holding_review_due" if code in positions else "review_due"
                queue_event(conn, mode, code, setup_id, kind, stored["reviewed_at"],
                            {**payload, "due_at": due, "reviewed_at": stored["reviewed_at"]}, now)
            if not plan:
                kind = "new_opportunity" if setup.get("first_seen") == str(now.date()) else "research_due"
                queue_event(conn, mode, code, setup_id, kind, version, payload, now)
            if research[code].get("revision") and "company_news_or_setup_changed" in research[code]["reasons"]:
                queue_event(conn,mode,code,setup_id,"research_changed",research[code]["facts_version"],
                            {"research_revision":research[code]["revision"],"reasons":research[code]["reasons"]},now)
            for field, kind, direction in (("review_above","price_recovery",1),
                                           ("review_below","price_pullback",-1),
                                           ("invalidation_below","structure_risk",-1)):
                level = plan.get(field)
                if level and (price >= level if direction > 0 else price <= level):
                    queue_event(conn,mode,code,setup_id,kind,version,payload,now)
            if plan and stored.get("reviewed_at"):
                news = conn.execute("""SELECT title,content,risk_level,score,created_at,url FROM news_events
                    WHERE code=? AND created_at>? AND created_at<=?
                    AND (score>=2 OR risk_level IN ('high','高')) ORDER BY created_at DESC LIMIT 20""",
                    (code,stored["reviewed_at"],stamp(now))).fetchall()
                for item in news:
                    from data.news_evidence import is_aggregate_news
                    if is_aggregate_news(item):
                        continue
                    digest=hashlib.sha256((str(item["title"])+str(item["content"])).encode()).hexdigest()[:16]
                    kind="logic_risk" if item["risk_level"] in {"high","高"} else "news_changed"
                    queue_event(conn,mode,code,setup_id,kind,digest,{"title":item["title"],"created_at":item["created_at"]},now)
            # Retain a generic holding warning even before the model supplies a
            # price plan. This is an alert threshold, never an automatic sell.
            old = old_quotes.get(code, {})
            if code in positions and valid_quote(old, now, 600) and price / float(old["price"]) - 1 <= -.02:
                queue_event(conn,mode,code,setup_id,"holding_fast_drop",version,payload,now)
            volume = int(positions.get(code, 0))
            previous_volume = int(stored.get("position_volume", 0))
            if stored and volume != previous_volume:
                queue_event(conn,mode,code,setup_id,"position_changed",str(volume),payload,now)
                conn.execute("UPDATE opportunity_plans SET position_volume=? WHERE mode=? AND code=?", (volume,mode,code))
        # Budget changes can release an account-blocked opportunity without a
        # price crossing. Re-evaluate at the scheduled fallback as well.
        signature = hashlib.sha256(encode(positions).encode()).hexdigest()[:16]
        if not prior_account or prior_account.get("signature") == signature:
            return
        for code, stored in plans.items():
            if code in setups and stored["plan"].get("state") == "account_blocked" and valid_quote(quotes.get(code,{}),now):
                queue_event(conn,mode,code,setups[code]["setup_id"],"account_review",signature,{"positions":positions},now)


def processing_evidence(store, mode):
    """Return the claimed trigger facts for the current account worker."""
    ensure_tables(store)
    result = {}
    with store._get_conn() as conn:
        for row in conn.execute("SELECT code,id,kind,payload,created_at FROM opportunity_events WHERE mode=? AND status='processing' ORDER BY id", (mode,)):
            result.setdefault(row["code"], []).append({
                "id":row["id"], "kind":row["kind"], "created_at":row["created_at"],
                "facts":obj(row["payload"]),
            })
    return result


def claim_events(store, mode, now=None):
    now = now or datetime.now()
    ensure_tables(store)
    cfg = settings()
    with store._get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Uncertain interrupted execution is never automatically replayed.
        conn.execute("UPDATE opportunity_events SET status='needs_review',error='worker lease expired' WHERE status='processing' AND substr(batch_id,instr(batch_id,':')+1)<?",
                     (stamp(now-timedelta(minutes=45)),))
        conn.execute("UPDATE opportunity_events SET status='expired',finished_at=? WHERE status='pending' AND kind IN ('price_recovery','price_pullback') AND created_at<?",
                     (stamp(now),stamp(now-timedelta(minutes=cfg["event_max_age_minutes"]))))
        conn.execute("UPDATE opportunity_events SET status='expired',finished_at=? WHERE status='pending' AND created_at<?",
                     (stamp(now),str(now.date())))
        # A slow completed decision can supersede a timer queued during the
        # model call. Discard that old timer instead of immediately re-running.
        for row in conn.execute("SELECT id,code,payload FROM opportunity_events WHERE mode=? AND status='pending' AND kind IN ('review_due','holding_review_due')", (mode,)).fetchall():
            plan = conn.execute("SELECT reviewed_at FROM opportunity_plans WHERE mode=? AND code=?", (mode,row["code"])).fetchone()
            if not plan or plan["reviewed_at"] != obj(row["payload"]).get("reviewed_at"):
                conn.execute("UPDATE opportunity_events SET status='expired',finished_at=?,error='review superseded' WHERE id=?", (stamp(now),row["id"]))
        batches = conn.execute("""SELECT batch_id,
            MAX(kind IN ('structure_risk','holding_fast_drop','logic_risk')) risk,
            MAX(kind IN ('review_due','holding_review_due')) timed
            FROM opportunity_events WHERE mode=? AND batch_id!=''
            AND substr(batch_id,instr(batch_id,':')+1)>=? GROUP BY batch_id""", (mode,str(now.date()))).fetchall()
        ordinary = [r for r in batches if not r["risk"] and not r["timed"]]
        timed = [r for r in batches if not r["risk"] and r["timed"]]
        def cooling(batches, minutes):
            return bool(batches and max(r["batch_id"].split(":",1)[1] for r in batches) > stamp(now-timedelta(minutes=minutes)))
        exhausted = len(ordinary) >= cfg["max_event_runs_per_mode_per_day"]
        ordinary_cooling = cooling(ordinary, cfg["event_cooldown_minutes"])
        review_allowed = not cooling(timed, cfg["review_cooldown_minutes"])
        rows = conn.execute("SELECT * FROM opportunity_events WHERE mode=? AND status='pending' ORDER BY CASE WHEN kind IN ('structure_risk','holding_fast_drop','logic_risk') THEN 0 WHEN kind='holding_review_due' THEN 1 WHEN kind='new_opportunity' THEN 2 WHEN kind='review_due' THEN 3 ELSE 4 END,created_at,id",(mode,)).fetchall()
        rows = [r for r in rows if
                r["kind"] in {"structure_risk","holding_fast_drop","logic_risk"} or
                (review_allowed if r["kind"] in {"review_due","holding_review_due"} else
                 not exhausted and (not ordinary_cooling or r["kind"] == "new_opportunity"))]
        # Expire last-session observations. Fresh observations can generate
        # today's event; old events never authorize a fresh account action.
        rows = [r for r in rows if r["created_at"][:10] == str(now.date())]
        # Every decision already reviews all actual holdings. Coalesce their
        # timers into that single account review; they must not consume all
        # candidate slots and starve due candidates on every 15-minute tick.
        holding_due = list(dict.fromkeys(r["code"] for r in rows if r["kind"] == "holding_review_due"))
        codes = holding_due + list(dict.fromkeys(r["code"] for r in rows if r["code"] not in holding_due))[:cfg["event_batch_size"]]
        selected = [dict(r) for r in rows if r["code"] in codes]
        if not selected:
            return []
        batch_id = f"{mode}:{stamp(now)}"
        for row in selected:
            conn.execute("UPDATE opportunity_events SET status='processing',batch_id=? WHERE id=?", (batch_id,row["id"]))
        return selected


def finish_events(store, rows, success, error=""):
    with store._get_conn() as conn:
        conn.executemany("UPDATE opportunity_events SET status=?,finished_at=?,error=? WHERE id=? AND status='processing'",
                         [("done" if success else "needs_review",stamp(),error[:500],r["id"]) for r in rows])
