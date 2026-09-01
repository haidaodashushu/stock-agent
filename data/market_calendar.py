"""A股交易日历守门。

规则：
- 周六日默认休市；调休/特殊交易日可写入 config/market_calendar.json 的 trading_days。
- 节假日/临时休市写入 holidays。
- 关键交易脚本启动时调用 ensure_market_open()，休市则打印原因并退出。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
CAL_PATH = ROOT / "config" / "market_calendar.json"


@dataclass(frozen=True)
class MarketDay:
    date: str
    is_open: bool
    reason: str = ""


def _load_calendar() -> dict:
    if not CAL_PATH.exists():
        return {"holidays": {}, "trading_days": {}}
    with CAL_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _to_date(value: Optional[str | date | datetime] = None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def market_day(value: Optional[str | date | datetime] = None) -> MarketDay:
    d = _to_date(value)
    ds = d.isoformat()
    cal = _load_calendar()
    holidays = cal.get("holidays") or {}
    trading_days = cal.get("trading_days") or {}

    if ds in holidays:
        return MarketDay(ds, False, str(holidays[ds] or "节假日/休市"))
    if ds in trading_days:
        return MarketDay(ds, True, str(trading_days[ds] or "特殊交易日"))
    if d.weekday() >= 5:
        return MarketDay(ds, False, "周末休市")
    return MarketDay(ds, True, "交易日")


def is_market_open(value: Optional[str | date | datetime] = None) -> bool:
    return market_day(value).is_open


def ensure_market_open(value: Optional[str | date | datetime] = None, *, task: str = "交易任务") -> bool:
    md = market_day(value)
    if md.is_open:
        return True
    print(f"休市跳过：{task} {md.date} {md.reason}")
    return False


def is_actionable_trading_time(value: Optional[datetime] = None) -> bool:
    """Whether orders may be created for the regular A-share session.

    The end boundaries are deliberately exclusive: a model that finishes at
    11:30 or 15:00 must not create a simulated fill or a live suggestion after
    the exchange has stopped accepting regular-session orders.
    """
    now = value or datetime.now()
    if not market_day(now).is_open:
        return False
    minute = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minute < 11 * 60 + 30 or 13 * 60 <= minute < 15 * 60


def ensure_actionable_trading_time(
    value: Optional[datetime] = None, *, task: str = "交易任务",
) -> bool:
    now = value or datetime.now()
    md = market_day(now)
    if not md.is_open:
        print(f"休市跳过：{task} {md.date} {md.reason}")
        return False
    if is_actionable_trading_time(now):
        return True
    print(f"非交易时段跳过：{task} {now.strftime('%Y-%m-%d %H:%M:%S')}")
    return False
