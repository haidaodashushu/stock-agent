#!/usr/bin/env python3
"""Validate AI trade signals and execute them through SimTrader only."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from account.reconcile import fetch_live_prices, is_tradeable_a_share  # noqa: E402
from account.portfolio_policy import (  # noqa: E402
    SIM_HARD_MAX_POSITIONS,
    SIM_TARGET_MAX_POSITIONS,
)
from account.trader import SimTrader  # noqa: E402
from data.market_calendar import ensure_actionable_trading_time  # noqa: E402
from data.store.sqlite_store import StockStore  # noqa: E402


BUY_ACTIONS = {"buy", "add"}
SELL_ACTIONS = {"sell", "reduce", "clear"}
NOOP_ACTIONS = {"hold", "watch", "noop", "skip"}
VALID_ACTIONS = BUY_ACTIONS | SELL_ACTIONS | NOOP_ACTIONS


def _load_json(path: str | None) -> Any:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return json.load(sys.stdin)


def _signals(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("signals"), list):
            return [x for x in payload["signals"] if isinstance(x, dict)]
        if isinstance(payload.get("code"), (str, int)):
            return [payload]
    raise ValueError("signal payload must be a signal object, a list, or {'signals': [...]}")


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


def _latest_name(code: str) -> str:
    store = StockStore()
    conn = store._get_conn()
    try:
        row = conn.execute("SELECT name FROM stocks WHERE code=? LIMIT 1", (code,)).fetchone()
        return str(row[0]) if row and row[0] else code
    finally:
        conn.close()


def _normalize(signal: dict[str, Any]) -> dict[str, Any]:
    code = str(signal.get("code", "")).zfill(6)
    action = str(signal.get("action", "hold")).lower().strip()
    if action not in VALID_ACTIONS:
        raise ValueError(f"{code}: invalid action {action!r}")
    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"invalid code {code!r}")
    return {
        "code": code,
        "name": str(signal.get("name") or _latest_name(code)),
        "action": action,
        "confidence": str(signal.get("confidence", "medium")).lower(),
        "target_amount": _float(signal.get("target_amount")),
        "volume": _int(signal.get("volume")),
        "sell_pct": _float(signal.get("sell_pct"), 1.0),
        "reason": str(signal.get("reason", "")).strip()[:240],
        "risk": str(signal.get("risk", "")).strip()[:240],
        "replacement_code": str(signal.get("replacement_code") or "").strip().zfill(6)
        if str(signal.get("replacement_code") or "").strip()
        else "",
        "replacement_edge": str(signal.get("replacement_edge") or "").strip().lower(),
        "replacement_reason": str(signal.get("replacement_reason") or "").strip()[:240],
        "raw": signal,
    }


def _buy_volume(target_amount: float, price: float, cash: float) -> int:
    amount = target_amount if target_amount > 0 else min(cash * 0.2, cash * 0.95)
    amount = min(amount, cash * 0.95)
    return int(amount / price / 100) * 100


def _sell_volume(signal: dict[str, Any], available: int) -> int:
    if signal["action"] == "clear":
        return available
    if signal["volume"] > 0:
        return min(signal["volume"], available)
    pct = signal["sell_pct"]
    if pct <= 0:
        pct = 0.5 if signal["action"] == "reduce" else 1.0
    if pct > 1:
        pct = pct / 100
    return int(available * min(pct, 1.0) / 100) * 100


def execute(payload: Any, *, dry_run: bool = False) -> dict[str, Any]:
    trader = SimTrader()
    normalized = [_normalize(s) for s in _signals(payload)]
    # Capacity-changing exits must settle before replacement entries are checked.
    action_priority = {
        **{action: 0 for action in SELL_ACTIONS},
        **{action: 1 for action in NOOP_ACTIONS},
        **{action: 2 for action in BUY_ACTIONS},
    }
    normalized.sort(key=lambda row: action_priority[row["action"]])
    prices = fetch_live_prices([s["code"] for s in normalized])
    results = []
    projected_positions = {position.code for position in trader.portfolio.positions}
    successful_risk_actions: set[str] = set()

    for signal in normalized:
        code = signal["code"]
        action = signal["action"]
        price = float(prices.get(code, 0))
        result = {
            "code": code,
            "name": signal["name"],
            "action": action,
            "confidence": signal["confidence"],
            "price": round(price, 2),
            "executed": False,
            "dry_run": dry_run,
            "reason": signal["reason"],
            "risk": signal["risk"],
            "replacement_code": signal["replacement_code"],
            "replacement_reason": signal["replacement_reason"],
            "errors": [],
            "order": None,
        }

        if action in NOOP_ACTIONS:
            result["executed"] = False
            result["message"] = "no_trade_action"
            results.append(result)
            continue
        if price <= 0:
            result["errors"].append("腾讯实时价缺失或为0，拒绝执行")
            results.append(result)
            continue
        if action in BUY_ACTIONS and not is_tradeable_a_share(code):
            result["errors"].append("禁止买入科创板/北证代码")
            results.append(result)
            continue

        reason = f"AI信号 {signal['confidence']} | {signal['reason']}"
        if signal["risk"]:
            reason = f"{reason} | 风险:{signal['risk']}"

        if action in BUY_ACTIONS:
            is_new_position = code not in projected_positions
            if is_new_position and len(projected_positions) >= SIM_TARGET_MAX_POSITIONS:
                replacement = signal["replacement_code"]
                if not replacement:
                    result["errors"].append(
                        f"持仓已达目标上沿{SIM_TARGET_MAX_POSITIONS}只，新开仓必须指定替换仓"
                    )
                    results.append(result)
                    continue
                if replacement not in successful_risk_actions:
                    result["errors"].append(
                        f"替换仓{replacement}本轮尚未成功减仓或退出，拒绝新开仓"
                    )
                    results.append(result)
                    continue
            if is_new_position and len(projected_positions) >= SIM_HARD_MAX_POSITIONS:
                result["errors"].append(
                    f"模拟盘持仓已达硬上限{SIM_HARD_MAX_POSITIONS}只，拒绝新开仓"
                )
                results.append(result)
                continue
            volume = _buy_volume(signal["target_amount"], price, trader.portfolio.available_cash)
            result["volume"] = volume
            if volume < 100:
                result["errors"].append("按现金/目标金额计算不足100股，拒绝买入")
            elif dry_run:
                result["message"] = "dry_run_buy_validated"
                projected_positions.add(code)
            else:
                order = trader.buy(code, signal["name"], price, volume, reason=reason, strategy="ai_intraday_signal")
                if order:
                    result["executed"] = True
                    result["order"] = order.to_dict()
                    projected_positions.add(code)
                else:
                    result["errors"].append("SimTrader.buy 拒绝执行")

        if action in SELL_ACTIONS:
            pos = trader.portfolio.get_position(code)
            if not pos:
                result["errors"].append("无持仓，拒绝卖出")
            else:
                available = max(0, pos.volume - trader._today_buy_volume(code))
                volume = _sell_volume(signal, available)
                result["available_to_sell"] = available
                result["volume"] = volume
                if volume < 100:
                    result["errors"].append("T+1可卖或信号数量不足100股，拒绝卖出")
                elif dry_run:
                    result["message"] = "dry_run_sell_validated"
                    successful_risk_actions.add(code)
                    if volume >= pos.volume:
                        projected_positions.discard(code)
                else:
                    order = trader.sell(code, price, volume=volume, reason=reason, strategy="ai_intraday_signal")
                    if order:
                        result["executed"] = True
                        result["order"] = order.to_dict()
                        successful_risk_actions.add(code)
                        if not trader.portfolio.has_position(code):
                            projected_positions.discard(code)
                    else:
                        result["errors"].append("SimTrader.sell 拒绝执行")

        results.append(result)

    trader.portfolio._recalc()
    return {
        "schema": "trade_signal_execution.v1",
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": dry_run,
        "results": results,
        "account": trader.portfolio.summary(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute AI trade signal JSON through SimTrader")
    parser.add_argument("--signal-file", help="JSON file containing one signal or {'signals': [...]}")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-closed", action="store_true", help="Bypass market calendar guard for diagnostics")
    args = parser.parse_args()

    if not args.allow_closed and not ensure_actionable_trading_time(task="AI交易信号执行"):
        return 0
    payload = _load_json(args.signal_file)
    print(json.dumps(execute(payload, dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
