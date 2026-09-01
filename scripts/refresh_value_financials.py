#!/usr/bin/env python3
"""Incrementally refresh financial factors for value-investing universe.

This script is a medium-frequency data job. It updates financial_factors and
value_data_freshness only; it never creates trade signals or orders.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.contracts import FinancialFactor
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


def default_period_candidates(now: datetime | None = None) -> list[tuple[str, int]]:
    """Recent likely disclosed quarters, newest first."""
    now = now or datetime.now()
    year = now.year
    if now.month <= 4:
        return [(str(year - 1), 4), (str(year - 1), 3), (str(year - 1), 2)]
    if now.month <= 8:
        return [(str(year), 1), (str(year - 1), 4), (str(year - 1), 3)]
    if now.month <= 10:
        return [(str(year), 2), (str(year), 1), (str(year - 1), 4)]
    return [(str(year), 3), (str(year), 2), (str(year), 1)]


def upsert_financial_factor(factor: FinancialFactor, *, store: StockStore) -> None:
    factor = normalize_factor(factor)
    conn = store._get_conn()
    try:
        conn.execute(
            """INSERT INTO financial_factors
               (code, period, roe, roa, gross_margin, net_margin, eps,
                revenue_yoy, profit_yoy, debt_ratio, source, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
               ON CONFLICT(code, period, source) DO UPDATE SET
                 roe=excluded.roe,
                 roa=excluded.roa,
                 gross_margin=excluded.gross_margin,
                 net_margin=excluded.net_margin,
                 eps=excluded.eps,
                 revenue_yoy=excluded.revenue_yoy,
                 profit_yoy=excluded.profit_yoy,
                 debt_ratio=excluded.debt_ratio,
                 updated_at=datetime('now','localtime')""",
            (
                factor.code,
                factor.period,
                factor.roe,
                factor.roa,
                factor.gross_margin,
                factor.net_margin,
                factor.eps,
                factor.revenue_yoy,
                factor.profit_yoy,
                factor.debt_ratio,
                factor.source,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def is_informative_factor(factor: FinancialFactor) -> bool:
    values = (
        factor.roe,
        factor.roa,
        factor.gross_margin,
        factor.net_margin,
        factor.eps,
        factor.revenue_yoy,
        factor.profit_yoy,
        factor.debt_ratio,
    )
    return any(abs(float(v or 0)) > 1e-9 for v in values)


def normalize_period(period: str) -> str:
    value = str(period or "").strip()
    compact = value.replace("-", "").replace("/", "")
    if len(compact) >= 8 and compact[:4].isdigit():
        year = compact[:4]
        suffix = compact[4:8]
        if suffix == "0331":
            return f"{year}Q1"
        if suffix == "0630":
            return f"{year}Q2"
        if suffix == "0930":
            return f"{year}Q3"
        if suffix == "1231":
            return f"{year}A"
    return value


def normalize_factor(factor: FinancialFactor) -> FinancialFactor:
    return FinancialFactor(
        code=str(factor.code).zfill(6),
        period=normalize_period(factor.period),
        roe=factor.roe,
        roa=factor.roa,
        gross_margin=factor.gross_margin,
        net_margin=factor.net_margin,
        eps=factor.eps,
        revenue_yoy=factor.revenue_yoy,
        profit_yoy=factor.profit_yoy,
        debt_ratio=factor.debt_ratio,
        source=factor.source,
    )


def fetch_iwencai_factor(code: str) -> FinancialFactor | None:
    from data.adapters.iwencai_intelligence_adapter import IwenCaiIntelligenceAdapter

    factors = IwenCaiIntelligenceAdapter().stock_financials(code)
    factors = [f for f in factors if str(f.code).zfill(6) == str(code).zfill(6) and is_informative_factor(f)]
    if not factors:
        return None
    return sorted(factors, key=lambda f: f.period, reverse=True)[0]


def fetch_baostock_factor(code: str, periods: Iterable[tuple[str, int]]) -> FinancialFactor | None:
    from data.adapters.baostock_adapter import BaoStockAdapter

    adapter = BaoStockAdapter()
    for year, quarter in periods:
        factors = adapter.get_financial_factors(code, year=year, quarter=quarter)
        for factor in factors:
            if is_informative_factor(factor):
                return factor
    return None


def fetch_financial_factor(code: str, *, provider: str, periods: list[tuple[str, int]]) -> tuple[FinancialFactor | None, str, str]:
    errors: list[str] = []
    if provider in ("auto", "iwencai"):
        try:
            factor = fetch_iwencai_factor(code)
            if factor:
                return normalize_factor(factor), "iwencai", ""
            errors.append("iwencai: no informative factor")
        except Exception as exc:
            errors.append(f"iwencai: {exc}")
            if provider == "iwencai":
                return None, "iwencai", "; ".join(errors)

    if provider in ("auto", "baostock"):
        try:
            factor = fetch_baostock_factor(code, periods)
            if factor:
                return normalize_factor(factor), "baostock", ""
            errors.append("baostock: no informative factor")
        except Exception as exc:
            errors.append(f"baostock: {exc}")
            if provider == "baostock":
                return None, "baostock", "; ".join(errors)

    return None, provider, "; ".join(errors)


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
    parser = argparse.ArgumentParser(description="增量刷新价值投资财务因子")
    parser.add_argument("--codes", default="", help="额外临时代码，逗号分隔")
    parser.add_argument("--latest-screen-limit", type=int, default=200)
    parser.add_argument("--refresh-limit", type=int, default=30)
    parser.add_argument("--max-age-hours", type=int, default=168, help="financial 成功时间超过多少小时视为过期")
    parser.add_argument("--tiers", default="core,candidate", help="刷新哪些层级")
    parser.add_argument("--provider", choices=["auto", "iwencai", "baostock"], default="auto")
    parser.add_argument("--force", action="store_true", help="忽略新鲜度，直接刷新 universe 中的前 N 只")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    parser.add_argument("--no-value-snapshot", action="store_true", help="财务成功后不重建 value_snapshot")
    parser.add_argument("--output", default="", help="输出 JSON 摘要")
    args = parser.parse_args()

    store = StockStore()
    universe = build_watch_universe(
        latest_screen_limit=args.latest_screen_limit,
        extra_codes=_parse_codes(args.codes),
        store=store,
    )
    if not args.dry_run:
        sync_value_universe(universe, store=store)

    selected_tiers = set(_tiers(args.tiers))
    if args.dry_run or args.force:
        due = [entry for entry in universe if entry.tier in selected_tiers][: args.refresh_limit]
    else:
        due = get_due_value_universe(
            data_type="financial",
            max_age_hours=args.max_age_hours,
            limit=args.refresh_limit,
            tiers=selected_tiers,
            store=store,
        )

    payload: dict[str, Any] = {
        "schema": "value_financial_refresh.v1",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": {
            "provider": args.provider,
            "force": args.force,
            "dry_run": args.dry_run,
            "max_age_hours": args.max_age_hours,
            "tiers": sorted(selected_tiers),
        },
        "period_candidates": [f"{y}Q{q}" for y, q in default_period_candidates()],
        "planned": [entry.to_dict() for entry in due],
        "refreshed": [],
        "missing": [],
        "errors": [],
    }

    if args.dry_run:
        _print_summary(payload)
        _write_output(args.output, payload)
        return 0

    periods = default_period_candidates()
    for entry in due:
        factor, source, error = fetch_financial_factor(entry.code, provider=args.provider, periods=periods)
        if factor:
            upsert_financial_factor(factor, store=store)
            mark_value_freshness(
                entry.code,
                "financial",
                status="ok",
                source=factor.source or source,
                metadata=asdict(factor),
                store=store,
            )
            refreshed_item = {
                "code": factor.code,
                "name": entry.name,
                "tier": entry.tier,
                "period": factor.period,
                "source": factor.source or source,
                "roe": factor.roe,
                "gross_margin": factor.gross_margin,
                "net_margin": factor.net_margin,
                "revenue_yoy": factor.revenue_yoy,
                "profit_yoy": factor.profit_yoy,
                "debt_ratio": factor.debt_ratio,
            }
            if not args.no_value_snapshot:
                snapshot = build_value_snapshot(entry.code, store=store)
                upsert_value_snapshot(snapshot, store=store)
                mark_value_freshness(
                    entry.code,
                    "value_snapshot",
                    status="ok",
                    source="refresh_value_financials.py",
                    metadata={
                        "company_type": snapshot.company_type,
                        "value_label": snapshot.value_label,
                        "composite_score": snapshot.composite_score,
                        "confidence": snapshot.confidence,
                    },
                    store=store,
                )
                refreshed_item["value_snapshot"] = {
                    "company_type": snapshot.company_type,
                    "value_label": snapshot.value_label,
                    "composite_score": snapshot.composite_score,
                    "confidence": snapshot.confidence,
                }
            mark_universe_refreshed(entry.code, store=store)
            payload["refreshed"].append(refreshed_item)
        else:
            status = "missing" if not error else "error"
            mark_value_freshness(
                entry.code,
                "financial",
                status=status,
                source=source,
                error=error,
                success=False,
                store=store,
            )
            bucket = "errors" if status == "error" else "missing"
            payload[bucket].append({"code": entry.code, "name": entry.name, "tier": entry.tier, "error": error})

    _print_summary(payload)
    _write_output(args.output, payload)
    return 0 if payload["refreshed"] or not payload["planned"] else 1


def _print_summary(payload: dict[str, Any]) -> None:
    print("价值财务因子增量刷新")
    print(
        f"planned={len(payload['planned'])} refreshed={len(payload['refreshed'])} "
        f"missing={len(payload['missing'])} errors={len(payload['errors'])}"
    )
    if payload["refreshed"]:
        print("\n刷新结果")
        print("code    name        tier       period      source       roe    gross  net    rev_yoy  profit_yoy")
        print("-" * 100)
        for item in payload["refreshed"]:
            print(
                f"{item['code']:<7} {item['name'][:8]:<10} {item['tier']:<10} "
                f"{item['period']:<11} {item['source'][:12]:<12} "
                f"{item['roe']:>5.1f} {item['gross_margin']:>7.1f} {item['net_margin']:>5.1f} "
                f"{item['revenue_yoy']:>8.1f} {item['profit_yoy']:>10.1f}"
            )
    if payload["errors"]:
        print("\n错误样例")
        for item in payload["errors"][:5]:
            print(f"{item['code']} {item.get('name','')}: {item.get('error','')[:180]}")


if __name__ == "__main__":
    raise SystemExit(main())
