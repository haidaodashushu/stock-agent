#!/usr/bin/env python3
"""Synchronize stock metadata, static themes, and report database coverage."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.services.finance_service import FinanceService
from data.fifteen_five_pool import FIFTEEN_FIVE_STOCKS
from data.store.sqlite_store import StockStore


def sync_concepts(store: StockStore) -> dict:
    concept_stocks: dict[str, list[str]] = {}
    for code, info in FIFTEEN_FIVE_STOCKS.items():
        for concept in info.get("concepts", []):
            concept_stocks.setdefault(str(concept), []).append(code)

    conn = store._get_conn()
    try:
        for concept, codes in sorted(concept_stocks.items()):
            conn.execute(
                """INSERT INTO concepts (name, category, stocks, updated_at)
                   VALUES (?, 'concept', ?, datetime('now','localtime'))
                   ON CONFLICT(name) DO UPDATE SET
                     category='concept', stocks=excluded.stocks,
                     updated_at=datetime('now','localtime')""",
                (concept, ",".join(sorted(codes))),
            )
        conn.commit()
        return {"concepts": len(concept_stocks), "theme_stocks": len(FIFTEEN_FIVE_STOCKS)}
    finally:
        conn.close()


def stock_health(store: StockStore) -> dict:
    conn = store._get_conn()
    try:
        codes = list(FIFTEEN_FIVE_STOCKS)
        placeholders = ",".join("?" for _ in codes)
        return {
            "stocks": conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0],
            "active_stocks": conn.execute("SELECT COUNT(*) FROM stocks WHERE is_active=1").fetchone()[0],
            "daily_price_codes": conn.execute("SELECT COUNT(DISTINCT code) FROM daily_prices").fetchone()[0],
            "daily_codes_missing_metadata": conn.execute(
                """SELECT COUNT(DISTINCT dp.code) FROM daily_prices dp
                   WHERE NOT EXISTS (SELECT 1 FROM stocks s WHERE s.code=dp.code)"""
            ).fetchone()[0],
            "concepts": conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0],
            "theme_pool_covered": conn.execute(
                f"SELECT COUNT(*) FROM stocks WHERE code IN ({placeholders})", codes
            ).fetchone()[0],
            "theme_pool_total": len(codes),
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="同步股票基础资料与十五五主题映射")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-only", action="store_true", help="只检查数据库覆盖，不访问网络或写库")
    mode.add_argument("--concepts-only", action="store_true", help="只同步仓库内主题映射，不访问 BaoStock")
    args = parser.parse_args()
    store = StockStore()
    try:
        result = {}
        if not args.verify_only and not args.concepts_only:
            result["stock_basic"] = FinanceService().sync_stock_basic()
        if not args.verify_only:
            result["themes"] = sync_concepts(store)
        result["health"] = stock_health(store)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
        return 0
    except ModuleNotFoundError as e:
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
