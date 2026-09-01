#!/usr/bin/env python3
"""同步 BaoStock 盈利能力财务因子到 financial_factors 表。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.services.finance_service import FinanceService
from data.store.sqlite_store import StockStore


def default_period() -> tuple[str, int]:
    now = datetime.now()
    # 简单取最近已披露季度；后续可按披露日历精细化
    if now.month <= 4:
        return str(now.year - 1), 4
    if now.month <= 8:
        return str(now.year), 1
    if now.month <= 10:
        return str(now.year), 2
    return str(now.year), 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", default="", help="逗号分隔股票代码；默认当前持仓")
    parser.add_argument("--year", default="")
    parser.add_argument("--quarter", type=int, default=0)
    args = parser.parse_args()

    year, quarter = (args.year, args.quarter) if args.year and args.quarter else default_period()
    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        store = StockStore()
        conn = store._get_conn()
        try:
            codes = [r["code"] for r in conn.execute("SELECT code FROM portfolio WHERE volume>0")]
        finally:
            conn.close()

    try:
        result = FinanceService().sync_profit_factors(codes, year=year, quarter=quarter)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        return 0
    except ModuleNotFoundError:
        print(json.dumps({
            "ok": False,
            "error": "baostock_not_installed",
            "message": "当前 Python 环境未安装 baostock，请安装后重试：python3 -m pip install baostock",
        }, ensure_ascii=False, indent=2))
        return 2
    except Exception as e:
        print(json.dumps({"ok": False, "error": type(e).__name__, "message": str(e)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
