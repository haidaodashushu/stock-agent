#!/usr/bin/env python3
"""Validate one AI decision, execute guarded layers, and render one short report."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.execute_live_trade_decision import execute as execute_live  # noqa: E402
from scripts.execute_trade_signal import execute as execute_simulated  # noqa: E402
from account.portfolio_policy import (  # noqa: E402
    SIM_HARD_MAX_POSITIONS,
    SIM_TARGET_MAX_POSITIONS,
    SIM_TARGET_MIN_POSITIONS,
    simulated_account_policy,
)
from data.market_calendar import ensure_actionable_trading_time  # noqa: E402
from data.trading_decision_repository import build_execution_context  # noqa: E402

SIM_ACTIONS = {"buy", "add", "hold", "reduce", "sell", "clear", "watch", "noop"}
LIVE_ACTIONS = {"buy", "sell", "hold", "watch", "noop"}
CONFIDENCES = {"strong", "medium", "weak"}
ACTION_NAMES = {
    "buy": "买入", "add": "加仓", "hold": "持有", "reduce": "减仓",
    "sell": "卖出", "clear": "清仓", "watch": "观察", "noop": "不操作",
}


def extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("AI response did not contain a JSON object")
        parsed = json.loads(value[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI response root must be an object")
    return parsed


def _normalize_rows(
    rows: list[dict[str, Any]], allowed_codes: set[str], actions: set[str],
) -> list[dict[str, Any]]:
    normalized = []
    seen: set[str] = set()
    for raw in rows:
        code = str(raw.get("code") or "").strip().zfill(6)
        action = str(raw.get("action") or "noop").strip().lower()
        confidence = str(raw.get("confidence") or "weak").strip().lower()
        if code not in allowed_codes:
            raise ValueError(f"{code}: code is not present in current facts")
        if action not in actions:
            raise ValueError(f"{code}: invalid action {action}")
        if confidence not in CONFIDENCES:
            raise ValueError(f"{code}: invalid confidence {confidence}")
        if code in seen:
            raise ValueError(f"{code}: multiple actions in the same decision group")
        seen.add(code)
        item = dict(raw)
        item.update({
            "code": code,
            "action": action,
            "confidence": confidence,
            "reason": str(raw.get("reason") or "")[:180],
            "risk": str(raw.get("risk") or "")[:150],
            "replacement_code": str(raw.get("replacement_code") or "").strip().zfill(6)
            if str(raw.get("replacement_code") or "").strip()
            else "",
            "replacement_edge": str(raw.get("replacement_edge") or "").strip().lower(),
            "replacement_reason": str(raw.get("replacement_reason") or "")[:180],
        })
        normalized.append(item)
    return normalized


def _root_items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = payload.get(key, [])
    if not isinstance(rows, list):
        raise ValueError(f"{key} must be an array")
    return [row for row in rows if isinstance(row, dict)]


def _market_view(
    payload: dict[str, Any], fallback: str, deterministic: dict[str, Any] | None = None,
) -> dict[str, str]:
    value = payload.get("market_view") if isinstance(payload.get("market_view"), dict) else {}
    deterministic = deterministic if isinstance(deterministic, dict) else {}
    regime = str(deterministic.get("regime") or value.get("regime") or "neutral")
    if regime not in {"strong", "neutral", "weak"}:
        regime = "neutral"
    return {
        "regime": regime,
        "summary": str(value.get("summary") or fallback)[:100],
        "source": str(deterministic.get("source") or "model")[:40],
    }


def _report(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    return {
        "focus": [str(item)[:80] for item in value.get("focus", [])]
        if isinstance(value.get("focus"), list)
        else [],
        "risk": str(value.get("risk") or "")[:100],
    }


def _account_scope(context: dict[str, Any]) -> tuple[dict[str, Any], list[dict], list[dict]]:
    account = context.get("account") if isinstance(context.get("account"), dict) else {}
    positions = context.get("positions") if isinstance(context.get("positions"), list) else []
    candidates = context.get("candidates") if isinstance(context.get("candidates"), list) else []
    return account, positions, candidates


def _reviewed_codes(payload: dict[str, Any], context: dict[str, Any]) -> list[str]:
    """Require an auditable proof that the complete decision scope was read."""
    required_raw = context.get("required_evidence_codes")
    if not isinstance(required_raw, list):
        # Every newly built production context carries this contract.  The
        # fallback keeps historical/unit contexts readable.
        return []
    required = [str(code).strip().zfill(6) for code in required_raw]
    raw = payload.get("reviewed_codes")
    if not isinstance(raw, list):
        raise ValueError("reviewed_codes must contain the complete decision universe")
    reviewed = [str(code).strip().zfill(6) for code in raw if str(code).strip()]
    if len(reviewed) != len(set(reviewed)):
        raise ValueError("reviewed_codes contains duplicates")
    missing = sorted(set(required) - set(reviewed))
    outside = sorted(set(reviewed) - set(required))
    if missing or outside:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if outside:
            details.append("outside=" + ",".join(outside))
        raise ValueError("reviewed_codes scope mismatch: " + "; ".join(details))
    return required


def _require_complete_decision_rows(
    rows: list[dict[str, Any]], context: dict[str, Any], field: str,
) -> None:
    """Require one auditable conclusion for every holding and candidate."""
    required_raw = context.get("required_evidence_codes")
    if not isinstance(required_raw, list):
        return
    required = {
        str(code).strip().zfill(6) for code in required_raw if str(code).strip()
    }
    decided = {row["code"] for row in rows}
    missing = sorted(required - decided)
    outside = sorted(decided - required)
    if missing or outside:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if outside:
            details.append("outside=" + ",".join(outside))
        raise ValueError(
            f"{field} must contain exactly one row for every required code: "
            + "; ".join(details)
        )


def _validate_new_entry_gate(row: dict[str, Any], candidates: list[dict]) -> None:
    """Apply persisted route/lifecycle eligibility as an execution hard gate."""
    if row.get("action") != "buy":
        return
    candidate = next(
        (item for item in candidates if str(item.get("code") or "").zfill(6) == row["code"]),
        None,
    )
    if not candidate:
        raise ValueError(f"{row['code']}: new buy requires an active candidate")
    selection = candidate.get("selection")
    if not isinstance(selection, dict) or not selection:
        return
    route = str(selection.get("entry_route") or "")
    if route not in {"early_start", "strong_continuation"}:
        raise ValueError(f"{row['code']}: entry route {route or '<empty>'} is not enabled")
    if selection.get("setup_stage") != "actionable" or selection.get("buy_eligible") is not True:
        raise ValueError(
            f"{row['code']}: candidate lifecycle is not actionable/buy_eligible"
        )


def _portfolio_review(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("portfolio_review")
    if not isinstance(raw, dict):
        return {}
    weakest = []
    for row in raw.get("weakest_holdings", []):
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip().zfill(6)
        if code:
            weakest.append({"code": code, "reason": str(row.get("reason") or "")[:180]})
    concentration = raw.get("industry_concentration", [])
    return {
        "current_count": int(raw.get("current_count") or 0),
        "capacity_state": str(raw.get("capacity_state") or ""),
        "weakest_holdings": weakest,
        "industry_concentration": [str(item)[:160] for item in concentration]
        if isinstance(concentration, list)
        else [],
    }


def validate_simulated_decision(
    payload: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    _, positions, candidates = _account_scope(context)
    reviewed_codes = _reviewed_codes(payload, context)
    allowed_codes = {
        str(row.get("code") or "").zfill(6)
        for row in positions + candidates
        if isinstance(row, dict)
    }
    signals = _normalize_rows(_root_items(payload, "signals"), allowed_codes, SIM_ACTIONS)
    _require_complete_decision_rows(signals, context, "signals")
    position_by_code = {
        str(row.get("code") or "").zfill(6): row
        for row in positions
        if isinstance(row, dict)
    }
    position_codes = set(position_by_code)
    for row in signals:
        _validate_new_entry_gate(row, candidates)
        if row["action"] in {"buy", "add"}:
            try:
                target_amount = float(row.get("target_amount") or 0)
            except (TypeError, ValueError):
                target_amount = 0
            if target_amount <= 0:
                raise ValueError(f"{row['code']}: simulated {row['action']} requires target_amount")
        if row["action"] == "buy" and row["code"] in position_codes:
            raise ValueError(f"{row['code']}: existing position must use add, not buy")
        if row["action"] == "add" and row["code"] not in position_codes:
            raise ValueError(f"{row['code']}: new position must use buy, not add")
    decided_position_codes = {row["code"] for row in signals}
    missing_positions = sorted(position_codes - decided_position_codes)
    if missing_positions:
        raise ValueError(
            "simulated decision omitted current positions: " + ",".join(missing_positions)
        )
    review = _portfolio_review(payload)
    current_count = len(position_codes)
    if current_count >= SIM_TARGET_MIN_POSITIONS:
        if not review:
            raise ValueError("simulated portfolio_review is required at or above target range")
        if review["current_count"] != current_count:
            raise ValueError(
                f"simulated portfolio_review current_count={review['current_count']} "
                f"does not match {current_count}"
            )
        expected_capacity = simulated_account_policy(current_count)["capacity_state"]
        if review["capacity_state"] != expected_capacity:
            raise ValueError(
                f"simulated portfolio_review capacity_state={review['capacity_state']} "
                f"does not match {expected_capacity}"
            )
    weakest_codes = {row["code"] for row in review.get("weakest_holdings", [])}
    if not weakest_codes.issubset(position_codes):
        raise ValueError("simulated weakest_holdings contains a non-position code")
    signal_by_code = {row["code"]: row for row in signals}
    new_buys = [row for row in signals if row["action"] == "buy"]
    free_slots = max(0, SIM_TARGET_MAX_POSITIONS - current_count)
    replacement_buys = new_buys[free_slots:]
    used_replacements: set[str] = set()
    for row in replacement_buys:
        replacement = row.get("replacement_code") or ""
        if replacement not in position_codes:
            raise ValueError(f"{row['code']}: target-range buy requires a current replacement_code")
        if replacement in used_replacements:
            raise ValueError(f"{row['code']}: replacement_code {replacement} is already used")
        old_signal = signal_by_code.get(replacement) or {}
        if old_signal.get("action") not in {"reduce", "sell", "clear"}:
            raise ValueError(
                f"{row['code']}: replacement {replacement} must reduce, sell or clear this cycle"
            )
        if row.get("replacement_edge") != "strong":
            raise ValueError(f"{row['code']}: replacement_edge must be strong")
        if not row.get("replacement_reason"):
            raise ValueError(f"{row['code']}: replacement_reason is required")
        if replacement not in weakest_codes:
            raise ValueError(f"{row['code']}: replacement must appear in weakest_holdings")
        used_replacements.add(replacement)
    def is_full_exit(row: dict[str, Any]) -> bool:
        if row["action"] == "clear":
            return True
        if row["action"] != "sell":
            return False
        try:
            sell_pct = float(row.get("sell_pct", 1.0))
        except (TypeError, ValueError):
            sell_pct = 1.0
        if sell_pct > 1:
            sell_pct /= 100
        position = position_by_code.get(row["code"], {}).get("position") or {}
        try:
            planned_volume = int(row.get("volume") or 0)
            position_volume = int(position.get("volume") or 0)
        except (TypeError, ValueError):
            planned_volume, position_volume = 0, 0
        return sell_pct >= 1 or bool(position_volume and planned_volume >= position_volume)

    full_exits = {
        row["code"] for row in signals
        if row["code"] in position_codes and is_full_exit(row)
    }
    projected_count = current_count + len(new_buys) - len(full_exits)
    if projected_count > SIM_HARD_MAX_POSITIONS and (
        current_count <= SIM_HARD_MAX_POSITIONS or bool(new_buys)
    ):
        raise ValueError(
            f"simulated projected position count {projected_count} exceeds hard max "
            f"{SIM_HARD_MAX_POSITIONS}"
        )
    return {
        "schema": "simulated_trading_decision.v1",
        "reviewed_codes": reviewed_codes,
        "market_view": _market_view(
            payload, "模拟盘本轮按事实与风控执行", context.get("market_regime"),
        ),
        "portfolio_review": review,
        "signals": signals,
        "report": _report(payload),
    }


def validate_live_decision(
    payload: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    _, positions, candidates = _account_scope(context)
    reviewed_codes = _reviewed_codes(payload, context)
    allowed_codes = {
        str(row.get("code") or "").zfill(6)
        for row in positions + candidates
        if isinstance(row, dict)
    }
    decisions = _normalize_rows(_root_items(payload, "decisions"), allowed_codes, LIVE_ACTIONS)
    _require_complete_decision_rows(decisions, context, "decisions")
    position_codes = {
        str(row.get("code") or "").zfill(6)
        for row in positions
        if isinstance(row, dict)
    }
    for row in decisions:
        _validate_new_entry_gate(row, candidates)
        if row["action"] == "sell" and row["code"] not in position_codes:
            raise ValueError(f"{row['code']}: live sell requires a live shadow position")
        if row["action"] == "buy":
            try:
                target_amount = float(row.get("target_amount") or 0)
                volume = int(row.get("volume") or 0)
            except (TypeError, ValueError):
                target_amount, volume = 0, 0
            if target_amount <= 0 and volume <= 0:
                raise ValueError(f"{row['code']}: live buy requires target_amount or volume")
    decided_position_codes = {row["code"] for row in decisions}
    missing_positions = sorted(position_codes - decided_position_codes)
    if missing_positions:
        raise ValueError(
            "live decision omitted current positions: " + ",".join(missing_positions)
        )
    return {
        "schema": "live_trading_decision.v1",
        "reviewed_codes": reviewed_codes,
        "market_view": _market_view(
            payload, "实盘本轮按事实与风控执行", context.get("market_regime"),
        ),
        "decisions": decisions,
        "report": _report(payload),
    }


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.0f}元"
    except (TypeError, ValueError):
        return "-"


def _result_line(row: dict[str, Any], live: bool = False) -> str:
    action = ACTION_NAMES.get(str(row.get("action")), str(row.get("action")))
    code, name = row.get("code", ""), row.get("name", "")
    if live and row.get("created_intent"):
        intent = row.get("intent") or {}
        return (
            f"- 🟠 实盘建议 {action} `{code} {name}`：{row.get('volume', 0)}股，"
            f"参考价 {row.get('price', 0):.2f}，有效至 {intent.get('expires_at', '-')}，"
            f"编号 `{intent.get('intent_id', '-')}`"
        )
    if row.get("executed"):
        order = row.get("order") or {}
        return (
            f"- ✅ 模拟盘{action} `{code} {name}`：{order.get('volume', row.get('volume', 0))}股 "
            f"@ {float(order.get('price', row.get('price', 0)) or 0):.2f}"
        )
    errors = row.get("errors") if isinstance(row.get("errors"), list) else []
    return f"- ⚠️ `{code} {name}` {action}未执行：{'；'.join(str(x) for x in errors) or row.get('reason') or '风控未通过'}"


def _stock_name_map(context: dict[str, Any], decision: dict[str, Any]) -> dict[str, str]:
    """Build the canonical code-to-name map used by user-facing reports."""
    rows: list[Any] = []
    for key in ("positions", "candidates"):
        value = context.get(key)
        if isinstance(value, list):
            rows.extend(value)
    for key in ("signals", "decisions"):
        value = decision.get(key)
        if isinstance(value, list):
            rows.extend(value)

    names: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_code = str(row.get("code") or "").strip()
        name = str(row.get("name") or "").strip()
        if not raw_code or not name:
            continue
        code = raw_code.zfill(6)
        if len(code) == 6 and code.isdigit() and code != name:
            names.setdefault(code, name)
    return names


def _render_stock_references(value: Any, names: dict[str, str]) -> str:
    """Turn bare known stock codes in prose into readable name-and-code labels."""
    text = str(value or "")
    if not text or not names:
        return text
    pattern = re.compile(
        r"(?<!\d)(" + "|".join(re.escape(code) for code in sorted(names)) + r")(?!\d)"
    )

    def replace(match: re.Match[str]) -> str:
        code = match.group(1)
        name = names[code]
        before = match.string[max(0, match.start() - len(name) - 1):match.start()]
        if before.endswith(f"{name}（") or before.endswith(f"{name}("):
            return code
        return f"{name}（{code}）"

    return pattern.sub(replace, text)


def render_report(
    context: dict[str, Any], mode: str, decision: dict[str, Any], result: dict[str, Any],
) -> str:
    stage = str(context.get("stage") or "")
    display_stage = f"{stage[:2]}:{stage[2:4]}" if len(stage) >= 4 else stage
    label = "模拟盘" if mode == "simulated" else "实盘"
    icon = "📈" if mode == "simulated" else "🛡️"
    market_view = decision.get("market_view") if isinstance(decision.get("market_view"), dict) else {}
    stock_names = _stock_name_map(context, decision)
    lines = [
        f"{icon} **{display_stage} {label}半小时操盘**",
        "",
        f"**判断**：{_render_stock_references(market_view.get('summary') or '本轮决策不可用', stock_names)}",
    ]
    account = result.get("account") if isinstance(result.get("account"), dict) else {}
    if not account:
        account = context.get("account") if isinstance(context.get("account"), dict) else {}
    lines.extend([
        "",
        f"**账户**：总资产 {_money(account.get('total_equity'))}，"
        f"现金 {_money(account.get('available_cash'))}，持仓 {account.get('position_count', 0)}只。",
    ])
    rows = result.get("results") if isinstance(result.get("results"), list) else []
    material = [
        row for row in rows
        if row.get("executed") or row.get("created_intent") or row.get("errors")
    ]
    lines.extend(["", "**动作**"])
    if material:
        lines.extend(_result_line(row, live=mode == "live") for row in material)
    else:
        lines.append("- 本轮无成交。" if mode == "simulated" else "- 本轮无新实盘建议单。")
    if result.get("error"):
        lines.append(f"- ⚠️ 执行层异常：{result['error']}")
    report = decision.get("report") if isinstance(decision.get("report"), dict) else {}
    focus = report.get("focus") if isinstance(report.get("focus"), list) else []
    if focus:
        lines.extend(
            ["", "**关注**"]
            + [f"- {_render_stock_references(item, stock_names)}" for item in focus]
        )
    if report.get("risk"):
        lines.extend([
            "",
            f"**风险**：{_render_stock_references(report['risk'], stock_names)}",
        ])
    boundary = "自动执行模拟成交" if mode == "simulated" else "仅生成供人工核对的建议单"
    lines.extend([
        "",
        f"数据截至 {context.get('as_of', '-')}；事实已刷新并写入数据库，资金流为可选因子。"
        f"本任务{boundary}。",
    ])
    return "\n".join(lines)[:3800]


def _failed_decision(mode: str, error: str) -> dict[str, Any]:
    action_key = "signals" if mode == "simulated" else "decisions"
    return {
        "schema": f"{mode}_trading_decision.v1",
        "status": "failed",
        "reviewed_codes": [],
        "error": error,
        "market_view": {"regime": "neutral", "summary": "AI 决策不可用，本轮不执行"},
        action_key: [],
        "report": {"focus": [], "risk": "决策服务或格式校验失败"},
    }


def _load_decision(path: str, mode: str, context: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = extract_json(Path(path).read_text(encoding="utf-8"))
        validator = validate_simulated_decision if mode == "simulated" else validate_live_decision
        decision = validator(payload, context)
        decision["status"] = "ok"
        return decision
    except Exception as exc:
        return _failed_decision(mode, str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute one account-isolated AI decision")
    parser.add_argument("--mode", required=True, choices=("simulated", "live"))
    parser.add_argument("--stage", required=True)
    parser.add_argument("--expected-as-of", required=True)
    parser.add_argument("--response", required=True)
    parser.add_argument("--decision-out", required=True)
    parser.add_argument("--result-out", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    context = build_execution_context(args.expected_as_of, args.mode, args.stage)
    decision = _load_decision(args.response, args.mode, context)
    decision["stage"] = context.get("stage")
    decision["as_of"] = context.get("as_of")
    Path(args.decision_out).write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    label = "模拟盘" if args.mode == "simulated" else "实盘"
    if decision.get("status") != "ok":
        result = {
            "schema": f"{args.mode}_trading_execution.v1",
            "dry_run": args.dry_run,
            "results": [],
            "account": context.get("account") or {},
            "error": f"{label}决策失败: {decision.get('error')}",
        }
    elif not args.dry_run and not ensure_actionable_trading_time(task=f"{label}AI决策执行"):
        result = {
            "schema": f"{args.mode}_trading_execution.v1",
            "dry_run": False,
            "skipped": True,
            "results": [],
            "account": context.get("account") or {},
            "error": f"{label}决策完成时已不在A股可操作交易时段，本轮未执行",
        }
    elif args.mode == "simulated":
        try:
            result = execute_simulated(
                {"signals": decision["signals"]}, dry_run=args.dry_run,
            )
        except Exception as exc:
            result = {
                "schema": "trade_signal_execution.v1",
                "dry_run": args.dry_run,
                "results": [],
                "account": context.get("account") or {},
                "error": f"模拟盘执行失败: {exc}",
            }
    else:
        try:
            result = execute_live(
                {"decisions": decision["decisions"]}, dry_run=args.dry_run,
            )
        except Exception as exc:
            result = {
                "schema": "live_trade_execution.v1",
                "dry_run": args.dry_run,
                "results": [],
                "account": context.get("account") or {},
                "error": f"实盘建议单执行失败: {exc}",
            }
    output = {
        "schema": f"{args.mode}_trading_cycle_execution.v1",
        "mode": args.mode,
        "stage": context.get("stage"),
        "dry_run": args.dry_run,
        "execution": result,
    }
    Path(args.result_out).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(render_report(context, args.mode, decision, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
