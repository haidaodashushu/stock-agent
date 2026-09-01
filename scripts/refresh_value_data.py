#!/usr/bin/env python3
"""Incrementally refresh value-investing data.

This script implements the "hot/cold layered" route:
1. Build/sync value_universe from portfolio, live intents, latest screen records,
   existing value snapshots, and optional manual codes.
2. Use value_data_freshness to refresh only stale value snapshots.
3. Persist snapshots and AI prompts without creating trade signals or orders.
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
    build_watch_universe,
    get_due_value_universe,
    mark_universe_refreshed,
    mark_value_freshness,
    sync_value_universe,
    upsert_value_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]


def _parse_codes(value: str) -> list[str]:
    return [x.strip().zfill(6) for x in value.split(",") if x.strip()]


def _tiers(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _write_output(path: str, payload: dict[str, Any]) -> None:
    if not path:
        return
    out = ROOT / path if not os.path.isabs(path) else Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON 输出: {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description="增量刷新价值投资分层数据")
    parser.add_argument("--codes", default="", help="额外临时代码，逗号分隔，会进入 temp 层")
    parser.add_argument("--latest-screen-limit", type=int, default=200, help="纳入最新盘前候选数量")
    parser.add_argument("--refresh-limit", type=int, default=80, help="本轮最多刷新多少只")
    parser.add_argument("--max-age-hours", type=int, default=24, help="value_snapshot 成功时间超过多少小时视为过期")
    parser.add_argument("--tiers", default="core,candidate,temp", help="刷新哪些层级")
    parser.add_argument("--force", action="store_true", help="忽略新鲜度，直接刷新 universe 中的前 N 只")
    parser.add_argument("--sync-only", action="store_true", help="只同步 value_universe，不刷新快照")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库、不生成 prompt")
    parser.add_argument("--no-ai-prompt", action="store_true", help="不生成 AI prompt 文件")
    parser.add_argument("--output", default="", help="输出 JSON 摘要")
    args = parser.parse_args()

    store = StockStore()
    extra_codes = _parse_codes(args.codes)
    universe = build_watch_universe(
        latest_screen_limit=args.latest_screen_limit,
        extra_codes=extra_codes,
        store=store,
    )
    synced = 0
    if not args.dry_run:
        synced = sync_value_universe(universe, store=store)

    selected_tiers = set(_tiers(args.tiers))
    if args.dry_run:
        due = [entry for entry in universe if entry.tier in selected_tiers][: args.refresh_limit]
    elif args.force:
        due = [entry for entry in universe if entry.tier in selected_tiers][: args.refresh_limit]
    else:
        due = get_due_value_universe(
            data_type="value_snapshot",
            max_age_hours=args.max_age_hours,
            limit=args.refresh_limit,
            tiers=selected_tiers,
            store=store,
        )

    payload: dict[str, Any] = {
        "schema": "value_refresh.v1",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": {
            "force": args.force,
            "sync_only": args.sync_only,
            "dry_run": args.dry_run,
            "max_age_hours": args.max_age_hours,
            "tiers": sorted(selected_tiers),
        },
        "universe": {
            "built": len(universe),
            "synced": synced,
            "by_tier": _count_by_tier(universe),
        },
        "planned": [entry.to_dict() for entry in due],
        "refreshed": [],
        "errors": [],
    }

    if args.sync_only or args.dry_run:
        _print_summary(payload)
        _write_output(args.output, payload)
        return 0

    for entry in due:
        try:
            snapshot = build_value_snapshot(entry.code, store=store, write_prompt=not args.no_ai_prompt)
            upsert_value_snapshot(snapshot, store=store)
            mark_value_freshness(
                entry.code,
                "value_snapshot",
                status="ok",
                source="refresh_value_data.py",
                metadata={
                    "company_type": snapshot.company_type,
                    "value_label": snapshot.value_label,
                    "composite_score": snapshot.composite_score,
                    "confidence": snapshot.confidence,
                },
                store=store,
            )
            mark_value_freshness(
                entry.code,
                "valuation",
                status="ok" if snapshot.facts.get("valuation", {}).get("pe") or snapshot.facts.get("valuation", {}).get("pb") else "missing",
                source=snapshot.facts.get("valuation", {}).get("source", "tencent_quote"),
                success=bool(snapshot.facts.get("valuation", {}).get("pe") or snapshot.facts.get("valuation", {}).get("pb")),
                metadata=snapshot.facts.get("valuation", {}),
                store=store,
            )
            mark_value_freshness(
                entry.code,
                "financial",
                status="ok" if snapshot.facts.get("financial", {}).get("period") else "missing",
                source=snapshot.facts.get("financial", {}).get("source", "financial_factors"),
                success=bool(snapshot.facts.get("financial", {}).get("period")),
                metadata=snapshot.facts.get("financial", {}),
                store=store,
            )
            mark_universe_refreshed(entry.code, store=store)
            payload["refreshed"].append({
                "code": snapshot.code,
                "name": snapshot.name,
                "tier": entry.tier,
                "company_type": snapshot.company_type,
                "value_label": snapshot.value_label,
                "composite_score": snapshot.composite_score,
                "trap_risk_score": snapshot.trap_risk_score,
                "confidence": snapshot.confidence,
            })
        except Exception as exc:
            mark_value_freshness(
                entry.code,
                "value_snapshot",
                status="error",
                source="refresh_value_data.py",
                error=str(exc),
                success=False,
                store=store,
            )
            payload["errors"].append({"code": entry.code, "error": str(exc)})
            print(f"⚠️ {entry.code} 刷新失败: {exc}", file=sys.stderr)

    _print_summary(payload)
    _write_output(args.output, payload)
    return 0 if payload["refreshed"] or not due else 1


def _count_by_tier(universe: list[Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for entry in universe:
        result[entry.tier] = result.get(entry.tier, 0) + 1
    return result


def _print_summary(payload: dict[str, Any]) -> None:
    print("价值数据增量刷新")
    print(f"universe: built={payload['universe']['built']} synced={payload['universe']['synced']} by_tier={payload['universe']['by_tier']}")
    print(f"planned={len(payload['planned'])} refreshed={len(payload['refreshed'])} errors={len(payload['errors'])}")
    if payload["refreshed"]:
        print("\n刷新结果")
        print("code    name        tier       type                    label                score  conf")
        print("-" * 88)
        for item in payload["refreshed"]:
            print(
                f"{item['code']:<7} "
                f"{item['name'][:8]:<10} "
                f"{item['tier']:<10} "
                f"{item['company_type']:<23} "
                f"{item['value_label']:<20} "
                f"{item['composite_score']:>5.1f} "
                f"{item['confidence']:>4.2f}"
            )


if __name__ == "__main__":
    raise SystemExit(main())
