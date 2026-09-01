#!/usr/bin/env python3
"""Cron/agent preflight: exit 0 on trading day, exit 10 on market holiday/weekend."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.market_calendar import market_day  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="A股交易日守门")
    p.add_argument("--date", default=None)
    p.add_argument("--task", default="交易任务")
    args = p.parse_args()
    md = market_day(args.date)
    if md.is_open:
        print(f"交易日：{args.task} {md.date} {md.reason}")
        return 0
    print(f"休市跳过：{args.task} {md.date} {md.reason}")
    return 10


if __name__ == "__main__":
    raise SystemExit(main())
