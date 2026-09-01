#!/usr/bin/env python3
"""Scan the fixed strategic pool and persist short-lived trading candidates."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.intraday_radar import (  # noqa: E402
    RadarPolicy,
    expected_volume_fraction,
    load_daily_setups,
    save_radar_result,
    score_launch_candidate,
)
from data.market_calendar import market_day  # noqa: E402
from data.services.sector_rotation_service import SectorRotationService  # noqa: E402
from data.store.sqlite_store import StockStore  # noqa: E402
from data.strategic_theme_pool import load_strategic_pool, strategic_pool_codes  # noqa: E402
from data.trading_state import (  # noqa: E402
    _ensure_sector_memberships,
    _fund_flows,
    _minute_states,
    _news,
    _sector_state,
    fetch_market_indices,
    fetch_quotes,
)


def _market_change(indices: dict) -> float:
    preferred = [
        row for key, row in indices.items()
        if key in {"sh000001", "sz399001", "sz399006", "sh000300"}
        and isinstance(row, dict) and "change_pct" in row
    ]
    rows = preferred or [row for row in indices.values() if isinstance(row, dict) and "change_pct" in row]
    return round(sum(float(row.get("change_pct") or 0) for row in rows) / len(rows), 2) if rows else 0.0


def _save_quote_snapshots(store: StockStore, quotes: dict[str, dict]) -> None:
    rows = []
    for quote in quotes.values():
        if float(quote.get("price") or 0) <= 0:
            continue
        rows.append({
            "代码": quote.get("code"), "名称": quote.get("name"),
            "最新价": quote.get("price"), "今开": quote.get("open"),
            "最高": quote.get("high"), "最低": quote.get("low"),
            "昨收": quote.get("prev_close"), "涨跌幅": quote.get("change_pct"),
            "成交量": quote.get("volume"), "成交额": quote.get("amount"),
        })
    if rows:
        store.save_realtime_snapshot(pd.DataFrame(rows), source="tencent_radar")


def scan(now: datetime | None = None, policy: RadarPolicy | None = None) -> dict:
    now = now or datetime.now()
    policy = policy or RadarPolicy()
    as_of = now.strftime("%Y-%m-%d %H:%M:%S")
    store = StockStore()
    pool = load_strategic_pool()
    codes = list(strategic_pool_codes())
    quotes = fetch_quotes(codes)
    _save_quote_snapshots(store, quotes)
    setups = load_daily_setups(store, codes)
    indices = fetch_market_indices()
    market_change = _market_change(indices)
    fraction = expected_volume_fraction(now)

    cheap = []
    for code in codes:
        quote = quotes.get(code) or {}
        quote_name = str(quote.get("name") or "").upper()
        if float(quote.get("price") or 0) <= 0 or "ST" in quote_name:
            continue
        scored = score_launch_candidate(
            code, quote, setups.get(code) or {"available": False},
            market_change_pct=market_change, volume_fraction=fraction,
        )
        if scored["score"] < policy.minimum_prefilter_score:
            continue
        cheap.append({"code": code, "quote": quote, "setup": setups.get(code) or {}, **scored})
    cheap.sort(key=lambda row: (-row["score"], row["code"]))
    deep_rows = cheap[: policy.prefilter_limit]
    deep_codes = [row["code"] for row in deep_rows]

    minutes = _minute_states(deep_codes)
    flows = _fund_flows(deep_codes, store, retry_missing=True)
    sectors = _sector_state()
    _ensure_sector_memberships(deep_codes, store)
    try:
        sector_contexts = SectorRotationService(store=store).get_stock_contexts(
            deep_codes, snapshot=sectors,
        )
    except Exception as exc:
        sector_contexts = {code: {"alignment": "unknown", "error": str(exc)} for code in deep_codes}
    news, _ = _news(store, deep_codes, as_of)

    selected = []
    for row in deep_rows:
        code = row["code"]
        scored = score_launch_candidate(
            code, row["quote"], row["setup"], market_change_pct=market_change,
            volume_fraction=fraction, intraday=minutes.get(code),
            fund_flow=flows.get(code), sector=sector_contexts.get(code),
            news=news.get(code),
        )
        families = set(scored["confirmation_families"])
        ignition_confirmed = (
            "price" in families
            and ("volume" in families or "intraday" in families)
            and len(families) >= 3
        )
        if (
            scored["score"] < policy.minimum_selection_score
            or not scored["actionable"] or not ignition_confirmed
        ):
            continue
        meta = pool["stocks"][code]
        selected.append({
            "code": code,
            "name": row["quote"].get("name") or meta["name"],
            "theme_group": meta["group"],
            "score": scored["score"],
            "price": row["quote"].get("price"),
            "change_pct": row["quote"].get("change_pct"),
            "triggers": scored["triggers"],
            "risk_tags": scored["risk_tags"],
            "evidence": {
                "source": "strategic_pool_intraday_radar",
                "pool_version": pool["version"],
                "radar_actionable": True,
                "confirmation_families": scored["confirmation_families"],
                "metrics": scored["metrics"],
                "setup": row["setup"],
                "quote": row["quote"],
                "intraday": minutes.get(code) or {},
                "fund_flow": flows.get(code) or {},
                "sector": sector_contexts.get(code) or {},
                "news": news.get(code) or [],
            },
        })
    selected.sort(key=lambda row: (-row["score"], row["code"]))
    selected = selected[: policy.selection_limit]
    market_context = {
        "market_change_pct": market_change,
        "volume_fraction": round(fraction, 3),
        "indices": indices,
        "sector_as_of": sectors.get("created_at"),
    }
    save_radar_result(
        store, as_of=as_of, pool_size=len(codes),
        quote_count=sum(float(row.get("price") or 0) > 0 for row in quotes.values()),
        prefiltered_count=len(deep_rows), candidates=selected,
        market_context=market_context, expiry_minutes=policy.expiry_minutes,
    )
    return {
        "as_of": as_of,
        "pool_size": len(codes),
        "quote_count": sum(float(row.get("price") or 0) > 0 for row in quotes.values()),
        "prefiltered_count": len(deep_rows),
        "selected_count": len(selected),
        "candidates": [
            {key: row[key] for key in ("code", "name", "theme_group", "score", "triggers", "risk_tags")}
            for row in selected
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="战略赛道固定池盘中启动雷达")
    parser.add_argument("--allow-closed", action="store_true", help="仅供诊断，允许非交易时段运行")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    now = datetime.now()
    market = market_day(now.date())
    minute = now.hour * 60 + now.minute
    in_session = 9 * 60 + 25 <= minute <= 11 * 60 + 35 or 12 * 60 + 55 <= minute <= 15 * 60 + 5
    if not args.allow_closed and (not market.is_open or not in_session):
        print(f"SKIP:{market.reason if not market.is_open else 'outside trading session'}")
        return 3
    try:
        print(json.dumps(scan(now), ensure_ascii=False, indent=2 if args.pretty else None))
        return 0
    except Exception as exc:
        print(f"intraday radar failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
