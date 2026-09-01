"""Database boundary for AI-assisted final stock selection.

The quantitative scanner stages one current candidate snapshot.  The agent gets a
small, read-only domain API over that snapshot and echoes its ``as_of`` in the
decision.  Only the deterministic execution layer may replace ``screen_records``.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from data.news_evidence import build_news_evidence, match_policy_evidence, recent_policy_evidence
from data.candidate_lifecycle import record_discoveries

if TYPE_CHECKING:
    from data.store.sqlite_store import StockStore

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("STOCK_DB_PATH") or ROOT / "data" / "stock_data.db")
MAX_EVIDENCE_CODES = 40


def _stock_store(store: StockStore | None = None) -> StockStore:
    """Keep pandas-heavy write dependencies out of the read-only MCP process."""
    if store is not None:
        return store
    from data.store.sqlite_store import StockStore

    return StockStore()


def _connect_readonly(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _text_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    if not value:
        return []
    return [item for item in str(value).split("|") if item]


def _clean(value: Any) -> Any:
    """Convert pandas/numpy values to stable JSON-compatible primitives."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _clean(value.item())
        except Exception:
            pass
    return str(value)


def _row_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    payload = _clean(dict(raw))
    payload["code"] = str(payload.get("code") or "").zfill(6)
    # ``route`` was the retired low/mid/high-location taxonomy.  Strip it at
    # the selection boundary so all downstream consumers see only the
    # executable setup taxonomy in ``entry_route``.
    payload.pop("route", None)
    payload["extra"] = _object(payload.get("extra"))
    return payload


def stage_candidate_pool(
    rows: Iterable[Mapping[str, Any]],
    *,
    run_date: str,
    run_time: str,
    run_label: str,
    target: str,
    expected_daily_date: str,
    generated_at: str,
    market_context: Mapping[str, Any] | None = None,
    store: StockStore | None = None,
) -> dict[str, Any]:
    """Atomically replace the current AI-readable candidate snapshot."""
    candidates = [_row_payload(row) for row in rows]
    if not candidates:
        raise ValueError("candidate pool is empty")
    as_of = generated_at
    store = _stock_store(store)
    conn = store._get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM screen_candidate_pool")
        conn.execute("DELETE FROM screen_candidate_state")
        conn.execute(
            """INSERT INTO screen_candidate_state
               (slot,as_of,run_date,run_time,run_label,target,expected_daily_date,
                status,candidate_count,selected_count,market_context,error,created_at,completed_at)
               VALUES (1,?,?,?,?,?,?, 'ready', ?,0,?,'',?,'')""",
            (
                as_of,
                run_date,
                run_time,
                run_label,
                target,
                expected_daily_date,
                len(candidates),
                json.dumps(_clean(market_context or {}), ensure_ascii=False),
                generated_at,
            ),
        )
        for rank, row in enumerate(candidates, start=1):
            conn.execute(
                """INSERT INTO screen_candidate_pool
                   (as_of,rank,code,name,price,quant_score,signal_type,trend,
                    pct_change,vol_ratio,zone,route,theme_group,evidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    as_of,
                    rank,
                    row["code"],
                    str(row.get("name") or ""),
                    float(row.get("price") or 0),
                    float(row.get("final_score", row.get("score")) or 0),
                    str(row.get("signal_type") or "watch"),
                    str(row.get("trend") or ""),
                    float(row.get("pct_change") or 0),
                    float(row.get("vol_ratio") or 0),
                    str(row.get("zone") or ""),
                    str(row.get("entry_route") or "unclassified"),
                    str(row.get("theme_group") or ""),
                    json.dumps(row, ensure_ascii=False),
                ),
            )
        record_discoveries(
            conn,
            candidates,
            as_of=as_of,
            evidence_date=expected_daily_date,
            target_trade_date=run_date,
            updated_at=generated_at,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "schema": "stock_selection_stage.v1",
        "status": "ready",
        "as_of": as_of,
        "run_date": run_date,
        "run_time": run_time,
        "run_label": run_label,
        "target": target,
        "expected_daily_date": expected_daily_date,
        "candidate_count": len(candidates),
        "lifecycle_updated": True,
    }


def _current_state(conn: sqlite3.Connection, expected_as_of: str | None = None) -> sqlite3.Row:
    state = conn.execute("SELECT * FROM screen_candidate_state WHERE slot=1").fetchone()
    if not state:
        raise RuntimeError("selection candidate pool is empty; run the quantitative stage first")
    if expected_as_of and str(state["as_of"]) != expected_as_of:
        raise ValueError(
            f"selection pool changed: requested {expected_as_of}, current {state['as_of']}"
        )
    return state


def get_selection_overview(*, db_path: Path | None = None) -> dict[str, Any]:
    """Return the current candidate universe and immutable snapshot version."""
    with _connect_readonly(db_path) as conn:
        state = _current_state(conn)
        rows = conn.execute(
            """SELECT rank,code,name,quant_score,signal_type,trend,pct_change,
                      vol_ratio,zone,route,theme_group,evidence
                 FROM screen_candidate_pool WHERE as_of=? ORDER BY rank""",
            (state["as_of"],),
        ).fetchall()
        lifecycle_rows = conn.execute(
            "SELECT * FROM candidate_lifecycle"
        ).fetchall()
        lifecycle_by_code = {
            str(row["code"]).zfill(6): dict(row) for row in lifecycle_rows
        }
    # Keep the overview as a complete, compact index.  Rich per-stock evidence
    # belongs exclusively to candidate_evidence; duplicating it here made a
    # 90-100 stock overview exceed the MCP result envelope and hid the tail of
    # required_evidence_codes from the selector.
    candidates = []
    coverage = {"logic": 0, "fundamental": 0, "fund_flow": 0, "theme": 0}
    for row in rows:
        evidence = _object(row["evidence"])
        extra = _object(evidence.get("extra"))
        lifecycle = lifecycle_by_code.get(str(row["code"]).zfill(6), {})
        sector_context = _object(evidence.get("sector_context"))
        concepts = _text_list(sector_context.get("concepts")) or _text_list(extra.get("concepts"))
        coverage["logic"] += int(bool(evidence.get("logic_available")))
        coverage["fundamental"] += int(bool(evidence.get("fundamental_available")))
        coverage["fund_flow"] += int(bool(float(evidence.get("fund_flow_score") or 0)))
        coverage["theme"] += int(bool(row["theme_group"] or concepts))
        candidates.append({
            "rank": row["rank"],
            "code": row["code"],
            "name": row["name"],
            "quant_score": row["quant_score"],
            "signal": row["signal_type"],
            "entry_route": evidence.get("entry_route") or lifecycle.get("entry_route"),
            "setup_stage": evidence.get("setup_stage"),
            "lifecycle_state": lifecycle.get("state"),
            "promotion_ready": lifecycle.get("state") == "actionable",
            "theme": row["theme_group"] or (concepts[0] if concepts else ""),
        })
    return {
        "schema": "stock_selection_overview.v1",
        "as_of": state["as_of"],
        "status": state["status"],
        "run": {
            "date": state["run_date"],
            "time": state["run_time"],
            "label": state["run_label"],
            "target": state["target"],
            "daily_data_through": state["expected_daily_date"],
        },
        "market": _object(state["market_context"]),
        "candidate_count": len(candidates),
        "coverage": coverage,
        "candidates": candidates,
        "required_evidence_codes": [row["code"] for row in candidates],
    }


def _latest_financials(conn: sqlite3.Connection, codes: list[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"""SELECT code,period,roe,roa,gross_margin,net_margin,eps,revenue_yoy,
                   profit_yoy,debt_ratio,source,updated_at
              FROM financial_factors
             WHERE code IN ({placeholders})
             ORDER BY code,period DESC,updated_at DESC,id DESC""",
        codes,
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result.setdefault(str(row["code"]).zfill(6), dict(row))
    return result


def _latest_fundamentals(conn: sqlite3.Connection, codes: list[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"""SELECT code,period,report_type,report_date,industry_demand_score,
                   future_demand_score,product_penetration_score,strategy_score,
                   candor_score,composite_score,confidence,summary,source,updated_at
              FROM fundamental_llm_scores
             WHERE code IN ({placeholders})
             ORDER BY code,report_date DESC,period DESC,updated_at DESC,id DESC""",
        codes,
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result.setdefault(str(row["code"]).zfill(6), dict(row))
    return result


def _recent_news(
    conn: sqlite3.Connection, codes: list[str], as_of: str,
) -> dict[str, list[dict[str, Any]]]:
    if not codes:
        return {}
    try:
        reference = datetime.fromisoformat(str(as_of))
    except ValueError:
        reference = datetime.now()
    since = (reference - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"""SELECT code,title,content,source,publish_at,url,category,sentiment,
                   score,risk_level,tags,created_at
              FROM news_events
             WHERE code IN ({placeholders})
               AND created_at<=?
               AND COALESCE(NULLIF(publish_at,''),created_at) BETWEEN ? AND ?
             ORDER BY code,publish_at DESC,id DESC""",
        [*codes, as_of, since, as_of],
    ).fetchall()
    result: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
    for row in rows:
        code = str(row["code"]).zfill(6)
        if len(result.setdefault(code, [])) < 5:
            result[code].append(build_news_evidence(row))
    return result


def _price_momentum(conn: sqlite3.Connection, codes: list[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"""SELECT code,date,close FROM daily_prices
              WHERE code IN ({placeholders}) ORDER BY code,date DESC""",
        codes,
    ).fetchall()
    grouped: dict[str, list[tuple[str, float]]] = {code: [] for code in codes}
    for row in rows:
        code = str(row["code"]).zfill(6)
        bucket = grouped.setdefault(code, [])
        if len(bucket) < 61:
            bucket.append((str(row["date"]), float(row["close"] or 0)))
    result = {}
    for code, prices in grouped.items():
        if not prices or prices[0][1] <= 0:
            continue
        latest = prices[0][1]
        metrics: dict[str, Any] = {"date": prices[0][0]}
        for days in (5, 20, 60):
            index = min(days, len(prices) - 1)
            base = prices[index][1]
            metrics[f"return_{days}d_pct"] = round((latest / base - 1) * 100, 2) if base > 0 else None
        result[code] = metrics
    return result


def get_candidate_evidence(
    codes: list[str], as_of: str, *, db_path: Path | None = None,
) -> dict[str, Any]:
    """Return detailed evidence for in-pool codes using one immutable ``as_of``."""
    normalized = list(dict.fromkeys(str(code).strip().zfill(6) for code in codes if str(code).strip()))
    if not normalized:
        raise ValueError("codes must not be empty")
    if len(normalized) > MAX_EVIDENCE_CODES:
        raise ValueError(f"at most {MAX_EVIDENCE_CODES} codes per call")
    with _connect_readonly(db_path) as conn:
        state = _current_state(conn, as_of)
        placeholders = ",".join("?" for _ in normalized)
        rows = conn.execute(
            f"""SELECT * FROM screen_candidate_pool
                  WHERE as_of=? AND code IN ({placeholders}) ORDER BY rank""",
            [as_of, *normalized],
        ).fetchall()
        by_code = {str(row["code"]).zfill(6): row for row in rows}
        unknown = [code for code in normalized if code not in by_code]
        if unknown:
            raise ValueError("codes outside current candidate pool: " + ",".join(unknown))
        financials = _latest_financials(conn, normalized)
        fundamentals = _latest_fundamentals(conn, normalized)
        news = _recent_news(conn, normalized, as_of)
        policy_context = recent_policy_evidence(conn, as_of)
        momentum = _price_momentum(conn, normalized)
        stock_rows = conn.execute(
            f"SELECT code,industry,exchange,list_date FROM stocks WHERE code IN ({placeholders})",
            normalized,
        ).fetchall()
        stocks = {str(row["code"]).zfill(6): dict(row) for row in stock_rows}
        lifecycle_rows = conn.execute(
            f"SELECT * FROM candidate_lifecycle WHERE code IN ({placeholders})",
            normalized,
        ).fetchall()
        lifecycle = {str(row["code"]).zfill(6): dict(row) for row in lifecycle_rows}

    evidence_rows = []
    for code in normalized:
        row = by_code[code]
        payload = _object(row["evidence"])
        extra = _object(payload.get("extra"))
        sector_context = _object(payload.get("sector_context"))
        stock_sector = sector_context or {}
        evidence_rows.append({
            "rank": row["rank"],
            "code": code,
            "name": row["name"],
            "company": stocks.get(code) or {},
            "quantitative": {
                "final_score": row["quant_score"],
                "base_score": payload.get("base_score"),
                "enrichment_score": payload.get("enrichment_score"),
                "theme_score": payload.get("theme_bonus"),
                "logic_score": payload.get("logic_score"),
                "fundamental_score": payload.get("fundamental_score"),
                "fund_flow_score": payload.get("fund_flow_score"),
                "sector_rotation_score": payload.get("sector_rotation_score"),
                "corporate_action_penalty": payload.get("corporate_action_penalty"),
                "theme_concentration_penalty": payload.get("theme_concentration_penalty"),
            },
            "technical": {
                "price": row["price"],
                "trend": row["trend"],
                "pct_change": row["pct_change"],
                "vol_ratio": row["vol_ratio"],
                "position_60d": payload.get("position_pct"),
                "zone": row["zone"],
                "buy_eligible": payload.get("buy_eligible"),
                "tags": _text_list(payload.get("signal_tags")),
                "risk_tags": _text_list(payload.get("risk_tags")),
                "entry_route": payload.get("entry_route") or row["route"] or "unclassified",
                "setup_stage": payload.get("setup_stage"),
                "setup_score": payload.get("setup_score"),
                "setup_triggers": _text_list(payload.get("setup_triggers")),
                "setup_risks": _text_list(payload.get("setup_risks")),
                "entry_metrics": payload.get("entry_metrics") or {},
                "momentum": momentum.get(code) or {},
            },
            "candidate_lifecycle": {
                key: lifecycle.get(code, {}).get(key)
                for key in (
                    "state", "entry_route", "first_seen_date", "last_seen_date",
                    "last_improved_date", "observation_sessions", "improving_streak",
                    "previous_score", "current_score", "best_score", "setup_score",
                    "invalidation_reason",
                )
            },
            "theme": {
                "group": row["theme_group"],
                "concepts": (
                    _text_list(sector_context.get("concepts"))
                    or _text_list(extra.get("concepts"))
                ),
                "sector_rotation_tags": (
                    _text_list(sector_context.get("tags"))
                    or _text_list(payload.get("sector_rotation_tags"))
                ),
            },
            "sector": sector_context or None,
            "matched_policy_evidence": match_policy_evidence(policy_context, stock_sector),
            "logic_change": _object(extra.get("logic_change")) or None,
            "structured_financial": financials.get(code),
            "fundamental_analysis": fundamentals.get(code),
            "recent_news": news.get(code) or [],
        })
    return {
        "schema": "stock_selection_evidence.v1",
        "as_of": state["as_of"],
        "count": len(evidence_rows),
        "policy_context": policy_context,
        "stocks": evidence_rows,
    }


def get_staged_rows(as_of: str, *, store: StockStore | None = None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load staged payloads for the deterministic execution layer."""
    store = _stock_store(store)
    conn = store._get_conn()
    try:
        state = dict(_current_state(conn, as_of))
        rows = conn.execute(
            "SELECT code,evidence FROM screen_candidate_pool WHERE as_of=? ORDER BY rank",
            (as_of,),
        ).fetchall()
        return state, {str(row["code"]).zfill(6): _object(row["evidence"]) for row in rows}
    finally:
        conn.close()


def update_selection_status(
    as_of: str,
    *,
    status: str,
    selected_count: int = 0,
    error: str = "",
    store: StockStore | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    if status not in {"selected", "failed"}:
        raise ValueError(f"invalid selection status: {status}")
    store = _stock_store(store)
    owns_conn = conn is None
    connection = conn or store._get_conn()
    try:
        cursor = connection.execute(
            """UPDATE screen_candidate_state
                  SET status=?,selected_count=?,error=?,completed_at=datetime('now','localtime')
                WHERE slot=1 AND as_of=?""",
            (status, int(selected_count), str(error)[:500], as_of),
        )
        if cursor.rowcount != 1:
            raise ValueError("selection candidate snapshot changed before status update")
        if owns_conn:
            connection.commit()
    finally:
        if owns_conn:
            connection.close()
