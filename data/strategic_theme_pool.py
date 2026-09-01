"""Validated access to the checked-in strategic-theme monitoring pool."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from config.runtime_paths import configurable_path

ROOT = Path(__file__).resolve().parents[1]
POOL_PATH = configurable_path(
    "STOCK_STRATEGIC_THEME_POOL_CONFIG", "config/strategic_theme_pool.local.json",
)
BLOCKED_PREFIXES = ("688", "8", "4")


class StrategicPoolError(ValueError):
    pass


@lru_cache(maxsize=1)
def load_strategic_pool(path: Path = POOL_PATH) -> dict[str, Any]:
    if path == POOL_PATH and not path.exists():
        path = ROOT / "config" / "strategic_theme_pool.example.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    target_size = int(raw.get("target_size") or 0)
    groups = raw.get("groups")
    if not isinstance(groups, list) or not groups:
        raise StrategicPoolError("strategic pool groups must be non-empty")

    stocks: dict[str, dict[str, str]] = {}
    group_counts: dict[str, int] = {}
    for group in groups:
        group_name = str(group.get("name") or "").strip()
        rows = group.get("stocks")
        if not group_name or not isinstance(rows, list):
            raise StrategicPoolError("every strategic group needs a name and stocks")
        group_counts[group_name] = len(rows)
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise StrategicPoolError(f"invalid pool row in {group_name}: {row!r}")
            code, name = str(row[0]).strip(), str(row[1]).strip()
            if not re.fullmatch(r"\d{6}", code) or code.startswith(BLOCKED_PREFIXES):
                raise StrategicPoolError(f"invalid or blocked pool code: {code!r}")
            if not name or code in stocks:
                raise StrategicPoolError(f"missing name or duplicate pool code: {code}")
            stocks[code] = {"code": code, "name": name, "group": group_name}

    if len(stocks) != target_size:
        raise StrategicPoolError(
            f"strategic pool contains {len(stocks)}, expected {target_size}"
        )
    return {
        "version": int(raw.get("version") or 1),
        "target_size": target_size,
        "description": str(raw.get("description") or ""),
        "group_counts": group_counts,
        "stocks": stocks,
    }


def strategic_pool_codes() -> tuple[str, ...]:
    return tuple(load_strategic_pool()["stocks"])


def strategic_pool_stock(code: str) -> dict[str, str] | None:
    return load_strategic_pool()["stocks"].get(str(code or "").zfill(6))


def is_strategic_pool_stock(code: str) -> bool:
    return strategic_pool_stock(code) is not None
