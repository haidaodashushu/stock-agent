"""Versioned active-candidate board independent from trading decisions."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any

from data.candidate_observations import latest_radar_candidates
from data.candidate_promotion import load_active_promotions
from data.candidate_lifecycle import load_lifecycle_candidates_by_codes
from data.opening_auction import latest_auction_watch_candidates
from data.store.sqlite_store import StockStore


ACTIVE_CANDIDATE_LIMIT = 15
AUCTION_CANDIDATE_LIMIT = 3
RADAR_CANDIDATE_LIMIT = 5
ENTRY_ROUTES = {"early_start", "strong_continuation"}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return default if math.isnan(number) or math.isinf(number) else number
    except (TypeError, ValueError):
        return default


def _priority(item: dict[str, Any]) -> tuple[int, float]:
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    ai = extra.get("ai_selection") if isinstance(extra.get("ai_selection"), dict) else {}
    try:
        rank = int(ai.get("rank"))
    except (TypeError, ValueError):
        rank = 9999
    return rank, -_float(item.get("score"))


def _morning_candidates(
    store: StockStore, trade_date: str,
) -> tuple[list[dict[str, Any]], str]:
    conn = store._get_conn()
    try:
        rows = conn.execute(
            """SELECT run_date,run_time,code,name,price,score,signal_type,
                      strategies,concepts,trend,pct_change,vol_ratio,extra,created_at
                 FROM screen_records WHERE run_date=?
                 ORDER BY score DESC,run_time DESC""",
            (trade_date,),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for raw in rows:
        item = dict(raw)
        item["code"] = str(item.get("code") or "").zfill(6)
        try:
            item["extra"] = json.loads(item.get("extra") or "{}")
        except (TypeError, json.JSONDecodeError):
            item["extra"] = {}
        extra = item["extra"]
        selector = extra.get("selector") if isinstance(extra.get("selector"), dict) else {}
        route = str(selector.get("entry_route") or "unclassified")
        # ``screen_records`` contains the final agent selection, not the broad
        # quantitative discovery feed.  Publishing a stock there is the daily
        # candidate-qualification decision; normal trading still decides
        # whether and when to buy it.
        selector["entry_route"] = route
        selector["setup_stage"] = "actionable"
        selector["buy_eligible"] = route in ENTRY_ROUTES
        selector["qualification_source"] = "daily_final_selection"
        extra["selector"] = selector
        item["extra"] = extra
        item["signal_type"] = "daily_candidate"
        result.append(item)
    result.sort(key=_priority)
    # ``run_date`` is the target trading date, while a nightly selection may
    # have actually been generated the previous evening.  Use the persisted
    # creation timestamp so source_versions never advertises future data.
    version = max((str(row.get("created_at") or "") for row in result), default="")
    return result, version


def _latest_radar_version(store: StockStore, now: datetime) -> str:
    conn = store._get_conn()
    try:
        row = conn.execute(
            """SELECT as_of FROM intraday_radar_runs
                WHERE status='ready' AND as_of<=? ORDER BY as_of DESC LIMIT 1""",
            (now.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchone()
    finally:
        conn.close()
    return str(row["as_of"] or "") if row else ""


def _latest_auction_version(store: StockStore, trade_date: str, now: datetime) -> str:
    conn = store._get_conn()
    try:
        row = conn.execute(
            """SELECT completed_at FROM opening_auction_runs
                WHERE trade_date=? AND phase='final' AND completed_at<=?
                ORDER BY completed_at DESC LIMIT 1""",
            (trade_date, now.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
    finally:
        conn.close()
    return str(row["completed_at"] or "") if row else ""


def _latest_promotion_version(store: StockStore, trade_date: str, now: datetime) -> str:
    conn = store._get_conn()
    try:
        row = conn.execute(
            """SELECT MAX(updated_at) AS updated_at
                 FROM intraday_candidate_promotions
                WHERE trade_date=? AND status='active' AND expires_at>=?""",
            (trade_date, now.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
    finally:
        conn.close()
    return str(row["updated_at"] or "") if row else ""


def _lifecycle_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Expose cross-day evidence without letting it control membership."""
    return {
        key: row.get(key)
        for key in (
            "state", "entry_route", "first_seen_date", "last_seen_date",
            "last_improved_date", "observation_sessions", "improving_streak",
            "stale_sessions", "previous_score", "current_score", "best_score",
            "setup_score", "invalidation_reason", "updated_at",
        )
    }


def _dynamic_item(row: dict[str, Any]) -> tuple[dict[str, Any], str]:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    triggers = row.get("triggers") if isinstance(row.get("triggers"), list) else []
    risk_tags = row.get("risk_tags") if isinstance(row.get("risk_tags"), list) else []
    is_auction = evidence.get("source") == "strategic_pool_opening_auction_watch"
    source = "opening_auction_watch" if is_auction else "intraday_radar"
    source_label = "集合竞价观察" if is_auction else "盘中雷达"
    setup = evidence.get("setup") if isinstance(evidence.get("setup"), dict) else {}
    position = _float(setup.get("position_60d_pct"), 50.0)
    zone = "低位" if position <= 35 else ("高位" if position >= 75 else "中位")
    payload = {
        "as_of": row.get("as_of"), "rank": row.get("rank"),
        "score": row.get("score"), "theme_group": row.get("theme_group"),
        "triggers": triggers, "risk_tags": risk_tags, "evidence": evidence,
    }
    item = {
        "run_date": str(row.get("as_of") or row.get("trade_date") or "")[:10],
        "run_time": str(row.get("as_of") or "")[11:19],
        "code": str(row.get("code") or "").zfill(6),
        "name": row.get("name") or str(row.get("code") or "").zfill(6),
        "price": row.get("price"), "score": row.get("score"),
        "signal_type": "auction_watch" if is_auction else "radar",
        "strategies": "|".join([source_label, *triggers[:4]]),
        "concepts": json.dumps([row.get("theme_group")], ensure_ascii=False),
        "trend": "", "pct_change": row.get("change_pct"),
        "vol_ratio": (evidence.get("metrics") or {}).get("volume_pace_20d"),
        "extra": {
            source: payload,
            "selector": {
                "zone": zone,
                "entry_route": "unclassified",
                "theme_group": row.get("theme_group"),
                # Auction and radar are discovery/verification facts.  They may
                # augment an already-qualified daily/lifecycle candidate but a
                # dynamic-only row cannot promote itself directly into a buy.
                "buy_eligible": False,
                "risk_tags": "|".join(risk_tags),
            },
            "ai_selection": {
                "rank": 100 + int(row.get("rank") or 0), "confidence": "medium",
                "reason": "；".join(triggers[:4]), "risk": "；".join(risk_tags[:3]),
            },
        },
    }
    return item, source


def _promotion_item(row: dict[str, Any]) -> tuple[dict[str, Any], str]:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    sources = evidence.get("sources") if isinstance(evidence.get("sources"), dict) else {}
    radar = sources.get("intraday_radar") if isinstance(sources.get("intraday_radar"), dict) else {}
    auction = sources.get("opening_auction_watch") \
        if isinstance(sources.get("opening_auction_watch"), dict) else {}
    source = radar or auction
    triggers = source.get("triggers") if isinstance(source.get("triggers"), list) else []
    risks = source.get("risk_tags") if isinstance(source.get("risk_tags"), list) else []
    entry_route = str(row.get("entry_route") or "unclassified")
    promotion = {
        "state": "promoted",
        "promoted_at": row.get("promoted_at"),
        "expires_at": row.get("expires_at"),
        "entry_route": entry_route,
        "confidence": row.get("confidence"),
        "reason": row.get("reason"),
        "risk": row.get("risk"),
        "source_types": row.get("source_types") or [],
    }
    return {
        "run_date": row.get("trade_date"),
        "run_time": str(row.get("promoted_at") or "")[11:19],
        "code": str(row.get("code") or "").zfill(6),
        "name": row.get("name") or evidence.get("name") or row.get("code"),
        "price": evidence.get("price"),
        "score": evidence.get("score"),
        "signal_type": "intraday_promoted",
        "strategies": "|".join(["盘中独立晋升", *triggers[:4]]),
        "concepts": json.dumps([evidence.get("theme_group")], ensure_ascii=False),
        "trend": "",
        "pct_change": evidence.get("change_pct"),
        "vol_ratio": ((source.get("evidence") or {}).get("metrics") or {}).get("volume_pace_20d"),
        "extra": {
            "candidate_promotion": promotion,
            "selector": {
                "entry_route": entry_route,
                "setup_stage": "actionable",
                "buy_eligible": True,
                "promotion_state": "promoted",
                "risk_tags": "|".join(risks),
                "theme_group": evidence.get("theme_group") or "",
            },
            "ai_selection": {
                "rank": 12 if row.get("confidence") == "strong" else 18,
                "confidence": row.get("confidence") or "medium",
                "reason": row.get("reason") or "",
                "risk": row.get("risk") or "",
            },
            **({"intraday_radar": radar} if radar else {}),
            **({"opening_auction_watch": auction} if auction else {}),
        },
    }, "intraday_promotion"


def compose_candidate_board(
    store: StockStore, *, trade_date: str, now: datetime,
    active_limit: int = ACTIVE_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    """Compose the formal candidate pool without mutating source records.

    Only the daily final selection and independently approved promotions can
    occupy one of the 15 formal slots.  Lifecycle and raw auction/radar rows are
    observation evidence and never enter the trading scope by themselves.
    """
    morning, morning_version = _morning_candidates(store, trade_date)
    lifecycle_by_code = {
        str(row.get("code") or "").zfill(6): row
        for row in load_lifecycle_candidates_by_codes(
            store, [item["code"] for item in morning],
        )
    }
    for item in morning:
        lifecycle = lifecycle_by_code.get(item["code"])
        if lifecycle:
            item["extra"]["candidate_lifecycle"] = _lifecycle_metadata(lifecycle)
    auction_rows = latest_auction_watch_candidates(
        store, now=now, limit=AUCTION_CANDIDATE_LIMIT,
    )
    radar_rows = latest_radar_candidates(store, now=now, limit=RADAR_CANDIDATE_LIMIT)
    promotion_rows = load_active_promotions(store, trade_date=trade_date, now=now)
    source_versions = {
        "morning": morning_version,
        "auction": _latest_auction_version(store, trade_date, now),
        "radar": _latest_radar_version(store, now),
        "promotion": _latest_promotion_version(store, trade_date, now),
    }

    morning = [
        item for item in morning
        if bool(((item.get("extra") or {}).get("selector") or {}).get("buy_eligible"))
    ]
    by_code = {item["code"]: item for item in morning}
    source_types = {item["code"]: ["daily_final_selection"] for item in morning}
    promotion_codes: list[str] = []
    for row in promotion_rows:
        promoted, source = _promotion_item(row)
        code = promoted["code"]
        promotion_sources = [
            str(value) for value in (row.get("source_types") or []) if value
        ]
        promotion_codes.append(code)
        if code in by_code:
            existing = by_code[code]
            extra = existing.get("extra") if isinstance(existing.get("extra"), dict) else {}
            promoted_extra = promoted["extra"]
            extra["candidate_promotion"] = promoted_extra["candidate_promotion"]
            for key in ("intraday_radar", "opening_auction_watch"):
                if promoted_extra.get(key):
                    extra[key] = promoted_extra[key]
            selector = extra.get("selector") if isinstance(extra.get("selector"), dict) else {}
            selector.update(promoted_extra["selector"])
            extra["selector"] = selector
            extra["ai_selection"] = promoted_extra["ai_selection"]
            existing["extra"] = extra
            existing["signal_type"] = "intraday_promoted"
            existing["strategies"] = "|".join(dict.fromkeys([
                *str(existing.get("strategies") or "").split("|"), "盘中独立晋升",
            ])).strip("|")
            source_types[code] = list(dict.fromkeys([
                *source_types[code], *promotion_sources, source,
            ]))
        else:
            by_code[code] = promoted
            source_types[code] = list(dict.fromkeys([*promotion_sources, source]))
    dynamic_expiry: dict[str, str] = {}
    for row in [*auction_rows, *radar_rows]:
        dynamic, source = _dynamic_item(row)
        code = dynamic["code"]
        dynamic_expiry[code] = max(dynamic_expiry.get(code, ""), str(row.get("expires_at") or ""))
        if code in by_code:
            existing = by_code[code]
            extra = existing.get("extra") if isinstance(existing.get("extra"), dict) else {}
            dynamic_extra = dynamic["extra"]
            extra[source] = dynamic_extra[source]
            # Dynamic evidence augments a morning candidate. It cannot replace
            # the morning selector's eligibility or AI attention rank.
            extra.setdefault("selector", dynamic_extra["selector"])
            extra.setdefault("ai_selection", dynamic_extra["ai_selection"])
            existing["extra"] = extra
            existing["strategies"] = "|".join(dict.fromkeys([
                *str(existing.get("strategies") or "").split("|"),
                "集合竞价观察" if source == "opening_auction_watch" else "盘中雷达",
            ])).strip("|")
            source_types[code] = list(dict.fromkeys([*source_types[code], source]))
        # A raw dynamic observation that has not been promoted deliberately
        # stops here; it belongs to the observation pool, not this board.

    morning_codes = {item["code"] for item in morning}
    promotion_only = [
        by_code[code] for code in promotion_codes if code not in morning_codes
    ]
    promotion_only.sort(key=_priority)
    morning_ordered = sorted(
        [item for item in by_code.values() if item["code"] in morning_codes],
        key=_priority,
    )
    active_limit = max(1, int(active_limit))
    # A successful independent promotion is a current buy-qualification fact.
    # When the pool is full it displaces the weakest daily selection; raw
    # observations never consume or replace a slot.
    promoted_active = promotion_only[:active_limit]
    remaining = max(0, active_limit - len(promoted_active))
    active = [*promoted_active, *morning_ordered[:remaining]]
    dropped_morning = morning_ordered[remaining:]
    replacements: dict[str, str] = {}
    open_slots = max(0, active_limit - len(morning_ordered))
    replacing_promotions = promoted_active[open_slots:]
    for incoming, outgoing in zip(replacing_promotions, reversed(dropped_morning)):
        replacements[incoming["code"]] = outgoing["code"]

    for rank, item in enumerate(active, 1):
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        sources = source_types.get(item["code"], ["daily_final_selection"])
        replaced = replacements.get(item["code"], "")
        extra["candidate_board"] = {
            "state": "candidate", "rank": rank, "source_types": sources,
            "primary_source": sources[-1], "replaced_code": replaced,
            "replacement_reason": (
                f"观察股通过统一买入判断，替换候选池最弱股票{replaced}"
                if replaced else "每日最终预选或统一晋升进入候选池"
            ),
        }
        item["extra"] = extra

    fingerprint_payload = {
        "trade_date": trade_date, "active_limit": active_limit,
        "source_versions": source_versions,
        # Include complete payloads so a same-code evidence or eligibility
        # update still creates a new immutable board version.
        "active": active,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "trade_date": trade_date, "active_limit": active_limit,
        "active": active, "reserve": [], "source_types": source_types,
        "dynamic_expiry": dynamic_expiry, "source_versions": source_versions,
        "source_fingerprint": fingerprint,
        "counts": {
            "morning": len(morning), "lifecycle": 0,
            "auction": len(auction_rows), "radar": len(radar_rows),
            "promotion": len(promotion_rows),
            "active": len(active), "reserve": 0,
        },
    }


def refresh_candidate_board(
    store: StockStore | None = None, *, now: datetime | None = None,
    trade_date: str | None = None, active_limit: int = ACTIVE_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    store = store or StockStore()
    now = now or datetime.now()
    trade_date = trade_date or now.date().isoformat()
    board = compose_candidate_board(
        store, trade_date=trade_date, now=now, active_limit=active_limit,
    )
    conn = store._get_conn()
    try:
        previous = conn.execute(
            """SELECT as_of,source_fingerprint FROM candidate_board_runs
                WHERE trade_date=? AND status='ready' ORDER BY as_of DESC LIMIT 1""",
            (trade_date,),
        ).fetchone()
        if previous and previous["source_fingerprint"] == board["source_fingerprint"]:
            return {
                "status": "unchanged", "as_of": previous["as_of"],
                "trade_date": trade_date, **board["counts"],
            }

        as_of = now.strftime("%Y-%m-%d %H:%M:%S")
        counts = board["counts"]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO candidate_board_runs
               (as_of,trade_date,status,active_limit,active_count,reserve_count,
                morning_count,auction_count,radar_count,source_versions,source_fingerprint)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (as_of, trade_date, "ready", board["active_limit"], counts["active"],
             counts["reserve"], counts["morning"], counts["auction"], counts["radar"],
             json.dumps(board["source_versions"], ensure_ascii=False), board["source_fingerprint"]),
        )
        member_rows = []
        for state in ("active",):
            for rank, item in enumerate(board[state], 1):
                code = item["code"]
                board_meta = item["extra"]["candidate_board"]
                sources = board["source_types"].get(code, [])
                selector = item["extra"].get("selector") if isinstance(item["extra"], dict) else {}
                expires_at = (
                    f"{trade_date} 15:05:00" if "daily_final_selection" in sources
                    else board["dynamic_expiry"].get(code, f"{trade_date} 15:05:00")
                )
                member_rows.append((
                    as_of, state, rank, code, item.get("name", ""), sources[-1] if sources else "",
                    json.dumps(sources, ensure_ascii=False), int(bool((selector or {}).get("buy_eligible"))),
                    board_meta.get("replaced_code", ""), board_meta.get("replacement_reason", ""),
                    expires_at, json.dumps(item, ensure_ascii=False),
                ))
        conn.executemany(
            """INSERT INTO candidate_board_members
               (as_of,state,rank,code,name,primary_source,source_types,buy_eligible,
                replaced_code,replacement_reason,expires_at,payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            member_rows,
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ready", "as_of": as_of, "trade_date": trade_date, **counts}


def load_active_candidate_board(
    store: StockStore, *, trade_date: str, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read one immutable ready board; never compose or replace on this path."""
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    conn = store._get_conn()
    try:
        run = conn.execute(
            """SELECT as_of FROM candidate_board_runs
                WHERE trade_date=? AND status='ready' AND as_of<=?
                ORDER BY as_of DESC LIMIT 1""",
            (trade_date, stamp),
        ).fetchone()
        if not run:
            return []
        rows = conn.execute(
            """SELECT payload FROM candidate_board_members
                WHERE as_of=? AND state='active' AND (expires_at='' OR expires_at>=?)
                ORDER BY rank""",
            (run["as_of"], stamp),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row["payload"]) for row in rows]


def candidate_board_status(
    store: StockStore, *, trade_date: str, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    conn = store._get_conn()
    try:
        row = conn.execute(
            """SELECT as_of,status,active_count,reserve_count,morning_count,
                      auction_count,radar_count,source_versions
                 FROM candidate_board_runs
                WHERE trade_date=? AND as_of<=? ORDER BY as_of DESC LIMIT 1""",
            (trade_date, now.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"status": "missing", "as_of": "", "active_count": 0}
    result = dict(row)
    try:
        result["source_versions"] = json.loads(result.get("source_versions") or "{}")
    except json.JSONDecodeError:
        result["source_versions"] = {}
    return result
