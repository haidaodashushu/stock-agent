#!/usr/bin/env python3
"""Serialize scheduled and event-driven decisions before snapshot refresh."""
from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from data import opportunity_trial as trial
from data.market_calendar import is_actionable_trading_time
from data.store.sqlite_store import StockStore
from data.trading_state import refresh_trading_state


def run(mode, event=False):
    now=datetime.now()
    if not trial.enabled() or not is_actionable_trading_time(now):
        return 0
    if event and (not trial.settings().get("event_decisions") or (now.hour,now.minute) >= (14,55) or (now.hour==11 and now.minute>=25)):
        return 0
    lock_path=ROOT/f".run/trading-{mode}.lock"
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        store=StockStore()
        events=trial.claim_events(store,mode) if event else []
        if event and not events:
            return 0
        stage=now.strftime("%H%M")
        try:
            if event:
                os.environ["STOCK_OPPORTUNITY_FOCUS"]=",".join(dict.fromkeys(r["code"] for r in events))
            else:
                os.environ.pop("STOCK_OPPORTUNITY_FOCUS",None)
            state=refresh_trading_state(stage,mode)
            # Event evidence is attached to the persisted stock dossier; the
            # decision still refreshes and revalidates all actual holdings.
            command=[sys.executable,str(ROOT/"scripts/run_stock_agent.py"),"--task",f"trading-{mode}","--stage",stage]
            if os.environ.get("STOCK_TRADING_DRY_RUN")=="1":
                command.append("--dry-run")
            result=subprocess.run(command,cwd=ROOT,check=False)
            with store._get_conn() as conn:
                submission=conn.execute("SELECT status FROM agent_decision_submissions WHERE task='trading' AND mode=? AND as_of=?",(mode,state["as_of"])).fetchone()
            success=result.returncode==0 and submission is not None and submission["status"]=="ready"
            if events:
                trial.finish_events(store,events,success,"" if success else "agent did not complete this snapshot")
            if success and os.environ.get("STOCK_TRADING_DRY_RUN")!="1":
                subprocess.run([sys.executable,str(ROOT/"scripts/send_agent_outbox.py")],cwd=ROOT,check=False)
            return 0 if success else 1
        except Exception as exc:
            if events:
                trial.finish_events(store,events,False,str(exc))
            print(f"opportunity trading failed ({mode}): {exc}",file=sys.stderr)
            return 1


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode",required=True,choices=("simulated","live"))
    parser.add_argument("--event",action="store_true")
    args=parser.parse_args()
    return run(args.mode,args.event)


if __name__=="__main__":
    raise SystemExit(main())
