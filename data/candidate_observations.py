"""Lightweight readers for dynamic candidate observations.

This module intentionally depends only on the standard library and
``StockStore``.  The account-free promotion MCP can therefore read persisted
auction/radar facts without importing the pandas-heavy radar scanner or the
external iWenCai auction adapter.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

from data.store.sqlite_store import StockStore


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return default if math.isnan(result) or math.isinf(result) else result
    except (TypeError, ValueError):
        return default


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_daily_setup(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build the radar's prior-close setup from plain row mappings."""
    ordered = sorted((dict(row) for row in rows), key=lambda row: str(row.get("date") or ""))[-65:]
    if len(ordered) < 21:
        return {"available": False, "error": "fewer than 21 complete daily bars"}
    close = [_number(row.get("close")) for row in ordered]
    high = [_number(row.get("high")) for row in ordered]
    low = [_number(row.get("low")) for row in ordered]
    volume = [_number(row.get("volume")) for row in ordered]
    last = close[-1]
    prior20_high = max(high[-20:])
    high60, low60 = max(high[-60:]), min(low[-60:])

    def ret(days: int) -> float:
        base = close[-days - 1] if len(close) > days else last
        return round((last / base - 1) * 100, 2) if base else 0.0

    def ma(days: int) -> float:
        return _mean(close[-days:])

    previous_avg = _mean(volume[:-1][-20:])
    avg20_volume = _mean(volume[-20:])
    previous_volume_ratio = volume[-1] / previous_avg if previous_avg else 0.0
    ranges = [
        (high[index] - low[index]) / close[index - 1] * 100
        for index in range(1, len(close)) if close[index - 1]
    ]
    atr5, atr20 = _mean(ranges[-5:]), _mean(ranges[-20:])
    position60 = (last - low60) / (high60 - low60) * 100 if high60 > low60 else 50.0
    return {
        "available": True,
        "as_of": str(ordered[-1].get("date") or "")[:10],
        "close": round(last, 3),
        "ret_1d_pct": ret(1),
        "ret_5d_pct": ret(5),
        "ret_20d_pct": ret(20),
        "ma5": round(ma(5), 3),
        "ma10": round(ma(10), 3),
        "ma20": round(ma(20), 3),
        "prior20_high": round(prior20_high, 3),
        "position_60d_pct": round(position60, 2),
        "previous_volume_ratio_20d": round(previous_volume_ratio, 2),
        "avg20_volume": round(avg20_volume, 2),
        "range_compression_5v20": round(atr5 / atr20, 2) if atr20 else None,
    }


def load_daily_setups(store: StockStore, codes: Iterable[str]) -> dict[str, dict[str, Any]]:
    normalized = list(dict.fromkeys(str(code).zfill(6) for code in codes))
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    conn = store._get_conn()
    try:
        rows = conn.execute(
            f"""SELECT code,date,open,high,low,close,volume,amount
                  FROM daily_prices
                 WHERE code IN ({placeholders}) AND adjust_flag='qfq'
                   AND date < date('now','localtime')
                 ORDER BY code,date""",
            normalized,
        ).fetchall()
    finally:
        conn.close()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        grouped[str(item.get("code") or "").zfill(6)].append(item)
    return {code: build_daily_setup(items) for code, items in grouped.items()}


def latest_radar_candidates(
    store: StockStore, *, now: datetime | None = None, limit: int = 5,
) -> list[dict[str, Any]]:
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    conn = store._get_conn()
    try:
        run = conn.execute(
            """SELECT as_of FROM intraday_radar_runs
                WHERE status='ready' AND as_of<=? ORDER BY as_of DESC LIMIT 1""",
            (stamp,),
        ).fetchone()
        if not run:
            return []
        rows = conn.execute(
            """SELECT * FROM intraday_radar_candidates
                WHERE as_of=? AND expires_at>=? ORDER BY rank LIMIT ?""",
            (run["as_of"], stamp, max(1, int(limit))),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for raw in rows:
        item = dict(raw)
        for key in ("triggers", "risk_tags", "evidence"):
            try:
                item[key] = json.loads(item.get(key) or ("{}" if key == "evidence" else "[]"))
            except json.JSONDecodeError:
                item[key] = {} if key == "evidence" else []
        result.append(item)
    return result


def latest_auction_watch_candidates(
    store: StockStore, *, now: datetime | None = None, limit: int = 3,
) -> list[dict[str, Any]]:
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    conn = store._get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM opening_auction_watch_candidates
                WHERE generated_at<=? AND expires_at>=?
                ORDER BY trade_date DESC, rank LIMIT ?""",
            (stamp, stamp, max(1, int(limit))),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for raw in rows:
        item = dict(raw)
        item["price"] = item.pop("auction_price", 0)
        item["as_of"] = item.get("generated_at")
        for key in ("triggers", "risk_tags", "evidence"):
            try:
                item[key] = json.loads(item.get(key) or ("{}" if key == "evidence" else "[]"))
            except json.JSONDecodeError:
                item[key] = {} if key == "evidence" else []
        result.append(item)
    return result
