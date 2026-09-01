#!/usr/bin/env python3
"""按本地自选配置执行到期的监控。

由 cron 高频触发（例如每 5 分钟），脚本内部按每只股票配置的：
- enabled
- strategies
- time_windows
- interval_minutes
决定是否真正运行。
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.watchlist_config import list_items, mark_run, normalize_code  # noqa: E402
from data.market_calendar import ensure_market_open  # noqa: E402


def parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":", 1)
    return time(int(h), int(m))


def in_windows(now: datetime, windows: list[str]) -> bool:
    if not windows:
        return True
    t = now.time()
    for w in windows:
        if "-" not in w:
            continue
        a, b = w.split("-", 1)
        try:
            if parse_hhmm(a) <= t <= parse_hhmm(b):
                return True
        except Exception:
            continue
    return False


def due(item: dict, now: datetime) -> bool:
    if not item.get("enabled", True):
        return False
    if not in_windows(now, item.get("time_windows") or []):
        return False
    last = item.get("last_run_at")
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return True
    # Cron fires on wall-clock boundaries (e.g. 10:30:00, 11:30:00), while
    # last_run_at is written after the monitor finishes (e.g. 10:30:29).
    # Give one polling period a small grace so the next exact boundary is not
    # skipped just because the previous run took a few seconds.
    required = int(item.get("interval_minutes") or 60) * 60
    grace_seconds = int(item.get("due_grace_seconds") or 90)
    return (now - last_dt).total_seconds() >= max(0, required - grace_seconds)


def run_strategy(code: str, strategy: str, mode: str) -> None:
    if strategy == "washout_start":
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "update_washout_monitor.py"), code, "--mode", mode],
            cwd=str(ROOT),
            check=True,
        )
    else:
        print(f"跳过 {code}: 策略 {strategy} 尚未接入执行器")


def mode_for_time(now: datetime) -> str:
    hm = now.strftime("%H:%M")
    if hm < "11:30":
        return "open"
    if hm < "15:00":
        return "midday"
    return "close"


def main() -> int:
    now = datetime.now()
    if not ensure_market_open(now, task="自选监控-配置化轮询"):
        return 0
    mode = mode_for_time(now)
    ran = []
    skipped = []
    for item in list_items(enabled_only=True):
        code = normalize_code(item.get("code"))
        if not due(item, now):
            skipped.append(code)
            continue
        strategies = item.get("strategies") or ["washout_start"]
        for st in strategies:
            run_strategy(code, st, mode)
        mark_run(code, now)
        ran.append(code)
    print(f"自选监控 {now:%Y-%m-%d %H:%M:%S} mode={mode}")
    print("已执行:", ",".join(ran) if ran else "无")
    print("跳过:", ",".join(skipped) if skipped else "无")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
