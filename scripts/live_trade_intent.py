#!/usr/bin/env python3
"""实盘操盘建议单。

模式：
- propose: 生成操盘建议单，输出可直接发给用户的飞书文本。系统不自动连接券商下单。
- fill: 用户手动成交后回填成交价/数量，状态改为 filled。
- list: 查看待处理建议单。
- cancel: 取消建议单。

示例：
  python3 scripts/live_trade_intent.py propose buy 600460 士兰微 --price 41.20 --volume 3000 --reason "突破放量" --strategy manual_live
  python3 scripts/live_trade_intent.py fill L20260618... --price 41.18 --volume 3000 --note "同花顺已买"
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.store.sqlite_store import StockStore  # noqa: E402
from data.live_manual_account import (  # noqa: E402
    blocked_prefix_text,
    capital_flow_summary,
    execution_deviation_warnings,
    expire_stale_proposed_intents,
    is_live_buy_allowed,
    load_config,
    max_single_buy_amount,
    validate_intent,
)

LIVE_CFG = load_config()
MIN_LOT = int(LIVE_CFG.get("min_lot") or 100)
MAX_SINGLE_AMOUNT = max_single_buy_amount(LIVE_CFG)
DEFAULT_EXPIRE_MINUTES = 15


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conn() -> sqlite3.Connection:
    c = StockStore()._get_conn()
    return c


def make_intent_id() -> str:
    return "L" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6].upper()


def normalize_code(code: str) -> str:
    return str(code).strip().zfill(6)


def validate(action: str, code: str, price: float, volume: int, db=None) -> list[str]:
    issues = []
    if action not in {"buy", "sell"}:
        issues.append("方向必须是 buy/sell")
    if price <= 0:
        issues.append("价格必须大于0")
    if volume <= 0:
        issues.append("数量必须大于0")
    if volume % MIN_LOT != 0:
        issues.append("A股买入/建议数量应为100股整数倍")
    if action == "buy" and not is_live_buy_allowed(code, LIVE_CFG):
        issues.append(f"实盘禁止买入 {blocked_prefix_text(LIVE_CFG)} 开头代码")
    amount = round(price * volume, 2)
    if db is not None:
        issues.extend(validate_intent(db, action, code, price, volume))
    elif action == "buy" and MAX_SINGLE_AMOUNT is not None and amount > MAX_SINGLE_AMOUNT:
        issues.append(f"单笔买入金额 {amount:,.0f} 超过当前硬限制 {MAX_SINGLE_AMOUNT:,.0f}")
    return list(dict.fromkeys(issues))


def format_action(action: str) -> str:
    return "买入" if action == "buy" else "卖出"


def propose(args: argparse.Namespace) -> int:
    action = args.action.lower()
    code = normalize_code(args.code)
    price = round(float(args.price), 2)
    volume = int(args.volume)
    amount = round(price * volume, 2)

    c = conn()
    try:
        issues = validate(action, code, price, volume, c)
        if issues:
            print("❌ 建议单被风控拒绝：")
            for i in issues:
                print(f"- {i}")
            return 2

        intent_id = make_intent_id()
        expires_at = (datetime.now() + timedelta(minutes=int(args.expire_minutes))).strftime("%Y-%m-%d %H:%M:%S")
        single_limit_text = (
            f"单笔硬上限 {MAX_SINGLE_AMOUNT:,.0f} 元"
            if MAX_SINGLE_AMOUNT is not None
            else "单笔不设硬上限，以可用现金为准"
        )
        funding = capital_flow_summary(LIVE_CFG)
        risk_note = args.risk_note or (
            "实盘操盘模式：系统负责决策并生成建议单，不自动连接券商下单；请在券商/同花顺手动核对代码、价格、数量。"
            f"实盘累计投入 {funding['net_contributed_capital']:,.0f} 元，"
            f"当前可用现金以影子账户实时重建结果为准，"
            f"持仓数量不设上限，{single_limit_text}。"
        )
        c.execute(
            """INSERT INTO live_trade_intents
               (intent_id, code, name, action, suggested_price, suggested_volume,
                suggested_amount, limit_price, reason, strategy, risk_note, status, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
            (intent_id, code, args.name or "", action, price, volume, amount,
             float(args.limit_price or price), args.reason or "", args.strategy or "manual_live", risk_note, expires_at),
        )
        c.commit()
    finally:
        c.close()

    print("【实盘操盘建议单】")
    print(f"编号：{intent_id}")
    print(f"操作：{format_action(action)} {code} {args.name or ''}".rstrip())
    print(f"建议价：¥{price:.2f}")
    print(f"数量：{volume}股")
    print(f"金额：约 ¥{amount:,.2f}")
    print(f"有效期：至 {expires_at}")
    print(f"策略：{args.strategy or 'manual_live'}")
    print(f"理由：{args.reason or '-'}")
    print(f"风控：{risk_note}")
    print("")
    print("请你在广发/同花顺手动核对并下单。成交后回复我：")
    print(f"成交 {intent_id} 价格 数量")
    print(f"例如：成交 {intent_id} {price:.2f} {volume}")
    return 0


def list_intents(args: argparse.Namespace) -> int:
    c = conn()
    try:
        expire_stale_proposed_intents(c)
        rows = c.execute(
            """SELECT * FROM live_trade_intents
               WHERE (?='all' OR status=?)
               ORDER BY id DESC LIMIT ?""",
            (args.status, args.status, int(args.limit)),
        ).fetchall()
    finally:
        c.close()
    if not rows:
        print("无建议单")
        return 0
    for r in rows:
        print(
            f"{r['intent_id']} {r['status']} {format_action(r['action'])} {r['code']} {r['name']} "
            f"建议 {r['suggested_volume']}股 @{r['suggested_price']:.2f} "
            f"创建 {r['created_at']} 到期 {r['expires_at']}"
        )
    return 0


def fill(args: argparse.Namespace) -> int:
    intent_id = args.intent_id.strip()
    price = round(float(args.price), 2)
    volume = int(args.volume)
    amount = round(price * volume, 2)
    filled_at = str(getattr(args, "filled_at", "") or "").strip()
    if filled_at:
        try:
            filled_at = datetime.strptime(filled_at, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            print("❌ 成交时间格式必须为 YYYY-MM-DD HH:MM:SS")
            return 2
    else:
        filled_at = now_str()
    c = conn()
    try:
        expire_stale_proposed_intents(c)
        row = c.execute("SELECT * FROM live_trade_intents WHERE intent_id=?", (intent_id,)).fetchone()
        if not row:
            print(f"❌ 未找到建议单 {intent_id}")
            return 2
        if row["status"] not in {"proposed", "filled"} and not args.force:
            print(f"❌ 建议单状态为 {row['status']}，不能回填/修改成交；如确需覆盖加 --force")
            return 2
        warnings = execution_deviation_warnings(row, price, volume, LIVE_CFG)
        note = str(args.note or "").strip()
        if warnings:
            warning_note = "执行偏离警告：" + "；".join(warnings)
            note = f"{note}；{warning_note}" if note else warning_note
        c.execute(
            """UPDATE live_trade_intents
               SET status='filled', filled_price=?, filled_volume=?, filled_amount=?,
                   filled_at=?, user_note=?
               WHERE intent_id=?""",
            (price, volume, amount, filled_at, note, intent_id),
        )
        c.commit()
    finally:
        c.close()
    print(f"✅ 已回填成交 {intent_id}: {volume}股 @ ¥{price:.2f}，金额 ¥{amount:,.2f}")
    for warning in warnings:
        print(f"⚠️ {warning}；已按真实成交记账，但不视为按建议价格执行")
    print("说明：这只记录真实手工成交结果；是否同步到模拟盘/实盘影子账户需另行执行对账流程。")
    return 0


def cancel(args: argparse.Namespace) -> int:
    c = conn()
    try:
        cur = c.execute(
            "UPDATE live_trade_intents SET status=?, user_note=? WHERE intent_id=? AND status='proposed'",
            (args.status, args.note or "", args.intent_id),
        )
        c.commit()
        if cur.rowcount == 0:
            print("未取消：建议单不存在或不是 proposed 状态")
            return 1
    finally:
        c.close()
    print(f"✅ {args.intent_id} 已标记为 {args.status}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="实盘操盘建议单")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("propose", help="生成建议单")
    pp.add_argument("action", choices=["buy", "sell"])
    pp.add_argument("code")
    pp.add_argument("name")
    pp.add_argument("--price", type=float, required=True)
    pp.add_argument("--volume", type=int, required=True)
    pp.add_argument("--limit-price", type=float, default=0)
    pp.add_argument("--reason", default="")
    pp.add_argument("--strategy", default="manual_live")
    pp.add_argument("--risk-note", default="")
    pp.add_argument("--expire-minutes", type=int, default=DEFAULT_EXPIRE_MINUTES)
    pp.set_defaults(func=propose)

    lp = sub.add_parser("list", help="查看建议单")
    lp.add_argument("--status", default="proposed", choices=["proposed", "filled", "cancelled", "rejected", "expired", "all"])
    lp.add_argument("--limit", type=int, default=20)
    lp.set_defaults(func=list_intents)

    fp = sub.add_parser("fill", help="回填成交")
    fp.add_argument("intent_id")
    fp.add_argument("--price", type=float, required=True)
    fp.add_argument("--volume", type=int, required=True)
    fp.add_argument("--note", default="")
    fp.add_argument(
        "--filled-at",
        default="",
        help="真实成交时间（YYYY-MM-DD HH:MM:SS）；补录历史成交时使用",
    )
    fp.add_argument("--force", action="store_true")
    fp.set_defaults(func=fill)

    cp = sub.add_parser("cancel", help="取消/拒绝建议单")
    cp.add_argument("intent_id")
    cp.add_argument("--status", default="cancelled", choices=["cancelled", "rejected", "expired"])
    cp.add_argument("--note", default="")
    cp.set_defaults(func=cancel)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
