"""
account/reconcile.py - 订单账本重算与账户一致性校验

原则：orders 是唯一交易流水；portfolio/account_state 是可重建快照。
默认按现有订单重放并报告违规；T+1 等规则在 SimTrader 执行层硬拦截。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging
import urllib.request

from config.runtime_paths import configurable_path
from data.store.sqlite_store import StockStore

logger = logging.getLogger(__name__)

INITIAL_CAPITAL = 1_000_000.0
FORBIDDEN_PREFIXES = ("688", "8", "4")
DEFAULT_RESOLVED_ISSUES_PATH = configurable_path(
    "STOCK_RECONCILE_ISSUES_CONFIG", "config/reconcile_resolved_issues.local.json",
)


@dataclass
class ReconcileIssue:
    level: str
    code: str
    message: str
    order_id: Any = None


@dataclass
class ReconcileResult:
    cash: float
    market_value: float
    total_equity: float
    total_profit: float
    positions: List[Dict[str, Any]] = field(default_factory=list)
    issues: List[ReconcileIssue] = field(default_factory=list)
    order_count: int = 0
    buy_total: float = 0.0
    sell_total: float = 0.0


def is_tradeable_a_share(code: str) -> bool:
    """本系统交易白名单：排除科创板/北证。"""
    code = str(code).zfill(6)
    return not code.startswith(FORBIDDEN_PREFIXES)


def _order_date(created_at: str) -> str:
    return (created_at or "")[:10]


def load_resolved_issue_order_ids(path: str | Path = DEFAULT_RESOLVED_ISSUES_PATH) -> set[int]:
    """读取已由人工确认处理完成、不再重复提示的历史订单编号。"""
    config_path = Path(path)
    if not config_path.exists():
        return set()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("load resolved reconcile issues failed: %s", exc)
        return set()

    if not isinstance(payload, dict):
        logger.warning("resolved reconcile issues config must be a JSON object")
        return set()
    values = payload.get("resolved_order_ids", [])
    if not isinstance(values, list):
        logger.warning("resolved_order_ids must be a JSON array")
        return set()

    resolved: set[int] = set()
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            order_id = value
        elif isinstance(value, str) and value.strip().isdecimal():
            try:
                order_id = int(value.strip())
            except ValueError:
                continue
        else:
            continue
        if order_id > 0:
            resolved.add(order_id)
    return resolved


def fetch_live_prices(codes: List[str], timeout: int = 10) -> Dict[str, float]:
    """腾讯行情批量取现价；失败时返回空dict。"""
    if not codes:
        return {}
    prices: Dict[str, float] = {}
    for i in range(0, len(codes), 80):
        batch = codes[i:i + 80]
        symbols = [("sh" + c if c.startswith("6") else "sz" + c) for c in batch]
        url = f'http://qt.gtimg.cn/q={",".join(symbols)}'
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = resp.read().decode("gbk", errors="ignore")
            for line in data.strip().split("\n"):
                if '="' not in line or "~" not in line:
                    continue
                content = line.split('="', 1)[1].rstrip('";')
                fields = content.split("~")
                if len(fields) < 4:
                    continue
                code = fields[2].strip()
                try:
                    price = float(fields[3]) if fields[3] else 0.0
                except ValueError:
                    price = 0.0
                if code and price > 0:
                    prices[code] = price
        except Exception as e:
            logger.warning("fetch_live_prices failed: %s", e)
    return prices


def reconcile(store: Optional[StockStore] = None, *, apply: bool = False,
              live_prices: Optional[Dict[str, float]] = None,
              initial_capital: float = INITIAL_CAPITAL,
              resolved_issue_order_ids: Optional[set[int]] = None) -> ReconcileResult:
    """从 orders 重算 cash/portfolio/account_state。

    apply=False 只返回结果；apply=True 将重建 portfolio/account_state 与今日 daily_equity。
    已确认处理完成的历史订单可通过 resolved_issue_order_ids 隐藏其历史异常。
    """
    store = store or StockStore()
    conn = store._get_conn()
    issues: List[ReconcileIssue] = []
    if resolved_issue_order_ids is None:
        resolved_issue_order_ids = load_resolved_issue_order_ids()
    lots: Dict[str, Dict[str, Any]] = {}
    cash = float(initial_capital)
    buy_total = 0.0
    sell_total = 0.0

    try:
        rows = conn.execute("SELECT * FROM orders ORDER BY datetime(created_at), id").fetchall()
        for r in rows:
            oid = r["id"]
            code = str(r["code"]).zfill(6)
            name = r["name"] or code
            direction = (r["direction"] or "").lower()
            price = float(r["price"] or 0)
            volume = int(r["volume"] or 0)
            amount = float(r["amount"] or 0)
            commission = float(r["commission"] or 0)
            tax = float(r["tax"] or 0)
            created_at = r["created_at"] or ""
            date = _order_date(created_at)

            if direction not in {"buy", "sell"}:
                issues.append(ReconcileIssue("error", code, f"无效方向: {direction!r}", oid))
                continue
            if price <= 0 or volume <= 0 or amount <= 0:
                issues.append(ReconcileIssue("error", code, f"无效成交 price={price} volume={volume} amount={amount}", oid))
                continue
            expected_amount = round(price * volume, 2)
            if abs(expected_amount - amount) > 0.02:
                issues.append(ReconcileIssue("warn", code, f"成交金额不匹配: amount={amount:.2f}, price*volume={expected_amount:.2f}", oid))
            if direction == "buy":
                if not is_tradeable_a_share(code):
                    issues.append(ReconcileIssue("error", code, "禁止板块买入(科创板/北证)", oid))
                if volume % 100 != 0:
                    issues.append(ReconcileIssue("warn", code, f"买入股数非100整数倍: {volume}", oid))
                total_cost = round(amount + commission, 2)
                if total_cost > cash + 1e-6:
                    issues.append(ReconcileIssue("error", code, f"资金不足: 需{total_cost:.2f}, 现金{cash:.2f}", oid))
                cash = round(cash - total_cost, 2)
                buy_total = round(buy_total + amount, 2)
                lot = lots.setdefault(code, {"code": code, "name": name, "volume": 0, "cost_amount": 0.0, "today_buys": {}})
                lot["volume"] += volume
                lot["cost_amount"] = round(lot["cost_amount"] + amount, 2)
                lot["today_buys"][date] = lot["today_buys"].get(date, 0) + volume
            else:
                lot = lots.get(code)
                if not lot or lot["volume"] <= 0:
                    issues.append(ReconcileIssue("error", code, "卖出但无持仓", oid))
                    continue
                if volume > lot["volume"]:
                    issues.append(ReconcileIssue("error", code, f"卖出超过持仓: 卖{volume}, 持{lot['volume']}", oid))
                    volume = lot["volume"]
                today_buy_vol = lot.get("today_buys", {}).get(date, 0)
                available_t1 = max(0, lot["volume"] - today_buy_vol)
                if volume > available_t1:
                    issues.append(ReconcileIssue("error", code, f"T+1违规: 当日可卖{available_t1}, 实卖{volume}", oid))
                avg_cost = lot["cost_amount"] / lot["volume"] if lot["volume"] else 0
                lot["volume"] -= volume
                lot["cost_amount"] = round(avg_cost * lot["volume"], 2)
                cash = round(cash + amount - commission - tax, 2)
                sell_total = round(sell_total + amount, 2)

        active_codes = [c for c, p in lots.items() if p["volume"] > 0]
        if live_prices is None:
            live_prices = fetch_live_prices(active_codes)

        positions: List[Dict[str, Any]] = []
        market_value = 0.0
        for code in active_codes:
            lot = lots[code]
            volume = int(lot["volume"])
            cost_price = round(lot["cost_amount"] / volume, 2) if volume else 0
            price = float(live_prices.get(code) or cost_price)
            mv = round(volume * price, 2)
            profit = round(mv - volume * cost_price, 2)
            profit_pct = round((price - cost_price) / cost_price * 100, 2) if cost_price else 0
            market_value = round(market_value + mv, 2)
            positions.append({
                "code": code,
                "name": lot["name"],
                "volume": volume,
                "available": volume,
                "cost_price": cost_price,
                "current_price": round(price, 2),
                "market_value": mv,
                "profit": profit,
                "profit_pct": profit_pct,
                "high_since_entry": max(round(price, 2), cost_price),
            })

        total_equity = round(cash + market_value, 2)
        total_profit = round(total_equity - initial_capital, 2)
        issues = [issue for issue in issues if issue.order_id not in resolved_issue_order_ids]
        result = ReconcileResult(
            cash=round(cash, 2),
            market_value=market_value,
            total_equity=total_equity,
            total_profit=total_profit,
            positions=positions,
            issues=issues,
            order_count=len(rows),
            buy_total=buy_total,
            sell_total=sell_total,
        )

        if apply:
            conn.execute("DELETE FROM portfolio")
            for p in positions:
                conn.execute(
                    """INSERT INTO portfolio
                       (code, name, volume, available, cost_price, current_price, market_value,
                        profit, profit_pct, high_since_entry, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
                    (p["code"], p["name"], p["volume"], p["available"], p["cost_price"],
                     p["current_price"], p["market_value"], p["profit"], p["profit_pct"],
                     p["high_since_entry"])
                )
            conn.execute(
                """UPDATE account_state
                   SET available_cash=?, total_equity=?, total_profit=?, updated_at=datetime('now','localtime')
                   WHERE id=1""",
                (result.cash, result.total_equity, result.total_profit)
            )
            today = datetime.now().strftime("%Y-%m-%d")
            conn.execute(
                """INSERT OR REPLACE INTO daily_equity
                   (date, total_equity, available_cash, market_value, total_profit)
                   VALUES (?, ?, ?, ?, ?)""",
                (today, result.total_equity, result.cash, result.market_value, result.total_profit)
            )
            conn.commit()

        return result
    finally:
        conn.close()
