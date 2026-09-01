#!/usr/bin/env python3
"""Atomically switch scheduled stock operations between Hermes and Codex Agent.

Install preserves unrelated crontab entries, snapshots Hermes jobs and the Web
unit, pauses only the stock profile jobs, then disables only the stock Hermes
gateway.  Rollback restores the exact snapshot recorded by install.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HERMES_HOME = Path(
    os.environ.get("STOCK_HERMES_HOME", Path.home() / ".hermes/profiles/stock")
).expanduser()
HERMES = Path(
    os.environ.get("HERMES_BIN", shutil.which("hermes") or Path.home() / ".local/bin/hermes")
).expanduser()
WEB_UNIT = Path.home() / ".config/systemd/user/stock-web.service"
BACKUP_ROOT = ROOT / "data/runtime/scheduler-backups"
ACTIVE_POINTER = BACKUP_ROOT / "active-backup"
BEGIN = "# BEGIN STOCK_AGENT_RUNTIME"
END = "# END STOCK_AGENT_RUNTIME"


def _run(command: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, text=True, capture_output=True, check=False, env=env)
    if check and completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    return completed


def _crontab() -> str:
    result = _run(["crontab", "-l"], check=False)
    return result.stdout if result.returncode == 0 else ""


def _without_managed_block(value: str) -> str:
    out = []
    inside = False
    for line in value.splitlines():
        if line.strip() == BEGIN:
            inside = True
            continue
        if line.strip() == END:
            inside = False
            continue
        if not inside:
            out.append(line)
    return "\n".join(out).strip() + ("\n" if out else "")


def _install_crontab(value: str) -> None:
    completed = subprocess.run(["crontab", "-"], input=value, text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "failed to install crontab")


def _hermes_env() -> dict[str, str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(HERMES_HOME)
    return env


def _preflight() -> None:
    from config.runtime_paths import configurable_path
    from data.feishu_client import load_feishu_settings

    load_feishu_settings(configurable_path("STOCK_RUNTIME_CONFIG", "config/runtime.local.json"))
    _run(["codex", "login", "status"])
    _run(["lark-cli", "--profile", "stock", "auth", "status"])
    _run([
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "scripts/stock_candidate_promotion_mcp.py"), "--check",
    ])
    for path in (
        ROOT / "scripts/stock_scheduled_job.sh",
        ROOT / "scripts/stock_agent_selection_cycle.sh",
        ROOT / "scripts/stock_agent_trading_cycle.sh",
        ROOT / "scripts/stock_agent_candidate_promotion.sh",
    ):
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"scheduler entrypoint is not executable: {path}")


def install() -> dict[str, object]:
    if ACTIVE_POINTER.exists():
        raise RuntimeError(f"agent runtime already has an active backup: {ACTIVE_POINTER.read_text().strip()}")
    _preflight()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_ROOT / stamp
    backup.mkdir(parents=True, exist_ok=False)
    old_crontab = _crontab()
    (backup / "crontab.txt").write_text(old_crontab, encoding="utf-8")
    jobs_path = HERMES_HOME / "cron/jobs.json"
    jobs = json.loads(jobs_path.read_text(encoding="utf-8")) if jobs_path.exists() else {"jobs": []}
    (backup / "hermes-jobs.json").write_text(
        json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    if WEB_UNIT.exists():
        shutil.copy2(WEB_UNIT, backup / "stock-web.service")
    gateway_active = _run(
        ["systemctl", "--user", "is-active", "hermes-gateway-stock.service"], check=False,
    ).returncode == 0
    gateway_enabled = _run(
        ["systemctl", "--user", "is-enabled", "hermes-gateway-stock.service"], check=False,
    ).returncode == 0
    active_job_ids = [str(row["id"]) for row in jobs.get("jobs", []) if row.get("enabled")]
    manifest = {
        "created_at": datetime.now().isoformat(),
        "active_job_ids": active_job_ids,
        "gateway_active": gateway_active,
        "gateway_enabled": gateway_enabled,
    }
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )

    # Complete all recoverable snapshots before changing scheduler state.
    ACTIVE_POINTER.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_POINTER.write_text(str(backup), encoding="utf-8")
    for job_id in active_job_ids:
        _run([str(HERMES), "cron", "pause", job_id], env=_hermes_env())
    managed = (
        (ROOT / "config/stock-agent.crontab")
        .read_text(encoding="utf-8")
        .replace("{{STOCK_ROOT}}", str(ROOT))
        .strip()
        + "\n"
    )
    if "{{" in managed or "}}" in managed:
        raise RuntimeError("unresolved placeholder in config/stock-agent.crontab")
    _install_crontab(_without_managed_block(old_crontab) + managed)

    old_keyring = HERMES_HOME / "secrets/iwencai_api_keys"
    new_keyring = Path.home() / ".config/stock/iwencai_api_keys"
    if old_keyring.exists() and not new_keyring.exists():
        new_keyring.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_keyring, new_keyring)
        new_keyring.chmod(0o600)

    WEB_UNIT.parent.mkdir(parents=True, exist_ok=True)
    WEB_UNIT.write_text(
        "[Unit]\n"
        "Description=Stock Web Panel\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={ROOT}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        f"ExecStart={ROOT}/.venv/bin/python "
        "-m uvicorn web.app:app --host 127.0.0.1 --port 8899\n"
        "Restart=on-failure\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "restart", "stock-web.service"])
    _run(["systemctl", "--user", "disable", "--now", "hermes-gateway-stock.service"])
    return {"status": "installed", "backup": str(backup), "paused_jobs": len(active_job_ids)}


def rollback() -> dict[str, object]:
    if not ACTIVE_POINTER.exists():
        raise RuntimeError("no active agent-runtime backup exists")
    backup = Path(ACTIVE_POINTER.read_text(encoding="utf-8").strip())
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    _install_crontab((backup / "crontab.txt").read_text(encoding="utf-8"))
    if (backup / "stock-web.service").exists():
        shutil.copy2(backup / "stock-web.service", WEB_UNIT)
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "restart", "stock-web.service"])
    if manifest.get("gateway_enabled"):
        _run(["systemctl", "--user", "enable", "hermes-gateway-stock.service"])
    if manifest.get("gateway_active"):
        _run(["systemctl", "--user", "start", "hermes-gateway-stock.service"])
    for job_id in manifest.get("active_job_ids", []):
        _run([str(HERMES), "cron", "resume", str(job_id)], env=_hermes_env())
    ACTIVE_POINTER.unlink()
    return {"status": "rolled_back", "backup": str(backup)}


def status() -> dict[str, object]:
    value = ACTIVE_POINTER.read_text(encoding="utf-8").strip() if ACTIVE_POINTER.exists() else ""
    return {
        "runtime": "codex-agent" if value else "hermes",
        "backup": value,
        "managed_crontab": BEGIN in _crontab(),
        "hermes_gateway_active": _run(
            ["systemctl", "--user", "is-active", "hermes-gateway-stock.service"], check=False,
        ).returncode == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "rollback", "status", "preflight"))
    args = parser.parse_args()
    try:
        if args.action == "install":
            result = install()
        elif args.action == "rollback":
            result = rollback()
        elif args.action == "preflight":
            _preflight()
            result = {"status": "ready"}
        else:
            result = status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
