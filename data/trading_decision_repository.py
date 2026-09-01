"""Read-only decision views for the trading agent.

This module is the only database surface exposed to the AI.  It deliberately
returns domain objects instead of accepting SQL, and every follow-up read must
use the ``as_of`` value returned by :func:`get_trading_overview`.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("STOCK_DB_PATH") or ROOT / "data" / "stock_data.db")
MAX_EVIDENCE_CODES = 50
TradingMode = Literal["simulated", "live"]


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
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


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item]
    except (TypeError, ValueError):
        pass
    return [item for item in str(value).split("|") if item]


def _snapshot(
    conn: sqlite3.Connection,
    mode: TradingMode,
    expected_as_of: str | None = None,
) -> tuple[dict[str, Any], list[sqlite3.Row], str]:
    market_row = conn.execute(
        "SELECT payload, updated_at FROM trading_market_state WHERE mode=?",
        (mode,),
    ).fetchone()
    if not market_row:
        raise RuntimeError(f"{mode} trading state is empty; refresh is required")
    as_of = str(market_row["updated_at"])
    if expected_as_of and expected_as_of != as_of:
        raise ValueError(
            f"trading state changed: requested {expected_as_of}, current {as_of}; restart the decision"
        )
    rows = conn.execute(
        """SELECT code, name, is_candidate, is_sim_holding, is_live_holding,
                  screen_score, screen_signal, payload, updated_at
             FROM trading_stock_state WHERE mode=?
            ORDER BY is_sim_holding DESC, is_live_holding DESC, screen_score DESC, code""",
        (mode,),
    ).fetchall()
    return _object(market_row["payload"]), rows, as_of


def _zone(technical: dict[str, Any], selector: dict[str, Any]) -> str:
    if selector.get("zone"):
        return str(selector["zone"])
    try:
        position = float(technical.get("position_60d_pct"))
    except (TypeError, ValueError):
        return "unknown"
    if position <= 35:
        return "low"
    if position >= 75:
        return "high"
    return "middle"


def _compact_stock(item: dict[str, Any], updated_at: str) -> dict[str, Any]:
    quote = _object(item.get("quote"))
    technical = _object(item.get("technical"))
    intraday = _object(item.get("intraday"))
    half_hour = _object(intraday.get("half_hour"))
    fund_flow = _object(item.get("fund_flow"))
    fund_detail = _object(fund_flow.get("detail"))
    sector = _object(item.get("sector"))
    screen = _object(item.get("screen"))
    extra = _object(screen.get("extra"))
    selector = _object(extra.get("selector"))
    lifecycle = _object(extra.get("candidate_lifecycle"))
    promotion = _object(extra.get("candidate_promotion"))
    logic_change = _object(extra.get("logic_change"))
    fundamental = _object(extra.get("fundamental_llm"))
    ai_selection = _object(extra.get("ai_selection"))
    zone = _zone(technical, selector)
    entry_route = str(selector.get("entry_route") or "unclassified")
    news = [
        {
            "title": str(row.get("title") or "")[:100],
            "summary": str(row.get("summary") or "")[:260],
            "evidence_snippets": _text_list(row.get("evidence_snippets"))[:2],
            "content_available": bool(row.get("content_available")),
            "analysis_basis": row.get("analysis_basis"),
            "content_digest": row.get("content_digest"),
            "source": row.get("source"),
            "published_at": row.get("published_at") or row.get("publish_at"),
            "sentiment": row.get("sentiment"),
            "score": row.get("score"),
            "risk": row.get("risk") or row.get("risk_level"),
            "tags": _text_list(row.get("tags"))[:12],
            "mentioned_topics": _text_list(row.get("mentioned_topics"))[:8],
        }
        for row in _list(item.get("news"))[:3]
        if isinstance(row, dict)
    ]
    policy_evidence = [
        {
            "title": str(row.get("title") or "")[:100],
            "summary": str(row.get("summary") or "")[:260],
            "evidence_snippets": _text_list(row.get("evidence_snippets"))[:2],
            "source": row.get("source"),
            "published_at": row.get("published_at"),
            "score": row.get("score"),
            "matched_topics": _text_list(row.get("matched_topics"))[:8],
            "analysis_basis": row.get("analysis_basis"),
            "evidence_type": row.get("evidence_type"),
            "source_tier": row.get("source_tier"),
            "confidence": row.get("confidence"),
        }
        for row in _list(item.get("policy_evidence"))[:3]
        if isinstance(row, dict)
    ]
    return {
        "code": str(item.get("code") or "").zfill(6),
        "name": item.get("name"),
        "scope": {
            "candidate": bool(item.get("is_candidate")),
            "sim_holding": bool(item.get("is_sim_holding")),
            "live_holding": bool(item.get("is_live_holding")),
        },
        "sim_position": _object(item.get("position")) or None,
        "live_position": _object(item.get("live_position")) or None,
        "selection": {
            "date": screen.get("run_date"),
            "score": screen.get("score"),
            "signal": screen.get("signal_type"),
            "trend": screen.get("trend"),
            "tags": _text_list(screen.get("tags"))[:12],
            "concepts": (
                _text_list(screen.get("concepts"))
                or _text_list(extra.get("concepts"))
                or _text_list(sector.get("concepts"))
                or _text_list(item.get("themes"))
            )[:6],
            "zone": zone,
            "entry_route": entry_route,
            "setup_stage": selector.get("setup_stage"),
            "setup_score": selector.get("setup_score"),
            "setup_triggers": _text_list(selector.get("setup_triggers")),
            "buy_eligible": selector.get("buy_eligible"),
            "risk_tags": _text_list(selector.get("risk_tags")),
            "logic_change": logic_change or None,
            "fundamental": fundamental or None,
            "ai_selection": ai_selection or None,
            "lifecycle": lifecycle or None,
            "promotion": promotion or None,
        },
        "quote": {
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "volume": quote.get("volume"),
            "amount": quote.get("amount"),
            "source": quote.get("source"),
            "error": quote.get("error"),
        },
        "technical": {
            "date": technical.get("daily_date"),
            "trend": technical.get("trend"),
            "ma5": technical.get("ma5"),
            "ma10": technical.get("ma10"),
            "ma20": technical.get("ma20"),
            "above_ma5": technical.get("above_ma5"),
            "above_ma10": technical.get("above_ma10"),
            "above_ma20": technical.get("above_ma20"),
            "position_60d_pct": technical.get("position_60d_pct"),
            "vol_ratio": technical.get("vol_ratio"),
            "macd_cross": technical.get("macd_cross"),
            "macd_dead": technical.get("macd_dead"),
            "return_5d_pct": technical.get("return_5d_pct"),
            "return_20d_pct": technical.get("return_20d_pct"),
            "return_60d_pct": technical.get("return_60d_pct"),
            "rs_5d_percentile": technical.get("rs_5d_percentile"),
            "rs_20d_percentile": technical.get("rs_20d_percentile"),
            "rs_60d_percentile": technical.get("rs_60d_percentile"),
        },
        "intraday": {
            "last_time": intraday.get("last_time"),
            "last_5m_pct": intraday.get("last_5m_pct"),
            "last_15m_pct": intraday.get("last_15m_pct"),
            "pullback_from_high_pct": intraday.get("pullback_from_high_pct"),
            "above_vwap": intraday.get("above_vwap"),
            "vwap": intraday.get("vwap"),
            "half_hour": {
                "available": bool(half_hour.get("available")),
                "price_change_pct": half_hour.get("price_change_pct"),
                "volume_ratio": half_hour.get("volume_last30_vs_prev30"),
                "amount_ratio": half_hour.get("amount_last30_vs_prev30"),
                "above_vwap": half_hour.get("above_vwap_now"),
                "signal": half_hour.get("volume_price_signal"),
            },
            "error": intraday.get("error"),
        },
        "fund_flow": {
            "status": fund_flow.get("status") or ("available" if fund_detail else "unavailable"),
            "freshness": fund_flow.get("freshness"),
            "summary": fund_flow.get("summary"),
            "main_net": fund_detail.get("main_net_inflow"),
            "big_net": fund_detail.get("big_net_inflow"),
            "retail_net": fund_detail.get("retail_net_inflow"),
            "main_pct": fund_detail.get("main_net_pct"),
            "data_date": fund_detail.get("date"),
            "source": fund_detail.get("source") or fund_flow.get("source"),
            "reliability": fund_detail.get("reliability"),
            "observed_at": fund_flow.get("observed_at"),
            "cache_age_seconds": fund_flow.get("cache_age_seconds"),
            "error": fund_flow.get("error"),
        },
        "sector": {
            "membership_status": sector.get("membership_status") or "missing",
            "membership_as_of": sector.get("membership_as_of"),
            "primary_industry": sector.get("primary_industry"),
            "industries": _text_list(sector.get("industries"))[:4],
            "concepts": _text_list(sector.get("concepts"))[:12],
            "rotation_status": sector.get("rotation_status") or "unavailable",
            "rotation_as_of": sector.get("rotation_as_of"),
            "rotation_source": sector.get("rotation_source"),
            "rotation_score": sector.get("rotation_score"),
            "alignment": sector.get("alignment") or "unknown",
            "matches": [
                {
                    "name": row.get("name"),
                    "stage": row.get("stage"),
                    "score": row.get("score"),
                    "stock_boost": row.get("stock_boost"),
                    "membership": row.get("matched_membership"),
                    "membership_type": row.get("membership_type"),
                    "membership_source": row.get("membership_source"),
                    "match_type": row.get("match_type"),
                }
                for row in _list(sector.get("matches"))[:5]
                if isinstance(row, dict)
            ],
            "error": sector.get("error"),
        },
        "news": news,
        "policy_evidence": policy_evidence,
        "updated_at": updated_at,
    }


def _mode_stock(compact: dict[str, Any], mode: TradingMode) -> dict[str, Any]:
    position_key = "sim_position" if mode == "simulated" else "live_position"
    holding_key = "sim_holding" if mode == "simulated" else "live_holding"
    return {
        key: value
        for key, value in compact.items()
        if key not in {"scope", "sim_position", "live_position"}
    } | {
        "scope": {
            "candidate": bool(compact.get("scope", {}).get("candidate")),
            "holding": bool(compact.get("scope", {}).get(holding_key)),
        },
        "position": compact.get(position_key),
    }


def get_trading_overview(mode: TradingMode) -> dict[str, Any]:
    """Return one account and only that account's decision universe."""
    with _connect() as conn:
        market, rows, as_of = _snapshot(conn, mode)
    market_context = _object(market.get("market"))
    indices = _object(market_context.get("indices"))
    sector_context = _object(market_context.get("sector_rotation"))
    policy_context = _list(market_context.get("policy_context"))
    sectors = _list(sector_context.get("signals"))
    universe = []
    for row in rows:
        item = _object(row["payload"])
        quote = _object(item.get("quote"))
        technical = _object(item.get("technical"))
        screen = _object(item.get("screen"))
        extra = _object(screen.get("extra"))
        selector = _object(extra.get("selector"))
        ai_selection = _object(extra.get("ai_selection"))
        zone = _zone(technical, selector)
        position = _object(
            item.get("position") if mode == "simulated" else item.get("live_position")
        )
        holding_key = "is_sim_holding" if mode == "simulated" else "is_live_holding"
        universe.append({
            "code": str(row["code"]).zfill(6),
            "name": item.get("name") or row["name"],
            "scope": {
                "candidate": bool(row["is_candidate"]),
                "holding": bool(row[holding_key]),
            },
            "screen_score": row["screen_score"],
            "screen_signal": row["screen_signal"],
            "ai_rank": ai_selection.get("rank"),
            "ai_confidence": ai_selection.get("confidence"),
            "zone": zone,
            "entry_route": str(selector.get("entry_route") or "unclassified"),
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "profit_pct": position.get("profit_pct"),
            "available_to_sell": position.get("available_to_sell"),
        })
    def universe_priority(item: dict[str, Any]) -> tuple[int, int, float, str]:
        try:
            ai_rank = int(item.get("ai_rank") or 9999)
        except (TypeError, ValueError):
            ai_rank = 9999
        try:
            screen_score = float(item.get("screen_score") or 0)
        except (TypeError, ValueError):
            screen_score = 0.0
        return (
            0 if item["scope"]["holding"] else 1,
            ai_rank,
            -screen_score,
            item["code"],
        )

    universe.sort(key=universe_priority)
    return {
        "schema": f"stock_{mode}_trading_overview.v1",
        "mode": mode,
        "as_of": as_of,
        "stage": market.get("stage"),
        "refresh": market.get("refresh") or {},
        "market": {
            "regime": market_context.get("regime") or {
                "regime": "neutral",
                "summary": "市场状态数据不可用，按中性处理",
                "source": "deterministic_indices.v1",
            },
            "indices": [
                {"name": row.get("name"), "change_pct": row.get("change_pct")}
                for row in indices.values()
                if isinstance(row, dict) and row.get("name")
            ],
            "sector_context": {
                "status": sector_context.get("status") or "unavailable",
                "as_of": sector_context.get("created_at"),
                "source": sector_context.get("source"),
                "error": sector_context.get("error"),
            },
            "leading_sectors": [
                {"name": row.get("name"), "score": row.get("score"), "stage": row.get("stage")}
                for row in sectors[:5]
                if isinstance(row, dict)
            ],
            "policy_context": policy_context[:5],
        },
        "account": market.get("account") or {},
        "account_policy": market.get("account_policy") or {},
        "universe": universe,
        "required_evidence_codes": [row["code"] for row in universe],
    }


def get_stock_evidence(
    codes: list[str], as_of: str, mode: TradingMode,
) -> dict[str, Any]:
    """Return detailed evidence projected for exactly one account mode."""
    normalized = list(dict.fromkeys(str(code).strip().zfill(6) for code in codes if str(code).strip()))
    if not normalized:
        raise ValueError("codes must not be empty")
    if len(normalized) > MAX_EVIDENCE_CODES:
        raise ValueError(f"at most {MAX_EVIDENCE_CODES} codes per call")
    with _connect() as conn:
        _, rows, current_as_of = _snapshot(conn, mode, as_of)
    by_code = {str(row["code"]).zfill(6): row for row in rows}
    unknown = [code for code in normalized if code not in by_code]
    if unknown:
        raise ValueError("codes outside current decision universe: " + ",".join(unknown))
    evidence = [
        _mode_stock(
            _compact_stock(
                _object(by_code[code]["payload"]),
                str(by_code[code]["updated_at"]),
            ),
            mode,
        )
        for code in normalized
    ]
    return {
        "schema": f"stock_{mode}_evidence.v1",
        "mode": mode,
        "as_of": current_as_of,
        "count": len(evidence),
        "stocks": evidence,
    }


def get_recent_trading_activity(
    as_of: str, mode: TradingMode, limit: int = 10,
) -> dict[str, Any]:
    """Return only the selected account's recent activity."""
    safe_limit = max(1, min(int(limit), 20))
    with _connect() as conn:
        _, _, current_as_of = _snapshot(conn, mode, as_of)
        if mode == "simulated":
            activity = [dict(row) for row in conn.execute(
                """SELECT order_id, code, name, direction, price, volume, amount,
                          reason, strategy, created_at
                     FROM orders ORDER BY id DESC LIMIT ?""",
                (safe_limit,),
            ).fetchall()]
        else:
            activity = [dict(row) for row in conn.execute(
                """SELECT intent_id, code, name, action, suggested_price,
                          suggested_volume, suggested_amount, limit_price,
                          reason, status, expires_at, filled_price,
                          filled_volume, filled_at, user_note, created_at
                     FROM live_trade_intents
                    ORDER BY id DESC LIMIT ?""",
                (safe_limit,),
            ).fetchall()]
    return {
        "schema": f"stock_{mode}_trading_activity.v1",
        "mode": mode,
        "as_of": current_as_of,
        "activity": activity,
    }


def build_execution_context(
    expected_as_of: str,
    mode: TradingMode,
    expected_stage: str | None = None,
) -> dict[str, Any]:
    """Rebuild one executor's context from its account-specific DB version."""
    with _connect() as conn:
        market, rows, as_of = _snapshot(conn, mode, expected_as_of)
    stage = str(market.get("stage") or "")
    if expected_stage and stage != expected_stage:
        raise ValueError(f"trading stage changed: requested {expected_stage}, current {stage}")
    compact = [
        _mode_stock(
            _compact_stock(_object(row["payload"]), str(row["updated_at"])), mode,
        )
        for row in rows
    ]
    positions = [row for row in compact if row["scope"]["holding"]]
    position_codes = {row["code"] for row in positions}
    candidates = [
        row for row in compact
        if row["scope"]["candidate"] and row["code"] not in position_codes
    ]
    market_context = _object(market.get("market"))
    market_regime = market_context.get("regime")
    return {
        "schema": f"{mode}_trading_execution_context.v1",
        "status": "ok",
        "mode": mode,
        "stage": stage,
        "as_of": as_of,
        "account": market.get("account") or {},
        "risk": market.get("account_policy") or {},
        "market_regime": market_regime if isinstance(market_regime, dict) else {},
        "positions": positions,
        "candidates": candidates,
        "required_evidence_codes": [row["code"] for row in [*positions, *candidates]],
    }
