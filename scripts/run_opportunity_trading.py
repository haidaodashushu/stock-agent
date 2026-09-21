#!/usr/bin/env python3
"""Serialize scheduled and event-driven decisions before snapshot refresh."""
from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from data import opportunity_trial as trial
from data.market_calendar import is_actionable_trading_time
from data.store.sqlite_store import StockStore
from data.trading_state import refresh_trading_state


def notify_failure(mode, as_of):
    """Preserve the scheduled trading failure alert when using the trial runner."""
    if os.environ.get("STOCK_TRADING_DRY_RUN") == "1":
        return
    label = "实盘建议" if mode == "live" else "模拟盘操盘"
    # A failure can happen after an executor has started. Do not claim that no
    # order exists, and never retry the decision just to produce a report.
    content = (f"⚠️ {as_of} {label}任务异常\n\n"
               "本轮未完成有效业务提交，请核对运行日志与建议/成交记录。"
               "系统不会自动重放旧决策。")
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt") as alert:
            alert.write(content)
            alert.flush()
            result = subprocess.run(
                [sys.executable, str(ROOT/"scripts/send_configured_message.py"),
                 "--file", alert.name, "--message-type", "text",
                 "--idempotency-key", f"agent_fail_{mode}_{as_of}"], cwd=ROOT, check=False, timeout=60,
            )
    except Exception as exc:
        print(f"trading failure alert failed ({mode}, {as_of}): {type(exc).__name__}", file=sys.stderr)
        return
    if result.returncode:
        print(f"trading failure alert failed ({mode}, {as_of})", file=sys.stderr)


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
        as_of=now.strftime("%Y-%m-%d %H:%M:%S")
        try:
            if event:
                os.environ["STOCK_OPPORTUNITY_FOCUS"]=",".join(dict.fromkeys(r["code"] for r in events))
            else:
                os.environ.pop("STOCK_OPPORTUNITY_FOCUS",None)
            state=refresh_trading_state(stage,mode)
            as_of=state["as_of"]
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
            if not success:
                notify_failure(mode, as_of)
            return 0 if success else 1
        except Exception as exc:
            if events:
                trial.finish_events(store,events,False,str(exc))
            print(f"opportunity trading failed ({mode}): {exc}",file=sys.stderr)
            notify_failure(mode, as_of)
            return 1


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode",required=True,choices=("simulated","live"))
    parser.add_argument("--event",action="store_true")
    args=parser.parse_args()
    return run(args.mode,args.event)


if __name__=="__main__":
    raise SystemExit(main())
