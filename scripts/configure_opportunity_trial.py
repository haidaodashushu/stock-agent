#!/usr/bin/env python3
"""Install or restore the existing STOCK_AGENT_RUNTIME cron block.

Uses the production ledger and host configuration with the isolated checkout.
No account actions or messages are performed. The original cron is preserved.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def configure(production: Path,rollback=False):
    production=production.resolve()
    backup=ROOT/".run/pretrial.crontab"
    state=ROOT/".run/opportunity-deployment.json"
    config=ROOT/"config/opportunity_trial.local.json"
    current=subprocess.check_output(["crontab","-l"],text=True)
    if rollback:
        if not backup.exists() or not state.exists():
            raise RuntimeError("no deployment backup")
        installed=json.loads(state.read_text())["installed_crontab"]
        if current!=installed:
            raise RuntimeError("cron changed since install; preserve unrelated edits and inspect manually")
        payload=json.loads(config.read_text());payload["enabled"]=False
        config.write_text(json.dumps(payload,indent=2)+"\n")
        subprocess.run(["crontab","-"],input=backup.read_text(),text=True,check=True)
        print("restored pretrial cron; trial disabled")
        return
    if ROOT==production:
        raise ValueError("trial requires an isolated checkout")
    if backup.exists():
        raise RuntimeError("deployment already initialized; do not overwrite original cron")
    if not (production/"data/stock_data.db").is_file() or not (production/".venv/bin/python").exists():
        raise RuntimeError("production ledger or environment missing")
    begin,end="# BEGIN STOCK_AGENT_RUNTIME","# END STOCK_AGENT_RUNTIME"
    if current.count(begin)!=1 or current.count(end)!=1:
        raise RuntimeError("expected exactly one existing runtime cron block")
    # All pre-existing tasks keep their schedule and env configuration; only
    # the checkout changes. The two account workers never run old and new cron
    # entries concurrently after installation.
    start=current.index(begin);finish=current.index(end)+len(end)
    block=current[start:finish].replace(str(production)+"/scripts/stock_scheduled_job.sh",str(ROOT)+"/scripts/stock_scheduled_job.sh")
    additions="\n".join([
      f"*/3 9-11,13-14 * * 1-5 {ROOT}/scripts/stock_scheduled_job.sh opportunity-monitor >> {ROOT}/logs/opportunity_monitor.log 2>&1",
      f"* 9-11,13-14 * * 1-5 {ROOT}/scripts/stock_scheduled_job.sh opportunity-simulated >> {ROOT}/logs/opportunity_trading.log 2>&1",
      f"* 9-11,13-14 * * 1-5 {ROOT}/scripts/stock_scheduled_job.sh opportunity-live >> {ROOT}/logs/opportunity_trading.log 2>&1",
    ])
    block=block.replace(end,additions+"\n"+end)
    installed=current[:start]+block+current[finish:]
    for directory in (ROOT/".run",ROOT/"logs"):
        directory.mkdir(parents=True,exist_ok=True)
    for source in (production/"config").glob("*.local.json"):
        if source.name == config.name:
            continue
        target=ROOT/"config"/source.name
        if not target.exists():
            target.symlink_to(source)
    venv=ROOT/".venv"
    if not venv.exists():
        venv.symlink_to(production/".venv",target_is_directory=True)
    env={"STOCK_PYTHON":str(production/".venv/bin/python"),
         "STOCK_DB_PATH":str(production/"data/stock_data.db"),
         "STOCK_OPPORTUNITY_CONFIG":str(config)}
    (ROOT/"config/trial_runtime.local.env").write_text("".join(f"export {k}={shlex.quote(v)}\n" for k,v in env.items()))
    payload=json.loads((ROOT/"config/opportunity_trial.json").read_text());payload["enabled"]=True
    config.write_text(json.dumps(payload,indent=2)+"\n")
    backup.write_text(current)
    state.write_text(json.dumps({"production_root":str(production),"trial_root":str(ROOT),"installed_crontab":installed},indent=2)+"\n")
    subprocess.run(["crontab","-"],input=installed,text=True,check=True)
    print("installed trial cron; original cron saved at",backup)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--production-root",required=True,type=Path)
    parser.add_argument("--rollback",action="store_true")
    args=parser.parse_args()
    configure(args.production_root,args.rollback)


if __name__=="__main__":
    main()
