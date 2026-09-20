#!/usr/bin/env python3
"""Quote-only observation, with optional handoff to guarded decision workers."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import opportunity_trial as trial
from data.candidate_board import load_active_candidate_board
from data.live_manual_account import account_snapshot
from data.market_calendar import is_actionable_trading_time
from data.store.sqlite_store import StockStore
from data.trading_data_quality import valid_quote
from data.trading_state import fetch_quotes


def account_positions(store):
    with store._get_conn() as conn:
        simulated = {r["code"]:int(r["volume"]) for r in conn.execute("SELECT code,volume FROM portfolio WHERE volume>0")}
        live = account_snapshot(conn, quotes={}, expire_pending=False)
    return {"simulated":simulated,
            "live":{r["code"]:int(r["volume"]) for r in live["positions"]}}


def monitor(store, now=None, quote_fetcher=fetch_quotes):
    now = now or datetime.now()
    if not trial.enabled() or not is_actionable_trading_time(now):
        return {"status":"skipped", "reason":"disabled or market closed"}
    start = time.monotonic()
    trial.ingest(store, load_active_candidate_board(store, trade_date=str(now.date())), now)
    accounts = account_positions(store)
    holdings = set(accounts["simulated"]) | set(accounts["live"])
    setups = trial.load_setups(store, now, holdings)
    codes = sorted(holdings | set(setups))
    quotes = {c:q for c,q in trial.read_cache(store,"quote",codes,45,now).items() if valid_quote(q,now)}
    missing = [c for c in codes if c not in quotes]
    quotes.update(quote_fetcher(missing) if missing else {})
    completed = datetime.now() if now.date() == datetime.now().date() else now
    valid = {c:q for c,q in quotes.items() if valid_quote(q, completed, trial.settings()["quote_max_age_seconds"])}
    for mode, positions in accounts.items():
        trial.observe(store, mode, quotes, positions, completed)
        trial.write_cache(store,"monitor_account",{mode:{"signature":hashlib.sha256(trial.encode(positions).encode()).hexdigest()[:16]}},completed)
    trial.write_cache(store,"quote",valid,completed)
    trial.write_cache(store,"monitor_quote",valid,completed)
    payload = {"status":"ok" if len(valid)==len(codes) else "partial",
               "scope_count":len(codes), "quote_ok":len(valid),
               "quote_batches":(len(missing)+79)//80,
               "missing_codes":sorted(set(codes)-set(valid)),
               "elapsed_seconds":round(time.monotonic()-start,3)}
    with store._get_conn() as conn:
        conn.execute("INSERT INTO opportunity_monitor_runs(created_at,scope_count,quote_ok,elapsed_seconds,payload) VALUES(?,?,?,?,?)",
                     (trial.stamp(completed),len(codes),len(valid),payload["elapsed_seconds"],trial.encode(payload)))
    return payload


def summary(store):
    trial.ensure_tables(store)
    from data.stock_research import ensure_tables
    ensure_tables(store)
    with store._get_conn() as conn:
        has_financials = conn.execute("SELECT 1 FROM sqlite_master WHERE name='research_company_inputs'").fetchone()
        financials = dict(conn.execute("SELECT COUNT(*) cached,SUM(error!='') refresh_errors,MIN(fetched_at) oldest,MAX(fetched_at) newest FROM research_company_inputs").fetchone()) if has_financials else {"cached":0}
        has_assessments = conn.execute("SELECT 1 FROM sqlite_master WHERE name='trading_assessments'").fetchone()
        assessments = [dict(r) for r in conn.execute("""SELECT mode,action,research_grade,timing_grade,evidence_grade,
            portfolio_grade,COUNT(*) count FROM trading_assessments WHERE as_of>=?
            GROUP BY mode,action,research_grade,timing_grade,evidence_grade,portfolio_grade""", (str(datetime.now().date()),))] if has_assessments else []
        return {
            "config":trial.settings(),
            "financial_inputs":financials,
            "monitor":dict(conn.execute("SELECT COUNT(*) runs,SUM(scope_count) observations,SUM(quote_ok) valid,MAX(elapsed_seconds) slowest_seconds FROM opportunity_monitor_runs WHERE created_at>=?",(str(datetime.now().date()),)).fetchone()),
            "events":[dict(r) for r in conn.execute("SELECT mode,status,kind,COUNT(*) count FROM opportunity_events WHERE created_at>=? GROUP BY mode,status,kind",(str(datetime.now().date()),))],
            "recent_failures":[dict(r) for r in conn.execute("SELECT mode,code,kind,error,created_at FROM opportunity_events WHERE status='needs_review' ORDER BY id DESC LIMIT 10")],
            "plans":[dict(r) for r in conn.execute("SELECT mode,COUNT(*) count,MAX(reviewed_at) latest FROM opportunity_plans GROUP BY mode")],
            "research":[dict(r) for r in conn.execute("SELECT status,COUNT(*) count,MAX(evidence_as_of) latest FROM stock_research_profiles GROUP BY status")],
            "assessments":assessments,
        }


def wake_workers():
    """Start guarded queue consumers without blocking candidate discovery."""
    if not trial.enabled() or not is_actionable_trading_time(datetime.now()):
        return
    log_path = ROOT/"logs/opportunity_trading.log"
    log_path.parent.mkdir(parents=True,exist_ok=True)
    with log_path.open("a") as log:
        for mode in ("simulated","live"):
            subprocess.Popen([sys.executable,str(ROOT/"scripts/run_opportunity_trading.py"),"--mode",mode,"--event"],
                             cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize",action="store_true")
    parser.add_argument("--summary",action="store_true")
    parser.add_argument("--wake",action="store_true",help="wake guarded AI consumers after monitoring")
    args = parser.parse_args()
    lock_path=ROOT/".run/opportunity-monitor.lock"
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        store=StockStore()
        if args.initialize:
            value={"adopted_records":trial.bootstrap(store)}
        elif args.summary:
            value=summary(store)
        else:
            value=monitor(store)
        print(json.dumps(value,ensure_ascii=False))
        if args.wake and value.get("status") in {"ok","partial"}:
            wake_workers()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
