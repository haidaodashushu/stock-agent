"""LLM financial-report / MD&A scoring factor.

This module stores and reads medium-term fundamental evidence extracted from
annual/semiannual reports. It is an input to candidate ranking, not an intraday
execution trigger.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Mapping, Any

from data.store.sqlite_store import StockStore


@dataclass(frozen=True)
class FundamentalLLMScore:
    code: str
    name: str = ""
    period: str = ""
    report_type: str = ""
    report_date: str = ""
    industry_demand_score: float = 2.5
    future_demand_score: float = 2.5
    product_penetration_score: float = 2.5
    strategy_score: float = 1.5
    candor_score: float = 0.5
    composite_score: float = 2.5
    confidence: float = 0.0
    summary: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    model: str = ""

    @property
    def boost(self) -> float:
        """Conservative selection boost, capped so technical confirmation still rules."""
        score = self.composite_score
        if score >= 4.5:
            boost = 1.5
        elif score >= 4.0:
            boost = 1.0
        elif score >= 3.5:
            boost = 0.6
        elif score >= 3.0:
            boost = 0.2
        elif score <= 2.0:
            boost = -1.0
        elif score <= 2.5:
            boost = -0.4
        else:
            boost = 0.0

        if self.future_demand_score <= 2.0:
            boost -= 0.6
        if self.candor_score <= 0:
            boost -= 0.4

        confidence = self.confidence
        if 0 < confidence < 0.5:
            boost *= 0.5
        return round(max(-1.5, min(1.5, boost)), 2)

    @property
    def tags(self) -> list[str]:
        tags: list[str] = []
        if self.boost >= 1.0:
            tags.append("LLM好赛道")
        elif self.boost > 0:
            tags.append("LLM赛道加分")
        elif self.boost < 0:
            tags.append("LLM基本面扣分")
        if self.industry_demand_score >= 4.0:
            tags.append("行业需求上行")
        if self.future_demand_score <= 2.0:
            tags.append("未来需求转弱")
        if self.candor_score <= 0:
            tags.append("坦诚度低")
        return tags

    def to_extra(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "report_type": self.report_type,
            "report_date": self.report_date,
            "composite_score": self.composite_score,
            "boost": self.boost,
            "industry_demand_score": self.industry_demand_score,
            "future_demand_score": self.future_demand_score,
            "strategy_score": self.strategy_score,
            "candor_score": self.candor_score,
            "confidence": self.confidence,
            "summary": self.summary,
            "source": self.source,
            "model": self.model,
        }


def get_fundamental_llm_scores(
    codes: Iterable[str],
    *,
    max_age_days: int = 550,
    store: StockStore | None = None,
) -> dict[str, FundamentalLLMScore]:
    """Return latest available LLM report score for each code."""
    normalized = [str(c).zfill(6) for c in codes if str(c or "").strip()]
    if not normalized:
        return {}

    cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    placeholders = ",".join("?" for _ in normalized)
    sql = f"""
        SELECT *
        FROM fundamental_llm_scores
        WHERE code IN ({placeholders})
          AND (report_date='' OR report_date>=?)
        ORDER BY code, report_date DESC, period DESC, updated_at DESC, id DESC
    """
    store = store or StockStore()
    conn = store._get_conn()
    try:
        rows = conn.execute(sql, [*normalized, cutoff]).fetchall()
    finally:
        conn.close()

    result: dict[str, FundamentalLLMScore] = {}
    for row in rows:
        code = str(row["code"]).zfill(6)
        if code in result:
            continue
        result[code] = _row_to_score(row)
    return result


def upsert_fundamental_llm_score(
    score: FundamentalLLMScore | Mapping[str, Any],
    *,
    store: StockStore | None = None,
) -> None:
    """Insert or update one parsed financial-report score."""
    if not isinstance(score, FundamentalLLMScore):
        score = score_from_mapping(score)

    store = store or StockStore()
    conn = store._get_conn()
    try:
        conn.execute(
            """INSERT INTO fundamental_llm_scores
               (code, name, period, report_type, report_date,
                industry_demand_score, future_demand_score, product_penetration_score,
                strategy_score, candor_score, composite_score, confidence,
                summary, evidence, source, model, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
               ON CONFLICT(code, period, source, model) DO UPDATE SET
                 name=excluded.name,
                 report_type=excluded.report_type,
                 report_date=excluded.report_date,
                 industry_demand_score=excluded.industry_demand_score,
                 future_demand_score=excluded.future_demand_score,
                 product_penetration_score=excluded.product_penetration_score,
                 strategy_score=excluded.strategy_score,
                 candor_score=excluded.candor_score,
                 composite_score=excluded.composite_score,
                 confidence=excluded.confidence,
                 summary=excluded.summary,
                 evidence=excluded.evidence,
                 updated_at=datetime('now','localtime')""",
            (
                score.code, score.name, score.period, score.report_type, score.report_date,
                score.industry_demand_score, score.future_demand_score,
                score.product_penetration_score, score.strategy_score,
                score.candor_score, score.composite_score, score.confidence,
                score.summary, json.dumps(score.evidence, ensure_ascii=False),
                score.source, score.model,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def score_from_mapping(raw: Mapping[str, Any]) -> FundamentalLLMScore:
    composite = raw.get("composite_score")
    if composite in (None, ""):
        composite = _default_composite(raw)
    return FundamentalLLMScore(
        code=str(raw.get("code", "")).zfill(6),
        name=str(raw.get("name", "") or ""),
        period=str(raw.get("period", "") or ""),
        report_type=str(raw.get("report_type", "") or ""),
        report_date=str(raw.get("report_date", "") or ""),
        industry_demand_score=_float(raw.get("industry_demand_score"), 2.5),
        future_demand_score=_float(raw.get("future_demand_score"), 2.5),
        product_penetration_score=_float(raw.get("product_penetration_score"), 2.5),
        strategy_score=_float(raw.get("strategy_score"), 1.5),
        candor_score=_float(raw.get("candor_score"), 0.5),
        composite_score=_float(composite, 2.5),
        confidence=_float(raw.get("confidence"), 0.0),
        summary=str(raw.get("summary", "") or ""),
        evidence=_parse_evidence(raw.get("evidence")),
        source=str(raw.get("source", "") or ""),
        model=str(raw.get("model", "") or ""),
    )


def _row_to_score(row) -> FundamentalLLMScore:
    return FundamentalLLMScore(
        code=str(row["code"]).zfill(6),
        name=row["name"] or "",
        period=row["period"] or "",
        report_type=row["report_type"] or "",
        report_date=row["report_date"] or "",
        industry_demand_score=_float(row["industry_demand_score"], 2.5),
        future_demand_score=_float(row["future_demand_score"], 2.5),
        product_penetration_score=_float(row["product_penetration_score"], 2.5),
        strategy_score=_float(row["strategy_score"], 1.5),
        candor_score=_float(row["candor_score"], 0.5),
        composite_score=_float(row["composite_score"], 2.5),
        confidence=_float(row["confidence"], 0.0),
        summary=row["summary"] or "",
        evidence=_parse_evidence(row["evidence"]),
        source=row["source"] or "",
        model=row["model"] or "",
    )


def _default_composite(raw: Mapping[str, Any]) -> float:
    industry = _float(raw.get("industry_demand_score"), 2.5)
    future = _float(raw.get("future_demand_score"), 2.5)
    penetration = _float(raw.get("product_penetration_score"), 2.5)
    strategy = _float(raw.get("strategy_score"), 1.5) / 3.0 * 5.0
    candor = _float(raw.get("candor_score"), 0.5) * 5.0
    score = industry * 0.35 + future * 0.25 + penetration * 0.15 + strategy * 0.15 + candor * 0.10
    return round(max(0.0, min(5.0, score)), 2)


def _float(value: Any, default: float) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_evidence(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {"items": parsed}
    except Exception:
        return {"raw": str(value)}
