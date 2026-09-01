"""Corporate action risk helpers for stock screening.

Currently covers near-term A-share ex-dividend/ex-right events from EastMoney.
The selector uses this as a short-term trading friction signal, not as a
fundamental negative signal.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from data.market_calendar import is_market_open

logger = logging.getLogger(__name__)

EASTMONEY_SHARE_BONUS_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


@dataclass(frozen=True)
class CorporateActionRisk:
    code: str
    ex_date: str
    record_date: str
    plan: str
    cash_per_share: float
    penalty: float
    tag: str


def next_market_day(value: date | datetime | str | None = None) -> date:
    """Return the next open market day after *value*."""
    if value is None:
        d = date.today()
    elif isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    else:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()

    cur = d + timedelta(days=1)
    for _ in range(14):
        if is_market_open(cur):
            return cur
        cur += timedelta(days=1)
    return d + timedelta(days=1)


def get_next_day_corporate_action_risks(
    codes: Iterable[str],
    *,
    as_of: date | datetime | str | None = None,
    timeout: float = 6.0,
) -> dict[str, CorporateActionRisk]:
    """Fetch ex-dividend/ex-right events whose ex-date is the next market day.

    This is intentionally scoped to a small candidate list. It should be called
    after the technical selector has narrowed the universe.
    """
    normalized = [str(c).zfill(6) for c in codes if str(c or "").strip()]
    if not normalized:
        return {}

    target = next_market_day(as_of).isoformat()
    requested = set(normalized)
    risks: dict[str, CorporateActionRisk] = {}
    try:
        rows = _fetch_share_bonus_by_ex_date(target, timeout=timeout)
    except Exception as exc:
        logger.warning("次日除权除息批量查询跳过: %s", exc)
        return {}

    for row in rows:
        code = str(row.get("SECURITY_CODE") or "").zfill(6)
        if code not in requested:
            continue
        ex_date = str(row.get("EX_DIVIDEND_DATE") or "")[:10]
        if ex_date != target:
            continue

        cash_per_10 = _safe_float(row.get("PRETAX_BONUS_RMB"))
        cash_per_share = round(cash_per_10 / 10.0, 4) if cash_per_10 else 0.0
        bonus_ratio = _safe_float(row.get("BONUS_RATIO"))
        transfer_ratio = _safe_float(row.get("IT_RATIO"))
        plan = str(row.get("IMPL_PLAN_PROFILE") or "")
        penalty = _compute_penalty(cash_per_share, bonus_ratio, transfer_ratio)
        tag = f"次日除权除息-{plan}" if plan else "次日除权除息"

        risks[code] = CorporateActionRisk(
            code=code,
            ex_date=ex_date,
            record_date=str(row.get("EQUITY_RECORD_DATE") or "")[:10],
            plan=plan,
            cash_per_share=cash_per_share,
            penalty=penalty,
            tag=tag,
        )

    return risks


def _fetch_share_bonus_by_ex_date(ex_date: str, *, timeout: float) -> list[dict]:
    """Fetch the market-wide ex-date list once, then filter locally by code."""
    params = {
        "sortColumns": "SECURITY_CODE",
        "sortTypes": "1",
        "pageSize": "500",
        "pageNumber": "1",
        "reportName": "RPT_SHAREBONUS_DET",
        "columns": "ALL",
        "filter": f"(EX_DIVIDEND_DATE='{ex_date}')",
    }
    url = EASTMONEY_SHARE_BONUS_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.eastmoney.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8-sig"))
    return list((payload.get("result") or {}).get("data") or [])


def _safe_float(value) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _compute_penalty(cash_per_share: float, bonus_ratio: float, transfer_ratio: float) -> float:
    """Small rank penalty for short-term ex-dividend/ex-right disturbance."""
    if bonus_ratio or transfer_ratio:
        return -1.0
    if cash_per_share >= 0.30:
        return -0.8
    if cash_per_share >= 0.10:
        return -0.5
    return -0.3
