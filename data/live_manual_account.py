"""实盘操盘账户（影子账本）。

从 live_trade_intents 的 filled 记录重建资金/持仓；proposed 记录用于风控占位。
不连接真实券商账户，只服务于建议单和成交回填记录。
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple

from config.runtime_paths import configurable_path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = configurable_path(
    "STOCK_LIVE_ACCOUNT_CONFIG", "config/live_manual_account.local.json",
)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "initial_cash": 20000,
        "capital_flows": [],
        "max_positions": None,
        "target_position_amount": 10000,
        "max_single_buy_amount": None,
        "min_lot": 100,
        "blocked_boards": ["300", "301", "688", "8", "4"],
    }


def capital_flow_summary(cfg: dict | None = None) -> dict:
    """Summarize explicit external cash movements for the live account.

    The original starting cash remains immutable. Later deposits and withdrawals
    are separate facts so rebuilding the account never turns external funding
    into trading profit.
    """
    cfg = cfg or load_config()
    initial_cash = float(cfg.get("initial_cash") or 20000)
    deposits = 0.0
    withdrawals = 0.0
    flows = cfg.get("capital_flows") or []
    if not isinstance(flows, list):
        raise ValueError("capital_flows must be a list")
    for flow in flows:
        if not isinstance(flow, dict):
            raise ValueError("capital_flows entries must be objects")
        flow_type = str(flow.get("type") or "").strip().lower()
        try:
            amount = abs(float(flow.get("amount") or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("capital flow amount must be numeric") from exc
        if amount <= 0:
            raise ValueError("capital flow amount must be positive")
        if flow_type == "deposit":
            deposits += amount
        elif flow_type == "withdrawal":
            withdrawals += amount
        else:
            raise ValueError(f"unsupported capital flow type: {flow_type or '<empty>'}")
    net_external = round(deposits - withdrawals, 2)
    return {
        "initial_cash": round(initial_cash, 2),
        "external_deposits": round(deposits, 2),
        "external_withdrawals": round(withdrawals, 2),
        "net_external_cash_flow": net_external,
        "net_contributed_capital": round(initial_cash + net_external, 2),
        "capital_flow_count": len(flows),
    }


def blocked_prefixes(cfg: dict | None = None) -> Tuple[str, ...]:
    cfg = cfg or load_config()
    raw = cfg["blocked_boards"] if "blocked_boards" in cfg else ["300", "301", "688", "8", "4"]
    return tuple(str(item).strip() for item in raw if str(item).strip())


def is_live_buy_allowed(code: str, cfg: dict | None = None) -> bool:
    code = str(code).zfill(6)
    return not code.startswith(blocked_prefixes(cfg))


def blocked_prefix_text(cfg: dict | None = None) -> str:
    return "/".join(blocked_prefixes(cfg))


def max_single_buy_amount(cfg: dict) -> float | None:
    raw = cfg.get("max_single_buy_amount")
    if raw in (None, "", 0, "0"):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def max_positions_limit(cfg: dict) -> int | None:
    """Return a positive cap, or ``None`` when position count is unlimited."""
    raw = cfg.get("max_positions")
    if raw in (None, "", 0, "0"):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def execution_deviation_warnings(
    intent, price: float, volume: int, cfg: dict | None = None,
) -> List[str]:
    """Describe a real manual fill that departed from the advice contract.

    A real broker fill must remain recordable even when the user executed
    outside the suggested range.  The shadow ledger therefore records it and
    marks the deviation instead of pretending the advice was followed.
    """
    cfg = cfg or load_config()
    warnings: List[str] = []
    keys = set(intent.keys())
    action = str(intent["action"] if "action" in keys else "")
    suggested = float(intent["suggested_price"] or 0) if "suggested_price" in keys else 0.0
    limit_price = float(intent["limit_price"] or 0) if "limit_price" in keys else 0.0
    suggested_volume = int(intent["suggested_volume"] or 0) if "suggested_volume" in keys else 0
    threshold = float(cfg.get("max_decision_price_drift_pct") or 2.0)
    if action == "buy" and limit_price > 0 and price > limit_price:
        warnings.append(
            f"成交价{price:.2f}高于建议最高价{limit_price:.2f}"
        )
    if suggested > 0:
        drift = abs(price / suggested - 1) * 100
        if drift > threshold:
            warnings.append(
                f"成交价偏离建议价{drift:.2f}%，超过{threshold:.2f}%"
            )
    if suggested_volume > 0 and volume > suggested_volume:
        warnings.append(
            f"成交数量{volume}股超过建议数量{suggested_volume}股"
        )
    return warnings


def expire_stale_proposed_intents(conn) -> int:
    """Mark proposed live intents as expired after their explicit expiry time."""
    cur = conn.execute(
        """UPDATE live_trade_intents
           SET status='expired',
               user_note=CASE
                   WHEN COALESCE(user_note, '') = '' THEN '系统自动过期'
                   WHEN instr(user_note, '系统自动过期') > 0 THEN user_note
                   ELSE user_note || '；系统自动过期'
               END
           WHERE status='proposed'
             AND COALESCE(TRIM(expires_at), '') <> ''
             AND datetime(expires_at) < datetime('now','localtime')"""
    )
    conn.commit()
    return int(cur.rowcount or 0)


def _fetch_quotes(codes: List[str]) -> Dict[str, dict]:
    if not codes:
        return {}
    symbols = []
    for code in codes:
        c = str(code).zfill(6)
        symbols.append(("sh" if c.startswith("6") else "sz") + c)
    url = "http://qt.gtimg.cn/q=" + ",".join(symbols)
    quotes: Dict[str, dict] = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=5).read().decode("gbk", errors="ignore")
    except Exception:
        return quotes
    for line in data.strip().split("\n"):
        if '="' not in line or "~" not in line:
            continue
        fields = line.split('="', 1)[1].rstrip('";').split("~")
        if len(fields) < 5:
            continue
        code = fields[2].strip().zfill(6)
        try:
            price = float(fields[3]) if fields[3] else 0.0
            prev_close = float(fields[4]) if fields[4] else 0.0
        except Exception:
            continue
        if price <= 0:
            continue
        quotes[code] = {
            "code": code,
            "name": fields[1].strip(),
            "price": price,
            "prev_close": prev_close,
            "day_change": round(price - prev_close, 2) if prev_close else 0.0,
            "day_change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
        }
    return quotes


def account_snapshot(
    conn, quotes: Dict[str, dict] | None = None, *, expire_pending: bool = True,
) -> dict:
    """Rebuild the live shadow account.

    Callers in the decision path pass quotes loaded from ``trading_stock_state``
    so context construction stays database-only. Other callers may omit quotes
    and retain the direct Tencent refresh used by the Web and execution layers.
    """
    if expire_pending:
        expire_stale_proposed_intents(conn)
    cfg = load_config()
    funding = capital_flow_summary(cfg)
    initial_cash = funding["initial_cash"]
    contributed_capital = funding["net_contributed_capital"]
    cash = contributed_capital
    lots: Dict[str, dict] = {}
    realized = 0.0
    realized_trades = []
    today = date.today().isoformat()
    today_realized = 0.0

    rows = conn.execute(
        """SELECT * FROM live_trade_intents
           WHERE status='filled'
           ORDER BY filled_at, id"""
    ).fetchall()
    for r in rows:
        code = str(r["code"]).zfill(6)
        name = r["name"] or code
        action = r["action"]
        price = float(r["filled_price"] or r["suggested_price"] or 0)
        volume = int(r["filled_volume"] or r["suggested_volume"] or 0)
        amount = round(price * volume, 2)
        if volume <= 0 or price <= 0:
            continue
        pos = lots.setdefault(code, {
            "code": code,
            "name": name,
            "volume": 0,
            "cost_amount": 0.0,
            "today_buy_volume": 0,
        })
        filled_at = str(r["filled_at"] or "")
        is_today_fill = filled_at[:10] == today
        if action == "buy":
            cash -= amount
            pos["volume"] += volume
            pos["cost_amount"] += amount
            if is_today_fill:
                pos["today_buy_volume"] += volume
        elif action == "sell":
            cash += amount
            sell_vol = min(volume, pos["volume"])
            avg_cost = pos["cost_amount"] / pos["volume"] if pos["volume"] else 0.0
            realized_profit = round((price - avg_cost) * sell_vol, 2)
            realized += realized_profit
            if is_today_fill:
                today_realized += realized_profit
            realized_trades.append({
                "intent_id": r["intent_id"],
                "code": code,
                "name": name,
                "volume": sell_vol,
                "cost_price": round(avg_cost, 2),
                "sell_price": round(price, 2),
                "cost_amount": round(avg_cost * sell_vol, 2),
                "sell_amount": round(price * sell_vol, 2),
                "profit": realized_profit,
                "profit_pct": round((price - avg_cost) / avg_cost * 100, 2) if avg_cost else 0.0,
                "filled_at": filled_at,
                "is_today": filled_at[:10] == today,
                "reason": r["reason"] or "",
                "user_note": r["user_note"] or "",
            })
            pos["volume"] -= sell_vol
            pos["cost_amount"] = max(0.0, pos["cost_amount"] - avg_cost * sell_vol)

    lots = {c: p for c, p in lots.items() if p["volume"] > 0}
    quotes = _fetch_quotes(list(lots.keys())) if quotes is None else quotes
    positions = []
    market_value = 0.0
    unrealized = 0.0
    for code, p in lots.items():
        q = quotes.get(code, {})
        price = float(q.get("price") or (p["cost_amount"] / p["volume"] if p["volume"] else 0))
        mv = round(price * p["volume"], 2)
        cost_price = round(p["cost_amount"] / p["volume"], 2) if p["volume"] else 0.0
        pnl = round(mv - p["cost_amount"], 2)
        today_buy_volume = int(p.get("today_buy_volume") or 0)
        available_to_sell = max(0, int(p["volume"]) - today_buy_volume)
        market_value += mv
        unrealized += pnl
        positions.append({
            "code": code,
            "name": q.get("name") or p["name"],
            "volume": p["volume"],
            "today_buy_volume": today_buy_volume,
            "available_to_sell": available_to_sell,
            "cost_price": cost_price,
            "current_price": round(price, 2),
            "market_value": mv,
            "profit": pnl,
            "profit_pct": round(pnl / p["cost_amount"] * 100, 2) if p["cost_amount"] else 0.0,
            "day_change": q.get("day_change", 0.0),
            "day_change_pct": q.get("day_change_pct", 0.0),
        })

    pending = conn.execute("SELECT * FROM live_trade_intents WHERE status='proposed' ORDER BY id DESC").fetchall()
    pending_buy_amount = round(sum(float(r["suggested_amount"] or 0) for r in pending if r["action"] == "buy"), 2)
    total_equity = round(cash + market_value, 2)
    return {
        "config": cfg,
        "summary": {
            **funding,
            "available_cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "total_equity": total_equity,
            "total_profit": round(total_equity - contributed_capital, 2),
            "total_profit_pct": round((total_equity - contributed_capital) / contributed_capital * 100, 2) if contributed_capital else 0.0,
            "unrealized": round(unrealized, 2),
            "realized": round(realized, 2),
            "today_realized": round(today_realized, 2),
            "realized_trade_count": len(realized_trades),
            "today_realized_trade_count": sum(1 for item in realized_trades if item["is_today"]),
            "position_count": len(positions),
            "max_positions": max_positions_limit(cfg),
            "pending_count": len(pending),
            "pending_buy_amount": pending_buy_amount,
        },
        "positions": positions,
        "realized_trades": list(reversed(realized_trades)),
        "today_realized_trades": [item for item in reversed(realized_trades) if item["is_today"]],
    }


def validate_intent(conn, action: str, code: str, price: float, volume: int) -> List[str]:
    cfg = load_config()
    snap = account_snapshot(conn)
    issues: List[str] = []
    amount = round(price * volume, 2)
    min_lot = int(cfg.get("min_lot") or 100)
    max_single = max_single_buy_amount(cfg)
    max_positions = max_positions_limit(cfg)

    if volume % min_lot != 0:
        issues.append(f"数量必须是{min_lot}股整数倍")
    if action == "buy":
        if max_single is not None and amount > max_single:
            issues.append(f"单笔买入金额 {amount:,.0f} 超过配置的单笔买入上限 {max_single:,.0f}")
        available_after_pending = snap["summary"]["available_cash"] - snap["summary"].get("pending_buy_amount", 0)
        if amount > available_after_pending:
            issues.append(f"可用现金不足：建议金额 {amount:,.0f}，扣除待执行买单后可用 {available_after_pending:,.0f}")
        if max_positions is not None:
            holding_codes = {p["code"] for p in snap["positions"]}
            pending_buy_codes = {str(r["code"]).zfill(6) for r in conn.execute("SELECT code FROM live_trade_intents WHERE status='proposed' AND action='buy'").fetchall()}
            future_codes = set(holding_codes) | set(pending_buy_codes) | {str(code).zfill(6)}
            if len(future_codes) > max_positions:
                issues.append(f"最多同时持有/待买 {max_positions} 只，当前将达到 {len(future_codes)} 只")
    elif action == "sell":
        pos = next((p for p in snap["positions"] if p["code"] == str(code).zfill(6)), None)
        if not pos:
            issues.append("实盘影子账户无该持仓，不能生成卖出建议")
        else:
            available_to_sell = int(pos.get("available_to_sell") or 0)
            if volume > available_to_sell:
                issues.append(
                    f"A股T+1可卖数量不足：建议卖出{volume}股，当前可卖{available_to_sell}股"
                )
    return issues
