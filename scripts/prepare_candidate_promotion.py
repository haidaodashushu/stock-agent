#!/usr/bin/env python3
"""Print the immutable version only when dynamic stocks need evaluation."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.candidate_promotion import (  # noqa: E402
    build_promotion_snapshot,
    record_promotion_noop,
)
from data.market_calendar import market_day  # noqa: E402
from data.store.sqlite_store import StockStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare candidate promotion evaluation")
    parser.add_argument("--allow-closed", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    now = datetime.now()
    if not args.allow_closed and not market_day(now.date()).is_open:
        return 3
    store = StockStore()
    snapshot = build_promotion_snapshot(store, now=now)
    if not snapshot["required_evidence_codes"]:
        if snapshot.get("source_candidate_count"):
            record_promotion_noop(snapshot, store, now=now)
        return 3
    conn = store._get_conn()
    try:
        completed = conn.execute(
            "SELECT 1 FROM candidate_promotion_runs WHERE as_of=?",
            (snapshot["as_of"],),
        ).fetchone()
    finally:
        conn.close()
    if completed:
        return 3
    result = {
        "as_of": snapshot["as_of"],
        "trade_date": snapshot["trade_date"],
        "candidate_count": len(snapshot["required_evidence_codes"]),
        "codes": snapshot["required_evidence_codes"],
    }
    if args.pretty:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(snapshot["as_of"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
