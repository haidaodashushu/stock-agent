"""Conservative structured-financial factor used during candidate enrichment."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from data.store.sqlite_store import StockStore


@dataclass(frozen=True)
class FinancialScore:
    code: str
    period: str = ""
    boost: float = 0.0
    tags: tuple[str, ...] = ()
    source: str = ""


def get_financial_scores(
    codes: Iterable[str],
    *,
    store: StockStore | None = None,
) -> dict[str, FinancialScore]:
    normalized = [str(code).zfill(6) for code in codes if str(code or "").strip()]
    if not normalized:
        return {}

    placeholders = ",".join("?" for _ in normalized)
    store = store or StockStore()
    conn = store._get_conn()
    try:
        rows = conn.execute(
            f"""SELECT code,period,roe,revenue_yoy,profit_yoy,debt_ratio,source,updated_at,id
                FROM financial_factors
                WHERE code IN ({placeholders})
                ORDER BY code,period DESC,updated_at DESC,id DESC""",
            normalized,
        ).fetchall()
    finally:
        conn.close()

    result: dict[str, FinancialScore] = {}
    for row in rows:
        code = str(row["code"]).zfill(6)
        if code in result:
            continue
        result[code] = _score_row(row)
    return result


def _score_row(row) -> FinancialScore:
    profit_yoy = float(row["profit_yoy"] or 0)
    revenue_yoy = float(row["revenue_yoy"] or 0)
    roe = float(row["roe"] or 0)
    debt_ratio = float(row["debt_ratio"] or 0)
    boost = 0.0
    tags: list[str] = []

    if profit_yoy >= 50:
        boost += 1.0
        tags.append("利润高增")
    elif profit_yoy >= 20:
        boost += 0.6
        tags.append("利润增长")
    elif profit_yoy <= -30:
        boost -= 1.0
        tags.append("利润下滑")

    if revenue_yoy >= 30:
        boost += 0.5
        tags.append("营收高增")
    elif revenue_yoy <= -20:
        boost -= 0.5
        tags.append("营收下滑")

    if roe >= 15:
        boost += 0.4
        tags.append("ROE较好")
    elif roe < 0:
        boost -= 0.4
        tags.append("ROE为负")

    if debt_ratio >= 80:
        boost -= 0.4
        tags.append("高负债")

    return FinancialScore(
        code=str(row["code"]).zfill(6),
        period=str(row["period"] or ""),
        boost=round(max(-1.5, min(1.5, boost)), 2),
        tags=tuple(tags),
        source=str(row["source"] or ""),
    )
