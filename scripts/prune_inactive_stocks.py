#!/usr/bin/env python3
"""清理腾讯行情明确标记为 D 的失效股票。

D 通常表示退市、吸收合并、转换代码等不再交易的老代码。脚本默认 dry-run；
加 --apply 后删除行情/筛选等派生数据，并将 stocks 标记为 inactive。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.store.sqlite_store import StockStore  # noqa: E402


def prefix(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("6"):
        return "sh"
    if code.startswith(("8", "4", "9")):
        return "bj"
    return "sz"


def scan_quote_status(codes: list[str]) -> tuple[list[dict], list[str]]:
    inactive: list[dict] = []
    noquote: list[str] = []
    for i in range(0, len(codes), 80):
        batch = codes[i:i + 80]
        url = "http://qt.gtimg.cn/q=" + ",".join(prefix(c) + c for c in batch)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=15).read().decode("gbk", errors="ignore")
        except Exception:
            noquote.extend(batch)
            continue

        seen: set[str] = set()
        for line in data.strip().split("\n"):
            if '="' not in line:
                continue
            fields = line.split('="', 1)[1].rstrip('";').split("~")
            if len(fields) < 41:
                continue
            code = fields[2].strip().zfill(6)
            seen.add(code)
            if fields[40].strip() == "D":
                inactive.append({"code": code, "name": fields[1].strip(), "status": "D"})
        noquote.extend([c for c in batch if c not in seen])
    return inactive, noquote


def main() -> int:
    parser = argparse.ArgumentParser(description="清理失效股票数据")
    parser.add_argument("--apply", action="store_true", help="实际写库；默认只预览")
    args = parser.parse_args()

    store = StockStore()
    conn = store._get_conn()
    try:
        rows = conn.execute("SELECT DISTINCT code FROM daily_prices ORDER BY code").fetchall()
        codes = [r["code"] for r in rows]
        inactive, noquote = scan_quote_status(codes)
        by_code = {x["code"]: x for x in inactive}
        for row in conn.execute("SELECT code, name FROM stocks").fetchall():
            code = row["code"]
            if code in by_code and not by_code[code].get("name"):
                by_code[code]["name"] = row["name"] or code

        targets = sorted(by_code)
        report = {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "apply": args.apply,
            "target_count": len(targets),
            "noquote_count": len(noquote),
            "targets": [by_code[c] for c in targets],
            "noquote": noquote,
        }
        out_dir = ROOT / "logs" / "maintenance"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"prune_inactive_stocks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"扫描完成：D状态 {len(targets)} 只，未取到行情 {len(noquote)} 只")
        print(f"报告：{out_path}")
        if targets[:20]:
            print("样本：" + "、".join(f"{c} {by_code[c].get('name','')}" for c in targets[:20]))

        if not args.apply or not targets:
            return 0

        tables = ["daily_prices", "realtime_snapshots", "signals", "screen_records", "news_events"]
        deleted: dict[str, int] = {}
        for table in tables:
            try:
                before = conn.total_changes
                conn.executemany(f"DELETE FROM {table} WHERE code=?", [(c,) for c in targets])
                deleted[table] = conn.total_changes - before
            except Exception as e:
                deleted[table] = -1
                print(f"跳过 {table}: {e}")

        conn.executemany(
            "UPDATE stocks SET is_active=0, updated_at=datetime('now','localtime') WHERE code=?",
            [(c,) for c in targets],
        )
        conn.commit()
        print("已清理：" + json.dumps(deleted, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
