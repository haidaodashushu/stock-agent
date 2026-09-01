"""Single deterministic market-regime classifier shared by every decision path."""
from __future__ import annotations

import math
from typing import Any


MARKET_REGIME_RULE_VERSION = 1


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return default if math.isnan(number) or math.isinf(number) else number
    except (TypeError, ValueError):
        return default


def classify_market_regime(indices: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Classify one deterministic market regime from an index snapshot."""
    valid = {
        symbol: _float(row.get("change_pct"))
        for symbol, row in indices.items()
        if isinstance(row, dict)
        and row.get("name")
        and row.get("change_pct") is not None
    }
    broad_symbols = ("sh000001", "sz399001", "sh000300")
    broad = [valid[symbol] for symbol in broad_symbols if symbol in valid]
    changes = list(valid.values())
    total = len(changes)
    advancing = sum(1 for value in changes if value > 0.05)
    declining = sum(1 for value in changes if value < -0.05)
    average = round(sum(changes) / total, 2) if total else 0.0
    broad_average = round(sum(broad) / len(broad), 2) if broad else average

    regime = "neutral"
    if total >= 3:
        breadth_threshold = math.ceil(total * 0.6)
        if broad_average >= 0.6 and advancing >= breadth_threshold:
            regime = "strong"
        elif broad_average <= -0.6 and declining >= breadth_threshold:
            regime = "weak"

    label = {"strong": "偏强", "neutral": "中性", "weak": "偏弱"}[regime]
    return {
        "regime": regime,
        "summary": (
            f"市场{label}：宽基均值{broad_average:+.2f}%，"
            f"{advancing}涨/{declining}跌（有效指数{total}个）"
        ),
        "broad_average_pct": broad_average,
        "index_average_pct": average,
        "advancing_count": advancing,
        "declining_count": declining,
        "index_count": total,
        "source": f"deterministic_indices.v{MARKET_REGIME_RULE_VERSION}",
    }
