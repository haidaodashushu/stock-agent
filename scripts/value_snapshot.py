#!/usr/bin/env python3
"""Build value-investing snapshots.

Phase 1 only: generate facts, rule scores, AI prompt files, and persist
value_snapshots. This script never creates trading signals or orders.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.store.sqlite_store import StockStore
from data.value_investing import (
    build_value_snapshot,
    collect_latest_screen_codes,
    collect_portfolio_codes,
    upsert_value_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]


def _dedup(codes: list[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for raw in codes:
        code = str(raw or "").strip().zfill(6)
        if not code or code == "000000" or code in seen:
            continue
        out.append(code)
        seen.add(code)
    return out


def _load_codes_from_file(path: str) -> list[str]:
    if not path:
        return []
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith(".json"):
        raw = json.loads(text)
        if isinstance(raw, dict):
            raw = raw.get("codes") or raw.get("items") or []
        if isinstance(raw, list):
            return [str(x.get("code", x) if isinstance(x, dict) else x).zfill(6) for x in raw]
    return [x.strip().zfill(6) for x in text.replace("\n", ",").split(",") if x.strip()]


def _collect_codes(args: argparse.Namespace, store: StockStore) -> list[str]:
    codes: list[str] = []
    if args.codes:
        codes.extend(args.codes.split(","))
    if args.codes_file:
        codes.extend(_load_codes_from_file(args.codes_file))
    if args.from_latest_screen:
        codes.extend(collect_latest_screen_codes(args.limit, store=store))
    if args.from_portfolio:
        codes.extend(collect_portfolio_codes(store=store))
    return _dedup(codes)


def _print_table(items: list[dict[str, Any]]) -> None:
    if not items:
        print("无 value snapshot")
        return
    print("\n价值快照结果")
    print("code    name        type                    label                score  trap  watch")
    print("-" * 86)
    for item in items:
        print(
            f"{item['code']:<7} "
            f"{item['name'][:8]:<10} "
            f"{item['company_type']:<23} "
            f"{item['value_label']:<20} "
            f"{item['composite_score']:>5.1f} "
            f"{item['trap_risk_score']:>5.1f} "
            f"{'Y' if item['watch_pool'] else 'N'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成价值投资事实快照/规则评分")
    parser.add_argument("--codes", default="", help="逗号分隔股票代码，如 002594,002138")
    parser.add_argument("--codes-file", default="", help="代码文件，支持 JSON 或逗号/换行文本")
    parser.add_argument("--from-latest-screen", action="store_true", help="使用最新 screen_records 候选")
    parser.add_argument("--from-portfolio", action="store_true", help="使用当前模拟盘持仓")
    parser.add_argument("--limit", type=int, default=30, help="latest screen 数量")
    parser.add_argument("--as-of", default=datetime.now().date().isoformat(), help="快照日期 YYYY-MM-DD")
    parser.add_argument("--output", default="", help="输出 JSON 文件")
    parser.add_argument("--no-db", action="store_true", help="只输出，不写入 value_snapshots 表")
    parser.add_argument("--no-ai-prompt", action="store_true", help="不生成 AI prompt 文件")
    args = parser.parse_args()

    store = StockStore()
    codes = _collect_codes(args, store)
    if not codes:
        raise SystemExit("没有待处理代码；使用 --codes / --codes-file / --from-latest-screen / --from-portfolio")

    results = []
    errors = []
    for code in codes:
        try:
            snapshot = build_value_snapshot(
                code,
                store=store,
                as_of=args.as_of,
                write_prompt=not args.no_ai_prompt,
            )
            if not args.no_db:
                upsert_value_snapshot(snapshot, store=store)
            results.append(snapshot.to_dict())
        except Exception as exc:
            errors.append({"code": code, "error": str(exc)})
            print(f"⚠️ {code} 生成失败: {exc}", file=sys.stderr)

    payload = {
        "schema": "value_snapshot.v1",
        "as_of": args.as_of,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_policy": "facts_first; observation_only; no_trade_signal",
        "items": results,
        "errors": errors,
    }
    if args.output:
        path = ROOT / args.output if not os.path.isabs(args.output) else Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 输出: {path}")
    _print_table(results)
    if errors:
        print(f"\n失败 {len(errors)} 条: {errors}")
    print(f"\n完成：成功 {len(results)} / 总计 {len(codes)}；写库={'否' if args.no_db else '是'}；AI prompt={'否' if args.no_ai_prompt else '是'}")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
