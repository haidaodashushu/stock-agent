"""Durable, idempotent boundary for model-initiated stock decisions.

The model never writes arbitrary database rows.  A task-specific MCP submit
tool validates a complete decision first, then claims one immutable snapshot
here before invoking the deterministic executor.  A claimed snapshot is never
silently re-executed after an ambiguous failure.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from data.store.sqlite_store import StockStore

SubmissionState = Literal["claimed", "ready", "processing", "failed"]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def submission_key(task: str, mode: str, as_of: str) -> str:
    """Build a stable, compact key for one task snapshot."""
    canonical = f"{task.strip()}|{mode.strip()}|{as_of.strip()}"
    suffix = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"{task.strip()}:{mode.strip() or '-'}:{suffix}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


@dataclass(frozen=True)
class ClaimResult:
    state: SubmissionState
    submission_key: str
    existing: dict[str, Any] | None = None


def _row_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["decision"] = _decode(item.get("decision"))
    item["result"] = _decode(item.get("result"))
    return item


def claim_submission(
    *,
    store: StockStore,
    task: str,
    mode: str,
    as_of: str,
    stage: str,
    provider: str,
    model: str,
    decision: dict[str, Any],
) -> ClaimResult:
    """Atomically claim a validated decision, or report its prior state."""
    key = submission_key(task, mode, as_of)
    conn = store._get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM agent_decision_submissions WHERE submission_key=?",
            (key,),
        ).fetchone()
        if row:
            conn.commit()
            existing = _row_dict(row)
            status = str(existing.get("status") or "failed")
            state: SubmissionState = status if status in {"ready", "processing", "failed"} else "failed"  # type: ignore[assignment]
            return ClaimResult(state=state, submission_key=key, existing=existing)
        conn.execute(
            """INSERT INTO agent_decision_submissions
               (submission_key, task, mode, as_of, stage, provider, model,
                status, decision, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', ?, ?)""",
            (key, task, mode, as_of, stage, provider, model, _json(decision), _now()),
        )
        conn.commit()
        return ClaimResult(state="claimed", submission_key=key)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_submission(
    *, store: StockStore, key: str, result: dict[str, Any], report: str,
) -> None:
    conn = store._get_conn()
    try:
        changed = conn.execute(
            """UPDATE agent_decision_submissions
                  SET status='ready', result=?, report=?, error='', completed_at=?
                WHERE submission_key=? AND status='processing'""",
            (_json(result), report, _now(), key),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f"submission {key} is not in processing state")
        conn.commit()
    finally:
        conn.close()


def fail_submission(*, store: StockStore, key: str, error: str) -> None:
    conn = store._get_conn()
    try:
        conn.execute(
            """UPDATE agent_decision_submissions
                  SET status='failed', error=?, completed_at=?
                WHERE submission_key=? AND status='processing'""",
            (str(error)[:1000], _now(), key),
        )
        conn.commit()
    finally:
        conn.close()


def adopt_completed_submission(
    *, store: StockStore, key: str, result: dict[str, Any], report: str,
) -> None:
    """Reconcile a claimed row with an already-committed legacy task result.

    This is deliberately narrower than retry: callers must first prove the
    deterministic downstream table already contains a successful result for
    the exact immutable ``as_of``.
    """
    conn = store._get_conn()
    try:
        changed = conn.execute(
            """UPDATE agent_decision_submissions
                  SET status='ready', result=?, report=?, error='', completed_at=?
                WHERE submission_key=? AND status IN ('processing','failed')""",
            (_json(result), report, _now(), key),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f"submission {key} cannot adopt a completed result")
        conn.commit()
    finally:
        conn.close()


def get_submission(
    *, store: StockStore, task: str, mode: str, as_of: str,
) -> dict[str, Any] | None:
    key = submission_key(task, mode, as_of)
    conn = store._get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM agent_decision_submissions WHERE submission_key=?", (key,)
        ).fetchone()
        return _row_dict(row) if row else None
    finally:
        conn.close()


def enqueue_message(
    *,
    store: StockStore,
    submission_key: str,
    message_type: Literal["text", "interactive"],
    content: str,
    channel: str = "feishu",
    suffix: str = "report",
) -> None:
    conn = store._get_conn()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO agent_message_outbox
               (dedupe_key, submission_key, channel, message_type, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"{submission_key}:{channel}:{suffix}", submission_key, channel, message_type, content, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def agent_runtime_health(*, store: StockStore) -> dict[str, Any]:
    """Compact provider/submission/outbox health for the Web console."""
    conn = store._get_conn()
    try:
        rows = conn.execute(
            """SELECT s.* FROM agent_decision_submissions s
                JOIN (
                    SELECT task,mode,MAX(created_at) AS created_at
                      FROM agent_decision_submissions GROUP BY task,mode
                ) latest
                  ON latest.task=s.task AND latest.mode=s.mode
                 AND latest.created_at=s.created_at
                ORDER BY s.task,s.mode"""
        ).fetchall()
        outbox_failed = conn.execute(
            "SELECT COUNT(*) FROM agent_message_outbox WHERE status='failed'"
        ).fetchone()[0]
        outbox_pending = conn.execute(
            "SELECT COUNT(*) FROM agent_message_outbox WHERE status IN ('pending','sending')"
        ).fetchone()[0]
    finally:
        conn.close()
    latest = [
        {
            "task": row["task"], "mode": row["mode"], "status": row["status"],
            "provider": row["provider"], "model": row["model"],
            "as_of": row["as_of"], "created_at": row["created_at"],
            "error": row["error"],
        }
        for row in rows
    ]
    failures = [row for row in latest if row["status"] == "failed"]
    if failures or outbox_failed:
        message = f"Agent异常 {len(failures)} 项，飞书待重试 {outbox_failed} 条"
        status = "failed"
    elif latest:
        message = f"Codex Agent 正常，最近完成 {len(latest)} 类任务"
        status = "healthy"
    else:
        message = "Codex Agent 已启用，等待首轮决策"
        status = "pending"
    return {
        "status": status, "healthy": status != "failed", "message": message,
        "latest": latest, "outbox_pending": outbox_pending, "outbox_failed": outbox_failed,
    }
