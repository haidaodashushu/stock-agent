#!/usr/bin/env python3
"""Rank intraday price/turnover absorption over an arbitrary time window.

Examples:
  .venv/bin/python scripts/analyze_intraday_flow.py \
    --codes 300308,300502,600584,688256 \
    --start 13:47 --end 14:15 --follow-end 15:00

  .venv/bin/python scripts/analyze_intraday_flow.py \
    --top-by-turnover 100 --start 13:47 --end 14:15 \
    --follow-end 15:00 --json-out reports/intraday-flow.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.fetcher.tencent_quote import TencentQuoteFetcher
from data.intraday_flow import aggregate_groups, analyze_many
from data.trading_state import fetch_quotes

DB_PATH = ROOT / "data" / "stock_data.db"
DEFAULT_ETFS = {
    "159915": "创业板ETF易方达",
    "588000": "科创50ETF华夏",
    "510300": "沪深300ETF华泰柏瑞",
    "512100": "中证1000ETF南方",
    "563360": "A500ETF华泰柏瑞",
    "159352": "A500ETF南方",
    "510050": "上证50ETF",
    "510500": "中证500ETF",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="分析任意盘中区间的价格反应、双边成交额与后续保留率；不等同于主力净流入"
    )
    universe = parser.add_mutually_exclusive_group(required=True)
    universe.add_argument("--codes", help="逗号分隔股票/ETF代码")
    universe.add_argument(
        "--top-by-turnover",
        type=int,
        metavar="N",
        help="从本地最新A股池按腾讯当日成交额选前N只",
    )
    parser.add_argument("--include-default-etfs", action="store_true", help="追加常用宽基ETF")
    parser.add_argument("--start", required=True, help="区间起点 HH:MM/HHMM")
    parser.add_argument("--end", required=True, help="区间终点 HH:MM/HHMM")
    parser.add_argument("--follow-end", help="后续观察终点 HH:MM/HHMM，例如15:00")
    parser.add_argument(
        "--expect-date",
        default=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"),
        help="要求腾讯分钟数据的交易日 YYYYMMDD；默认上海当前日期",
    )
    parser.add_argument("--allow-date-mismatch", action="store_true", help="允许分析非expect-date数据")
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--limit", type=int, default=30, help="终端最多展示多少只")
    parser.add_argument(
        "--sort-by",
        choices=("turnover", "price-change", "net-change", "retention"),
        default="turnover",
    )
    parser.add_argument("--groups-file", help='JSON板块映射，格式：{"CPO":["300308",...]}')
    parser.add_argument("--groups-from-db", action="store_true", help="使用concepts表已有概念成员聚合")
    parser.add_argument("--json-out", help="完整JSON输出路径")
    return parser.parse_args()


def load_stock_names(connection: sqlite3.Connection) -> dict[str, str]:
    return {str(code).zfill(6): str(name or "") for code, name in connection.execute("SELECT code,name FROM stocks")}


def select_items(args: argparse.Namespace, connection: sqlite3.Connection) -> list[dict[str, Any]]:
    names = load_stock_names(connection)
    if args.codes:
        codes = []
        for raw in args.codes.split(","):
            code = raw.strip().lower().removeprefix("sh").removeprefix("sz")
            if code:
                codes.append(code.zfill(6))
        quote_map = fetch_quotes(codes)
        items = [
            {
                "code": code,
                "name": names.get(code) or quote_map.get(code, {}).get("name") or DEFAULT_ETFS.get(code, ""),
                "day_change_pct": quote_map.get(code, {}).get("change_pct"),
                "day_turnover_quote": quote_map.get(code, {}).get("amount"),
            }
            for code in dict.fromkeys(codes)
        ]
    else:
        latest = connection.execute("SELECT MAX(date) FROM daily_prices").fetchone()[0]
        codes = [
            str(row[0]).zfill(6)
            for row in connection.execute("SELECT DISTINCT code FROM daily_prices WHERE date=?", (latest,))
            if str(row[0]).zfill(6).startswith(("0", "3", "6"))
        ]
        quote_map = fetch_quotes(codes)
        ranked = sorted(quote_map.values(), key=lambda row: float(row.get("amount") or 0), reverse=True)
        items = [
            {
                "code": row["code"],
                "name": row.get("name") or names.get(row["code"], ""),
                "day_change_pct": row.get("change_pct"),
                "day_turnover_quote": row.get("amount"),
            }
            for row in ranked[: max(1, args.top_by_turnover)]
        ]
    if args.include_default_etfs:
        existing = {item["code"] for item in items}
        items.extend(
            {"code": code, "name": name, "kind": "etf"}
            for code, name in DEFAULT_ETFS.items()
            if code not in existing
        )
    return items


def load_groups(args: argparse.Namespace, connection: sqlite3.Connection) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    if args.groups_from_db:
        for name, stocks in connection.execute("SELECT name,stocks FROM concepts WHERE stocks<>''"):
            codes = [part.strip().zfill(6) for part in str(stocks).split(",") if part.strip()]
            if codes:
                groups[str(name)] = codes
    if args.groups_file:
        loaded = json.loads(Path(args.groups_file).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not all(isinstance(value, list) for value in loaded.values()):
            raise ValueError("groups-file 必须是板块名到代码数组的JSON对象")
        groups.update({str(name): [str(code).zfill(6) for code in codes] for name, codes in loaded.items()})
    return groups


def fmt_pct(value: Any) -> str:
    return "-" if value is None else f"{float(value):+.2f}%"


def fmt_amount(value: Any) -> str:
    return "-" if value is None else f"{float(value) / 100_000_000:.2f}亿"


def main() -> int:
    args = parse_args()
    with sqlite3.connect(DB_PATH) as connection:
        items = select_items(args, connection)
        groups = load_groups(args, connection)

    fetcher = TencentQuoteFetcher()
    records, errors = analyze_many(
        items,
        fetcher.fetch_minute,
        args.start,
        args.end,
        follow_end=args.follow_end,
        amount_mode="cumulative",
        max_workers=args.max_workers,
        retries=args.retries,
    )

    if not args.allow_date_mismatch:
        accepted = []
        for row in records:
            if row.get("trading_date") != args.expect_date:
                errors.append(
                    {
                        "code": row["code"],
                        "name": row.get("name", ""),
                        "error": f"交易日期不匹配: {row.get('trading_date') or 'missing'} != {args.expect_date}",
                    }
                )
            else:
                accepted.append(row)
        records = accepted

    sort_fields = {
        "turnover": "turnover_amount",
        "price-change": "price_change_pct",
        "net-change": "net_change_pct",
        "retention": "retention_ratio",
    }
    sort_field = sort_fields[args.sort_by]
    records.sort(
        key=lambda row: (
            row.get(sort_field) is not None,
            row.get(sort_field, 0),
        ),
        reverse=True,
    )
    group_rows = aggregate_groups(records, groups) if groups else []
    payload = {
        "contract": "intraday_price_turnover_proxy_v1",
        "source": "tencent_ifzq",
        "expected_trading_date": args.expect_date,
        "requested_start": args.start,
        "requested_end": args.end,
        "follow_end": args.follow_end,
        "disclaimer": "turnover_amount为买卖双方总成交额，不是主力净流入；价格上涨仅表示该时段买方更主动。",
        "records": records,
        "groups": group_rows,
        "errors": errors,
    }

    print(
        f"区间量价承接代理 | {args.expect_date} {args.start}-{args.end}"
        + (f" -> {args.follow_end}" if args.follow_end else "")
    )
    print("代码     名称       区间涨跌   区间成交额  截至观测占比 后续涨跌   净涨跌    保留率")
    for row in records[: max(0, args.limit)]:
        retention = row.get("retention_ratio")
        retention_text = "-" if retention is None else f"{float(retention) * 100:+.1f}%"
        print(
            f"{row['code']} {str(row.get('name') or '')[:8]:<8} "
            f"{fmt_pct(row.get('price_change_pct')):>9} "
            f"{fmt_amount(row.get('turnover_amount')):>10} "
            f"{fmt_pct(row.get('turnover_observed_pct')):>8} "
            f"{fmt_pct(row.get('follow_change_pct')):>9} "
            f"{fmt_pct(row.get('net_change_pct')):>9} "
            f"{retention_text:>8}"
        )
    if group_rows:
        print("\n板块聚合（区间成交额加权）")
        print("板块             样本  区间成交额  区间涨跌   后续涨跌   保留率")
        for row in group_rows[: max(0, args.limit)]:
            retention = row.get("weighted_retention_ratio")
            retention_text = "-" if retention is None else f"{float(retention) * 100:+.1f}%"
            print(
                f"{row['group'][:16]:<16} {row['count']:>4} "
                f"{fmt_amount(row['turnover_amount']):>10} "
                f"{fmt_pct(row.get('weighted_price_change_pct')):>9} "
                f"{fmt_pct(row.get('weighted_follow_change_pct')):>9} "
                f"{retention_text:>8}"
            )
    if errors:
        print(f"\n失败/跳过 {len(errors)} 只：")
        for row in errors[:10]:
            print(f"- {row['code']} {row.get('name','')}: {row['error']}")

    if args.json_out:
        output = Path(args.json_out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {output}")
    return 0 if records else 2


if __name__ == "__main__":
    raise SystemExit(main())
