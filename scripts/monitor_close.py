#!/usr/bin/env python3
"""monitor_close.py - 收盘复盘脚本（被 cron 调用）"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from account.trader import SimTrader
from data.fifteen_five_pool import FIFTEEN_FIVE_STOCKS
from data.loader import DataLoader
from data.market_calendar import ensure_market_open
from data.store.sqlite_store import StockStore


def money(value: float) -> str:
    return f"{value:,.0f}"


def signed_money(value: float) -> str:
    return f"{value:+,.0f}"


def pct(value: float) -> str:
    return f"{value:+.1f}%"


def clip(text: str, limit: int = 42) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def get_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """获取腾讯实时行情。"""
    results: dict[str, dict[str, Any]] = {}
    for i in range(0, len(codes), 80):
        batch = codes[i : i + 80]
        symbols = []
        for code in batch:
            prefix = "sh" if code.startswith("6") else "sz"
            symbols.append(f"{prefix}{code}")
        url = f"http://qt.gtimg.cn/q={','.join(symbols)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=10)
            data = resp.read().decode("gbk", errors="ignore")
        except Exception:
            continue

        for line in data.strip().split("\n"):
            if '="' not in line or "~" not in line:
                continue
            content = line.split('="', 1)[1].rstrip('";')
            fields = content.split("~")
            if len(fields) < 40:
                continue
            code = fields[2].strip()
            try:
                price = float(fields[3]) if fields[3] else 0
                prev_close = float(fields[4]) if fields[4] else 0
                chg_pct = (price - prev_close) / prev_close * 100 if price and prev_close else 0
            except Exception:
                price = 0
                prev_close = 0
                chg_pct = 0
            results[code] = {
                "name": fields[1],
                "price": price,
                "prev_close": prev_close,
                "open": float(fields[5]) if fields[5] else 0,
                "high": float(fields[33]) if len(fields) > 33 and fields[33] else 0,
                "low": float(fields[34]) if len(fields) > 34 and fields[34] else 0,
                "volume": int(fields[6]) if fields[6] else 0,
                "chg_pct": chg_pct,
            }
    return results


def update_positions_to_close(trader: SimTrader, loader: DataLoader, quotes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for position in list(trader.portfolio.positions):
        quote = quotes.get(position.code, {})
        price = float(quote.get("price") or 0)
        if price <= 0:
            df = loader.get_daily(position.code)
            if df is not None and not df.empty:
                price = float(df.iloc[-1]["close"])
        if price <= 0:
            price = position.current_price or position.cost_price

        position.update_market(price)
        pnl_pct = (price - position.cost_price) / position.cost_price * 100 if position.cost_price > 0 else 0
        today_chg = float(quote.get("chg_pct") or 0)
        concepts = FIFTEEN_FIVE_STOCKS.get(position.code, {}).get("concepts", [])[:2]
        rows.append(
            {
                "code": position.code,
                "name": position.name,
                "volume": position.volume,
                "cost_price": round(position.cost_price, 2),
                "close_price": round(price, 2),
                "today_chg_pct": round(today_chg, 2),
                "pnl_pct": round(pnl_pct, 2),
                "market_value": round(position.volume * price, 2),
                "concepts": concepts,
                "is_fifteen_five": position.code in FIFTEEN_FIVE_STOCKS,
            }
        )
    trader.portfolio._recalc()
    trader._save_portfolio()
    return rows


def summarize_orders(trader: SimTrader, today: str) -> list[dict[str, Any]]:
    orders = [order for order in trader.portfolio.orders if order.created_at.startswith(today)]
    orders.sort(key=lambda order: order.created_at)
    rows = []
    for order in orders:
        rows.append(
            {
                "time": order.created_at[11:16] if len(order.created_at) >= 16 else order.created_at,
                "action": order.action,
                "code": order.code,
                "name": order.name,
                "volume": order.volume,
                "price": round(order.price, 2),
                "amount": round(order.amount, 2),
                "reason": order.reason or "",
                "strategy": order.strategy or "",
                "created_at": order.created_at,
            }
        )
    return rows


def get_fund_flows(positions: list[dict[str, Any]]) -> tuple[list[dict[str, str]], str]:
    if not positions:
        return [], ""
    try:
        from data.fund_flow_filter import FundFlowFilter

        fff = FundFlowFilter()
        flows = fff.batch_summarize([row["code"] for row in positions])
    except Exception as exc:
        return [], f"资金流向查询跳过: {exc}"

    rows = []
    for position in positions:
        _, summary = flows.get(position["code"], (None, ""))
        if summary:
            rows.append({"code": position["code"], "name": position["name"], "summary": summary})
    return rows, ""


def save_daily_equity(store: StockStore, today: str, summary: dict[str, Any]) -> None:
    conn = store._get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily_equity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL UNIQUE,
        total_equity REAL, available_cash REAL, market_value REAL, total_profit REAL,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )"""
    )
    conn.execute(
        """INSERT OR REPLACE INTO daily_equity
        (date, total_equity, available_cash, market_value, total_profit)
        VALUES (?, ?, ?, ?, ?)""",
        (
            today,
            summary["total_equity"],
            summary["available_cash"],
            summary["position_market_value"],
            summary["total_profit"],
        ),
    )
    conn.commit()
    conn.close()


def previous_daily_equity(store: StockStore, today: str) -> dict[str, Any] | None:
    conn = store._get_conn()
    try:
        row = conn.execute(
            """SELECT date, total_equity
            FROM daily_equity
            WHERE date < ?
            ORDER BY date DESC
            LIMIT 1""",
            (today,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def calculate_daily_return(
    summary: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    if not previous or not previous.get("total_equity"):
        return {"profit": None, "profit_pct": None, "previous_date": None}
    previous_equity = float(previous["total_equity"])
    profit = float(summary["total_equity"]) - previous_equity
    return {
        "profit": round(profit, 2),
        "profit_pct": round(profit / previous_equity * 100, 4),
        "previous_date": str(previous["date"]),
    }


def build_report(
    today: str,
    summary: dict[str, Any],
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    fund_flows: list[dict[str, str]],
    warnings: list[str],
    daily_return: dict[str, Any],
) -> dict[str, Any]:
    order_count = len(orders)
    position_count = len(positions)
    daily_profit = daily_return.get("profit")
    if daily_profit is None:
        daily_text = "今日收益暂无前一交易日基准"
    else:
        daily_text = (
            f"今日收益 {signed_money(float(daily_profit))} "
            f"({float(daily_return['profit_pct']):+.2f}%)"
        )
    summary_text = (
        f"{daily_text}；收盘总资产 {money(float(summary['total_equity']))}，"
        f"总盈亏 {signed_money(float(summary['total_profit']))} ({summary['total_profit_pct']:+.2f}%)；"
        f"持仓 {position_count} 只，今日交易 {order_count} 笔。"
    )
    tone_profit = float(daily_profit) if daily_profit is not None else float(summary.get("total_profit", 0))
    return {
        "profile": "close_review",
        "title": f"每日收盘复盘 {today}",
        "tone": "success" if tone_profit >= 0 else "warning",
        "summary": summary_text,
        "account": {
            "total_equity": round(float(summary["total_equity"]), 2),
            "daily_profit": round(float(daily_profit), 2) if daily_profit is not None else None,
            "daily_profit_pct": (
                round(float(daily_return["profit_pct"]), 4)
                if daily_return.get("profit_pct") is not None
                else None
            ),
            "daily_profit_basis_date": daily_return.get("previous_date"),
            "available_cash": round(float(summary["available_cash"]), 2),
            "position_market_value": round(float(summary["position_market_value"]), 2),
            "position_count": int(summary["position_count"]),
            "total_profit": round(float(summary["total_profit"]), 2),
            "total_profit_pct": round(float(summary["total_profit_pct"]), 4),
        },
        "positions": positions,
        "orders": orders,
        "fund_flows": fund_flows,
        "warnings": warnings or ["无关键异常"],
        "source": "monitor_close.py + SimTrader + 腾讯行情 + FundFlowFilter",
    }


def print_report(report: dict[str, Any]) -> None:
    account = report["account"]
    print(f"=== {report['title']} ===")
    print(
        f"收盘总资产: {money(account['total_equity'])}  "
        f"现金: {money(account['available_cash'])}  "
        f"持仓市值: {money(account['position_market_value'])}"
    )
    if account.get("daily_profit") is not None:
        print(
            f"今日收益: {signed_money(account['daily_profit'])} "
            f"({account['daily_profit_pct']:+.2f}%)"
        )
    else:
        print("今日收益: 暂无前一交易日基准")
    print(f"总盈亏: {signed_money(account['total_profit'])} ({account['total_profit_pct']:+.2f}%)")
    print()

    if report["positions"]:
        print("持仓表现:")
        for row in report["positions"]:
            direction = "↑" if row["today_chg_pct"] > 0 else ("↓" if row["today_chg_pct"] < 0 else "→")
            mark = "✨" if row["is_fifteen_five"] else ""
            tag = f" [{','.join(row['concepts'])}]" if row["concepts"] else ""
            print(
                f"{mark}{row['code']} {row['name']} {row['volume']}股 "
                f"成本 {row['cost_price']:.2f} 收盘 {row['close_price']:.2f} "
                f"{direction}{pct(row['today_chg_pct'])} 持仓盈亏 {pct(row['pnl_pct'])}{tag}"
            )
    else:
        print("持仓表现: 空仓")

    if report["orders"]:
        print(f"\n今日交易 ({len(report['orders'])} 笔):")
        for row in report["orders"][-12:]:
            action = "买" if row["action"] == "buy" else "卖"
            print(
                f"{row['time']} {action} {row['code']} {row['name']} "
                f"{row['volume']}股 @{row['price']:.2f} | {clip(row['reason'])}"
            )
    else:
        print("\n今日交易: 无")

    if report["fund_flows"]:
        print("\n收盘资金流向:")
        for row in report["fund_flows"]:
            print(f"{row['code']} {row['name']}: {row['summary']}")

    for warning in report["warnings"]:
        if warning != "无关键异常":
            print(f"\n{warning}")

    print(
        f"\n每日权益快照已保存: {money(account['total_equity'])} "
        f"(盈亏 {signed_money(account['total_profit'])})"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", help="Write structured close review JSON to this path.")
    args = parser.parse_args()

    if not ensure_market_open(task="每日收盘复盘"):
        return 0

    today = datetime.now().strftime("%Y-%m-%d")
    trader = SimTrader()
    loader = DataLoader()
    store = StockStore()
    positions_before = list(trader.portfolio.positions)
    quotes = get_quotes([position.code for position in positions_before])
    positions = update_positions_to_close(trader, loader, quotes)
    summary = trader.portfolio.summary()
    daily_return = calculate_daily_return(summary, previous_daily_equity(store, today))
    orders = summarize_orders(trader, today)
    fund_flows, fund_flow_warning = get_fund_flows(positions)
    warnings = [fund_flow_warning] if fund_flow_warning else []

    save_daily_equity(store, today, summary)
    report = build_report(today, summary, positions, orders, fund_flows, warnings, daily_return)
    print_report(report)

    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
