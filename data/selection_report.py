"""Read the latest durable AI stock-selection report for presentation clients."""
from __future__ import annotations

import json
from typing import Any

from data.store.sqlite_store import StockStore


def _object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def latest_selection_report(store: StockStore) -> dict[str, Any] | None:
    """Return the latest completed selection result, falling back to screen rows."""
    conn = store._get_conn()
    try:
        submission = conn.execute(
            """SELECT result FROM agent_decision_submissions
                 WHERE task='selection' AND status='ready' AND result <> ''
                 ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        if submission:
            report = _object(submission["result"])
            if report.get("profile") == "screening_report":
                report["summary"] = str(report.get("summary") or "").replace("。；", "；")
                return report

        latest = conn.execute(
            """SELECT run_date,run_time FROM screen_records
                 ORDER BY run_date DESC,run_time DESC LIMIT 1"""
        ).fetchone()
        if not latest:
            return None
        rows = conn.execute(
            """SELECT * FROM screen_records WHERE run_date=? AND run_time=?
                 ORDER BY score DESC LIMIT 10""",
            (latest["run_date"], latest["run_time"]),
        ).fetchall()
    finally:
        conn.close()

    candidates: list[dict[str, Any]] = []
    run_label = "股票预选"
    generated_at = ""
    target = ""
    for rank, row in enumerate(rows, 1):
        extra = _object(row["extra"])
        ai = extra.get("ai_selection") if isinstance(extra.get("ai_selection"), dict) else {}
        run_label = str(extra.get("run_label") or run_label)
        generated_at = str(extra.get("generated_at") or generated_at)
        target = str(extra.get("target") or target)
        candidates.append({
            "code": str(row["code"] or "").zfill(6),
            "name": str(row["name"] or ""),
            "score": round(float(row["score"] or 0), 1),
            "signal_type": str(row["signal_type"] or "watch"),
            "trend": str(row["trend"] or ""),
            "pct_change": round(float(row["pct_change"] or 0), 2),
            "vol_ratio": round(float(row["vol_ratio"] or 0), 2),
            "tags": [item for item in str(row["strategies"] or "").split("|") if item][:5],
            "logic_level": str((_object(extra.get("logic_change"))).get("level") or ""),
            "ai_rank": ai.get("rank", rank),
            "ai_confidence": str(ai.get("confidence") or ""),
            "ai_reason": str(ai.get("reason") or ""),
            "ai_risk": str(ai.get("risk") or ""),
        })
    buy_count = sum(item["signal_type"] == "buy" for item in candidates)
    return {
        "profile": "screening_report",
        "title": f"{run_label} {latest['run_date']}",
        "tone": "success",
        "summary": f"{run_label}完成，本轮候选由量化证据与 AI 综合筛选生成。",
        "run": {
            "label": run_label,
            "target": target,
            "run_date": latest["run_date"],
            "generated_at": generated_at,
            "count": len(candidates),
            "buy_count": buy_count,
            "watch_count": len(candidates) - buy_count,
            "tech_count": 0,
            "strong_logic_count": sum(item["logic_level"] == "strong" for item in candidates),
            "mainline_text": "无明显集中主线",
        },
        "candidates": candidates,
        "warnings": ["预选结果不等于交易指令"],
        "source": "TechnicalScoringSelector + stock-selection MCP + agent final selection",
    }
