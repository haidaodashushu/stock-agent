"""Account-free intraday candidate promotion.

Opening-auction and radar rows are immutable discovery facts.  This module
exposes those facts to a read-only AI evaluator, validates one conclusion per
dynamic stock, and persists same-day candidate eligibility.  It never reads an
account and never creates a trade or advice intent.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any

from data.candidate_observations import (
    latest_auction_watch_candidates,
    latest_radar_candidates,
    load_daily_setups,
)
from data.store.sqlite_store import StockStore
from data.market_regime import classify_market_regime


PROMOTION_DECISIONS = {"promote", "watch", "reject"}
ENTRY_ROUTES = {"early_start", "strong_continuation"}
CONFIDENCES = {"strong", "medium", "weak"}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return default if math.isnan(number) or math.isinf(number) else number
    except (TypeError, ValueError):
        return default


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, type(fallback)):
        return value
    if not value:
        return fallback
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def _active_promoted_codes(store: StockStore, trade_date: str, stamp: str) -> set[str]:
    conn = store._get_conn()
    try:
        rows = conn.execute(
            """SELECT code FROM intraday_candidate_promotions
                WHERE trade_date=? AND status='active' AND expires_at>=?""",
            (trade_date, stamp),
        ).fetchall()
    finally:
        conn.close()
    return {str(row["code"]).zfill(6) for row in rows}


def _already_buy_eligible_codes(store: StockStore, trade_date: str, stamp: str) -> set[str]:
    """Do not re-promote a dynamic stock already eligible on the formal board."""
    conn = store._get_conn()
    try:
        run = conn.execute(
            """SELECT as_of FROM candidate_board_runs
                WHERE trade_date=? AND status='ready' AND as_of<=?
                ORDER BY as_of DESC LIMIT 1""",
            (trade_date, stamp),
        ).fetchone()
        if not run:
            return set()
        rows = conn.execute(
            """SELECT code FROM candidate_board_members
                WHERE as_of=? AND buy_eligible=1""",
            (run["as_of"],),
        ).fetchall()
    finally:
        conn.close()
    return {str(row["code"]).zfill(6) for row in rows}


def _latest_radar_market(store: StockStore, trade_date: str, stamp: str) -> dict[str, Any]:
    conn = store._get_conn()
    try:
        row = conn.execute(
            """SELECT as_of,market_context FROM intraday_radar_runs
                WHERE status='ready' AND substr(as_of,1,10)=? AND as_of<=?
                ORDER BY as_of DESC LIMIT 1""",
            (trade_date, stamp),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {
            "as_of": "",
            "regime": {
                "regime": "neutral", "summary": "尚无盘中指数快照，按中性处理",
                "source": "deterministic_indices.v1",
            },
            "indices": {},
        }
    context = _json(row["market_context"], {})
    indices = context.get("indices") if isinstance(context.get("indices"), dict) else {}
    return {
        "as_of": str(row["as_of"]),
        "regime": classify_market_regime(indices),
        "indices": indices,
        "market_change_pct": context.get("market_change_pct"),
        "sector_as_of": context.get("sector_as_of"),
    }


def build_promotion_snapshot(
    store: StockStore | None = None, *, now: datetime | None = None,
) -> dict[str, Any]:
    """Build one immutable view over the latest unexpired dynamic sources."""
    store = store or StockStore()
    now = now or datetime.now()
    trade_date = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    excluded = _active_promoted_codes(store, trade_date, stamp) | _already_buy_eligible_codes(
        store, trade_date, stamp,
    )
    source_rows: list[tuple[str, dict[str, Any]]] = [
        *(('opening_auction_watch', row) for row in latest_auction_watch_candidates(
            store, now=now, limit=20,
        )),
        *(('intraday_radar', row) for row in latest_radar_candidates(
            store, now=now, limit=20,
        )),
    ]
    by_code: dict[str, dict[str, Any]] = {}
    for source, raw in source_rows:
        code = str(raw.get("code") or "").zfill(6)
        if not code:
            continue
        row = dict(raw)
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        item = by_code.setdefault(code, {
            "code": code,
            "name": row.get("name") or code,
            "price": row.get("price"),
            "change_pct": row.get("change_pct"),
            "score": row.get("score"),
            "theme_group": row.get("theme_group") or "",
            "source_types": [],
            "sources": {},
        })
        item["source_types"].append(source)
        item["sources"][source] = {
            "as_of": row.get("as_of") or row.get("generated_at"),
            "score": row.get("score"),
            "price": row.get("price"),
            "change_pct": row.get("change_pct"),
            "triggers": row.get("triggers") if isinstance(row.get("triggers"), list) else [],
            "risk_tags": row.get("risk_tags") if isinstance(row.get("risk_tags"), list) else [],
            "evidence": evidence,
        }
        # Radar is later and richer than the opening observation.
        if source == "intraday_radar":
            for key in ("name", "price", "change_pct", "score", "theme_group"):
                if row.get(key) not in (None, ""):
                    item[key] = row[key]

    codes = sorted(by_code)
    setups = load_daily_setups(store, codes)
    for code, item in by_code.items():
        radar = item["sources"].get("intraday_radar") or {}
        radar_evidence = radar.get("evidence") if isinstance(radar.get("evidence"), dict) else {}
        item["daily_setup"] = radar_evidence.get("setup") or setups.get(code) or {"available": False}
        item["source_types"] = list(dict.fromkeys(item["source_types"]))

    all_candidates = sorted(
        by_code.values(), key=lambda row: (-_float(row.get("score")), row["code"]),
    )
    candidates = [row for row in all_candidates if row["code"] not in excluded]
    market = _latest_radar_market(store, trade_date, stamp)
    fingerprint_payload = {
        "trade_date": trade_date,
        "market": market,
        # Keep the source version independent from decisions already applied.
        # Otherwise excluding a newly promoted code would look like a fresh
        # radar generation and re-run the AI without new market evidence.
        "candidates": all_candidates,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    source_times = [
        str(source.get("as_of") or "")
        for row in all_candidates for source in row.get("sources", {}).values()
    ]
    source_as_of = max(source_times, default=stamp)
    return {
        "schema": "candidate_promotion_snapshot.v1",
        "trade_date": trade_date,
        "as_of": f"{source_as_of}#{fingerprint[:12]}",
        "source_fingerprint": fingerprint,
        "market": market,
        "candidates": candidates,
        "source_candidate_count": len(all_candidates),
        "required_evidence_codes": [row["code"] for row in candidates],
    }


def get_promotion_overview(store: StockStore | None = None) -> dict[str, Any]:
    snapshot = build_promotion_snapshot(store)
    return {
        "schema": "candidate_promotion_overview.v1",
        "trade_date": snapshot["trade_date"],
        "as_of": snapshot["as_of"],
        "source_fingerprint": snapshot["source_fingerprint"],
        "market": snapshot["market"],
        "candidate_count": len(snapshot["candidates"]),
        "candidates": [
            {
                "code": row["code"], "name": row["name"], "price": row.get("price"),
                "change_pct": row.get("change_pct"), "score": row.get("score"),
                "theme_group": row.get("theme_group"), "source_types": row["source_types"],
            }
            for row in snapshot["candidates"]
        ],
        "required_evidence_codes": snapshot["required_evidence_codes"],
    }


def get_promotion_evidence(
    codes: list[str], as_of: str, store: StockStore | None = None,
) -> dict[str, Any]:
    snapshot = build_promotion_snapshot(store)
    if as_of != snapshot["as_of"]:
        raise ValueError(
            f"promotion source changed: requested {as_of}, current {snapshot['as_of']}"
        )
    normalized = list(dict.fromkeys(
        str(code).strip().zfill(6) for code in codes if str(code).strip()
    ))
    by_code = {row["code"]: row for row in snapshot["candidates"]}
    unknown = [code for code in normalized if code not in by_code]
    if unknown:
        raise ValueError("codes outside promotion scope: " + ",".join(unknown))
    return {
        "schema": "candidate_promotion_evidence.v1",
        "as_of": snapshot["as_of"],
        "market": snapshot["market"],
        "stocks": [by_code[code] for code in normalized],
    }


def _normalized_decisions(payload: dict[str, Any], required: list[str]) -> list[dict[str, Any]]:
    reviewed_raw = payload.get("reviewed_codes")
    rows_raw = payload.get("decisions")
    if not isinstance(reviewed_raw, list) or not isinstance(rows_raw, list):
        raise ValueError("reviewed_codes and decisions must be arrays")
    reviewed = [str(code).strip().zfill(6) for code in reviewed_raw if str(code).strip()]
    required_set = set(required)
    if len(reviewed) != len(set(reviewed)) or set(reviewed) != required_set:
        raise ValueError("reviewed_codes must exactly match required_evidence_codes")
    normalized = []
    seen: set[str] = set()
    for raw in rows_raw:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip().zfill(6)
        decision = str(raw.get("decision") or "watch").strip().lower()
        route = str(raw.get("entry_route") or "unclassified").strip()
        confidence = str(raw.get("confidence") or "weak").strip().lower()
        if code not in required_set or code in seen:
            raise ValueError(f"{code}: outside scope or duplicate promotion decision")
        if decision not in PROMOTION_DECISIONS:
            raise ValueError(f"{code}: invalid promotion decision {decision}")
        if confidence not in CONFIDENCES:
            raise ValueError(f"{code}: invalid confidence {confidence}")
        if decision == "promote" and route not in ENTRY_ROUTES:
            raise ValueError(f"{code}: promoted stock requires a valid entry_route")
        if decision != "promote":
            route = "unclassified"
        normalized.append({
            "code": code,
            "name": str(raw.get("name") or "")[:40],
            "decision": decision,
            "entry_route": route,
            "confidence": confidence,
            "reason": str(raw.get("reason") or "")[:300],
            "risk": str(raw.get("risk") or "")[:240],
        })
        seen.add(code)
    if seen != required_set:
        raise ValueError("decisions must contain exactly one row for every required code")
    return normalized


def validate_promotion_decision(
    payload: dict[str, Any], expected_as_of: str, store: StockStore | None = None,
    *, now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a promotion response without writing promotion state."""
    store = store or StockStore()
    snapshot = build_promotion_snapshot(store, now=now or datetime.now())
    if expected_as_of != snapshot["as_of"]:
        raise ValueError(
            f"promotion source changed: requested {expected_as_of}, current {snapshot['as_of']}"
        )
    required = snapshot["required_evidence_codes"]
    decisions = _normalized_decisions(payload, required) if required else []
    return {"snapshot": snapshot, "decisions": decisions}


def apply_promotion_decision(
    payload: dict[str, Any], expected_as_of: str, store: StockStore | None = None,
    *, now: datetime | None = None,
) -> dict[str, Any]:
    """Persist AI qualification only; no account or order tables are touched."""
    store = store or StockStore()
    now = now or datetime.now()
    validated = validate_promotion_decision(
        payload, expected_as_of, store=store, now=now,
    )
    snapshot = validated["snapshot"]
    required = snapshot["required_evidence_codes"]
    if not required:
        return {"status": "no_candidates", "as_of": expected_as_of, "promoted": []}
    decisions = validated["decisions"]
    by_code = {row["code"]: row for row in snapshot["candidates"]}
    trade_date = snapshot["trade_date"]
    promoted_at = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = f"{trade_date} 15:05:00"
    promoted: list[dict[str, Any]] = []
    conn = store._get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row in decisions:
            evidence = by_code[row["code"]]
            sources = evidence.get("source_types") or []
            name = row["name"] or evidence.get("name") or row["code"]
            conn.execute(
                """INSERT INTO candidate_promotion_decisions
                   (as_of,trade_date,code,name,decision,entry_route,confidence,
                    reason,risk,source_types,evidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    expected_as_of, trade_date, row["code"], name, row["decision"],
                    row["entry_route"], row["confidence"], row["reason"], row["risk"],
                    json.dumps(sources, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                ),
            )
            if row["decision"] != "promote":
                continue
            cursor = conn.execute(
                """INSERT OR IGNORE INTO intraday_candidate_promotions
                   (trade_date,code,name,entry_route,confidence,promoted_at,expires_at,
                    source_types,reason,risk,evidence,status,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?)""",
                (
                    trade_date, row["code"], name, row["entry_route"], row["confidence"],
                    promoted_at, expires_at, json.dumps(sources, ensure_ascii=False),
                    row["reason"], row["risk"], json.dumps(evidence, ensure_ascii=False),
                    promoted_at,
                ),
            )
            if cursor.rowcount:
                promoted.append({
                    "code": row["code"], "name": name,
                    "entry_route": row["entry_route"], "promoted_at": promoted_at,
                })
        conn.execute(
            """INSERT INTO candidate_promotion_runs
               (as_of,trade_date,status,source_fingerprint,candidate_count,
                promoted_count,response,error)
               VALUES (?,?,?,?,?,?,?,'')""",
            (
                expected_as_of, trade_date, "ready", snapshot["source_fingerprint"],
                len(required), len(promoted), json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "status": "ready", "as_of": expected_as_of, "trade_date": trade_date,
        "evaluated_count": len(decisions), "promoted_count": len(promoted),
        "promoted": promoted,
    }


def record_promotion_failure(
    expected_as_of: str,
    error: str,
    store: StockStore | None = None,
    *,
    response: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist an infrastructure/model failure for the immutable source version."""
    store = store or StockStore()
    now = now or datetime.now()
    trade_date = str(expected_as_of or "")[:10] or now.date().isoformat()
    fingerprint = str(expected_as_of or "").partition("#")[2] or "unknown"
    candidate_count = 0
    try:
        snapshot = build_promotion_snapshot(store, now=now)
        if snapshot["as_of"] == expected_as_of:
            trade_date = snapshot["trade_date"]
            fingerprint = snapshot["source_fingerprint"]
            candidate_count = len(snapshot["required_evidence_codes"])
    except Exception:
        # Failure reporting must survive even when snapshot construction is the
        # operation that failed.
        pass
    message = str(error or "unknown candidate promotion failure")[:1200]
    conn = store._get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO candidate_promotion_runs
               (as_of,trade_date,status,source_fingerprint,candidate_count,
                promoted_count,response,error,created_at)
               VALUES (?,?,?,?,?,0,?,?,?)""",
            (
                expected_as_of, trade_date, "failed", fingerprint, candidate_count,
                str(response or "")[:8000], message,
                now.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "failed", "as_of": expected_as_of, "trade_date": trade_date,
        "candidate_count": candidate_count, "error": message,
    }


def record_promotion_noop(
    snapshot: dict[str, Any], store: StockStore | None = None,
    *, now: datetime | None = None,
) -> dict[str, Any]:
    """Acknowledge a dynamic source whose stocks are already formal candidates."""
    store = store or StockStore()
    now = now or datetime.now()
    conn = store._get_conn()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO candidate_promotion_runs
               (as_of,trade_date,status,source_fingerprint,candidate_count,
                promoted_count,response,error,created_at)
               VALUES (?,?,?,?,0,0,?,'',?)""",
            (
                snapshot["as_of"], snapshot["trade_date"], "ready",
                snapshot["source_fingerprint"],
                json.dumps({"result": "no_unqualified_dynamic_candidates"}),
                now.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ready", "as_of": snapshot["as_of"], "candidate_count": 0}


def promotion_health(
    store: StockStore, *, trade_date: str | None = None,
) -> dict[str, Any]:
    """Describe whether today's dynamic observations reached the promotion service."""
    trade_date = trade_date or datetime.now().date().isoformat()
    conn = store._get_conn()
    try:
        radar = conn.execute(
            """SELECT as_of,selected_count FROM intraday_radar_runs
                WHERE status='ready' AND selected_count>0 AND substr(as_of,1,10)=?
                ORDER BY as_of DESC LIMIT 1""",
            (trade_date,),
        ).fetchone()
        auction = conn.execute(
            """SELECT MAX(generated_at) AS as_of,COUNT(*) AS selected_count
                 FROM opening_auction_watch_candidates WHERE trade_date=?""",
            (trade_date,),
        ).fetchone()
        run = conn.execute(
            """SELECT * FROM candidate_promotion_runs WHERE trade_date=?
                ORDER BY created_at DESC,as_of DESC LIMIT 1""",
            (trade_date,),
        ).fetchone()
    finally:
        conn.close()

    sources = []
    if radar:
        sources.append(str(radar["as_of"] or ""))
    if auction and int(auction["selected_count"] or 0) > 0:
        sources.append(str(auction["as_of"] or ""))
    latest_source = max((stamp for stamp in sources if stamp), default="")
    if not latest_source:
        return {
            "status": "idle", "healthy": True, "trade_date": trade_date,
            "message": "今日尚无需要晋升判断的竞价或雷达信号",
        }
    if not run:
        return {
            "status": "failed", "healthy": False, "trade_date": trade_date,
            "latest_source_at": latest_source,
            "message": "动态信号已经产生，但晋升服务没有留下评估记录",
            "error": "missing candidate_promotion_run",
        }

    item = dict(run)
    run_source = str(item.get("as_of") or "").partition("#")[0]
    base = {
        "trade_date": trade_date,
        "latest_source_at": latest_source,
        "last_run_at": str(item.get("created_at") or ""),
        "candidate_count": int(item.get("candidate_count") or 0),
        "promoted_count": int(item.get("promoted_count") or 0),
    }
    if run_source < latest_source:
        return {
            **base, "status": "pending", "healthy": True,
            "message": "发现了更新的动态信号，等待本轮独立晋升评估",
        }
    if str(item.get("status")) == "failed":
        error = str(item.get("error") or "晋升服务执行失败")
        return {
            **base, "status": "failed", "healthy": False, "error": error,
            "message": f"晋升服务失败：{error}",
        }
    return {
        **base, "status": "healthy", "healthy": True,
        "message": (
            f"晋升服务正常：评估 {base['candidate_count']} 只，"
            f"晋升 {base['promoted_count']} 只"
        ),
    }


def load_active_promotions(
    store: StockStore, *, trade_date: str, now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now()
    conn = store._get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM intraday_candidate_promotions
                WHERE trade_date=? AND status='active' AND expires_at>=?
                ORDER BY CASE confidence WHEN 'strong' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                         promoted_at,code""",
            (trade_date, now.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for raw in rows:
        row = dict(raw)
        row["source_types"] = _json(row.get("source_types"), [])
        row["evidence"] = _json(row.get("evidence"), {})
        result.append(row)
    return result
