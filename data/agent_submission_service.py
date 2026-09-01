"""Task-specific validated write tools exposed to the stock decision agent."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from data.agent_submissions import (
    adopt_completed_submission,
    claim_submission,
    complete_submission,
    enqueue_message,
    fail_submission,
)
from data.candidate_promotion import apply_promotion_decision, validate_promotion_decision
from data.stock_selection_repository import get_staged_rows
from data.store.sqlite_store import StockStore
from data.trading_decision_repository import build_execution_context
from scripts.execute_stock_selection import validate_selection
from scripts.execute_trading_cycle import validate_live_decision, validate_simulated_decision

ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _existing_response(claim: Any) -> dict[str, Any]:
    existing = claim.existing or {}
    if claim.state == "ready":
        return {
            "status": "already_submitted",
            "submission_key": claim.submission_key,
            "result": existing.get("result") or {},
            "report": existing.get("report") or "",
        }
    return {
        "status": "blocked",
        "submission_key": claim.submission_key,
        "reason": (
            "This snapshot has an unfinished submission and will not be replayed automatically."
            if claim.state == "processing"
            else "This snapshot previously failed after being claimed; operator review is required."
        ),
        "error": existing.get("error") or "",
    }


def submit_trading_decision(
    *,
    mode: str,
    stage: str,
    as_of: str,
    decision: dict[str, Any],
    run_dir: Path,
    provider: str,
    model: str,
    dry_run: bool = False,
    store: StockStore | None = None,
) -> dict[str, Any]:
    """Validate, claim and execute one complete account-scoped decision."""
    if mode not in {"simulated", "live"}:
        return {"status": "rejected", "reason": f"invalid trading mode: {mode}"}
    try:
        context = build_execution_context(as_of, mode, stage)
        validator = validate_simulated_decision if mode == "simulated" else validate_live_decision
        validated = validator(decision, context)
        validated.update({"status": "ok", "stage": context["stage"], "as_of": context["as_of"]})
    except Exception as exc:
        return {"status": "rejected", "can_retry": True, "reason": str(exc)}

    store = store or StockStore()
    claim = claim_submission(
        store=store, task="trading", mode=mode, as_of=as_of, stage=stage,
        provider=provider, model=model, decision=validated,
    )
    if claim.state != "claimed":
        return _existing_response(claim)

    response_path = run_dir / "submitted-response.json"
    decision_path = run_dir / "decision.json"
    result_path = run_dir / "execution.json"
    _write_json(response_path, decision)
    command = [
        sys.executable, str(ROOT / "scripts" / "execute_trading_cycle.py"),
        "--mode", mode, "--stage", stage, "--expected-as-of", as_of,
        "--response", str(response_path), "--decision-out", str(decision_path),
        "--result-out", str(result_path),
    ]
    if dry_run:
        command.append("--dry-run")
    try:
        completed = subprocess.run(
            command, cwd=ROOT, text=True, capture_output=True, timeout=300, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or f"executor exited {completed.returncode}")
        executed_decision = json.loads(decision_path.read_text(encoding="utf-8"))
        if executed_decision.get("status") != "ok":
            raise RuntimeError(str(executed_decision.get("error") or "executor rejected decision"))
        result = json.loads(result_path.read_text(encoding="utf-8"))
        report = completed.stdout.strip()
        if mode == "live" and not dry_run:
            fortune_path = run_dir / "fortune.txt"
            fortune = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts/render_live_trade_fortune.py"),
                    "--result", str(result_path), "--as-of", as_of,
                    "--output", str(fortune_path),
                ],
                cwd=ROOT, text=True, capture_output=True, timeout=180, check=False,
            )
            if fortune.returncode == 0 and fortune_path.exists():
                report = report.rstrip() + "\n" + fortune_path.read_text(encoding="utf-8").strip()
        complete_submission(store=store, key=claim.submission_key, result=result, report=report)
        enqueue_message(
            store=store, submission_key=claim.submission_key,
            message_type="text", content=report,
        )
        return {
            "status": "submitted", "submission_key": claim.submission_key,
            "execution": result, "report": report,
        }
    except Exception as exc:
        fail_submission(store=store, key=claim.submission_key, error=str(exc))
        return {
            "status": "failed", "submission_key": claim.submission_key,
            "reason": str(exc), "operator_review_required": True,
        }


def submit_stock_selection(
    *,
    as_of: str,
    decision: dict[str, Any],
    run_dir: Path,
    provider: str,
    model: str,
    store: StockStore | None = None,
) -> dict[str, Any]:
    store = store or StockStore()
    try:
        state, staged = get_staged_rows(as_of, store=store)
        if str(state.get("status") or "") != "ready":
            return {
                "status": "rejected",
                "can_retry": False,
                "reason": f"AI selection snapshot is not ready: {state.get('status')}",
            }
        validated = validate_selection(
            decision, expected_as_of=as_of, allowed_codes=set(staged),
            routes_by_code={
                code: str(row.get("entry_route") or "unclassified")
                for code, row in staged.items()
            },
        )
    except Exception as exc:
        return {"status": "rejected", "can_retry": True, "reason": str(exc)}

    claim = claim_submission(
        store=store, task="selection", mode="", as_of=as_of,
        stage=str(state.get("run_label") or ""), provider=provider, model=model,
        decision=validated,
    )
    if claim.state != "claimed":
        return _existing_response(claim)

    response_path = run_dir / "submitted-response.json"
    decision_path = run_dir / "decision.json"
    report_path = run_dir / "report.json"
    _write_json(response_path, decision)
    command = [
        sys.executable, str(ROOT / "scripts" / "execute_stock_selection.py"),
        "--expected-as-of", as_of, "--response", str(response_path),
        "--decision-out", str(decision_path), "--report-out", str(report_path),
    ]
    try:
        completed = subprocess.run(
            command, cwd=ROOT, text=True, capture_output=True, timeout=300, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or f"executor exited {completed.returncode}")
        result = json.loads(report_path.read_text(encoding="utf-8"))
        summary = str(result.get("summary") or completed.stdout.strip())
        complete_submission(store=store, key=claim.submission_key, result=result, report=summary)
        enqueue_message(
            store=store, submission_key=claim.submission_key,
            message_type="interactive", content=json.dumps(result, ensure_ascii=False),
        )
        return {
            "status": "submitted", "submission_key": claim.submission_key,
            "selected_count": len(validated["selections"]), "report": summary,
        }
    except Exception as exc:
        fail_submission(store=store, key=claim.submission_key, error=str(exc))
        return {
            "status": "failed", "submission_key": claim.submission_key,
            "reason": str(exc), "operator_review_required": True,
        }


def submit_candidate_promotion(
    *,
    as_of: str,
    decision: dict[str, Any],
    provider: str,
    model: str,
    store: StockStore | None = None,
) -> dict[str, Any]:
    store = store or StockStore()
    try:
        validated = validate_promotion_decision(decision, as_of, store=store)
    except Exception as exc:
        return {"status": "rejected", "can_retry": True, "reason": str(exc)}
    claim = claim_submission(
        store=store, task="promotion", mode="", as_of=as_of, stage="intraday",
        provider=provider, model=model,
        decision={"as_of": as_of, "decisions": validated["decisions"]},
    )
    # During cutover, the immutable source may already have been completed by
    # the old runtime before the new submission ledger existed.  Adopt that
    # durable success instead of replaying inserts or treating it as failure.
    conn = store._get_conn()
    try:
        legacy = conn.execute(
            """SELECT trade_date,candidate_count,promoted_count,status
                 FROM candidate_promotion_runs
                WHERE as_of=?""",
            (as_of,),
        ).fetchone()
        legacy_decisions = conn.execute(
            """SELECT code,decision FROM candidate_promotion_decisions
                WHERE as_of=?""",
            (as_of,),
        ).fetchall()
        required_codes = set(validated["snapshot"]["required_evidence_codes"])
        decided_codes = {str(row["code"]) for row in legacy_decisions}
        legacy_complete = bool(legacy_decisions) and required_codes.issubset(decided_codes)
        promoted_rows = conn.execute(
            """SELECT code,name,entry_route,promoted_at
                 FROM intraday_candidate_promotions
                WHERE trade_date=? AND code IN (
                    SELECT code FROM candidate_promotion_decisions
                     WHERE as_of=? AND decision='promote'
                )""",
            (legacy["trade_date"], as_of),
        ).fetchall() if legacy and legacy_complete else []
    finally:
        conn.close()
    if legacy and legacy_complete:
        evaluated_count = len(legacy_decisions)
        promoted_count = sum(1 for row in legacy_decisions if row["decision"] == "promote")
        result = {
            "status": "ready", "as_of": as_of, "trade_date": legacy["trade_date"],
            "evaluated_count": evaluated_count,
            "promoted_count": promoted_count,
            "promoted": [dict(row) for row in promoted_rows],
            "adopted_legacy_result": True,
        }
        report = (
            f"盘中晋升已完成：评估 {result['evaluated_count']} 只，"
            f"晋升 {result['promoted_count']} 只（已收编切换前结果）。"
        )
        if claim.state in {"claimed", "failed", "processing"}:
            adopt_completed_submission(
                store=store, key=claim.submission_key, result=result, report=report,
            )
        conn = store._get_conn()
        try:
            conn.execute(
                """UPDATE candidate_promotion_runs
                      SET status='ready', candidate_count=?, promoted_count=?, error=''
                    WHERE as_of=?""",
                (evaluated_count, promoted_count, as_of),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "status": "already_submitted", "submission_key": claim.submission_key,
            "result": result, "report": report,
        }
    if claim.state != "claimed":
        return _existing_response(claim)
    try:
        result = apply_promotion_decision(decision, as_of, store=store)
        report = (
            f"盘中晋升完成：评估 {result.get('evaluated_count', 0)} 只，"
            f"晋升 {result.get('promoted_count', 0)} 只。"
        )
        complete_submission(store=store, key=claim.submission_key, result=result, report=report)
        return {
            "status": "submitted", "submission_key": claim.submission_key,
            "result": result, "report": report,
        }
    except Exception as exc:
        fail_submission(store=store, key=claim.submission_key, error=str(exc))
        return {
            "status": "failed", "submission_key": claim.submission_key,
            "reason": str(exc), "operator_review_required": True,
        }
