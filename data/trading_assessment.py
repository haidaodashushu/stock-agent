"""Auditable trading judgments. Grades are not probabilities or a weighted score."""
from __future__ import annotations

import math


GRADES = {
    "research": ("strong", "moderate", "weak", "unknown", "invalid"),
    "timing": ("ready", "wait", "unknown", "invalid"),
    "evidence": ("reliable", "partial", "insufficient", "conflicting"),
    "portfolio": ("fit", "conditional", "blocked"),
}
FAMILY_PATHS = {
    "price_structure": ("quote.price", "quote.change_pct", "quote.open", "quote.high", "quote.low",
                        "technical.ma", "technical.trend", "technical.above_", "technical.return_", "technical.position_",
                        "intraday.last_", "intraday.pullback_", "intraday.above_vwap", "intraday.vwap", "intraday.half_hour.price_change_pct"),
    "volume": ("intraday.half_hour.volume_ratio", "intraday.half_hour.amount_ratio", "fund_flow.", "quote.volume", "quote.amount", "technical.vol_ratio"),
    "company": ("research.", "selection.fundamental.", "selection.logic_change.", "news."),
    "sector": ("sector.", "policy_evidence."),
}


def text(value, name, limit=240):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} requires 1..{limit} characters")
    return value.strip()


def grade(value, dimension):
    if not isinstance(value, dict) or value.get("grade") not in GRADES[dimension]:
        raise ValueError(f"assessment.{dimension}.grade must be one of {GRADES[dimension]}")
    return {"grade":value["grade"], "reason":text(value.get("reason"), f"assessment.{dimension}.reason")}


def resolve_path(stock, path):
    value = stock
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError(f"confirmation source_path is absent from this snapshot: {path}")
    if value is None or value == "" or value == [] or value == {}:
        raise ValueError(f"confirmation source_path has no evidence: {path}")
    return value


def validate(row, stock, context):
    raw = row.get("assessment")
    if not isinstance(raw, dict):
        raise ValueError("assessment is required for every decision row")
    result = {key:grade(raw.get(key),key) for key in GRADES}
    result["route_reason"] = text(raw.get("route_reason"), "assessment.route_reason")
    result["confidence_reason"] = text(raw.get("confidence_reason"), "assessment.confidence_reason")
    families = raw.get("confirmations", [])
    if not isinstance(families,list) or len(families)>4:
        raise ValueError("assessment.confirmations must contain at most four evidence families")
    seen = set()
    paths = set()
    confirmations = []
    for entry in families:
        if not isinstance(entry,dict):
            raise ValueError("each confirmation must be an object")
        family = entry.get("family")
        if family not in FAMILY_PATHS or family in seen:
            raise ValueError("confirmation family invalid or repeated; correlated indicators count once")
        direction = entry.get("direction")
        if direction not in {"support", "oppose", "mixed"}:
            raise ValueError("confirmation direction must be support, oppose or mixed")
        path = text(entry.get("source_path"), "confirmation.source_path", 160)
        if not any(path.startswith(prefix) for prefix in FAMILY_PATHS[family]):
            raise ValueError("confirmation source_path does not belong to this evidence family")
        resolve_path(stock,path)
        if direction == "support" and path.startswith("fund_flow.") and (stock.get("fund_flow") or {}).get("status") != "available":
            raise ValueError("cached or missing fund flow is background, not current independent confirmation")
        if path in paths:
            raise ValueError("one source_path cannot count as two independent confirmations")
        paths.add(path)
        seen.add(family)
        confirmations.append({"family":family,"direction":direction,"source_path":path,
                              "basis":text(entry.get("basis"),"confirmation.basis")})
    result["confirmations"] = confirmations
    update = row.get("research_update")
    existing = ((stock.get("research") or {}).get("profile") or {}).get("quality")
    if update is not None:
        update["quality"] = result["research"]
    elif existing:
        if result["research"]["grade"] != existing.get("grade"):
            raise ValueError("research grade change requires a versioned research_update")
    elif result["research"]["grade"] != "unknown":
        raise ValueError("unrated research requires research_update before assigning a grade")

    increases_risk = row["action"] in {"buy", "add"}
    if increases_risk:
        if result["research"]["grade"] not in {"strong", "moderate"}:
            raise ValueError("new risk requires supported research; other high grades cannot offset it")
        if result["timing"]["grade"] != "ready":
            raise ValueError("buy/add conflicts with current timing grade")
        if result["evidence"]["grade"] in {"insufficient", "conflicting"}:
            raise ValueError("buy/add conflicts with evidence reliability")
        if result["portfolio"]["grade"] == "blocked":
            raise ValueError("buy/add conflicts with portfolio constraint")
        if row["confidence"] == "strong":
            support = {e["family"] for e in confirmations if e["direction"] == "support"}
            if result["evidence"]["grade"] != "reliable" or "price_structure" not in support or len(support)<2:
                raise ValueError("strong entry confidence requires reliable price structure plus an independent evidence family")
    if (row.get("watch_plan") or {}).get("state") == "account_blocked" and result["portfolio"]["grade"] != "blocked":
        raise ValueError("account_blocked plan must identify the portfolio constraint")
    row["assessment"] = result
    if increases_risk:
        row["position_plan"] = position_plan(row, stock, context)
    if row["action"] in {"sell", "reduce", "clear"}:
        raw_exit = row.get("exit_plan")
        if not isinstance(raw_exit,dict) or raw_exit.get("trigger") not in {"thesis_invalid", "structure_failure", "portfolio_rebalance", "risk_reduction"}:
            raise ValueError("exit_plan requires a concrete thesis, structure, portfolio or risk trigger")
        row["exit_plan"] = {"trigger":raw_exit["trigger"],
                            "reason":text(raw_exit.get("reason"),"exit_plan.reason"),
                            "why_now":text(raw_exit.get("why_now"),"exit_plan.why_now")}


def position_plan(row, stock, context):
    raw = row.get("position_plan")
    if not isinstance(raw,dict):
        raise ValueError("buy/add requires position_plan")
    result = {key:text(raw.get(key), f"position_plan.{key}") for key in
              ("amount_reason","invalidation_basis","risk_budget_reason","concentration_reason")}
    price = (stock.get("quote") or {}).get("price")
    level = raw.get("invalidation_price")
    if level is not None:
        if isinstance(level,bool) or not isinstance(level,(float,int)) or not math.isfinite(level) or level<=0:
            raise ValueError("invalidation_price must be a positive finite number or null")
        if price and level>=float(price):
            raise ValueError("buy invalidation_price must be below the current price")
    result["invalidation_price"] = level
    volume = float(row.get("volume") or 0)
    amount = volume*float(price or 0) if context.get("mode")=="live" and volume>0 else float(row.get("target_amount") or 0)
    if not math.isfinite(amount) or amount<=0:
        raise ValueError("position plan requires a finite positive requested amount")
    # This is a scenario on the requested incremental exposure, not a fill,
    # executable stop, guaranteed loss limit or a new execution-price check.
    result["scenario"] = {"available":False,"basis":"requested_increment_before_lots_fees_or_slippage"}
    if price and level:
        loss = amount*(float(price)-level)/float(price)
        equity = float((context.get("account") or {}).get("total_equity") or 0)
        result["scenario"].update(available=True,requested_notional=round(amount,2),
                                  loss_to_invalidation=round(loss,2),
                                  equity_pct=round(loss/equity*100,3) if math.isfinite(equity) and equity>0 else None)
    return result


def record(store, mode, decision, context):
    from data.opportunity_trial import encode
    facts = {r["code"]:r for r in context["positions"]+context["candidates"]}
    with store._get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS trading_assessments (
          mode TEXT NOT NULL,as_of TEXT NOT NULL,code TEXT NOT NULL,action TEXT NOT NULL,
          research_grade TEXT NOT NULL,timing_grade TEXT NOT NULL,evidence_grade TEXT NOT NULL,
          portfolio_grade TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(mode,as_of,code))""")
        for row in decision.get("signals",decision.get("decisions",[])):
            a = row.get("assessment")
            if not a:
                continue
            conn.execute("INSERT OR IGNORE INTO trading_assessments VALUES(?,?,?,?,?,?,?,?,?)",
                         (mode,context["as_of"],row["code"],row["action"],
                          *(a[k]["grade"] for k in GRADES),encode({
                              "decision":row,"research_revision":(facts[row["code"]].get("research") or {}).get("revision"),
                              "research_facts_version":(facts[row["code"]].get("research") or {}).get("facts_version"),
                              "market_regime":context.get("market_regime"),
                          })))
