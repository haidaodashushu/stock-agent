#!/usr/bin/env python3
"""Report collection coverage and strongest final auction observations."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.opening_auction import observation_coverage  # noqa: E402
from data.store.sqlite_store import StockStore  # noqa: E402


def report(days: int = 5, top: int = 10) -> dict:
    store = StockStore()
    coverage = observation_coverage(store, days)
    conn = store._get_conn()
    try:
        dates = [row["trade_date"] for row in coverage]
        strongest = []
        if dates:
            placeholders = ",".join("?" for _ in dates)
            rows = conn.execute(
                f"""SELECT trade_date,code,name,auction_change_pct,matched_amount_yuan,
                            unmatched_amount_signed,anomaly_type,rating
                       FROM opening_auction_final WHERE trade_date IN ({placeholders})
                      ORDER BY matched_amount_yuan DESC LIMIT ?""",
                [*dates, max(1, int(top))],
            ).fetchall()
            strongest = [dict(row) for row in rows]
    finally:
        conn.close()
    return {"coverage": coverage, "strongest_final_auctions": strongest, "trading_consumption": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(report(args.days, args.top), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
