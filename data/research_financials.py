"""Slow company inputs: shared daily cache, Fuyao first, IwenCai fills gaps.

Reads never call the network. Refreshes are capped, cached for a day and failures
back off for an hour. Metrics keep their report period and missing values.
"""
from datetime import datetime, timedelta
import json
import math
import re

FIELDS = {
    "roe": ("加权净资产收益率", "净资产收益率"),
    "roa": ("总资产收益率",), "gross_margin": ("销售毛利率", "毛利率"),
    "net_margin": ("销售净利率", "净利率"), "eps": ("基本每股收益", "每股收益"),
    "revenue_yoy": ("营业收入同比增长率",),
    "profit_yoy": ("归母净利润同比增长率", "净利润同比增长率"),
    "debt_ratio": ("资产负债率",),
    "operating_cash_flow": ("经营活动产生的现金流量净额",),
}


FUYAO_FIELDS = {
    "roe": ("index_weighted_avg_roe",), "roa": ("total_assets_net_ratio",),
    "gross_margin": ("sale_gross_margin",), "net_margin": ("sale_net_interest_ratio",),
    "debt_ratio": ("assets_debt_ratio",),
    "revenue_yoy": ("operating_income_yoy_growth_ratio", "calculate_operating_income_yoy_growth_ratio"),
    "profit_yoy": ("calculate_parent_holder_net_profit_yoy_growth_ratio",),
}


def finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def statement_period(row, now):
    # Publication date and accounting period are distinct; never use a future report.
    from zoneinfo import ZoneInfo
    try:
        published = datetime.fromtimestamp(float(row["report_date_ms"]) / 1000, ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        end = datetime.fromtimestamp(float(row["period_end_ms"]) / 1000, ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        if published > now or end > now or row.get("currency") != "CNY":
            return ""
        return period_date(end.strftime("%Y%m%d"))
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return ""


def period_date(value):
    value = str(value or "").replace("-", "").replace("/", "")
    if re.fullmatch(r"\d{4}(Q[1234]|A)", value):
        value = value[:4] + {"Q1":"0331", "Q2":"0630", "Q3":"0930", "Q4":"1231", "A":"1231"}[value[4:]]
    try:
        return datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d") if len(value) == 8 else ""
    except ValueError:
        return ""


def parse_financial(item, code):
    if str(item.get("股票代码", "")).split(".")[0] != code:
        raise ValueError("financial evidence code mismatch")
    periods = [period_date(p) for key in item for p in re.findall(r"\[(\d{8})\]", key)
               if any(alias in key for aliases in FIELDS.values() for alias in aliases)]
    period = max((p for p in periods if p), default="")
    values = {}
    for field, aliases in FIELDS.items():
        values[field] = None
        for alias in aliases:
            # Only explicitly dated fields are assigned to this report period.
            key = f"{alias}[{period}]"
            value = item.get(key)
            if isinstance(value, bool):
                continue
            try:
                number = float(str(value).replace("%", ""))
                if math.isfinite(number):
                    values[field] = number
                    break
            except (ValueError, TypeError):
                pass
    return {"code": code, "period": period, "source": "iwencai", **values}


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS research_company_inputs (
      code TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL,
      retry_after TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')""")


def latest_financial(conn, code, as_of):
    rows = conn.execute("SELECT * FROM financial_factors WHERE code=? AND updated_at<=?", (code,as_of)).fetchall()
    rows = [dict(row) for row in rows if period_date(row["period"]) and period_date(row["period"]) <= as_of[:10].replace("-", "")]
    result = max(rows, key=lambda row:(period_date(row["period"]),row["updated_at"]), default={})
    if result:
        result["period"] = period_date(result["period"])
        # Historical IwenCai parser used zero for missing fields. Its old zeros
        # cannot safely be interpreted as actual reported zeros.
        if result.get("source") == "iwencai_intelligence":
            for field in FIELDS:
                if result.get(field) == 0:
                    result[field] = None
            result["legacy_missing_values_uncertain"] = True
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_company_inputs'").fetchone()
    cached = conn.execute("SELECT * FROM research_company_inputs WHERE code=? AND fetched_at<=?",(code,as_of)).fetchone() if exists else None
    if cached:
        payload = json.loads(cached["payload"])
        if payload.get("period") and result.get("period", "") <= payload["period"] <= as_of[:10].replace("-", ""):
            result = {**payload, "updated_at": cached["fetched_at"]}
        result["refresh_error"] = cached["error"]
        result["evidence_stale"] = as_of > (datetime.fromisoformat(cached["fetched_at"])+timedelta(days=1)).isoformat(sep=" ")
    return result


def refresh_financials(store, codes, now=None, limit=3):
    from data.adapters.iwencai_client import IwenCaiClient
    from data.adapters.fuyao_adapter import FuyaoAdapter
    from data.services.finance_service import FinanceService
    now = now or datetime.now()
    stamp = now.isoformat(sep=" ", timespec="seconds")
    summary = {"requested":0, "available":0, "errors":{}}
    with store._get_conn() as conn:
        ensure_table(conn)
        states = {r["code"]:dict(r) for r in conn.execute("SELECT * FROM research_company_inputs")}
    due = [c for c in dict.fromkeys(codes) if states.get(c,{}).get("retry_after", "") <= stamp]
    due.sort(key=lambda c: states.get(c,{}).get("fetched_at", ""))
    for code in due[:limit]:
        # An account may be refreshing the same company concurrently. Reserve
        # the slot before network I/O; interrupted workers release it by expiry.
        with store._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM research_company_inputs WHERE code=?",(code,)).fetchone()
            if current and current["retry_after"] > stamp:
                continue
            lease = (now+timedelta(minutes=15)).isoformat(sep=" ",timespec="seconds")
            conn.execute("INSERT OR REPLACE INTO research_company_inputs VALUES(?,?,?,?,?)",
                (code,current["payload"] if current else "{}",current["fetched_at"] if current else stamp,lease,current["error"] if current else ""))
        summary["requested"] += 1
        errors = []
        evidence = {"code":code, "period":""}
        adapter = FuyaoAdapter()
        year, quarter = FinanceService.recent_periods(now)[0]
        report = f"{year}-{quarter}"
        income = []
        try:
            income = adapter.statement(code, "income")
            published = [row for row in income if statement_period(row, now)]
            if published:
                latest = max(published, key=lambda row: statement_period(row, now))
                period = statement_period(latest, now)
                report = period[:4] + "-" + str(int(period[4:6]) // 3)
        except Exception as exc:
            errors.append(str(exc))
        period = period_date(report[:4] + "Q" + report[-1])
        evidence = {"code": code, "period": period, "source": "fuyao", "field_sources": {}}
        try:
            primary = adapter.financials(code, report)
            if primary:
                evidence["supplement"] = primary
                for field, names in FUYAO_FIELDS.items():
                    for name in names:
                        value = finite(primary["indicators"].get(name))
                        if value is not None:
                            evidence[field] = value
                            evidence["field_sources"][field] = f"fuyao:{name}"
                            break
        except Exception as exc:
            errors.append(str(exc))
        for kind, field, key in (("income", "eps", "basic_eps"),
                                  ("cash-flow", "operating_cash_flow", "act_cash_flow_net")):
            try:
                rows = income if kind == "income" else adapter.statement(code, kind)
                for row in rows:
                    if statement_period(row, now) == period:
                        value = finite(row.get(key))
                        if value is not None:
                            evidence[field] = value
                            evidence["field_sources"][field] = f"fuyao:{key}"
                            break
            except Exception as exc:
                errors.append(str(exc))
        if any(evidence.get(field) is None for field in FIELDS):
            try:
                raw = IwenCaiClient(timeout=12).query2data(
                    f"{code} 最新财报 净资产收益率 总资产收益率 毛利率 净利率 每股收益 营业收入同比增长率 净利润同比增长率 资产负债率 经营现金流",
                    skill_id="hithink-finance-query", limit=1)
                matching = [r for r in raw.get("datas", []) if str(r.get("股票代码", "")).split(".")[0] == code]
                fallback = parse_financial(matching[0], code) if matching else {}
                fallback_period = fallback.get("period", "")
                has_primary = any(evidence.get(field) is not None for field in FIELDS)
                if fallback_period and fallback_period <= now.strftime("%Y%m%d"):
                    if not has_primary or fallback_period > period:
                        evidence = {**fallback, "field_sources": {
                            field: "iwencai" for field in FIELDS if fallback.get(field) is not None}}
                    elif fallback_period == period:
                        for field in FIELDS:
                            if evidence.get(field) is None and fallback.get(field) is not None:
                                evidence[field] = fallback[field]
                                evidence["field_sources"][field] = "iwencai"
                        if "iwencai" in evidence["field_sources"].values():
                            evidence["source"] = "fuyao+iwencai"
                else:
                    errors.append("iwencai report period unavailable")
            except Exception:
                errors.append("iwencai financial request failed")
        available = bool(evidence.get("period")) and (any(evidence.get(f) is not None for f in FIELDS) or bool(evidence.get("supplement")))
        if not available:
            errors.append("financial evidence unavailable")
            evidence = json.loads(states.get(code,{}).get("payload", "{}"))
        # Do not advance an old observation's timestamp on a failed refresh.
        fetched = stamp if available else states.get(code,{}).get("fetched_at", stamp)
        retry = now + (timedelta(days=1) if available else timedelta(hours=1))
        with store._get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO research_company_inputs VALUES(?,?,?,?,?)",
                         (code,json.dumps(evidence,ensure_ascii=False),fetched,retry.isoformat(sep=" ",timespec="seconds"),"; ".join(errors)))
            if available:
                factor_fields = [field for field in FIELDS if field != "operating_cash_flow"]
                conn.execute(
                    "INSERT OR REPLACE INTO financial_factors(code,period," + ",".join(factor_fields) + ",source,updated_at) VALUES(" + ",".join("?" for _ in range(len(factor_fields)+4)) + ")",
                    [code, FinanceService.normalize_period(evidence["period"]), *[evidence.get(field) for field in factor_fields], evidence.get("source", "fuyao"), fetched])
        summary["available"] += int(available)
        if errors: summary["errors"][code] = errors
    summary["deferred"] = max(0,len(due)-limit)
    return summary
