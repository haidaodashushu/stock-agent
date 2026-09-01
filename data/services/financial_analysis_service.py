"""On-demand financial analysis for the Web console.

This service is deliberately isolated from screening and trading. It refreshes
requested symbols, builds value snapshots, and optionally asks the configured
agent provider for company-type-aware interpretation.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from data.services.finance_service import FinanceService
from data.agent_runtime import CodexCliProvider
from data.store.sqlite_store import StockStore
from data.value_investing import (
    build_value_snapshot,
    mark_value_freshness,
    upsert_value_snapshot,
)


COMPANY_TYPE_LABELS = {
    "tech_growth": "科技成长",
    "mature_value": "成熟价值",
    "cyclical_manufacturing": "周期制造",
    "theme_speculation": "主题驱动",
    "unknown": "未分类",
}

COMPANY_TYPE_GUIDANCE = {
    "tech_growth": "重点看收入增长、毛利率趋势、研发与产品兑现、现金储备；不要用静态PE机械否定成长。",
    "mature_value": "重点看ROE、现金流、分红能力、负债和估值安全边际。",
    "cyclical_manufacturing": "重点看周期位置、利润正常化、库存和负债；警惕周期顶部的低PE。",
    "theme_speculation": "重点区分基本面兑现与题材预期，关注现金消耗、持续经营和估值透支。",
    "unknown": "先说明分类依据不足，再基于可验证财务和估值数据分析。",
}

AI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "conclusion": {"type": "string"},
                    "financial_view": {"type": "string"},
                    "valuation_view": {"type": "string"},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "focus": {"type": "array", "items": {"type": "string"}},
                    "missing_data": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "code",
                    "conclusion",
                    "financial_view",
                    "valuation_view",
                    "strengths",
                    "risks",
                    "focus",
                    "missing_data",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def normalize_codes(value: str | Iterable[Any], limit: int = 10) -> list[str]:
    if isinstance(value, str):
        raw_codes = re.split(r"[\s,，;；]+", value.strip())
    else:
        raw_codes = [str(item or "").strip() for item in value]
    result: list[str] = []
    seen: set[str] = set()
    invalid: list[str] = []
    for raw in raw_codes:
        if not raw:
            continue
        code = raw.split(".")[0].strip()
        if not re.fullmatch(r"\d{1,6}", code):
            invalid.append(raw)
            continue
        code = code.zfill(6)
        if code not in seen:
            result.append(code)
            seen.add(code)
    if invalid:
        raise ValueError(f"股票代码格式错误: {', '.join(invalid[:3])}")
    if not result:
        raise ValueError("至少输入一个股票代码")
    if len(result) > limit:
        raise ValueError(f"一次最多查询 {limit} 只股票")
    return result


class FinancialAnalysisService:
    def __init__(
        self,
        store: StockStore | None = None,
        finance: FinanceService | None = None,
        ai_runner: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    ):
        self.store = store or StockStore()
        self.finance = finance or FinanceService(store=self.store)
        self.ai_runner = ai_runner or self._run_llm_task

    def analyze(
        self,
        codes: str | Iterable[Any],
        *,
        refresh: bool = True,
        include_ai: bool = True,
    ) -> dict[str, Any]:
        normalized = normalize_codes(codes)
        items: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        for code in normalized:
            warnings: list[str] = []
            refreshed = False
            if refresh:
                factor, refresh_errors = self.finance.refresh_latest_factor(code)
                refreshed = factor is not None
                warnings.extend(refresh_errors)
            try:
                snapshot = build_value_snapshot(code, store=self.store, write_prompt=False)
                upsert_value_snapshot(snapshot, store=self.store)
                self._mark_freshness(snapshot, refreshed)
                item = self._snapshot_item(snapshot)
                item["warnings"] = warnings
                item["refreshed"] = refreshed
                items.append(item)
            except Exception as exc:
                errors.append({"code": code, "message": str(exc)})

        ai_error = ""
        if include_ai and items:
            try:
                ai_payload = self.ai_runner(items)
                advice_by_code = {
                    str(item.get("code", "")).zfill(6): item
                    for item in ai_payload.get("items", [])
                    if isinstance(item, dict)
                }
                for item in items:
                    item["ai_advice"] = advice_by_code.get(item["code"], {})
            except Exception as exc:
                ai_error = str(exc)
                for item in items:
                    item["ai_advice"] = {}

        return {
            "ok": bool(items),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "items": items,
            "errors": errors,
            "ai": {
                "requested": include_ai,
                "ok": bool(include_ai and not ai_error and items),
                "error": ai_error,
                "model": "openai/gpt-5.6-sol" if include_ai else "",
            },
            "scope": "web_query_only",
        }

    def _mark_freshness(self, snapshot, financial_refreshed: bool) -> None:
        valuation = snapshot.facts.get("valuation", {})
        financial = snapshot.facts.get("financial", {})
        mark_value_freshness(
            snapshot.code,
            "value_snapshot",
            status="ok",
            source="financial_analysis_web",
            metadata={
                "company_type": snapshot.company_type,
                "value_label": snapshot.value_label,
                "composite_score": snapshot.composite_score,
                "confidence": snapshot.confidence,
            },
            store=self.store,
        )
        mark_value_freshness(
            snapshot.code,
            "valuation",
            status="ok" if valuation.get("pe") or valuation.get("pb") else "missing",
            source=valuation.get("source", "tencent_quote"),
            success=bool(valuation.get("pe") or valuation.get("pb")),
            metadata=valuation,
            store=self.store,
        )
        mark_value_freshness(
            snapshot.code,
            "financial",
            status="ok" if financial.get("period") else "missing",
            source=financial.get("source", "financial_factors"),
            success=bool(financial.get("period")),
            metadata={**financial, "refreshed_now": financial_refreshed},
            store=self.store,
        )

    @staticmethod
    def _snapshot_item(snapshot) -> dict[str, Any]:
        facts = snapshot.facts
        return {
            "code": snapshot.code,
            "name": snapshot.name,
            "company_type": snapshot.company_type,
            "company_type_label": COMPANY_TYPE_LABELS.get(snapshot.company_type, "未分类"),
            "company_type_guidance": COMPANY_TYPE_GUIDANCE.get(snapshot.company_type, COMPANY_TYPE_GUIDANCE["unknown"]),
            "value_label": snapshot.value_label,
            "watch_pool": bool(snapshot.watch_pool),
            "confidence": snapshot.confidence,
            "rule_summary": snapshot.rule_summary,
            "scores": {
                "business_quality": snapshot.business_quality_score,
                "financial_quality": snapshot.financial_quality_score,
                "growth_credibility": snapshot.growth_credibility_score,
                "valuation_margin": snapshot.valuation_margin_score,
                "trap_risk": snapshot.trap_risk_score,
                "composite": snapshot.composite_score,
            },
            "quote": facts.get("quote", {}),
            "valuation": facts.get("valuation", {}),
            "financial": facts.get("financial", {}),
            "technical": facts.get("technical", {}),
            "concepts": facts.get("concepts", []),
            "data_freshness": facts.get("data_freshness", {}),
        }

    @staticmethod
    def _ai_input(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "code": item["code"],
                "name": item["name"],
                "company_type": item["company_type"],
                "company_type_guidance": item["company_type_guidance"],
                "valuation": item["valuation"],
                "financial": item["financial"],
                "scores": item["scores"],
                "data_freshness": item["data_freshness"],
            }
            for item in items
        ]

    def _run_llm_task(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = (
            "你是A股公司财务分析助手。只分析输入的结构化数据，按每家公司的company_type采用对应口径。"
            "科技成长公司不能因高PE或暂时亏损被机械否定；成熟价值公司重视盈利、负债与估值；"
            "周期制造公司重视周期位置和正常化利润；主题驱动公司重视兑现风险。"
            "明确区分数据缺失与真实负面，不生成买入、卖出、仓位或价格建议，不引用输入之外的事实。"
            "所有表述简洁，每个数组最多3项。"
            "必须严格返回以下结构，items数量和输入stocks数量一致、code保持一致，禁止增加或改名字段："
            '{"items":[{"code":"股票代码","conclusion":"结论","financial_view":"财务解读",'
            '"valuation_view":"估值解读","strengths":["优势"],"risks":["风险"],'
            '"focus":["后续关注"],"missing_data":["缺失数据"]}]}。'
        )
        input_payload = {"stocks": self._ai_input(items)}
        agent_prompt = "\n".join(
            [
                prompt,
                "输入数据：" + json.dumps(input_payload, ensure_ascii=False),
                "输出 JSON Schema：" + json.dumps(AI_SCHEMA, ensure_ascii=False),
                "只输出一个标准 JSON 对象，不要输出 Markdown 代码块或解释。",
            ]
        )
        try:
            with tempfile.TemporaryDirectory(prefix="stock-financial-agent-") as tmp:
                run_dir = Path(tmp)
                schema_path = run_dir / "output-schema.json"
                schema_path.write_text(
                    json.dumps(AI_SCHEMA, ensure_ascii=False), encoding="utf-8",
                )
                outcome = CodexCliProvider(timeout_seconds=120).run(
                    prompt=agent_prompt,
                    workspace=Path(__file__).resolve().parents[2],
                    run_dir=run_dir,
                    output_schema_path=schema_path,
                )
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"Agent AI 分析调用失败: {exc}") from exc
        if outcome.returncode != 0:
            detail = (outcome.stderr or outcome.events).strip()
            raise RuntimeError(f"Agent AI 分析失败: {detail[:500]}")
        parsed = self._extract_llm_json(outcome.final_message)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
            raise RuntimeError("AI返回缺少结构化 items")
        return parsed

    @staticmethod
    def _extract_llm_json(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            details = value.get("details")
            if isinstance(details, dict) and isinstance(details.get("json"), dict):
                return details["json"]
            if isinstance(value.get("json"), dict):
                return value["json"]
            for key in ("result", "data", "output"):
                parsed = FinancialAnalysisService._extract_llm_json(value.get(key))
                if parsed:
                    return parsed
            for block in value.get("content", []) if isinstance(value.get("content"), list) else []:
                parsed = FinancialAnalysisService._extract_llm_json(block)
                if parsed:
                    return parsed
        if isinstance(value, list):
            for item in value:
                parsed = FinancialAnalysisService._extract_llm_json(item)
                if parsed:
                    return parsed
        if isinstance(value, str):
            text = value.strip().removeprefix("```json").removesuffix("```").strip()
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        return None
