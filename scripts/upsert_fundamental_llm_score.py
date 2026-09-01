#!/usr/bin/env python3
"""Import LLM financial-report / MD&A scores.

Examples:
  python3 scripts/upsert_fundamental_llm_score.py --json data/fundamental_scores.json
  python3 scripts/upsert_fundamental_llm_score.py --code 600460 --name 士兰微 --period 2025A \
    --industry-demand 5 --future-demand 4 --strategy 2.5 --candor 1 \
    --summary "行业需求上行，管理层风险披露充分"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.fundamental_llm import score_from_mapping, upsert_fundamental_llm_score


def _load_json(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "items" in raw:
        raw = raw["items"]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("JSON must be an object, a list, or {'items': [...]}")
    return [dict(x) for x in raw]


def _from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "code": args.code,
        "name": args.name,
        "period": args.period,
        "report_type": args.report_type,
        "report_date": args.report_date,
        "industry_demand_score": args.industry_demand,
        "future_demand_score": args.future_demand,
        "product_penetration_score": args.product_penetration,
        "strategy_score": args.strategy,
        "candor_score": args.candor,
        "composite_score": args.composite,
        "confidence": args.confidence,
        "summary": args.summary,
        "evidence": json.loads(args.evidence) if args.evidence else {},
        "source": args.source,
        "model": args.model,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="导入 LLM 财报/MD&A 评分")
    p.add_argument("--json", help="JSON 文件，支持单条对象或列表")
    p.add_argument("--code", help="股票代码")
    p.add_argument("--name", default="", help="股票名称")
    p.add_argument("--period", default="", help="财报期，如 2025A / 2025H1")
    p.add_argument("--report-type", default="", help="annual / semiannual / quarterly")
    p.add_argument("--report-date", default="", help="报告披露或财报日期 YYYY-MM-DD")
    p.add_argument("--industry-demand", type=float, default=2.5, help="报告期行业需求 1-5")
    p.add_argument("--future-demand", type=float, default=2.5, help="未来行业需求 1-5")
    p.add_argument("--product-penetration", type=float, default=2.5, help="产品渗透率/空间 1-5")
    p.add_argument("--strategy", type=float, default=1.5, help="战略合理性 0-3")
    p.add_argument("--candor", type=float, default=0.5, help="管理层坦诚度 0/1")
    p.add_argument("--composite", type=float, default=None, help="综合分 0-5；不填则按分项计算")
    p.add_argument("--confidence", type=float, default=0.8, help="证据置信度 0-1")
    p.add_argument("--summary", default="", help="简短结论")
    p.add_argument("--evidence", default="", help="JSON 字符串，保存引用片段/理由")
    p.add_argument("--source", default="manual", help="来源")
    p.add_argument("--model", default="", help="LLM 模型名")
    args = p.parse_args()

    records = _load_json(args.json) if args.json else [_from_args(args)]
    if not records or not all(str(r.get("code", "")).strip() for r in records):
        raise SystemExit("缺少 code；用 --code 或在 JSON 中提供 code")

    inserted = 0
    for raw in records:
        score = score_from_mapping(raw)
        upsert_fundamental_llm_score(score)
        inserted += 1
        print(
            f"{score.code} {score.name} {score.period} "
            f"综合{score.composite_score:.2f} boost{score.boost:+.2f} "
            f"{'|'.join(score.tags)}"
        )
    print(f"✅ 已写入/更新 {inserted} 条 LLM 财报评分")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
