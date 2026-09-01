#!/usr/bin/env python3
"""Run one stock decision task through a provider-neutral agent boundary."""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.agent_runtime import CodexCliProvider  # noqa: E402
from data.agent_submissions import get_submission  # noqa: E402
from data.candidate_promotion import get_promotion_overview  # noqa: E402
from data.candidate_promotion import record_promotion_failure  # noqa: E402
from data.stock_selection_repository import get_selection_overview  # noqa: E402
from data.store.sqlite_store import StockStore  # noqa: E402
from data.trading_decision_repository import get_trading_overview  # noqa: E402

TASKS = {"selection", "promotion", "trading-simulated", "trading-live"}
PROMPTS = {
    "selection": ROOT / "config" / "agent_stock_selection_prompt.md",
    "promotion": ROOT / "config" / "agent_candidate_promotion_prompt.md",
    "trading-simulated": ROOT / "config" / "agent_simulated_trading_prompt.md",
    "trading-live": ROOT / "config" / "agent_live_trading_prompt.md",
}


def _safe(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", value)[:80]


def _task_snapshot(task: str) -> tuple[str, str, dict[str, Any]]:
    if task == "selection":
        overview = get_selection_overview()
        return "selection", "", overview
    if task == "promotion":
        overview = get_promotion_overview()
        return "promotion", "", overview
    mode = "simulated" if task == "trading-simulated" else "live"
    overview = get_trading_overview(mode)
    return "trading", mode, overview


def _submission_instruction(task: str) -> str:
    tool = {
        "selection": "submit_stock_selection",
        "promotion": "submit_candidate_promotion",
        "trading-simulated": "submit_trading_decision",
        "trading-live": "submit_trading_decision",
    }[task]
    return f"""

## 本运行时的提交约束

上面的“只返回 JSON”描述的是你要形成的完整决策对象，不是最终聊天文本。
你必须先按要求调用全部只读证据工具，然后调用 `{tool}`，将 overview.as_of 原样作为
`as_of`，把完整 JSON 对象作为 `decision`。只有工具返回 `submitted` 或
`already_submitted` 才算任务完成。若返回 `rejected` 且 `can_retry=true`，根据原因修正后
重新提交；若返回 `failed` 或 `blocked`，不得尝试绕过或改用命令/文件/数据库写入。
不要读取项目文件、运行 shell、访问网络或使用 MCP 之外的事实。最终回复只简述提交状态；
最终回复本身不会触发选股、晋升、成交或建议单。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one stock task with an agent provider")
    parser.add_argument("--task", required=True, choices=sorted(TASKS))
    parser.add_argument("--stage", default="")
    parser.add_argument("--provider", default="codex-cli")
    parser.add_argument("--model", default="")
    parser.add_argument("--codex-bin", default="")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--retry-delay", type=float, default=15.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-root", default=str(ROOT / "data" / "agent_runs"))
    parser.add_argument("--report-out", default="")
    args = parser.parse_args()
    if args.provider != "codex-cli":
        print(f"unsupported agent provider: {args.provider}", file=sys.stderr)
        return 2
    if args.task.startswith("trading-") and not args.stage:
        print("--stage is required for trading tasks", file=sys.stderr)
        return 2
    if args.max_attempts < 1 or args.retry_delay < 0:
        print("--max-attempts must be >= 1 and --retry-delay must be >= 0", file=sys.stderr)
        return 2

    store = StockStore()
    db_task, mode, overview = _task_snapshot(args.task)
    as_of = str(overview.get("as_of") or "")
    if not as_of:
        print("task overview did not provide as_of", file=sys.stderr)
        return 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.run_root).resolve() / f"{stamp}-{_safe(args.task)}-{_safe(as_of)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    lock_path = Path(args.run_root).resolve() / f".{_safe(args.task)}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"another {args.task} agent run is active", file=sys.stderr)
        return 75

    prompt_parts = []
    if args.task in {"promotion", "trading-simulated", "trading-live"}:
        prompt_parts.append(
            (ROOT / "config" / "agent_stock_entry_policy.md").read_text(encoding="utf-8")
        )
    if args.task in {"trading-simulated", "trading-live"}:
        prompt_parts.append(
            (ROOT / "config" / "agent_trading_policy.md").read_text(encoding="utf-8")
        )
    prompt_parts.append(PROMPTS[args.task].read_text(encoding="utf-8"))
    prompt = "\n\n".join(prompt_parts) + _submission_instruction(args.task)
    # Keep the virtualenv launcher path intact; resolving the symlink would
    # silently switch the MCP process back to the system interpreter.
    python = str(ROOT / ".venv" / "bin" / "python")
    common = [
        "--run-dir", str(run_dir), "--provider", args.provider,
        "--model", args.model or "gpt-5.6-sol",
    ]
    if args.task == "selection":
        mcp_script = ROOT / "scripts" / "stock_selection_mcp.py"
        mcp_args = [str(mcp_script), *common]
    elif args.task == "promotion":
        mcp_script = ROOT / "scripts" / "stock_candidate_promotion_mcp.py"
        mcp_args = [str(mcp_script), *common]
    else:
        mcp_script = ROOT / "scripts" / "stock_trading_mcp.py"
        mcp_args = [
            str(mcp_script), "--mode", mode, "--stage", args.stage, *common,
        ]
        if args.dry_run:
            mcp_args.append("--dry-run")

    (run_dir / "run.json").write_text(json.dumps({
        "task": args.task, "db_task": db_task, "mode": mode, "stage": args.stage,
        "as_of": as_of, "provider": args.provider,
        "model": args.model or "gpt-5.6-sol", "started_at": datetime.now().isoformat(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        provider = CodexCliProvider(
            executable=args.codex_bin or None, model=args.model or None,
            timeout_seconds=args.timeout,
        )
        attempts = []
        submission = None
        outcome = None
        for attempt in range(1, args.max_attempts + 1):
            attempt_dir = run_dir / f"attempt-{attempt}"
            attempt_dir.mkdir(parents=True, exist_ok=False)
            attempt_mcp_args = list(mcp_args)
            attempt_mcp_args[attempt_mcp_args.index("--run-dir") + 1] = str(attempt_dir)
            outcome = provider.run(
                prompt=prompt, workspace=ROOT, mcp_command=python,
                mcp_args=attempt_mcp_args,
                run_dir=attempt_dir,
            )
            submission = get_submission(
                store=store, task=db_task, mode=mode, as_of=as_of,
            )
            attempt_summary = {
                "attempt": attempt,
                "returncode": outcome.returncode,
                "submission_status": submission.get("status") if submission else "missing",
                "submission_key": submission.get("submission_key") if submission else "",
                "final_message": outcome.final_message,
                "completed_at": datetime.now().isoformat(),
            }
            attempts.append(attempt_summary)
            (attempt_dir / "outcome.json").write_text(
                json.dumps(attempt_summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if submission or attempt == args.max_attempts:
                break
            time.sleep(args.retry_delay)
        assert outcome is not None
        success = bool(submission and submission.get("status") == "ready")
        summary = dict(attempts[-1])
        summary["attempts"] = attempts
        (run_dir / "outcome.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        if not success:
            error = (
                str(submission.get("error") or "") if submission
                else outcome.stderr.strip() or outcome.final_message or "agent did not submit a decision"
            )
            try:
                if args.task == "promotion":
                    record_promotion_failure(as_of, error, store=store)
            except Exception as persist_exc:
                summary["failure_persistence_error"] = str(persist_exc)
            print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
            return outcome.returncode or 1
        if args.report_out:
            result = submission.get("result") if isinstance(submission.get("result"), dict) else {}
            Path(args.report_out).write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
        print(str(submission.get("report") or outcome.final_message or "agent submission ready"))
        return 0
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
