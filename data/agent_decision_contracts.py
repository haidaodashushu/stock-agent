"""Single source of truth for agent decision and submission contracts.

Prompts describe how to reason.  These contracts describe what the current
runtime can read, validate, and execute.  Read-only overview tools expose the
same objects that deterministic validators import, preventing prompt text from
drifting away from executable behavior.
"""
from __future__ import annotations

from typing import Any, Literal


CONTRACT_VERSION = "stock_agent_decision_contract.v1"

CONFIDENCES = frozenset({"strong", "medium", "weak"})
MARKET_REGIMES = frozenset({"strong", "neutral", "weak"})
ENTRY_ROUTES = frozenset({"early_start", "strong_continuation"})
PROMOTION_DECISIONS = frozenset({"promote", "watch", "reject"})

SIMULATED_ACTIONS = frozenset({
    "buy", "add", "hold", "reduce", "sell", "clear", "watch", "noop",
})
LIVE_ACTIONS = frozenset({"buy", "sell", "hold", "watch", "noop"})

SELECTION_MAX_RESULTS = 10
SELECTION_EVIDENCE_MAX_CODES = 40
SELECTION_EVIDENCE_RECOMMENDED_CODES = 8
TRADING_EVIDENCE_MAX_CODES = 50

TRADING_TEXT_LIMITS = {
    "reason": 180,
    "risk": 150,
    "replacement_reason": 180,
    "market_view.summary": 100,
    "report.focus_item": 80,
    "report.risk": 100,
}
PROMOTION_TEXT_LIMITS = {"name": 40, "reason": 300, "risk": 240}


def _values(items: frozenset[str]) -> list[str]:
    return sorted(items)


def selection_decision_contract() -> dict[str, Any]:
    return {
        "schema": CONTRACT_VERSION,
        "task": "selection",
        "evidence": {
            "tool": "candidate_evidence",
            "max_codes_per_call": SELECTION_EVIDENCE_MAX_CODES,
            "recommended_codes_per_call": SELECTION_EVIDENCE_RECOMMENDED_CODES,
            "required_coverage": "all required_evidence_codes",
        },
        "submission": {
            "tool": "submit_stock_selection",
            "rows_key": "selections",
            "reviewed_codes": "exactly all required_evidence_codes",
            "max_rows": SELECTION_MAX_RESULTS,
        },
        "values": {
            "confidence": _values(CONFIDENCES),
            "entry_route": _values(ENTRY_ROUTES),
        },
        "decision_shape": {
            "as_of": "selection_overview.as_of",
            "reviewed_codes": ["all required_evidence_codes"],
            "market_view": {"summary": "market and candidate-pool context"},
            "selections": [{
                "code": "in-scope code",
                "name": "stock name",
                "entry_route": "entry_route",
                "confidence": "confidence",
                "reason": "evidence-based qualification reason",
                "risk": "key risk or falsifiable invalidation condition",
            }],
            "report": {"focus": ["next-session checks"], "risk": "pool risk"},
        },
    }


def promotion_decision_contract() -> dict[str, Any]:
    return {
        "schema": CONTRACT_VERSION,
        "task": "promotion",
        "evidence": {
            "tool": "promotion_evidence",
            "required_coverage": "all required_evidence_codes",
        },
        "submission": {
            "tool": "submit_candidate_promotion",
            "rows_key": "decisions",
            "reviewed_codes": "exactly all required_evidence_codes",
            "one_row_per_required_code": True,
        },
        "values": {
            "decision": _values(PROMOTION_DECISIONS),
            "confidence": _values(CONFIDENCES),
            "entry_route": [*_values(ENTRY_ROUTES), "unclassified"],
        },
        "rules": {
            "promote_entry_route": "one enabled entry_route",
            "watch_or_reject_entry_route": "unclassified",
        },
        "text_limits": dict(PROMOTION_TEXT_LIMITS),
        "decision_shape": {
            "reviewed_codes": ["all required_evidence_codes"],
            "decisions": [{
                "code": "in-scope code",
                "name": "stock name",
                "decision": "decision",
                "entry_route": "entry_route",
                "confidence": "confidence",
                "reason": "current evidence and route judgment",
                "risk": "key risk or falsifiable invalidation condition",
            }],
        },
    }


def trading_decision_contract(
    mode: Literal["simulated", "live"], account_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    account_policy = account_policy if isinstance(account_policy, dict) else {}
    actions = SIMULATED_ACTIONS if mode == "simulated" else LIVE_ACTIONS
    rows_key = "signals" if mode == "simulated" else "decisions"
    action_requirements: dict[str, Any]
    if mode == "simulated":
        action_requirements = {
            "buy": "target_amount > 0; code must be an eligible active candidate",
            "add": "target_amount > 0; code must be an existing position",
            "reduce": "sell_pct or volume; default sell_pct is 0.5",
            "sell": "sell_pct or volume; default sell_pct is 1.0",
            "clear": "exits all currently sellable shares; sell_pct is ignored",
        }
    else:
        action_requirements = {
            "buy": (
                "target_amount > 0 or volume > 0; opens an eligible candidate or adds "
                "to an existing position"
            ),
            "sell": (
                "sell_pct or volume; partial reduction and full exit both use sell; "
                "default sell_pct is 1.0"
            ),
        }
    row_shape: dict[str, Any] = {
        "code": "in-scope code",
        "name": "stock name",
        "action": "action",
        "confidence": "confidence",
        "target_amount": "number when required",
        "volume": "optional integer shares",
        "sell_pct": "number when required",
        "reason": "evidence-based decision reason",
        "risk": "key risk",
    }
    row_shape["watch_plan"] = {
        "required_when": "overview.refresh.opportunity_trial=true",
        "state": "watch|account_blocked|data_pending|invalid|holding",
        "thesis": "durable original thesis, preserve unless new evidence changes it",
        "wait_reason": "why hold/wait/act now and what would change the decision",
        "review_above": "positive observed-structure price or null",
        "review_below": "positive pullback/review price or null",
        "invalidation_below": "positive original structural risk level or null",
        "invalidation_reason": "required when invalid; distinguish portfolio reduction",
        "review_after_minutes": "integer 15..240; use 60 when no price trigger is defensible",
        "requalified": "true only for an explicitly revalidated retained opportunity",
        "requalification_reason": "current route, structure, company evidence and account fit",
    }
    decision_shape: dict[str, Any] = {
        "reviewed_codes": ["all required_evidence_codes"],
        "market_view": {"regime": "overview regime", "summary": "text"},
        rows_key: [row_shape],
        "report": {"focus": ["next-round checks"], "risk": "portfolio risk"},
    }
    if mode == "simulated":
        row_shape.update({
            "replacement_code": "required replacement position when applicable",
            "replacement_edge": "strong when replacement is required",
            "replacement_reason": "evidence-based relative advantage",
        })
        decision_shape["portfolio_review"] = {
            "current_count": "current position count",
            "capacity_state": "account_policy.capacity_state",
            "weakest_holdings": [{"code": "position code", "reason": "why weaker"}],
            "industry_concentration": ["material concentration risk"],
        }
    else:
        row_shape.update({
            "price": "analysis price",
            "limit_price": "manual execution reference",
            "expire_minutes": "positive suggestion lifetime in minutes",
        })
    return {
        "schema": CONTRACT_VERSION,
        "task": f"trading-{mode}",
        "evidence": {
            "tool": "stock_evidence",
            "max_codes_per_call": TRADING_EVIDENCE_MAX_CODES,
            "required_coverage": "all required_evidence_codes",
            "activity_tool": "recent_trading_activity",
        },
        "submission": {
            "tool": "submit_trading_decision",
            "rows_key": rows_key,
            "reviewed_codes": "exactly all required_evidence_codes",
            "one_row_per_required_code": True,
        },
        "values": {
            "action": _values(actions),
            "confidence": _values(CONFIDENCES),
        },
        "action_requirements": action_requirements,
        "hard_constraints": {
            "market_regime": "copy overview.market.regime.regime",
            "new_entry_gate": "selection.buy_eligible=true and setup_stage=actionable",
            "sellable_volume": "never exceed position.available_to_sell",
            "blocked_prefixes": list(account_policy.get("blocked_prefixes") or []),
            "max_decision_price_drift_pct": account_policy.get(
                "max_decision_price_drift_pct"
            ),
            "position_policy": account_policy,
        },
        "text_limits": dict(TRADING_TEXT_LIMITS),
        "decision_shape": decision_shape,
    }
