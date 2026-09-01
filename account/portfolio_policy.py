"""Central simulated-portfolio capacity and replacement policy."""

from __future__ import annotations

from typing import Any


SIM_TARGET_MIN_POSITIONS = 10
SIM_TARGET_MAX_POSITIONS = 12
SIM_HARD_MAX_POSITIONS = 15


def simulated_account_policy(
    position_count: int,
    industry_exposure: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return one shared contract for the state, decision and execution layers."""
    count = max(0, int(position_count))
    if count > SIM_HARD_MAX_POSITIONS:
        capacity_state = "hard_breach"
    elif count > SIM_TARGET_MAX_POSITIONS:
        capacity_state = "above_target"
    elif count >= SIM_TARGET_MIN_POSITIONS:
        capacity_state = "within_target"
    else:
        capacity_state = "below_target"
    return {
        "blocked_prefixes": ["688", "8", "4"],
        "position_target": {
            "min": SIM_TARGET_MIN_POSITIONS,
            "max": SIM_TARGET_MAX_POSITIONS,
        },
        "max_positions": SIM_HARD_MAX_POSITIONS,
        "position_count": count,
        "capacity_state": capacity_state,
        "new_position_requires_replacement": count >= SIM_TARGET_MAX_POSITIONS,
        "replacement_edge_required": "strong",
        "buy_lot_size": 100,
        "t_plus_1": True,
        "industry_exposure": industry_exposure or [],
    }
