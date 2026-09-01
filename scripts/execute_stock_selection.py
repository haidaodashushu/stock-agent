#!/usr/bin/env python3
"""Validate one agent final-selection response and atomically publish it."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.stock_selection_repository import get_staged_rows  # noqa: E402
from data.candidate_board import refresh_candidate_board  # noqa: E402
from data.store.sqlite_store import StockStore  # noqa: E402
from scripts.daily_screen import _build_screening_report, _save_screen_results  # noqa: E402
from scripts.execute_trading_cycle import extract_json  # noqa: E402

CONFIDENCES = {"strong", "medium", "weak"}
REGIMES = {"strong", "neutral", "weak"}
ENTRY_ROUTES = {"early_start", "strong_continuation"}
MAX_SELECTIONS = 10


def _text(value: Any) -> str:
    return str(value or "").strip()


def validate_selection(
    payload: dict[str, Any], *, expected_as_of: str, allowed_codes: set[str],
    routes_by_code: dict[str, str] | None = None,
) -> dict[str, Any]:
    if _text(payload.get("as_of")) != expected_as_of:
        raise ValueError("AI response as_of does not match the staged candidate snapshot")
    raw_rows = payload.get("selections")
    if not isinstance(raw_rows, list):
        raise ValueError("selections must be an array")
    if len(raw_rows) > MAX_SELECTIONS:
        raise ValueError(f"AI selected {len(raw_rows)} stocks; maximum is {MAX_SELECTIONS}")

    raw_reviewed = payload.get("reviewed_codes")
    if not isinstance(raw_reviewed, list):
        raise ValueError("reviewed_codes must be an array containing the complete candidate pool")
    reviewed_codes = [
        _text(code).zfill(6) for code in raw_reviewed if _text(code)
    ]
    if len(reviewed_codes) != len(set(reviewed_codes)):
        raise ValueError("reviewed_codes contains duplicates")
    missing = sorted(allowed_codes - set(reviewed_codes))
    outside = sorted(set(reviewed_codes) - allowed_codes)
    if missing or outside:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if outside:
            details.append("outside=" + ",".join(outside))
        raise ValueError("AI did not review the complete candidate pool: " + "; ".join(details))

    selections = []
    seen: set[str] = set()
    for rank, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"selection #{rank} must be an object")
        code = _text(raw.get("code")).zfill(6)
        if code not in allowed_codes:
            raise ValueError(f"{code}: code is outside the staged candidate pool")
        if code in seen:
            raise ValueError(f"{code}: duplicated in AI final selection")
        seen.add(code)
        confidence = _text(raw.get("confidence") or "weak").lower()
        if confidence not in CONFIDENCES:
            raise ValueError(f"{code}: invalid confidence {confidence}")
        reason = _text(raw.get("reason"))
        risk = _text(raw.get("risk"))
        if not reason:
            raise ValueError(f"{code}: selection reason is required")
        if not risk:
            raise ValueError(f"{code}: selection risk is required")
        route = _text(raw.get("entry_route") or (routes_by_code or {}).get(code))
        if route not in ENTRY_ROUTES:
            raise ValueError(f"{code}: final candidate requires a valid entry_route")
        selections.append({
            "rank": rank,
            "code": code,
            "confidence": confidence,
            "entry_route": route,
            "reason": reason,
            "risk": risk,
        })

    market_raw = payload.get("market_view") if isinstance(payload.get("market_view"), dict) else {}
    regime = _text(market_raw.get("regime") or "neutral").lower()
    if regime not in REGIMES:
        raise ValueError(f"invalid market regime: {regime}")
    report_raw = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    focus_raw = report_raw.get("focus") if isinstance(report_raw.get("focus"), list) else []
    return {
        "schema": "ai_stock_selection.v1",
        "status": "ok",
        "as_of": expected_as_of,
        "reviewed_codes": reviewed_codes,
        "market_view": {
            "regime": regime,
            "summary": _text(market_raw.get("summary")),
        },
        "selections": selections,
        "report": {
            "focus": [_text(item) for item in focus_raw if _text(item)],
            "risk": _text(report_raw.get("risk")),
        },
    }


def _failure_report(state: dict[str, Any], error: str) -> dict[str, Any]:
    label = _text(state.get("run_label")) or "AI最终选股"
    run_date = _text(state.get("run_date")) or datetime.now().date().isoformat()
    return {
        "profile": "screening_report",
        "title": f"{label} {run_date}",
        "tone": "danger",
        "summary": "量化候选池已生成，但 AI 最终选股失败；原 screen_records 未被替换。",
        "run": {
            "label": label,
            "target": state.get("target", ""),
            "run_date": run_date,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": 0,
            "buy_count": 0,
            "watch_count": 0,
            "tech_count": 0,
            "strong_logic_count": 0,
            "mainline_text": "AI 选择未完成",
            "selection_method": "ai_failed",
        },
        "candidates": [],
        "warnings": [f"AI 最终选股失败：{error}"],
        "source": "TechnicalScoringSelector + stock-selection MCP + agent final selection",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and publish one AI final stock selection")
    parser.add_argument("--expected-as-of", required=True)
    parser.add_argument("--response", required=True)
    parser.add_argument("--decision-out", required=True)
    parser.add_argument("--report-out", required=True)
    args = parser.parse_args()

    store = StockStore()
    state: dict[str, Any] = {}
    try:
        state, staged = get_staged_rows(args.expected_as_of, store=store)
        payload = extract_json(Path(args.response).read_text(encoding="utf-8"))
        decision = validate_selection(
            payload,
            expected_as_of=args.expected_as_of,
            allowed_codes=set(staged),
            routes_by_code={
                code: _text(row.get("entry_route") or "unclassified")
                for code, row in staged.items()
            },
        )
        selected_rows = []
        ai_by_code = {}
        for selection in decision["selections"]:
            code = selection["code"]
            row = dict(staged[code])
            row["name"] = row.get("name") or code
            # Final daily selection is candidate qualification.  It does not
            # execute a trade; the half-hour account decision remains separate.
            row["entry_route"] = selection["entry_route"]
            row["setup_stage"] = "actionable"
            row["buy_eligible"] = True
            row["signal_type"] = "buy"
            extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
            extra = dict(extra)
            extra["ai_selection"] = selection
            row["extra"] = extra
            selected_rows.append(row)
            ai_by_code[code] = selection

        saved = _save_screen_results(
            store,
            selected_rows,
            run_date=str(state["run_date"]),
            run_time=str(state["run_time"]),
            run_label=str(state["run_label"]),
            target=str(state["target"]),
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ai_selections=ai_by_code,
            expected_as_of=args.expected_as_of,
        )
        if saved != len(selected_rows):
            raise RuntimeError(f"published {saved} of {len(selected_rows)} AI selections")
        # Candidate membership is a consequence of the final selection, so the
        # same transaction pipeline materializes it immediately.  Night runs
        # can build the next trading day's board before that date arrives.
        refresh_candidate_board(
            store,
            now=datetime.now(),
            trade_date=str(state["run_date"]),
        )

        report = _build_screening_report(
            selected_rows,
            str(state["run_date"]),
            str(state["run_label"]),
            str(state["target"]),
        )
        report["run"]["selection_method"] = "ai"
        if not selected_rows:
            report["summary"] = "AI 已读取完整候选池证据，本轮没有保留预选股。"
        report["ai"] = {
            "as_of": args.expected_as_of,
            "market_view": decision["market_view"],
            "focus": decision["report"]["focus"],
            "risk": decision["report"]["risk"],
        }
        market_summary = decision["market_view"].get("summary")
        if market_summary:
            report["summary"] = f"{market_summary}；{report['summary']}"
        if decision["report"].get("risk"):
            report.setdefault("data_notes", []).append(
                "AI候选池风险：" + decision["report"]["risk"]
            )
        Path(args.decision_out).write_text(
            json.dumps(decision, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        Path(args.report_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"AI 最终选股完成：候选池 {state['candidate_count']} 只，最终保留 {saved} 只。")
        return 0
    except Exception as exc:
        error = str(exc)
        failed = {
            "schema": "ai_stock_selection.v1",
            "status": "failed",
            "as_of": args.expected_as_of,
            "error": error,
            "selections": [],
        }
        Path(args.decision_out).write_text(
            json.dumps(failed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        Path(args.report_out).write_text(
            json.dumps(_failure_report(state, error), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"AI 最终选股失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
