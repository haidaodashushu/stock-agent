"""Deterministic intraday interval price/turnover analysis.

This module measures observable price movement and two-sided turnover.  It does
not infer exchange-certified net inflow or participant identity from turnover.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from typing import Any, Literal
from urllib.error import URLError

import pandas as pd

AmountMode = Literal["cumulative", "incremental"]


def normalize_market_time(value: str) -> str:
    """Normalize ``HH:MM`` or ``HHMM`` to a sortable four-digit market time."""
    normalized = str(value or "").strip().replace(":", "")
    if len(normalized) != 4 or not normalized.isdigit():
        raise ValueError(f"无效市场时间: {value!r}")
    hour, minute = int(normalized[:2]), int(normalized[2:])
    if hour > 23 or minute > 59:
        raise ValueError(f"无效市场时间: {value!r}")
    return normalized


def _prepare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"time", "price", "amount"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError("分钟数据缺少字段: " + ", ".join(sorted(missing)))
    prepared = frame.loc[:, [column for column in frame.columns if column in {"time", "price", "volume", "amount"}]].copy()
    prepared["time"] = prepared["time"].map(normalize_market_time)
    prepared["price"] = pd.to_numeric(prepared["price"], errors="coerce")
    prepared["amount"] = pd.to_numeric(prepared["amount"], errors="coerce")
    if "volume" in prepared:
        prepared["volume"] = pd.to_numeric(prepared["volume"], errors="coerce")
    prepared = prepared.dropna(subset=["price", "amount"])
    prepared = prepared[(prepared["price"] > 0) & (prepared["amount"] >= 0)]
    return prepared.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)


def _validate_amount_mode(requested: AmountMode) -> AmountMode:
    if requested not in {"cumulative", "incremental"}:
        raise ValueError(f"无效 amount_mode: {requested!r}")
    return requested


def _weighted(records: list[dict[str, Any]], field: str, weight_field: str) -> float | None:
    usable = [row for row in records if row.get(field) is not None and float(row.get(weight_field) or 0) > 0]
    denominator = sum(float(row[weight_field]) for row in usable)
    if not denominator:
        return None
    return sum(float(row[field]) * float(row[weight_field]) for row in usable) / denominator


def analyze_interval(
    frame: pd.DataFrame,
    start: str,
    end: str,
    *,
    amount_mode: AmountMode,
    follow_end: str | None = None,
) -> dict[str, Any]:
    """Analyze one intraday interval and optional post-interval follow-through.

    The start point is the first observation at or after ``start``.  The end and
    follow-up points are the last observations at or before their boundaries.
    For cumulative provider data, interval turnover is an endpoint difference;
    for incremental data it is the sum over ``(start_point, end_point]``.
    """
    source_attrs = dict(getattr(frame, "attrs", {}) or {})
    prepared = _prepare_frame(frame)
    start_boundary, end_boundary = normalize_market_time(start), normalize_market_time(end)
    if start_boundary >= end_boundary:
        raise ValueError("start 必须早于 end")
    follow_boundary = normalize_market_time(follow_end) if follow_end else None
    if follow_boundary and follow_boundary <= end_boundary:
        raise ValueError("follow_end 必须晚于 end")

    starts = prepared[prepared["time"] >= start_boundary]
    ends = prepared[prepared["time"] <= end_boundary]
    if starts.empty or ends.empty:
        raise ValueError("区间内没有有效分钟点")
    start_row, end_row = starts.iloc[0], ends.iloc[-1]
    if str(start_row["time"]) > str(end_row["time"]):
        raise ValueError("区间内没有有效分钟点")

    mode = _validate_amount_mode(amount_mode)
    start_time, end_time = str(start_row["time"]), str(end_row["time"])
    interval_rows = prepared[(prepared["time"] > start_time) & (prepared["time"] <= end_time)]
    if mode == "cumulative":
        if bool((prepared["amount"].diff().iloc[1:] < -1e-6).any()):
            raise ValueError("累计成交额出现下降，数据可能重置或口径错误")
        turnover = float(end_row["amount"]) - float(start_row["amount"])
        observed_turnover = float(prepared.iloc[-1]["amount"])
    else:
        turnover = float(interval_rows["amount"].sum())
        observed_turnover = float(prepared["amount"].sum())

    start_price, end_price = float(start_row["price"]), float(end_row["price"])
    price_change = (end_price / start_price - 1) * 100
    result: dict[str, Any] = {
        "source": str(source_attrs.get("source") or ""),
        "trading_date": str(source_attrs.get("trading_date") or ""),
        "requested_start": start_boundary,
        "requested_end": end_boundary,
        "start_time": start_time,
        "end_time": end_time,
        "start_price": start_price,
        "end_price": end_price,
        "price_change_pct": price_change,
        "turnover_amount": turnover,
        "observation_end_time": str(prepared.iloc[-1]["time"]),
        "observed_turnover_amount": observed_turnover,
        "turnover_observed_pct": turnover / observed_turnover * 100 if observed_turnover else None,
        "amount_mode": mode,
        "follow_end_time": None,
        "follow_price": None,
        "follow_change_pct": None,
        "net_change_pct": None,
        "retention_ratio": None,
    }
    if not follow_boundary:
        return result

    follows = prepared[prepared["time"] <= follow_boundary]
    if follows.empty or str(follows.iloc[-1]["time"]) <= end_time:
        raise ValueError("观察区间内没有有效分钟点")
    follow_row = follows.iloc[-1]
    follow_price = float(follow_row["price"])
    follow_change = (follow_price / end_price - 1) * 100
    net_change = (follow_price / start_price - 1) * 100
    result.update(
        {
            "follow_end_time": str(follow_row["time"]),
            "follow_price": follow_price,
            "follow_change_pct": follow_change,
            "net_change_pct": net_change,
            "retention_ratio": net_change / price_change if price_change else None,
        }
    )
    return result


def analyze_many(
    items: Iterable[Mapping[str, Any]],
    fetch_frame: Callable[[str], pd.DataFrame],
    start: str,
    end: str,
    *,
    amount_mode: AmountMode,
    follow_end: str | None = None,
    max_workers: int = 1,
    retries: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Fetch and analyze many instruments, returning ranked rows and errors."""
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    normalized_items = []
    for source_item in items:
        item = dict(source_item)
        item["code"] = str(item.get("code") or "").strip().zfill(6)
        normalized_items.append(item)

    def analyze_item(item: dict[str, Any]) -> dict[str, Any]:
        code = item["code"]
        if not code or code == "000000":
            raise ValueError("缺少证券代码")
        last_error: Exception | None = None
        attempts = max(1, int(retries))
        frame: pd.DataFrame | None = None
        for attempt in range(attempts):
            try:
                frame = fetch_frame(code)
                if frame is None or frame.empty:
                    raise TimeoutError("分钟数据为空或暂不可用")
                break
            except (TimeoutError, ConnectionError, OSError, URLError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.2 * (attempt + 1))
        if frame is None or frame.empty:
            assert last_error is not None
            raise last_error
        analysis = analyze_interval(
            frame,
            start,
            end,
            follow_end=follow_end,
            amount_mode=amount_mode,
        )
        return {**item, **analysis}

    workers = max(1, int(max_workers))
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(normalized_items)))) as pool:
        futures = {pool.submit(analyze_item, item): item for item in normalized_items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                records.append(future.result())
            except Exception as exc:
                errors.append(
                    {
                        "code": item["code"],
                        "name": str(item.get("name") or ""),
                        "error": str(exc),
                    }
                )
    records.sort(key=lambda row: float(row.get("turnover_amount") or 0), reverse=True)
    errors.sort(key=lambda row: row["code"])
    return records, errors


def aggregate_groups(
    records: Iterable[Mapping[str, Any]],
    groups: Mapping[str, Sequence[str]],
) -> list[dict[str, Any]]:
    """Aggregate interval records by explicit group membership.

    Returns one row per non-empty group.  Price and follow-through metrics are
    weighted by interval turnover; overlapping groups are intentionally allowed.
    """
    by_code = {str(row.get("code") or "").zfill(6): dict(row) for row in records if row.get("code")}
    result: list[dict[str, Any]] = []
    for group, codes in groups.items():
        members = [by_code[str(code).zfill(6)] for code in codes if str(code).zfill(6) in by_code]
        if not members:
            continue
        turnover = sum(float(row.get("turnover_amount") or 0) for row in members)
        result.append(
            {
                "group": str(group),
                "count": len(members),
                "codes": [str(row["code"]).zfill(6) for row in members],
                "turnover_amount": turnover,
                "weighted_price_change_pct": _weighted(members, "price_change_pct", "turnover_amount"),
                "weighted_follow_change_pct": _weighted(members, "follow_change_pct", "turnover_amount"),
                "weighted_net_change_pct": _weighted(members, "net_change_pct", "turnover_amount"),
                "weighted_retention_ratio": _weighted(members, "retention_ratio", "turnover_amount"),
            }
        )
    return sorted(result, key=lambda row: row["turnover_amount"], reverse=True)
