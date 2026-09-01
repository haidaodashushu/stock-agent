#!/usr/bin/env python3
"""账户账本重算/一致性检查。

用法：
  python3 scripts/reconcile_account.py          # 只检查
  python3 scripts/reconcile_account.py --apply  # 用 orders 重建 portfolio/account_state
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from account.reconcile import reconcile
from data.store.sqlite_store import StockStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="将重算结果写回 portfolio/account_state/daily_equity")
    args = parser.parse_args()

    result = reconcile(StockStore(), apply=args.apply)
    print("=== 账户账本重算 ===")
    print(f"订单: {result.order_count}笔  买入:{result.buy_total:,.0f}  卖出:{result.sell_total:,.0f}")
    print(f"现金: {result.cash:,.0f}  持仓市值:{result.market_value:,.0f}")
    print(f"总资产: {result.total_equity:,.0f}  总盈亏:{result.total_profit:+,.0f}")
    print(f"持仓: {len(result.positions)}只")
    for p in result.positions:
        print(f"  {p['code']} {p['name']:<8} {p['volume']}股 成本{p['cost_price']:.2f} 现价{p['current_price']:.2f} 盈亏{p['profit']:+,.0f}")

    if result.issues:
        print("\n=== 风险/异常 ===")
        for issue in result.issues:
            print(f"[{issue.level.upper()}] order#{issue.order_id} {issue.code}: {issue.message}")
    else:
        print("\n无账本异常。")

    if args.apply:
        print("\n✅ 已写回数据库。")
    else:
        print("\n未写回数据库；如需修正快照，运行 --apply。")

    return 1 if any(i.level == "error" for i in result.issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
