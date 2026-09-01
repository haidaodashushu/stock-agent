"""Persistent candidate lifecycle built from daily full-market discoveries.

The daily quantitative pool is an observation feed, not a disposable final
list.  This module records each observation and advances a stock only when its
setup improves across complete daily bars.  Intraday trading remains a separate
consumer and cannot mutate these states.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from data.candidate_promotion import promotion_health
from data.agent_submissions import agent_runtime_health
from data.strategic_theme_pool import load_strategic_pool


ACTIVE_STATES = ("warming", "actionable")
STATE_RANK = {
    "expired": 0,
    "invalidated": 0,
    "cooling": 1,
    "preparing": 2,
    "warming": 3,
    "actionable": 4,
}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if not value:
        return []
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if item]
        except (TypeError, json.JSONDecodeError):
            pass
    return [item for item in str(value).split("|") if item]


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["code"] = str(result.get("code") or "").zfill(6)
    return result


def _desired_state(row: Mapping[str, Any]) -> str:
    stage = str(row.get("setup_stage") or "preparing")
    if stage not in {"preparing", "warming", "actionable", "invalidated"}:
        return "preparing"
    return stage


def _invalidation_reason(row: Mapping[str, Any]) -> str:
    risks = _list(row.get("setup_risks")) or _list(row.get("risk_tags"))
    return "；".join(risks[:3])


@dataclass(frozen=True)
class LifecycleUpdate:
    code: str
    from_state: str
    to_state: str
    reason: str


def record_discoveries(
    conn,
    rows: Iterable[Mapping[str, Any]],
    *,
    as_of: str,
    evidence_date: str,
    target_trade_date: str,
    updated_at: str,
) -> list[LifecycleUpdate]:
    """Record one discovery snapshot and update cross-day lifecycle states.

    Multiple night/morning runs based on the same complete bar update evidence
    but do not count as multiple observation sessions.
    """
    candidates = [_payload(row) for row in rows]
    seen_codes = {row["code"] for row in candidates}
    previous_rows = {
        str(row["code"]).zfill(6): row
        for row in conn.execute("SELECT * FROM candidate_lifecycle").fetchall()
    }
    updates: list[LifecycleUpdate] = []

    for rank, row in enumerate(candidates, 1):
        code = row["code"]
        desired = _desired_state(row)
        route = str(row.get("entry_route") or "unclassified")
        setup_score = _float(row.get("setup_score"))
        final_score = _float(row.get("final_score", row.get("score")))
        eligible = _bool(row.get("buy_eligible")) and desired == "actionable"
        serialized = json.dumps(row, ensure_ascii=False)
        conn.execute(
            """INSERT INTO candidate_discovery_history
               (as_of,evidence_date,target_trade_date,rank,code,name,entry_route,
                setup_stage,setup_score,final_score,buy_eligible,evidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                as_of, evidence_date, target_trade_date, rank, code,
                str(row.get("name") or ""), route, desired, setup_score,
                final_score, int(eligible), serialized,
            ),
        )

        previous = previous_rows.get(code)
        if previous is None:
            # A single daily bar may create a strong-continuation action setup;
            # early-start setups require a second complete-bar observation.
            state = desired
            if desired == "actionable" and route != "strong_continuation":
                state = "warming"
                eligible = False
            reason = "首次发现" if state == "preparing" else f"首次发现并进入{state}"
            conn.execute(
                """INSERT INTO candidate_lifecycle
                   (code,name,state,entry_route,first_seen_date,last_seen_date,
                    last_evidence_date,last_improved_date,observation_sessions,
                    improving_streak,stale_sessions,previous_score,current_score,
                    best_score,setup_score,buy_eligible,invalidation_reason,payload,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    code, str(row.get("name") or ""), state, route,
                    target_trade_date, target_trade_date, evidence_date,
                    evidence_date if state in ACTIVE_STATES else "", 1, 0, 0,
                    final_score, final_score, final_score, setup_score,
                    0,
                    _invalidation_reason(row) if state == "invalidated" else "",
                    serialized, updated_at,
                ),
            )
            updates.append(LifecycleUpdate(code, "", state, reason))
            conn.execute(
                """INSERT INTO candidate_lifecycle_events
                   (code,evidence_date,from_state,to_state,reason,payload,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (code, evidence_date, "", state, reason, serialized, updated_at),
            )
            continue

        old = dict(previous)
        new_session = str(old["last_evidence_date"]) != evidence_date
        observations = int(old["observation_sessions"] or 0) + int(new_session)
        old_score = _float(old["current_score"])
        old_setup = _float(old["setup_score"])
        old_state = str(old["state"])
        improved = (
            STATE_RANK.get(desired, 0) > STATE_RANK.get(old_state, 0)
            or setup_score >= old_setup + 0.5
            or final_score >= old_score + 0.8
        )
        regressed = (
            desired == "invalidated"
            or STATE_RANK.get(desired, 0) < STATE_RANK.get(old_state, 0)
            or final_score <= old_score - 1.2
        )

        state = desired
        multi_day_early_ready = (
            route == "early_start"
            and observations >= 2
            and desired in {"warming", "actionable"}
            and setup_score >= 4.5
            and not regressed
            and not _invalidation_reason(row)
        )
        if multi_day_early_ready:
            # A controlled second-day pullback can be a better entry than the
            # first impulse.  The lifecycle supplies persistence while the
            # intraday consumer still has to verify VWAP/support retention.
            state = "actionable"
            eligible = False
        elif desired == "actionable" and route != "strong_continuation" and observations < 2:
            state = "warming"
        elif desired == "preparing" and old_state in ACTIVE_STATES and not regressed:
            # One quiet bar does not erase a multi-day setup.
            state = "warming"
        elif regressed and desired != "invalidated" and old_state in ACTIVE_STATES:
            state = "cooling"

        streak = 0
        if new_session:
            streak = int(old["improving_streak"] or 0) + 1 if improved else 0
        else:
            streak = int(old["improving_streak"] or 0)
        # Lifecycle is observation evidence only.  Formal qualification is
        # granted exclusively by daily final selection or independent promotion.
        eligible = False
        reason = (
            _invalidation_reason(row) or "结构失效"
            if state == "invalidated"
            else "证据持续改善" if improved
            else "证据衰减，进入冷却" if state == "cooling"
            else "更新同一交易日证据" if not new_session
            else "继续观察"
        )
        conn.execute(
            """UPDATE candidate_lifecycle
                  SET name=?,state=?,entry_route=?,last_seen_date=?,last_evidence_date=?,
                      last_improved_date=?,observation_sessions=?,improving_streak=?,
                      stale_sessions=0,previous_score=?,current_score=?,best_score=?,
                      setup_score=?,buy_eligible=?,invalidation_reason=?,payload=?,updated_at=?
                WHERE code=?""",
            (
                str(row.get("name") or old["name"]), state, route,
                target_trade_date, evidence_date,
                evidence_date if improved else str(old["last_improved_date"] or ""),
                observations, streak, old_score, final_score,
                max(_float(old["best_score"]), final_score), setup_score,
                int(eligible), _invalidation_reason(row) if state == "invalidated" else "",
                serialized, updated_at, code,
            ),
        )
        if state != old_state or improved:
            updates.append(LifecycleUpdate(code, old_state, state, reason))
            conn.execute(
                """INSERT INTO candidate_lifecycle_events
                   (code,evidence_date,from_state,to_state,reason,payload,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (code, evidence_date, old_state, state, reason, serialized, updated_at),
            )

    # Candidates that disappear from a new complete-bar TOP pool decay rather
    # than vanishing.  Same-bar reruns must not age them twice.
    if candidates:
        placeholders = ",".join("?" for _ in seen_codes)
        missing = conn.execute(
            f"""SELECT * FROM candidate_lifecycle
                  WHERE code NOT IN ({placeholders})
                    AND last_evidence_date < ?
                    AND state NOT IN ('invalidated','expired')""",
            [*seen_codes, evidence_date],
        ).fetchall()
    else:
        missing = []
    for raw in missing:
        old = dict(raw)
        stale = int(old["stale_sessions"] or 0) + 1
        state = "expired" if stale >= 3 else "cooling"
        reason = "连续未进入全市场发现池，退出候选" if state == "expired" else "本轮未再发现，进入冷却"
        conn.execute(
            """UPDATE candidate_lifecycle
                  SET state=?,stale_sessions=?,buy_eligible=0,last_evidence_date=?,
                      invalidation_reason=?,updated_at=? WHERE code=?""",
            (state, stale, evidence_date, reason if state == "expired" else "", updated_at, old["code"]),
        )
        if state != old["state"]:
            updates.append(LifecycleUpdate(old["code"], old["state"], state, reason))
            conn.execute(
                """INSERT INTO candidate_lifecycle_events
                   (code,evidence_date,from_state,to_state,reason,payload,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (old["code"], evidence_date, old["state"], state, reason, old["payload"], updated_at),
            )
    return updates


def load_lifecycle_candidates_by_codes(
    store, codes: list[str],
) -> list[dict[str, Any]]:
    """Load cross-day evidence for existing formal candidates only."""
    normalized = list(dict.fromkeys(
        str(code).strip().zfill(6) for code in codes if str(code).strip()
    ))
    if not normalized:
        return []
    placeholders = ",".join("?" for _ in normalized)
    conn = store._get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM candidate_lifecycle WHERE code IN ({placeholders})",
            normalized,
        ).fetchall()
    finally:
        conn.close()
    by_code = {str(row["code"]).zfill(6): dict(row) for row in rows}
    return [by_code[code] for code in normalized if code in by_code]


def overlay_latest_candidate_quotes(
    payload: dict[str, Any], quotes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Overlay Web-only quote marks without mutating persisted evidence."""
    for group in ("candidates", "observations"):
        for row in payload.get(group) or []:
            code = str(row.get("code") or "").zfill(6)
            quote = quotes.get(code) or {}
            if not quote.get("price"):
                continue
            row.setdefault("signal_price", row.get("price"))
            row.setdefault("signal_change_pct", row.get("change_pct"))
            row["price"] = quote["price"]
            row["change_pct"] = quote.get("day_change_pct")
            row["quote_as_of"] = quote.get("datetime") or ""
            row["quote_source"] = quote.get("source") or ""
    return payload


def load_lifecycle_snapshot(store, *, limit: int = 100) -> dict[str, Any]:
    """Return the two operational pools shown by Web.

    ``candidates`` is exactly the bounded formal scope consumed by trading.
    ``observations`` is the configured strategic pool minus formal candidates;
    lifecycle, auction and radar facts decorate it but cannot grant eligibility.
    """
    strategic = load_strategic_pool()
    strategic_stocks = strategic["stocks"]
    strategic_codes = list(strategic_stocks)
    conn = store._get_conn()
    try:
        run = conn.execute(
            """SELECT * FROM candidate_board_runs
                 ORDER BY as_of DESC LIMIT 1"""
        ).fetchone()
        member_rows = []
        if run:
            member_rows = conn.execute(
                """SELECT * FROM candidate_board_members
                     WHERE as_of=? AND state='active'
                     ORDER BY rank""",
                (run["as_of"],),
            ).fetchall()
        trade_date = str(run["trade_date"] if run else "")
        lifecycle_codes = list(dict.fromkeys([
            *strategic_codes,
            *(str(row["code"]).zfill(6) for row in member_rows),
        ]))
        if lifecycle_codes:
            placeholders = ",".join("?" for _ in lifecycle_codes)
            lifecycle_rows = conn.execute(
                f"SELECT * FROM candidate_lifecycle WHERE code IN ({placeholders})",
                lifecycle_codes,
            ).fetchall()
        else:
            lifecycle_rows = []
        radar_rows = conn.execute(
            """SELECT * FROM intraday_radar_candidates
                 WHERE substr(as_of,1,10)=? ORDER BY as_of,rank""",
            (trade_date,),
        ).fetchall() if trade_date else []
        auction_rows = conn.execute(
            """SELECT * FROM opening_auction_watch_candidates
                 WHERE trade_date=? ORDER BY generated_at,rank""",
            (trade_date,),
        ).fetchall() if trade_date else []
    finally:
        conn.close()

    lifecycle_by_code = {
        str(row["code"]).zfill(6): dict(row) for row in lifecycle_rows
    }
    candidates: list[dict[str, Any]] = []
    candidate_codes: set[str] = set()

    def lifecycle_details(code: str, fallback_payload: dict[str, Any]) -> dict[str, Any]:
        row = lifecycle_by_code.get(code) or {}
        try:
            evidence = json.loads(row.get("payload") or "{}") if row else {}
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        extra = fallback_payload.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        selector = extra.get("selector")
        if not isinstance(selector, dict):
            selector = {}
        promotion = extra.get("candidate_promotion")
        if not isinstance(promotion, dict):
            promotion = {}
        triggers = (
            _list(evidence.get("setup_triggers"))
            or _list(selector.get("setup_triggers"))
        )
        risks = (
            _list(evidence.get("setup_risks"))
            or _list(selector.get("setup_risks"))
            or _list(selector.get("risk_tags"))
        )
        return {
            "lifecycle_state": row.get("state") or "pending_first_scan",
            "entry_route": (
                row.get("entry_route")
                or evidence.get("entry_route")
                or selector.get("entry_route")
                or ""
            ),
            "setup_stage": row.get("state") or selector.get("setup_stage") or "",
            "setup_score": row.get("setup_score", selector.get("setup_score")),
            "observation_sessions": int(row.get("observation_sessions") or 0),
            "improving_streak": int(row.get("improving_streak") or 0),
            "current_score": row.get("current_score", fallback_payload.get("score")),
            "best_score": row.get("best_score"),
            "first_seen_date": row.get("first_seen_date") or "",
            "last_seen_date": row.get("last_seen_date") or "",
            "last_improved_date": row.get("last_improved_date") or "",
            "triggers": triggers[:8],
            "risks": risks[:6],
            "invalidation_reason": row.get("invalidation_reason") or "",
            "promotion_state": promotion.get("state") or "",
            "promoted_at": promotion.get("promoted_at") or "",
            "promotion_reason": promotion.get("reason") or "",
        }

    for raw in member_rows:
        member = dict(raw)
        code = str(member.get("code") or "").zfill(6)
        candidate_codes.add(code)
        try:
            payload = json.loads(member.get("payload") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        try:
            sources = json.loads(member.get("source_types") or "[]")
        except (TypeError, json.JSONDecodeError):
            sources = []
        candidates.append({
            "code": code,
            "name": member.get("name") or payload.get("name") or code,
            "price": payload.get("price"),
            "change_pct": payload.get("pct_change"),
            "pool_state": "candidate",
            "board_state": "active",
            "board_rank": int(member.get("rank") or 0),
            "primary_source": member.get("primary_source") or "",
            "sources": sources if isinstance(sources, list) else [],
            "buy_eligible": bool(member.get("buy_eligible")),
            "expires_at": member.get("expires_at") or "",
            **lifecycle_details(code, payload),
        })

    dynamic_by_code: dict[str, dict[str, Any]] = {}
    for raw in auction_rows:
        row = dict(raw)
        code = str(row.get("code") or "").zfill(6)
        dynamic_by_code[code] = {
            "source": "opening_auction_watch",
            "signal_at": str(row.get("generated_at") or ""),
            "expires_at": str(row.get("expires_at") or ""),
            "score": row.get("score"),
            "price": row.get("auction_price"),
            "change_pct": row.get("change_pct"),
            "triggers": _list(row.get("triggers"))[:8],
            "risks": _list(row.get("risk_tags"))[:6],
        }
    for raw in radar_rows:
        row = dict(raw)
        code = str(row.get("code") or "").zfill(6)
        dynamic_by_code[code] = {
            "source": "intraday_radar",
            "signal_at": str(row.get("as_of") or ""),
            "expires_at": str(row.get("expires_at") or ""),
            "score": row.get("score"),
            "price": row.get("price"),
            "change_pct": row.get("change_pct"),
            "triggers": _list(row.get("triggers"))[:8],
            "risks": _list(row.get("risk_tags"))[:6],
        }

    observations: list[dict[str, Any]] = []
    for code, meta in strategic_stocks.items():
        if code in candidate_codes:
            continue
        dynamic = dynamic_by_code.get(code) or {}
        lifecycle = lifecycle_details(code, {})
        observations.append({
            "code": code,
            "name": meta["name"],
            "theme_group": meta["group"],
            "pool_state": "observation",
            "price": dynamic.get("price"),
            "change_pct": dynamic.get("change_pct"),
            "buy_eligible": False,
            "observation_source": dynamic.get("source") or "strategic_theme_pool",
            "signal_at": dynamic.get("signal_at") or "",
            "expires_at": dynamic.get("expires_at") or "",
            "dynamic_score": dynamic.get("score"),
            "triggers": dynamic.get("triggers") or lifecycle.get("triggers") or [],
            "risks": dynamic.get("risks") or lifecycle.get("risks") or [],
            **{
                key: value for key, value in lifecycle.items()
                if key not in {"triggers", "risks"}
            },
        })
    observations.sort(key=lambda row: (
        0 if row["observation_source"] == "intraday_radar" else
        1 if row["observation_source"] == "opening_auction_watch" else 2,
        -_float(row.get("dynamic_score")),
        row["code"],
    ))

    state_counts: dict[str, int] = {}
    for row in lifecycle_rows:
        state = str(row["state"])
        state_counts[state] = state_counts.get(state, 0) + 1
    board = dict(run) if run else {}
    if board:
        try:
            board["source_versions"] = json.loads(board.get("source_versions") or "{}")
        except (TypeError, json.JSONDecodeError):
            board["source_versions"] = {}
    return {
        "strategy": "candidate_observation.v2",
        "as_of": str(board.get("as_of") or ""),
        "trade_date": str(board.get("trade_date") or ""),
        "board": board,
        "state_counts": state_counts,
        "candidate_limit": 15,
        "observation_pool": {
            "version": strategic.get("version"),
            "configured_count": strategic.get("target_size"),
            "description": strategic.get("description"),
        },
        "promotion_health": promotion_health(store, trade_date=trade_date or None),
        "agent_runtime_health": agent_runtime_health(store=store),
        "candidates": candidates[:15],
        "observations": observations[:max(1, int(limit))],
    }
