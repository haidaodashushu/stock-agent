#!/usr/bin/env python3
"""解析用户成交回报文本并回填 live_trade_intents。

支持：
  成交 L20260618150102-ABC123 41.20 3000
  修改成交 L... 41.20 3000
  已买 L... 41.20 3000
  已卖 L... 17.33 1000

也支持无编号、按最近待执行建议单匹配：
  成交 600460 41.20 300
  修改成交 600460 41.20 300
  买入 600460 41.20 300
  卖出 600460 41.20 300

无编号时若同代码有多条待执行建议单，会要求补充编号，避免误回填。
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.store.sqlite_store import StockStore  # noqa: E402
from data.live_manual_account import expire_stale_proposed_intents  # noqa: E402

ID_PATTERNS = [
    re.compile(r"(?:修改成交|成交|已买|已卖|买入|卖出)\s+(L\d{14}-[A-Z0-9]{6})\s+([0-9]+(?:\.[0-9]+)?)\s+(\d+)")
]
CODE_PATTERNS = [
    re.compile(r"(?:修改成交|成交|已买|已卖|买入|卖出)\s+([036]\d{5})\s+([0-9]+(?:\.[0-9]+)?)\s+(\d+)"),
    re.compile(r"([036]\d{5})\s+(?:修改成交|成交|已买|已卖|买入|卖出)\s+([0-9]+(?:\.[0-9]+)?)\s+(\d+)"),
]


def fill(intent_id: str, price: str, volume: str, note: str) -> int:
    return subprocess.call([
        sys.executable,
        str(ROOT / "scripts" / "live_trade_intent.py"),
        "fill",
        intent_id,
        "--price", price,
        "--volume", volume,
        "--note", note,
    ], cwd=str(ROOT))


def find_pending_intent(code: str) -> tuple[str | None, str]:
    conn = StockStore()._get_conn()
    try:
        expire_stale_proposed_intents(conn)
        rows = conn.execute(
            """SELECT intent_id, action, code, name, suggested_price, suggested_volume, created_at
               FROM live_trade_intents
               WHERE status='proposed' AND code=?
               ORDER BY id DESC""",
            (str(code).zfill(6),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None, f"未找到 {code} 的待执行建议单，请带上建议单编号。"
    if len(rows) > 1:
        items = [f"{r['intent_id']} {r['action']} {r['code']} {r['name']} @{r['suggested_price']} x{r['suggested_volume']} {r['created_at']}" for r in rows[:5]]
        return None, "同一代码有多条待执行建议单，请带编号：\n" + "\n".join(items)
    return rows[0]["intent_id"], ""


def main() -> int:
    text = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    for pat in ID_PATTERNS:
        m = pat.search(text)
        if m:
            intent_id, price, volume = m.groups()
            return fill(intent_id, price, volume, text)

    for pat in CODE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        code, price, volume = m.groups()
        intent_id, err = find_pending_intent(code)
        if not intent_id:
            print("❌ " + err)
            return 2
        return fill(intent_id, price, volume, text)

    print("❌ 未识别成交回报。格式示例：成交 L20260618150102-ABC123 41.20 300，或 成交 600460 41.20 300")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
