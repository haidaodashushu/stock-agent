#!/usr/bin/env python3
"""Capture one opening-auction phase and refresh candidate-only discoveries."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.market_calendar import market_day  # noqa: E402
from data.opening_auction import (  # noqa: E402
    AUCTION_PHASES,
    build_auction_watch_candidates,
    fetch_iwencai_auction_final,
    fetch_tencent_auction_snapshots,
    save_auction_run,
    save_auction_watch_candidates,
    save_iwencai_final,
    save_tencent_snapshots,
)
from data.store.sqlite_store import StockStore  # noqa: E402
from data.strategic_theme_pool import strategic_pool_codes  # noqa: E402


TARGET_TIMES = {
    "cancelable_end": "09:19:50",
    "locked_end": "09:24:50",
    "final": "09:25:05",
}
MAX_LATE_SECONDS = {"cancelable_end": 9, "locked_end": 9, "final": 75}


def _wait_until(now: datetime, clock: str, *, no_wait: bool, max_late_seconds: int = 75) -> None:
    target = datetime.strptime(f"{now.date()} {clock}", "%Y-%m-%d %H:%M:%S")
    delta = (target - now).total_seconds()
    if delta < -max_late_seconds:
        raise RuntimeError(f"missed observation target {clock} by {-delta:.0f}s")
    if delta > 0 and not no_wait:
        time.sleep(delta)


def capture(phase: str, *, no_wait: bool = False, now: datetime | None = None) -> dict:
    if phase not in AUCTION_PHASES:
        raise ValueError(f"unsupported phase: {phase}")
    now = now or datetime.now()
    day = now.date().isoformat()
    _wait_until(
        now, TARGET_TIMES[phase], no_wait=no_wait,
        max_late_seconds=MAX_LATE_SECONDS[phase],
    )
    started = datetime.now()
    store = StockStore()
    codes = list(strategic_pool_codes())
    snapshots, errors = fetch_tencent_auction_snapshots(codes, phase=phase)
    save_tencent_snapshots(store, snapshots)
    current_snapshots = [row for row in snapshots if row.get("provider_current")]
    if len(current_snapshots) != len(snapshots):
        errors.append(f"tencent stale provider timestamps: {len(snapshots) - len(current_snapshots)}")
    finals: list[dict] = []
    candidate_refresh = {
        "status": "not_applicable", "selected_count": 0, "codes": [],
        "direct_buy_eligible": False,
    }
    if phase == "final":
        # Give the final-result provider time to publish its 09:25 fields.
        _wait_until(datetime.now(), "09:27:00", no_wait=no_wait, max_late_seconds=180)
        finals, final_errors = fetch_iwencai_auction_final(codes, trade_date=day)
        errors.extend(final_errors)
        save_iwencai_final(store, finals)
        try:
            generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            candidates = build_auction_watch_candidates(store, trade_date=day)
            save_auction_watch_candidates(
                store, trade_date=day, generated_at=generated_at, candidates=candidates,
            )
            candidate_refresh = {
                "status": "ready", "selected_count": len(candidates),
                "codes": [row["code"] for row in candidates],
                "direct_buy_eligible": False,
            }
        except Exception as exc:
            errors.append(f"auction candidate refresh: {exc}")
            candidate_refresh["status"] = "failed"
    status = "ready" if (
        len(current_snapshots) == len(codes)
        and (phase != "final" or len(finals) == len(codes))
        and candidate_refresh["status"] != "failed"
    ) else "partial"
    completed = datetime.now()
    save_auction_run(
        store, trade_date=day, phase=phase, status=status, scope_count=len(codes),
        tencent_count=len(current_snapshots), iwencai_count=len(finals),
        started_at=started.strftime("%Y-%m-%d %H:%M:%S"),
        completed_at=completed.strftime("%Y-%m-%d %H:%M:%S"), errors=errors,
    )
    return {
        "trade_date": day, "phase": phase, "status": status, "scope_count": len(codes),
        "tencent_count": len(current_snapshots), "iwencai_count": len(finals), "errors": errors,
        "candidate_refresh": candidate_refresh,
        "trading_consumption": "candidate_only" if phase == "final" else False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="开盘集合竞价第一阶段观察采集")
    parser.add_argument("--phase", required=True, choices=AUCTION_PHASES)
    parser.add_argument("--no-wait", action="store_true", help="测试/诊断时不等待目标秒")
    parser.add_argument("--allow-closed", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    now = datetime.now()
    market = market_day(now)
    if not args.allow_closed and not market.is_open:
        return 0
    try:
        result = capture(args.phase, no_wait=args.no_wait, now=now)
    except Exception as exc:
        print(f"opening auction observation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
