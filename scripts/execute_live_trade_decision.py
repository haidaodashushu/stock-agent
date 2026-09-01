#!/usr/bin/env python3
"""Execute AI live-trading decisions by creating manual trade intents only.

The script is the execution layer for live trading. It never connects to a
brokerage account. Buy/sell decisions become rows in live_trade_intents after
hard risk checks; hold/watch/noop decisions are reported but not persisted as
orders.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from account.reconcile import fetch_live_prices  # noqa: E402
from data.live_manual_account import (  # noqa: E402
    account_snapshot,
    blocked_prefix_text,
    expire_stale_proposed_intents,
    is_live_buy_allowed,
    load_config,
    validate_intent,
)
from data.market_calendar import ensure_actionable_trading_time  # noqa: E402
from data.store.sqlite_store import StockStore  # noqa: E402


TRADE_ACTIONS = {"buy", "sell"}
SELL_ALIASES = {"reduce", "clear"}
NOOP_ACTIONS = {"hold", "watch", "noop", "skip"}
VALID_ACTIONS = TRADE_ACTIONS | SELL_ALIASES | NOOP_ACTIONS
DEFAULT_MAX_DECISION_PRICE_DRIFT_PCT = 2.0
DEFAULT_BUY_PRICE_BUFFER_PCT = 1.5


def _load_json(path: str | None) -> Any:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.load(sys.stdin)


def _decisions(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("decisions", "signals"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
        if isinstance(payload.get("code"), (str, int)):
            return [payload]
    raise ValueError("decision payload must be a decision object, list, or {'decisions': [...]}")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _make_intent_id() -> str:
    return "L" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6].upper()


def _normalize(decision: dict[str, Any]) -> dict[str, Any]:
    code = str(decision.get("code", "")).strip().zfill(6)
    action = str(decision.get("action", "hold")).lower().strip()
    if action in SELL_ALIASES:
        action = "sell"
    if action not in VALID_ACTIONS:
        raise ValueError(f"{code}: invalid action {action!r}")
    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"invalid code {code!r}")
    return {
        "code": code,
        "name": str(decision.get("name") or code),
        "action": action,
        "confidence": str(decision.get("confidence", "medium")).lower().strip(),
        "price": _float(decision.get("price")),
        "volume": _int(decision.get("volume")),
        "target_amount": _float(decision.get("target_amount")),
        "sell_pct": _float(decision.get("sell_pct"), 1.0),
        "limit_price": _float(decision.get("limit_price")),
        "expire_minutes": max(1, _int(decision.get("expire_minutes"), 15)),
        "reason": str(decision.get("reason", "")).strip()[:300],
        "risk": str(decision.get("risk", "")).strip()[:300],
        "raw": decision,
    }


def _live_position(snap: dict[str, Any], code: str) -> dict[str, Any] | None:
    for pos in snap.get("positions") or []:
        if str(pos.get("code", "")).zfill(6) == code:
            return pos
    return None


def _derive_volume(decision: dict[str, Any], snap: dict[str, Any], price: float) -> int:
    volume = int(decision["volume"] or 0)
    if volume > 0:
        return volume
    if price <= 0:
        return 0
    if decision["action"] == "buy" and decision["target_amount"] > 0:
        return int(decision["target_amount"] / price / 100) * 100
    if decision["action"] == "sell":
        pos = _live_position(snap, decision["code"])
        if not pos:
            return 0
        pct = decision["sell_pct"] or 1.0
        if pct > 1:
            pct = pct / 100
        sellable = int(pos.get("available_to_sell") or 0)
        return int(sellable * min(max(pct, 0.0), 1.0) / 100) * 100
    return 0


def _duplicate_pending(conn: sqlite3.Connection, action: str, code: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM live_trade_intents
           WHERE status='proposed' AND action=? AND code=?
           LIMIT 1""",
        (action, code),
    ).fetchone()
    return bool(row)


def _insert_intent(
    conn: sqlite3.Connection, decision: dict[str, Any], price: float,
    volume: int, effective_limit_price: float,
) -> dict[str, Any]:
    intent_id = _make_intent_id()
    amount = round(price * volume, 2)
    expires_at = (datetime.now() + timedelta(minutes=int(decision["expire_minutes"]))).strftime("%Y-%m-%d %H:%M:%S")
    risk_note = (
        "AI实盘三段式建议单；仅用于用户手动核对执行，不连接券商。"
        f" 风险: {decision['risk'] or '-'}"
    )
    conn.execute(
        """INSERT INTO live_trade_intents
           (intent_id, code, name, action, suggested_price, suggested_volume,
            suggested_amount, limit_price, reason, strategy, risk_note, status, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
        (
            intent_id,
            decision["code"],
            decision["name"],
            decision["action"],
            price,
            volume,
            amount,
            effective_limit_price,
            decision["reason"],
            "ai_live_trade_decision",
            risk_note,
            expires_at,
        ),
    )
    return {
        "intent_id": intent_id,
        "suggested_amount": amount,
        "expires_at": expires_at,
    }


def execute(payload: Any, *, dry_run: bool = False) -> dict[str, Any]:
    normalized = [_normalize(item) for item in _decisions(payload)]
    prices = fetch_live_prices([d["code"] for d in normalized])
    cfg = load_config()
    results = []
    conn = StockStore()._get_conn()
    conn.row_factory = sqlite3.Row
    try:
        expire_stale_proposed_intents(conn)
        snap = account_snapshot(conn)
        for decision in normalized:
            code = decision["code"]
            action = decision["action"]
            # Execute/quote the intent from a fresh market read. The AI price is
            # only a fallback when the execution-time quote source is unavailable.
            price = round(float(prices.get(code) or decision["price"] or 0), 2)
            decision_price = float(decision["price"] or 0)
            max_drift_pct = float(
                cfg.get("max_decision_price_drift_pct")
                or DEFAULT_MAX_DECISION_PRICE_DRIFT_PCT
            )
            buy_buffer_pct = float(
                cfg.get("default_buy_price_buffer_pct")
                or DEFAULT_BUY_PRICE_BUFFER_PCT
            )
            effective_limit_price = float(decision["limit_price"] or 0)
            if action == "buy" and effective_limit_price <= 0:
                effective_limit_price = round(
                    (decision_price or price) * (1 + buy_buffer_pct / 100), 2
                )
            volume = _derive_volume(decision, snap, price)
            result = {
                "code": code,
                "name": decision["name"],
                "action": action,
                "confidence": decision["confidence"],
                "price": price,
                "limit_price": effective_limit_price,
                "volume": volume,
                "executed": False,
                "created_intent": False,
                "dry_run": dry_run,
                "reason": decision["reason"],
                "risk": decision["risk"],
                "errors": [],
                "intent": None,
            }

            if action in NOOP_ACTIONS:
                result["message"] = "no_live_intent_action"
                results.append(result)
                continue

            if price <= 0:
                result["errors"].append("实时价/决策价缺失，拒绝生成实盘建议单")
            if volume <= 0:
                if action == "sell":
                    result["errors"].append("A股T+1可卖数量不足100股，拒绝生成卖出建议单")
                else:
                    result["errors"].append("建议数量缺失或不足100股，拒绝生成实盘建议单")
            if action == "buy" and not is_live_buy_allowed(code, cfg):
                result["errors"].append(f"实盘禁止买入 {blocked_prefix_text(cfg)} 开头代码")
            if action == "buy" and decision_price > 0 and price > 0:
                drift_pct = abs(price / decision_price - 1) * 100
                if drift_pct > max_drift_pct:
                    result["errors"].append(
                        f"实时价相对决策价偏离{drift_pct:.2f}%，超过{max_drift_pct:.2f}%，需重新分析"
                    )
            if _duplicate_pending(conn, action, code):
                result["errors"].append("同代码同方向已有待执行实盘建议单，拒绝重复生成")
            if not result["errors"]:
                result["errors"].extend(validate_intent(conn, action, code, price, volume))

            if result["errors"]:
                result["message"] = "rejected_by_live_risk"
                results.append(result)
                continue

            if dry_run:
                result["message"] = "dry_run_intent_validated"
                results.append(result)
                continue

            intent = _insert_intent(
                conn, decision, price, volume, effective_limit_price,
            )
            conn.commit()
            result["executed"] = True
            result["created_intent"] = True
            result["message"] = "live_intent_created"
            result["intent"] = intent
            results.append(result)
    finally:
        conn.close()

    created = [r for r in results if r.get("created_intent")]
    rejected = [r for r in results if r.get("errors")]
    noop = [r for r in results if r.get("message") == "no_live_intent_action"]
    return {
        "schema": "live_trade_execution.v1",
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": dry_run,
        "config": {
            "initial_cash": snap["summary"].get("initial_cash"),
            "net_external_cash_flow": snap["summary"].get("net_external_cash_flow"),
            "net_contributed_capital": snap["summary"].get("net_contributed_capital"),
            "max_positions": cfg.get("max_positions"),
            "manual_only": True,
        },
        "summary": {
            "created_intents": len(created),
            "rejected": len(rejected),
            "noop": len(noop),
            "total": len(results),
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create live manual trade intents from AI decision JSON")
    parser.add_argument("--decision-file", help="JSON file containing one decision or {'decisions': [...]}")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-closed", action="store_true")
    args = parser.parse_args()

    if not args.allow_closed and not ensure_actionable_trading_time(task="实盘AI建议单执行"):
        return 0

    payload = _load_json(args.decision_file)
    print(json.dumps(execute(payload, dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
