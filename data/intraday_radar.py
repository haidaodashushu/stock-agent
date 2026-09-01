"""Strategic-pool intraday launch radar.

The radar is deliberately a candidate generator, not a trading strategy.  It
combines prior-close setup facts with current ignition/confirmation facts and
persists at most a few short-lived candidates for the half-hour decision.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from data.store.sqlite_store import StockStore
from data.strategic_theme_pool import load_strategic_pool, strategic_pool_codes
from data.candidate_observations import (
    build_daily_setup,
    latest_radar_candidates,
    load_daily_setups,
)


@dataclass(frozen=True)
class RadarPolicy:
    prefilter_limit: int = 18
    selection_limit: int = 5
    minimum_prefilter_score: float = 3.5
    minimum_selection_score: float = 5.5
    expiry_minutes: int = 40


def radar_expiry(as_of: str, expiry_minutes: int) -> str:
    """Advance expiry in trading minutes, skipping the A-share lunch break."""
    current = datetime.fromisoformat(as_of)
    remaining = max(1, int(expiry_minutes))
    while remaining:
        current += timedelta(minutes=1)
        if current.hour == 11 and current.minute >= 30:
            current = current.replace(hour=13, minute=0)
        if current.hour >= 15:
            current = current.replace(hour=15, minute=0)
            break
        remaining -= 1
    return current.strftime("%Y-%m-%d %H:%M:%S")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return default if math.isnan(result) or math.isinf(result) else result
    except (TypeError, ValueError):
        return default


def expected_volume_fraction(now: datetime) -> float:
    """Coarse cumulative A-share volume curve used only for cheap triage.

    A time-of-day historical baseline would be preferable.  Until enough radar
    snapshots exist, this conservative piecewise curve prevents linear opening
    extrapolation from labelling nearly every stock as abnormal volume.
    """
    minute = now.hour * 60 + now.minute
    anchors = [
        (9 * 60 + 30, 0.03), (10 * 60, 0.25), (10 * 60 + 30, 0.42),
        (11 * 60, 0.56), (11 * 60 + 30, 0.62), (13 * 60, 0.63),
        (13 * 60 + 30, 0.74), (14 * 60, 0.84), (14 * 60 + 30, 0.93),
        (15 * 60, 1.0),
    ]
    if minute <= anchors[0][0]:
        return anchors[0][1]
    for (left_minute, left), (right_minute, right) in zip(anchors, anchors[1:]):
        if minute <= right_minute:
            width = max(1, right_minute - left_minute)
            return left + (right - left) * (minute - left_minute) / width
    return 1.0


def daily_setup(frame: pd.DataFrame) -> dict[str, Any]:
    """Build setup facts using complete daily bars only (normally through T-1)."""
    if frame is None or frame.empty:
        return {"available": False, "error": "fewer than 21 complete daily bars"}
    return build_daily_setup(frame.to_dict("records"))


def score_launch_candidate(
    code: str,
    quote: dict[str, Any],
    setup: dict[str, Any],
    *,
    market_change_pct: float = 0.0,
    volume_fraction: float = 1.0,
    intraday: dict[str, Any] | None = None,
    fund_flow: dict[str, Any] | None = None,
    sector: dict[str, Any] | None = None,
    news: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return an explainable score; no field alone can create a candidate."""
    price = _number(quote.get("price"))
    previous = _number(quote.get("prev_close") or setup.get("close"))
    opening = _number(quote.get("open"))
    high = _number(quote.get("high"))
    change = _number(quote.get("change_pct"))
    relative = change - _number(market_change_pct)
    pullback = (price / high - 1) * 100 if price and high else 0.0
    prior20_high = _number(setup.get("prior20_high"))
    breakout = (price / prior20_high - 1) * 100 if price and prior20_high else -99.0
    avg20_volume = _number(setup.get("avg20_volume"))
    pace = (
        _number(quote.get("volume")) / max(volume_fraction, 0.03) / avg20_volume
        if avg20_volume else 0.0
    )
    growth_board = str(code).startswith("30")
    ignition_floor = 3.0 if growth_board else 2.0
    strong_floor = 6.0 if growth_board else 4.0
    limit_guard = 19.2 if growth_board else 9.5

    score = 0.0
    triggers: list[str] = []
    risks: list[str] = []
    families: set[str] = set()

    if setup.get("available"):
        ret20 = _number(setup.get("ret_20d_pct"))
        pos60 = _number(setup.get("position_60d_pct"), 50.0)
        previous_vr = _number(setup.get("previous_volume_ratio_20d"))
        compression = setup.get("range_compression_5v20")
        if ret20 < 0 and pos60 <= 35:
            score += 0.75
            triggers.append("低位修复准备")
            families.add("setup")
        if previous_vr >= 1.2:
            score += 0.75
            triggers.append("前一日量能异动")
            families.add("setup")
        if compression is not None and _number(compression) <= 0.9:
            score += 0.5
            triggers.append("近期波动收敛")
            families.add("setup")
        if _number(setup.get("ret_5d_pct")) >= 18:
            score -= 1.0
            risks.append("短期涨幅已集中")

    if change >= ignition_floor:
        score += 1.0
        triggers.append("盘中涨幅进入点火区")
        families.add("price")
    if change >= strong_floor:
        score += 0.75
        triggers.append("盘中价格加速")
    if relative >= 1.5:
        score += 1.0
        triggers.append("显著强于市场")
        families.add("relative_strength")
    if pace >= 1.3:
        score += 1.0
        triggers.append("成交速度放大")
        families.add("volume")
    if pace >= 2.0:
        score += 0.75
        triggers.append("成交速度显著放大")
    if breakout >= -1.0:
        score += 1.0
        triggers.append("接近或突破20日平台")
        families.add("price")
    if pullback >= -1.5 and change > 0:
        score += 0.75
        triggers.append("贴近日内高点")
        families.add("price")
    if opening and price >= opening:
        score += 0.25

    intraday = intraday or {}
    half_hour = intraday.get("half_hour") if isinstance(intraday.get("half_hour"), dict) else {}
    if intraday.get("above_vwap") is True:
        score += 0.75
        triggers.append("站上VWAP")
        families.add("intraday")
    if _number(intraday.get("last_15m_pct")) > 0:
        score += 0.5
        triggers.append("最近15分钟继续增强")
        families.add("intraday")
    if half_hour.get("available") and _number(half_hour.get("price_pct")) > 0:
        score += 0.5
        triggers.append("半小时价格增强")
        families.add("intraday")
    if half_hour.get("available") and _number(half_hour.get("volume_ratio")) >= 1.0:
        score += 0.5
        triggers.append("半小时量能确认")
        families.add("intraday")

    fund_flow = fund_flow or {}
    detail = fund_flow.get("detail") if isinstance(fund_flow.get("detail"), dict) else {}
    main_net = _number(detail.get("main_net_inflow"))
    if fund_flow.get("status") == "available" and main_net > 0:
        score += 0.75
        triggers.append("实时主力资金净流入")
        families.add("fund_flow")
    elif fund_flow.get("status") == "available" and main_net < 0:
        score -= 0.5
        risks.append("实时主力资金净流出")

    sector = sector or {}
    if sector.get("alignment") == "positive":
        score += 0.75
        triggers.append("赛道轮动同向")
        families.add("sector")
    positive_news = [row for row in (news or []) if _number(row.get("score")) >= 0.8]
    if positive_news:
        score += 0.5
        triggers.append("近期增量事件支持")
        families.add("logic")

    actionable = True
    if pullback < -3.0:
        score -= 2.0
        risks.append("冲高回落超过3%")
        actionable = False
    if pace >= 1.8 and change < 1.0:
        score -= 1.5
        risks.append("放量但价格未响应")
    if change >= limit_guard:
        risks.append("已接近涨停，成交可执行性低")
        actionable = False
    if opening and (opening / previous - 1) * 100 >= 5 and price < opening:
        score -= 1.5
        risks.append("高开回落")

    return {
        "score": round(score, 2),
        "actionable": actionable,
        "confirmation_families": sorted(families),
        "triggers": list(dict.fromkeys(triggers)),
        "risk_tags": list(dict.fromkeys(risks)),
        "metrics": {
            "change_pct": round(change, 2),
            "relative_market_pct": round(relative, 2),
            "volume_pace_20d": round(pace, 2),
            "breakout_20d_pct": round(breakout, 2),
            "pullback_from_high_pct": round(pullback, 2),
        },
    }


def save_radar_result(
    store: StockStore,
    *,
    as_of: str,
    pool_size: int,
    quote_count: int,
    prefiltered_count: int,
    candidates: list[dict[str, Any]],
    market_context: dict[str, Any],
    expiry_minutes: int = RadarPolicy().expiry_minutes,
    error: str = "",
) -> None:
    expires_at = radar_expiry(as_of, expiry_minutes)
    conn = store._get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO intraday_radar_runs
               (as_of,status,pool_size,quote_count,prefiltered_count,selected_count,
                market_context,error,created_at)
               VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
            (
                as_of, "ready" if not error else "failed", pool_size, quote_count,
                prefiltered_count, len(candidates), json.dumps(market_context, ensure_ascii=False),
                str(error)[:500],
            ),
        )
        conn.execute("DELETE FROM intraday_radar_candidates WHERE as_of=?", (as_of,))
        conn.executemany(
            """INSERT INTO intraday_radar_candidates
               (as_of,rank,code,name,theme_group,score,price,change_pct,triggers,
                risk_tags,evidence,expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    as_of, rank, row["code"], row.get("name", ""), row.get("theme_group", ""),
                    row["score"], row.get("price", 0), row.get("change_pct", 0),
                    json.dumps(row.get("triggers", []), ensure_ascii=False),
                    json.dumps(row.get("risk_tags", []), ensure_ascii=False),
                    json.dumps(row.get("evidence", {}), ensure_ascii=False), expires_at,
                )
                for rank, row in enumerate(candidates, 1)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def pool_summary() -> dict[str, Any]:
    pool = load_strategic_pool()
    return {
        "version": pool["version"],
        "size": len(strategic_pool_codes()),
        "groups": pool["group_counts"],
    }
