#!/usr/bin/env python3
"""Run the strategic-pool radar only at decision-preparation time slots."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.market_calendar import market_day  # noqa: E402
from scripts.scan_intraday_radar import scan  # noqa: E402


TARGET_SLOTS = {
    (9, 50), (10, 20), (10, 50), (11, 20), (11, 30),
    (13, 0), (13, 20), (13, 50), (14, 20),
}


def main() -> int:
    now = datetime.now().replace(second=0, microsecond=0)
    if (now.hour, now.minute) not in TARGET_SLOTS or not market_day(now.date()).is_open:
        # The cron wakes on a coarse cadence.  Tell its wrapper that this tick
        # intentionally did no work, so board refresh/promotion are skipped too.
        return 3
    try:
        print(json.dumps(scan(now), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"scheduled intraday radar failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
