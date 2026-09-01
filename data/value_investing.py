"""Value-investing snapshot and rule scoring.

This module builds a conservative facts-first value snapshot. It is designed
as an observation and AI-explanation input, not as a trading trigger.
"""
from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from data.fundamental_llm import get_fundamental_llm_scores
from data.loader import DataLoader
from data.store.sqlite_store import StockStore


ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = ROOT / "data" / "reports" / "value_ai"

TECH_GROWTH_KEYWORDS = (
    "科技", "半导体", "芯片", "算力", "人工智能", "AI", "机器人", "软件",
    "信创", "云计算", "数据", "光模块", "通信", "新能源", "新材料",
    "军工", "航空", "卫星", "创新药", "医疗器械",
)
CYCLE_KEYWORDS = ("钢铁", "煤炭", "有色", "化工", "航运", "猪", "养殖", "地产")
MATURE_VALUE_KEYWORDS = ("银行", "保险", "电力", "公用", "高速", "白酒", "消费")

TIER_PRIORITY = {
    "core": 100,
    "candidate": 70,
    "temp": 45,
    "basic": 20,
}
TIER_RANK = {
    "basic": 1,
    "temp": 2,
    "candidate": 3,
    "core": 4,
}


@dataclass(frozen=True)
class ValueSnapshot:
    code: str
    name: str
    as_of: str
    company_type: str
    value_label: str
    watch_pool: bool
    business_quality_score: float
    financial_quality_score: float
    growth_credibility_score: float
    valuation_margin_score: float
    trap_risk_score: float
    composite_score: float
    confidence: float
    facts: dict[str, Any] = field(default_factory=dict)
    rule_summary: str = ""
    ai_prompt_path: str = ""
    source: str = "value_snapshot.py"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["watch_pool"] = bool(self.watch_pool)
        return data


@dataclass(frozen=True)
class ValueUniverseEntry:
    code: str
    name: str = ""
    tier: str = "candidate"
    priority: int = 50
    reasons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    status: str = "active"
    last_refreshed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_value_snapshot(
    code: str,
    *,
    store: StockStore | None = None,
    loader: DataLoader | None = None,
    as_of: str | None = None,
    write_prompt: bool = True,
) -> ValueSnapshot:
    """Build one value snapshot from verified local data and light quote data."""
    code = str(code).zfill(6)
    store = store or StockStore()
    loader = loader or DataLoader()
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")

    stock = _stock_basic(store, code)
    quote = fetch_tencent_quote_extended(code)
    financial = _latest_financial_factor(store, code)
    llm_score = get_fundamental_llm_scores([code], store=store).get(code)
    news = _latest_news(store, code)
    daily = _daily_technical(loader, code, quote)
    concepts = _concepts(store, code)

    name = quote.get("name") or stock.get("name") or code
    company_type = classify_company_type(stock, concepts, llm_score)
    facts = {
        "stock": stock,
        "quote": quote,
        "valuation": _valuation_facts(quote),
        "financial": financial,
        "fundamental_llm": llm_score.to_extra() if llm_score else {},
        "technical": daily,
        "concepts": concepts,
        "latest_news": news,
        "data_freshness": {
            "as_of": as_of,
            "quote_source": quote.get("source", ""),
            "financial_period": financial.get("period", ""),
            "daily_date": daily.get("daily_date", ""),
            "llm_report_date": llm_score.report_date if llm_score else "",
        },
    }

    scores = score_value_facts(company_type, facts)
    value_label = label_value(scores["valuation_margin_score"], scores["trap_risk_score"])
    watch_pool = bool(scores["composite_score"] >= 60 and scores["trap_risk_score"] < 65)
    confidence = confidence_from_facts(facts)
    summary = summarize_rules(company_type, value_label, scores, facts)

    prompt_path = ""
    snapshot_stub = {
        "code": code,
        "name": name,
        "as_of": as_of,
        "company_type": company_type,
        "value_label": value_label,
        "watch_pool": watch_pool,
        "scores": scores,
        "confidence": confidence,
        "facts": facts,
        "rule_summary": summary,
    }
    if write_prompt:
        prompt_path = write_ai_prompt(snapshot_stub)

    return ValueSnapshot(
        code=code,
        name=name,
        as_of=as_of,
        company_type=company_type,
        value_label=value_label,
        watch_pool=watch_pool,
        business_quality_score=scores["business_quality_score"],
        financial_quality_score=scores["financial_quality_score"],
        growth_credibility_score=scores["growth_credibility_score"],
        valuation_margin_score=scores["valuation_margin_score"],
        trap_risk_score=scores["trap_risk_score"],
        composite_score=scores["composite_score"],
        confidence=confidence,
        facts=facts,
        rule_summary=summary,
        ai_prompt_path=prompt_path,
    )


def upsert_value_universe_entry(
    entry: ValueUniverseEntry | dict[str, Any],
    *,
    store: StockStore | None = None,
) -> None:
    """Insert/update one value-universe entry, preserving the strongest tier."""
    if not isinstance(entry, ValueUniverseEntry):
        entry = ValueUniverseEntry(
            code=str(entry.get("code", "")).zfill(6),
            name=str(entry.get("name", "") or ""),
            tier=str(entry.get("tier", "candidate") or "candidate"),
            priority=int(entry.get("priority", TIER_PRIORITY.get(str(entry.get("tier", "candidate")), 50))),
            reasons=list(entry.get("reasons") or []),
            sources=list(entry.get("sources") or []),
            status=str(entry.get("status", "active") or "active"),
        )

    store = store or StockStore()
    conn = store._get_conn()
    try:
        existing = conn.execute("SELECT * FROM value_universe WHERE code=?", (entry.code,)).fetchone()
        tier = entry.tier
        priority = entry.priority
        reasons = list(entry.reasons)
        sources = list(entry.sources)
        name = entry.name
        if existing:
            old_tier = existing["tier"] or "candidate"
            if TIER_RANK.get(old_tier, 0) > TIER_RANK.get(tier, 0):
                tier = old_tier
            priority = max(priority, int(existing["priority"] or 0))
            reasons = _merge_str_lists(_parse_json_list(existing["reasons"]), reasons)
            sources = _merge_str_lists(_parse_json_list(existing["sources"]), sources)
            name = name or existing["name"] or ""
        conn.execute(
            """INSERT INTO value_universe
               (code, name, tier, priority, reasons, sources, status, last_seen_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'), datetime('now','localtime'))
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name,
                 tier=excluded.tier,
                 priority=excluded.priority,
                 reasons=excluded.reasons,
                 sources=excluded.sources,
                 status=excluded.status,
                 last_seen_at=datetime('now','localtime'),
                 updated_at=datetime('now','localtime')""",
            (
                entry.code,
                name,
                tier,
                priority,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(sources, ensure_ascii=False),
                entry.status,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def build_watch_universe(
    *,
    latest_screen_limit: int = 200,
    extra_codes: Iterable[str] | None = None,
    store: StockStore | None = None,
) -> list[ValueUniverseEntry]:
    """Build the prioritized value universe from current platform state."""
    store = store or StockStore()
    entries: dict[str, ValueUniverseEntry] = {}

    def add(code: Any, *, name: str = "", tier: str, source: str, reason: str, priority: int | None = None) -> None:
        code_s = str(code or "").strip().zfill(6)
        if not code_s or code_s == "000000":
            return
        current = entries.get(code_s)
        next_priority = priority if priority is not None else TIER_PRIORITY.get(tier, 50)
        if current:
            best_tier = tier if TIER_RANK.get(tier, 0) > TIER_RANK.get(current.tier, 0) else current.tier
            entries[code_s] = ValueUniverseEntry(
                code=code_s,
                name=name or current.name,
                tier=best_tier,
                priority=max(current.priority, next_priority),
                reasons=_merge_str_lists(current.reasons, [reason]),
                sources=_merge_str_lists(current.sources, [source]),
            )
        else:
            entries[code_s] = ValueUniverseEntry(
                code=code_s,
                name=name,
                tier=tier,
                priority=next_priority,
                reasons=[reason],
                sources=[source],
            )

    conn = store._get_conn()
    try:
        for row in conn.execute("SELECT code,name FROM portfolio WHERE volume>0 ORDER BY code"):
            add(row["code"], name=row["name"] or "", tier="core", source="portfolio", reason="模拟盘持仓")

        for row in conn.execute(
            """SELECT code,name,action,status FROM live_trade_intents
               WHERE status IN ('proposed','filled')
               ORDER BY created_at DESC LIMIT 50"""
        ):
            add(row["code"], name=row["name"] or "", tier="core", source="live_trade_intents", reason=f"实盘建议单/{row['status']}")

        latest = conn.execute("SELECT MAX(run_date || ' ' || run_time) AS latest FROM screen_records").fetchone()
        latest_key = latest["latest"] if latest else ""
        if latest_key:
            for row in conn.execute(
                """SELECT code,name,score,signal_type FROM screen_records
                   WHERE run_date || ' ' || run_time=?
                   ORDER BY score DESC LIMIT ?""",
                (latest_key, latest_screen_limit),
            ):
                tier = "candidate"
                reason = f"最新盘前候选/{row['signal_type']}"
                add(row["code"], name=row["name"] or "", tier=tier, source="screen_records", reason=reason, priority=70)

        for row in conn.execute(
            """SELECT code,name,composite_score,watch_pool FROM value_snapshots
               ORDER BY created_at DESC LIMIT 500"""
        ):
            tier = "candidate" if int(row["watch_pool"] or 0) else "temp"
            add(row["code"], name=row["name"] or "", tier=tier, source="value_snapshots", reason="已有价值快照")
    finally:
        conn.close()

    for code in extra_codes or []:
        add(code, tier="temp", source="manual", reason="手动/临时查询", priority=55)

    return sorted(entries.values(), key=lambda x: (TIER_RANK.get(x.tier, 0), x.priority, x.code), reverse=True)


def sync_value_universe(
    entries: Iterable[ValueUniverseEntry],
    *,
    store: StockStore | None = None,
) -> int:
    count = 0
    for entry in entries:
        upsert_value_universe_entry(entry, store=store)
        count += 1
    return count


def get_due_value_universe(
    *,
    data_type: str = "value_snapshot",
    max_age_hours: int = 24,
    limit: int = 100,
    tiers: Iterable[str] | None = None,
    store: StockStore | None = None,
) -> list[ValueUniverseEntry]:
    """Return active universe entries whose freshness is missing/stale."""
    store = store or StockStore()
    tier_list = list(tiers or ["core", "candidate", "temp"])
    placeholders = ",".join("?" for _ in tier_list)
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = store._get_conn()
    try:
        rows = conn.execute(
            f"""SELECT vu.*
                FROM value_universe vu
                LEFT JOIN value_data_freshness vf
                  ON vf.code=vu.code AND vf.data_type=?
                WHERE vu.status='active'
                  AND vu.tier IN ({placeholders})
                  AND (
                    vf.code IS NULL
                    OR vf.status IN ('missing','stale','error')
                    OR vf.last_success_at=''
                    OR vf.last_success_at < ?
                  )
                ORDER BY vu.priority DESC, vu.last_refreshed_at ASC, vu.code
                LIMIT ?""",
            [data_type, *tier_list, cutoff, limit],
        ).fetchall()
        return [_row_to_universe_entry(row) for row in rows]
    finally:
        conn.close()


def mark_value_freshness(
    code: str,
    data_type: str,
    *,
    status: str,
    source: str = "",
    error: str = "",
    metadata: dict[str, Any] | None = None,
    success: bool | None = None,
    store: StockStore | None = None,
) -> None:
    """Update data freshness without mutating trading state."""
    store = store or StockStore()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    success = status == "ok" if success is None else success
    last_success_at = now if success else ""
    conn = store._get_conn()
    try:
        if success:
            conn.execute(
                """INSERT INTO value_data_freshness
                   (code, data_type, last_success_at, last_attempt_at, status, source, error, metadata, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code, data_type) DO UPDATE SET
                     last_success_at=excluded.last_success_at,
                     last_attempt_at=excluded.last_attempt_at,
                     status=excluded.status,
                     source=excluded.source,
                     error=excluded.error,
                     metadata=excluded.metadata,
                     updated_at=excluded.updated_at""",
                (
                    str(code).zfill(6),
                    data_type,
                    last_success_at,
                    now,
                    status,
                    source,
                    error,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO value_data_freshness
                   (code, data_type, last_success_at, last_attempt_at, status, source, error, metadata, updated_at)
                   VALUES (?, ?, '', ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(code, data_type) DO UPDATE SET
                     last_attempt_at=excluded.last_attempt_at,
                     status=excluded.status,
                     source=excluded.source,
                     error=excluded.error,
                     metadata=excluded.metadata,
                     updated_at=excluded.updated_at""",
                (
                    str(code).zfill(6),
                    data_type,
                    now,
                    status,
                    source,
                    error,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def mark_universe_refreshed(code: str, *, store: StockStore | None = None) -> None:
    store = store or StockStore()
    conn = store._get_conn()
    try:
        conn.execute(
            """UPDATE value_universe
               SET last_refreshed_at=datetime('now','localtime'),
                   updated_at=datetime('now','localtime')
               WHERE code=?""",
            (str(code).zfill(6),),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_value_snapshot(snapshot: ValueSnapshot, *, store: StockStore | None = None) -> None:
    """Persist a snapshot. This does not affect signals, orders, or portfolio."""
    store = store or StockStore()
    conn = store._get_conn()
    try:
        conn.execute(
            """INSERT INTO value_snapshots
               (code, name, as_of, company_type, value_label, watch_pool,
                business_quality_score, financial_quality_score, growth_credibility_score,
                valuation_margin_score, trap_risk_score, composite_score, confidence,
                facts, rule_summary, ai_prompt_path, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
               ON CONFLICT(code, as_of, source) DO UPDATE SET
                 name=excluded.name,
                 company_type=excluded.company_type,
                 value_label=excluded.value_label,
                 watch_pool=excluded.watch_pool,
                 business_quality_score=excluded.business_quality_score,
                 financial_quality_score=excluded.financial_quality_score,
                 growth_credibility_score=excluded.growth_credibility_score,
                 valuation_margin_score=excluded.valuation_margin_score,
                 trap_risk_score=excluded.trap_risk_score,
                 composite_score=excluded.composite_score,
                 confidence=excluded.confidence,
                 facts=excluded.facts,
                 rule_summary=excluded.rule_summary,
                 ai_prompt_path=excluded.ai_prompt_path,
                 created_at=datetime('now','localtime')""",
            (
                snapshot.code,
                snapshot.name,
                snapshot.as_of,
                snapshot.company_type,
                snapshot.value_label,
                1 if snapshot.watch_pool else 0,
                snapshot.business_quality_score,
                snapshot.financial_quality_score,
                snapshot.growth_credibility_score,
                snapshot.valuation_margin_score,
                snapshot.trap_risk_score,
                snapshot.composite_score,
                snapshot.confidence,
                json.dumps(snapshot.facts, ensure_ascii=False),
                snapshot.rule_summary,
                snapshot.ai_prompt_path,
                snapshot.source,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def collect_latest_screen_codes(limit: int = 30, *, store: StockStore | None = None) -> list[str]:
    store = store or StockStore()
    conn = store._get_conn()
    try:
        latest = conn.execute("SELECT MAX(run_date || ' ' || run_time) AS latest FROM screen_records").fetchone()
        latest_key = latest["latest"] if latest else ""
        if not latest_key:
            return []
        rows = conn.execute(
            """SELECT code FROM screen_records
               WHERE run_date || ' ' || run_time=?
               ORDER BY score DESC LIMIT ?""",
            (latest_key, limit),
        ).fetchall()
        return _dedup(r["code"] for r in rows)
    finally:
        conn.close()


def collect_portfolio_codes(*, store: StockStore | None = None) -> list[str]:
    store = store or StockStore()
    conn = store._get_conn()
    try:
        rows = conn.execute("SELECT code FROM portfolio WHERE volume>0 ORDER BY code").fetchall()
        return _dedup(r["code"] for r in rows)
    finally:
        conn.close()


def _row_to_universe_entry(row: Any) -> ValueUniverseEntry:
    return ValueUniverseEntry(
        code=str(row["code"]).zfill(6),
        name=row["name"] or "",
        tier=row["tier"] or "candidate",
        priority=int(row["priority"] or 50),
        reasons=_parse_json_list(row["reasons"]),
        sources=_parse_json_list(row["sources"]),
        status=row["status"] or "active",
        last_refreshed_at=row["last_refreshed_at"] or "",
    )


def fetch_tencent_quote_extended(code: str) -> dict[str, Any]:
    """Fetch Tencent quote fields used by value snapshots.

    Tencent's q endpoint exposes PE/PB/market-cap fields. Field names are kept
    explicit here and marked as tencent_quote so downstream AI can treat them as
    external valuation facts, not audited financial statements.
    """
    code = str(code).zfill(6)
    symbol = ("sh" if code.startswith("6") else "sz") + code
    url = f"http://qt.gtimg.cn/q={symbol}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="ignore")
        if '="' not in text:
            return {"code": code, "source": "tencent_quote", "error": "empty response"}
        fields = text.split('="', 1)[1].rstrip('";\n').split("~")
    except Exception as exc:
        return {"code": code, "source": "tencent_quote", "error": str(exc)}

    def field(idx: int, default: str = "") -> str:
        return fields[idx] if idx < len(fields) else default

    return {
        "code": code,
        "name": field(1),
        "price": _float(field(3)),
        "prev_close": _float(field(4)),
        "open": _float(field(5)),
        "high": _float(field(33)),
        "low": _float(field(34)),
        "change_pct": _float(field(32)),
        "volume": _float(field(36)),
        "amount": _float(field(57)) * 10000 if _float(field(57)) else _float(field(37)) * 10000,
        "turnover_pct": _float(field(38)),
        "pe": _float(field(39)),
        "pb": _float(field(46)),
        "float_market_cap_yi": _float(field(44)),
        "total_market_cap_yi": _float(field(45)),
        "year_high": _float(field(67)),
        "year_low": _float(field(68)),
        "quote_time": field(30),
        "source": "tencent_quote",
    }


def classify_company_type(stock: dict[str, Any], concepts: list[str], llm_score: Any | None = None) -> str:
    text = " ".join([stock.get("industry", ""), stock.get("name", ""), *concepts])
    if any(k in text for k in TECH_GROWTH_KEYWORDS):
        return "tech_growth"
    if any(k in text for k in CYCLE_KEYWORDS):
        return "cyclical_manufacturing"
    if any(k in text for k in MATURE_VALUE_KEYWORDS):
        return "mature_value"
    if llm_score and (llm_score.future_demand_score >= 4.0 or llm_score.product_penetration_score >= 4.0):
        return "tech_growth"
    return "unknown"


def score_value_facts(company_type: str, facts: dict[str, Any]) -> dict[str, float]:
    financial = facts.get("financial", {})
    valuation = facts.get("valuation", {})
    llm = facts.get("fundamental_llm", {})
    tech = facts.get("technical", {})
    news = facts.get("latest_news", [])

    business = 45.0
    if llm:
        business += (_float(llm.get("composite_score"), 2.5) - 2.5) * 12
        business += (_float(llm.get("confidence"), 0.0)) * 8
    if company_type == "tech_growth":
        business += 6
    elif company_type == "mature_value":
        business += 4

    fin = 45.0
    roe = _float(financial.get("roe"))
    gross = _float(financial.get("gross_margin"))
    net_margin = _float(financial.get("net_margin"))
    debt = _float(financial.get("debt_ratio"))
    if roe:
        fin += min(20, max(-15, (roe - 8) * 1.1))
    if gross:
        fin += min(10, max(-6, (gross - 20) * 0.25))
    if net_margin:
        fin += min(10, max(-8, (net_margin - 8) * 0.5))
    if debt:
        fin -= max(0, debt - 60) * 0.45

    growth = 42.0
    revenue_yoy = _float(financial.get("revenue_yoy"))
    profit_yoy = _float(financial.get("profit_yoy"))
    if revenue_yoy:
        growth += min(20, max(-12, revenue_yoy * 0.45))
    if profit_yoy:
        growth += min(18, max(-15, profit_yoy * 0.35))
    if llm:
        growth += (_float(llm.get("future_demand_score"), 2.5) - 2.5) * 8
        growth += (_float(llm.get("product_penetration_score"), 2.5) - 2.5) * 6

    margin = 45.0
    pe = _float(valuation.get("pe"))
    pb = _float(valuation.get("pb"))
    pos60 = _float(tech.get("position_60d_pct"), 50)
    if company_type == "tech_growth":
        if 0 < pe <= 35:
            margin += 12
        elif pe > 60:
            margin -= 14
        if 0 < pb <= 5:
            margin += 6
        elif pb > 8:
            margin -= 8
    else:
        if 0 < pe <= 15:
            margin += 18
        elif 15 < pe <= 25:
            margin += 8
        elif pe > 35:
            margin -= 15
        if 0 < pb <= 2:
            margin += 10
        elif pb > 5:
            margin -= 10
    if pos60 <= 20:
        margin += 12
    elif pos60 >= 80:
        margin -= 10

    trap = 25.0
    if revenue_yoy < -10:
        trap += 15
    if profit_yoy < -15:
        trap += 18
    if roe and roe < 5:
        trap += 12
    if debt > 70:
        trap += 12
    if any(str(x.get("risk_level", "")).lower() == "high" for x in news if isinstance(x, dict)):
        trap += 12
    if pe <= 0 and valuation:
        trap += 10

    business = _clamp(business)
    fin = _clamp(fin)
    growth = _clamp(growth)
    margin = _clamp(margin)
    trap = _clamp(trap)
    composite = business * 0.25 + fin * 0.25 + growth * 0.25 + margin * 0.20 - trap * 0.15 + 12
    return {
        "business_quality_score": round(business, 1),
        "financial_quality_score": round(fin, 1),
        "growth_credibility_score": round(growth, 1),
        "valuation_margin_score": round(margin, 1),
        "trap_risk_score": round(trap, 1),
        "composite_score": round(_clamp(composite), 1),
    }


def label_value(valuation_margin_score: float, trap_risk_score: float) -> str:
    if trap_risk_score >= 65 and valuation_margin_score >= 55:
        return "deep_value_trap_risk"
    if valuation_margin_score >= 75:
        return "undervalued"
    if valuation_margin_score >= 62:
        return "reasonable_low"
    if valuation_margin_score >= 45:
        return "reasonable"
    if valuation_margin_score >= 32:
        return "reasonable_high"
    return "overvalued"


def confidence_from_facts(facts: dict[str, Any]) -> float:
    score = 0.25
    if facts.get("quote", {}).get("price"):
        score += 0.18
    if facts.get("technical", {}).get("daily_date"):
        score += 0.18
    if facts.get("financial", {}).get("period"):
        score += 0.22
    if facts.get("fundamental_llm"):
        score += 0.10
    if facts.get("valuation", {}).get("pe") or facts.get("valuation", {}).get("pb"):
        score += 0.07
    return round(min(score, 0.95), 2)


def summarize_rules(
    company_type: str,
    value_label: str,
    scores: dict[str, float],
    facts: dict[str, Any],
) -> str:
    quote = facts.get("quote", {})
    valuation = facts.get("valuation", {})
    tech = facts.get("technical", {})
    parts = [
        f"类型={company_type}",
        f"价值状态={value_label}",
        f"综合分={scores['composite_score']}",
        f"PE={valuation.get('pe', 0):.2f}",
        f"PB={valuation.get('pb', 0):.2f}",
        f"60日位置={tech.get('position_60d_pct', 0):.1f}%",
    ]
    if quote.get("price"):
        parts.insert(0, f"价格={quote['price']:.2f}")
    return "；".join(parts)


def write_ai_prompt(snapshot: dict[str, Any], out_dir: Path | None = None) -> str:
    out_dir = out_dir or PROMPT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    code = snapshot["code"]
    as_of = str(snapshot["as_of"]).replace("-", "")
    path = out_dir / f"{code}_{as_of}.value_prompt.json"
    prompt = {
        "task": "只基于 facts 和 rule_scores 判断价值观察池状态；不要编造缺失数据，不要输出投资建议。",
        "required_output_json": {
            "code": code,
            "name": snapshot.get("name", ""),
            "company_type": "mature_value|tech_growth|cyclical_manufacturing|theme_speculation|unknown",
            "value_label": "overvalued|reasonable_high|reasonable|reasonable_low|undervalued|deep_value_trap_risk",
            "watch_pool": False,
            "confidence": 0.0,
            "reason": "只引用 facts 中的事实",
            "buy_conditions": ["价值层只给条件，不触发交易"],
            "risk_flags": ["只引用 facts 中的风险或缺失数据"],
            "missing_data": ["缺失但影响判断的数据"],
        },
        "rule_result": {
            "company_type": snapshot["company_type"],
            "value_label": snapshot["value_label"],
            "watch_pool": snapshot["watch_pool"],
            "scores": snapshot["scores"],
            "confidence": snapshot["confidence"],
            "rule_summary": snapshot["rule_summary"],
        },
        "facts": snapshot["facts"],
    }
    path.write_text(json.dumps(prompt, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _stock_basic(store: StockStore, code: str) -> dict[str, Any]:
    conn = store._get_conn()
    try:
        row = conn.execute(
            "SELECT code,name,exchange,industry,market_cap,list_date,updated_at FROM stocks WHERE code=?",
            (code,),
        ).fetchone()
        return dict(row) if row else {"code": code}
    finally:
        conn.close()


def _latest_financial_factor(store: StockStore, code: str) -> dict[str, Any]:
    conn = store._get_conn()
    try:
        row = conn.execute(
            """SELECT code,period,roe,roa,gross_margin,net_margin,eps,
                      revenue_yoy,profit_yoy,debt_ratio,source,updated_at
               FROM financial_factors
               WHERE code=?
               ORDER BY period DESC, updated_at DESC, id DESC
               LIMIT 1""",
            (code,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _latest_news(store: StockStore, code: str, limit: int = 5) -> list[dict[str, Any]]:
    conn = store._get_conn()
    try:
        rows = conn.execute(
            """SELECT title,source,publish_at,category,sentiment,score,risk_level,tags
               FROM news_events
               WHERE code=?
               ORDER BY publish_at DESC, id DESC LIMIT ?""",
            (code, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _concepts(store: StockStore, code: str) -> list[str]:
    conn = store._get_conn()
    try:
        rows = conn.execute(
            "SELECT name FROM concepts WHERE stocks LIKE ? ORDER BY name",
            (f"%{code}%",),
        ).fetchall()
        return [r["name"] for r in rows]
    finally:
        conn.close()


def _valuation_facts(quote: dict[str, Any]) -> dict[str, Any]:
    return {
        "pe": _float(quote.get("pe")),
        "pb": _float(quote.get("pb")),
        "float_market_cap_yi": _float(quote.get("float_market_cap_yi")),
        "total_market_cap_yi": _float(quote.get("total_market_cap_yi")),
        "turnover_pct": _float(quote.get("turnover_pct")),
        "source": quote.get("source", ""),
    }


def _daily_technical(loader: DataLoader, code: str, quote: dict[str, Any]) -> dict[str, Any]:
    df = loader.get_daily(code)
    if df is None or df.empty:
        return {}
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    price = _float(quote.get("price"))
    today = datetime.now().date()
    if price > 0 and (df["date"].iloc[-1].date() < today):
        row = {
            "date": pd.Timestamp(today),
            "open": _float(quote.get("open"), price),
            "high": _float(quote.get("high"), price),
            "low": _float(quote.get("low"), price),
            "close": price,
            "volume": _float(quote.get("volume")),
            "amount": _float(quote.get("amount")),
        }
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df else close
    low = df["low"].astype(float) if "low" in df else close
    latest = float(close.iloc[-1])
    ma5 = close.rolling(5).mean().iloc[-1] if len(close) >= 5 else 0
    ma10 = close.rolling(10).mean().iloc[-1] if len(close) >= 10 else 0
    ma20 = close.rolling(20).mean().iloc[-1] if len(close) >= 20 else 0
    ma60 = close.rolling(60).mean().iloc[-1] if len(close) >= 60 else 0
    low_60 = float(low.tail(min(len(low), 60)).min())
    high_60 = float(high.tail(min(len(high), 60)).max())
    pos60 = ((latest - low_60) / (high_60 - low_60) * 100) if high_60 > low_60 else 50
    return {
        "daily_date": str(df["date"].iloc[-1].date()),
        "close": round(latest, 2),
        "ma5": round(float(ma5), 2) if not math.isnan(ma5) else 0,
        "ma10": round(float(ma10), 2) if not math.isnan(ma10) else 0,
        "ma20": round(float(ma20), 2) if not math.isnan(ma20) else 0,
        "ma60": round(float(ma60), 2) if not math.isnan(ma60) else 0,
        "above_ma5": bool(ma5 and latest >= ma5),
        "above_ma10": bool(ma10 and latest >= ma10),
        "above_ma20": bool(ma20 and latest >= ma20),
        "position_60d_pct": round(pos60, 1),
        "low_60": round(low_60, 2),
        "high_60": round(high_60, 2),
    }


def _dedup(codes: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen = set()
    for raw in codes:
        code = str(raw or "").strip().zfill(6)
        if not code or code == "000000" or code in seen:
            continue
        out.append(code)
        seen.add(code)
    return out


def _parse_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        pass
    return [str(value)]


def _merge_str_lists(left: Iterable[Any], right: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen = set()
    for raw in [*left, *right]:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        result = float(str(value).replace("%", ""))
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))
