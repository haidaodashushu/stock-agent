"""Versioned stock research shared by accounts; account plans stay separate."""
from __future__ import annotations

import hashlib
from datetime import datetime

from data import opportunity_trial as trial


def digest(value):
    return hashlib.sha256(trial.encode(value).encode()).hexdigest()[:24]


def ensure_tables(store):
    with store._get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS stock_research_profiles (
          code TEXT PRIMARY KEY, revision TEXT NOT NULL, facts_version TEXT NOT NULL,
          profile TEXT NOT NULL, evidence_as_of TEXT NOT NULL,
          expires_on TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stock_research_history (
          code TEXT NOT NULL, revision TEXT NOT NULL, facts_version TEXT NOT NULL,
          profile TEXT NOT NULL, evidence_as_of TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(code,revision));
        """)


def contexts(store, codes, setups, now=None):
    """Cheap local change detection. Prices/daily refreshes do not expire research.

    Full daily structure stays in current trading evidence. Material company
    inputs and significant ingested news invalidate the stored interpretation.
    """
    now = now or datetime.now()
    ensure_tables(store)
    result = {}
    with store._get_conn() as conn:
        for code in codes:
            prior = conn.execute("SELECT * FROM stock_research_profiles WHERE code=?", (code,)).fetchone()
            if prior and prior["evidence_as_of"] > trial.stamp(now):
                prior = None
            financial = conn.execute("SELECT * FROM financial_factors WHERE code=? AND updated_at<=? ORDER BY period DESC LIMIT 1", (code,trial.stamp(now))).fetchone()
            company = {k:v for k,v in dict(financial or {}).items() if k not in {"id","updated_at"}}
            # A newly ingested older publication still counts as new knowledge.
            news = conn.execute("""SELECT title,content,risk_level,score,publish_at FROM news_events
              WHERE code=? AND created_at<=? AND (score>=2 OR risk_level IN ('high','高'))
              ORDER BY created_at DESC,id DESC LIMIT 8""", (code,trial.stamp(now))).fetchall()
            source = setups.get(code, {})
            extra = trial.obj(trial.obj(source.get("source")).get("extra"))
            route = trial.obj(extra.get("selector")).get("entry_route") or trial.obj(extra.get("ai_selection")).get("entry_route")
            version = digest({"company":company,"news":[dict(r) for r in news],
                              "setup_id":source.get("setup_id"),"route":route})
            reasons = []
            if not prior:
                reasons.append("no_research")
            else:
                if prior["facts_version"] != version:
                    reasons.append("company_news_or_setup_changed")
                if prior["expires_on"] < str(now.date()):
                    reasons.append("research_expired")
                if prior["status"] != "ready":
                    reasons.append("research_data_pending")
                if trial.settings().get("decision_assessment") and not trial.obj(prior["profile"]).get("quality"):
                    reasons.append("research_quality_unrated")
            result[code] = {
                "status":"refresh_required" if reasons else "ready",
                "reasons":reasons,"facts_version":version,
                "revision":prior["revision"] if prior else None,
                "evidence_as_of":prior["evidence_as_of"] if prior else None,
                "expires_on":prior["expires_on"] if prior else None,
                "profile":trial.obj(prior["profile"]) if prior else None,
                "financial_period":company.get("period"),
            }
    return result


def validate_update(raw, research):
    if not isinstance(raw, dict) or raw.get("facts_version") != research["facts_version"]:
        raise ValueError("research_update must reference this snapshot's research.facts_version")
    if raw.get("status") not in {"ready", "data_pending"}:
        raise ValueError("research_update.status must be ready or data_pending")
    result = {"facts_version":raw["facts_version"],"status":raw["status"]}
    for field in ("thesis","company_view","trend_view","risks","refresh_condition"):
        value = raw.get(field)
        if not isinstance(value,str) or not value.strip() or len(value)>300:
            raise ValueError(f"research_update.{field} requires 1..300 characters")
        result[field] = value.strip()
    if raw.get("quality") is not None:
        from data.trading_assessment import grade
        result["quality"] = grade(raw["quality"],"research")
    return result


def record_updates(store, decision, context):
    """Persist only validated research, with optimistic version protection."""
    ensure_tables(store)
    facts = {r["code"]:r for r in context["positions"]+context["candidates"]}
    rows = decision.get("signals",decision.get("decisions",[]))
    with store._get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            update = row.get("research_update")
            if not update:
                continue
            research = facts[row["code"]].get("research") or {}
            update = validate_update(update,research)
            prior = conn.execute("SELECT revision FROM stock_research_profiles WHERE code=?",(row["code"],)).fetchone()
            if (prior[0] if prior else None) != research.get("revision"):
                # Another account completed research against the shared version.
                # Keep its result; do not overwrite it with a stale base version.
                continue
            profile = {k:v for k,v in update.items() if k not in {"facts_version","status"}}
            revision = digest({"profile":profile,"facts":update["facts_version"],"as_of":context["as_of"],"status":update["status"]})
            expires = trial.expiry(context["as_of"][:10],trial.settings().get("research_valid_sessions",5))
            conn.execute("INSERT OR REPLACE INTO stock_research_profiles VALUES(?,?,?,?,?,?,?)",
                         (row["code"],revision,update["facts_version"],trial.encode(profile),context["as_of"],expires,update["status"]))
            conn.execute("INSERT OR IGNORE INTO stock_research_history VALUES(?,?,?,?,?,?)",
                         (row["code"],revision,update["facts_version"],trial.encode(profile),context["as_of"],trial.stamp()))


def daily_technical(store, code, compute):
    """Reuse computed daily facts until the verified source window changes."""
    with store._get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='adjusted_daily_windows'").fetchone()
        row = conn.execute("SELECT * FROM adjusted_daily_windows WHERE code=?",(code,)).fetchone() if exists else None
    if not row:
        return compute(code,store)
    version = digest(dict(row))
    cached = trial.read_cache(store,"daily_technical",[code],7*86400).get(code,{})
    if cached.get("source_version") == version:
        return cached["facts"]
    facts = compute(code,store)
    if not facts.get("error"):
        trial.write_cache(store,"daily_technical",{code:{"source_version":version,"facts":facts}})
    return facts


def record_observations(store, mode, context):
    facts = {}
    for row in context["positions"]+context["candidates"]:
        facts[row["code"]] = {
            "as_of":context["as_of"],"quote":row.get("quote") or {},
            "daily_date":(row.get("technical") or {}).get("date"),
            "research_facts_version":(row.get("research") or {}).get("facts_version"),
        }
    trial.write_cache(store,f"decision_observation_{mode}",facts)


def annotate_changes(store, mode, items, now=None):
    previous = trial.read_cache(store,f"decision_observation_{mode}",[r["code"] for r in items],7*86400,now)
    for item in items:
        before = previous.get(item["code"])
        changes = {"baseline":"last_completed_account_decision","available":bool(before)}
        if before:
            old_price = (before.get("quote") or {}).get("price")
            price = (item.get("quote") or {}).get("price")
            from data.trading_data_quality import valid_quote,source_datetime
            baseline_time = source_datetime(before.get("as_of"))
            baseline_valid = baseline_time and valid_quote(before.get("quote") or {},baseline_time)
            current_valid = valid_quote(item.get("quote") or {},now or datetime.now())
            changes.update({
                "as_of":before["as_of"],
                "price_change_pct":round((float(price)/float(old_price)-1)*100,2) if baseline_valid and current_valid else None,
                "daily_updated":(item.get("technical") or {}).get("daily_date") != before.get("daily_date"),
                "research_inputs_changed":(item.get("research") or {}).get("facts_version") != before.get("research_facts_version"),
            })
        item["changes_since_last_decision"] = changes
