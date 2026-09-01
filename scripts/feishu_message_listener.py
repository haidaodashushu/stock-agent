#!/usr/bin/env python3
"""Receive trusted Feishu messages, acknowledge, process, and reply."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.agent_runtime import CodexCliProvider  # noqa: E402
from data.feishu_client import load_feishu_settings  # noqa: E402
from data.live_fill_service import (  # noqa: E402
    LiveFillError,
    looks_like_fill_report,
    process_fill_report,
    render_fill_result,
)
from data.store.sqlite_store import StockStore  # noqa: E402
from config.runtime_paths import configurable_path  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ListenerConfig:
    config_path: Path
    binary: str
    profile: str
    chat_id: str
    allowed_sender_ids: frozenset[str]
    ack_emoji: str
    agent_enabled: bool
    agent_model: str
    agent_timeout_seconds: int
    max_workers: int


def load_listener_config(path: Path) -> ListenerConfig:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    settings = load_feishu_settings(path)
    feishu = parsed.get("feishu") if isinstance(parsed, dict) else {}
    receive = feishu.get("receive") if isinstance(feishu, dict) else {}
    receive = receive if isinstance(receive, dict) else {}
    if not bool(receive.get("enabled", False)):
        raise RuntimeError("Feishu receive is disabled in runtime config")
    if settings["kind"] != "chat":
        raise RuntimeError("Feishu receive currently requires a configured chat target")
    configured_senders = receive.get("allowed_sender_ids") or []
    env_senders = os.environ.get("STOCK_FEISHU_ALLOWED_SENDER_IDS", "")
    sender_ids = {
        str(value).strip()
        for value in [*configured_senders, *env_senders.split(",")]
        if str(value).strip()
    }
    if not sender_ids or any(not value.startswith("ou_") for value in sender_ids):
        raise RuntimeError("Feishu receive requires explicit ou_ allowed_sender_ids")
    agent = parsed.get("agent") if isinstance(parsed, dict) else {}
    agent = agent if isinstance(agent, dict) else {}
    return ListenerConfig(
        config_path=path,
        binary=settings["binary"],
        profile=settings["profile"],
        chat_id=settings["target"],
        allowed_sender_ids=frozenset(sender_ids),
        ack_emoji=str(receive.get("ack_emoji") or "OnIt"),
        agent_enabled=bool(receive.get("agent_enabled", True)),
        agent_model=str(agent.get("model") or "gpt-5.6-sol"),
        agent_timeout_seconds=max(30, int(receive.get("agent_timeout_seconds") or 600)),
        max_workers=max(1, min(4, int(receive.get("max_workers") or 2))),
    )


class LarkInboundClient:
    def __init__(self, config: ListenerConfig) -> None:
        self.config = config

    def _run(self, args: list[str], *, timeout: int = 60) -> str:
        completed = subprocess.run(
            [self.config.binary, "--profile", self.config.profile, *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={
                **os.environ,
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
            },
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "lark-cli failed"
            raise RuntimeError(detail[:1200])
        return completed.stdout

    def react(self, message_id: str) -> None:
        self._run([
            "im", "reactions", "create", "--as", "bot",
            "--params", json.dumps({"message_id": message_id}),
            "--data", json.dumps({
                "reaction_type": {"emoji_type": self.config.ack_emoji},
            }),
        ])

    def reply(self, message_id: str, content: str) -> None:
        self._run([
            "im", "+messages-reply", "--as", "bot",
            "--message-id", message_id,
            "--markdown", content[:12000],
            "--idempotency-key", f"stock-inbound-{message_id}"[:50],
        ])


class CodexMessageAgent:
    def __init__(self, config: ListenerConfig, store: StockStore) -> None:
        self.config = config
        self.store = store

    def _recent_context(self, message_id: str) -> str:
        conn = self.store._get_conn()
        try:
            rows = conn.execute(
                """SELECT content, result FROM feishu_inbound_messages
                   WHERE message_id<>? AND status='succeeded' AND handler='codex'
                   ORDER BY received_at DESC LIMIT 4""",
                (message_id,),
            ).fetchall()
        finally:
            conn.close()
        blocks = []
        for row in reversed(rows):
            blocks.append(
                "用户：" + str(row["content"] or "")[:1200]
                + "\n助手：" + str(row["result"] or "")[:1800]
            )
        return "\n\n".join(blocks)

    def process(self, event: dict[str, Any]) -> str:
        message_id = str(event["message_id"])
        history = self._recent_context(message_id)
        prompt = f"""你正在处理股票项目专用飞书群里、来自唯一授权用户的一条消息。

用户消息：
{str(event.get('content') or '')}

最近的有限对话上下文（可能为空）：
{history or '无'}

请在当前股票项目中完成这条消息的实际意图。问题类请求先检查项目事实再回答；明确要求修改时可以修改并做相称验证。
遵守项目 AGENTS.md 和安全边界。不要调用多代理。不要发送飞书或其他外部消息，也不要创建新的 Codex 任务；
最终回复会由入站服务原样发回当前飞书消息。回复使用简洁中文，必须说明实际完成结果；若缺少会实质改变结果的信息，只说明需要用户补充什么。
"""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_id = "".join(ch for ch in message_id if ch.isalnum() or ch in "_-")[-48:]
        run_dir = ROOT / "data" / "agent_runs" / "feishu-inbound" / f"{stamp}-{safe_id}"
        provider = CodexCliProvider(
            model=self.config.agent_model,
            timeout_seconds=self.config.agent_timeout_seconds,
            sandbox="workspace-write",
        )
        outcome = provider.run(prompt=prompt, workspace=ROOT, run_dir=run_dir)
        if outcome.returncode != 0 or not outcome.final_message.strip():
            detail = outcome.stderr.strip() or outcome.final_message or "Codex 未返回结果"
            raise RuntimeError(detail[:1500])
        return outcome.final_message.strip()


def event_message_time(event: dict[str, Any]) -> datetime:
    raw = str(event.get("create_time") or "").strip()
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=SHANGHAI).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return datetime.now(tz=SHANGHAI).replace(tzinfo=None)


def _claim_event(store: StockStore, event: dict[str, Any], message_at: datetime) -> bool:
    conn = store._get_conn()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO feishu_inbound_messages
               (message_id,event_id,chat_id,sender_id,message_type,content,status,message_at)
               VALUES (?,?,?,?,?,?,'processing',?)""",
            (
                str(event["message_id"]),
                str(event.get("event_id") or ""),
                str(event.get("chat_id") or ""),
                str(event.get("sender_id") or ""),
                str(event.get("message_type") or ""),
                str(event.get("content") or ""),
                message_at.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _update_event(store: StockStore, message_id: str, **fields: str) -> None:
    if not fields:
        return
    allowed = {
        "status", "handler", "ack_status", "result", "error", "processed_at", "replied_at",
    }
    if set(fields) - allowed:
        raise ValueError("invalid inbound event update")
    assignments = ", ".join(f"{key}=?" for key in fields)
    conn = store._get_conn()
    try:
        conn.execute(
            f"UPDATE feishu_inbound_messages SET {assignments} WHERE message_id=?",
            (*fields.values(), message_id),
        )
        conn.commit()
    finally:
        conn.close()


def handle_event(
    event: dict[str, Any],
    *,
    config: ListenerConfig,
    store: StockStore | None = None,
    lark: LarkInboundClient | None = None,
    agent: CodexMessageAgent | None = None,
) -> str:
    """Handle one flattened im.message.receive_v1 event."""
    message_id = str(event.get("message_id") or "")
    if not message_id.startswith("om_"):
        return "ignored_invalid"
    if (
        str(event.get("chat_id") or "") != config.chat_id
        or str(event.get("sender_id") or "") not in config.allowed_sender_ids
        or str(event.get("sender_type") or "") != "user"
    ):
        return "ignored_untrusted"

    stock_store = store or StockStore()
    message_at = event_message_time(event)
    if not _claim_event(stock_store, event, message_at):
        return "duplicate"

    lark_client = lark or LarkInboundClient(config)
    try:
        lark_client.react(message_id)
        _update_event(stock_store, message_id, ack_status="sent")
    except Exception as exc:
        _update_event(stock_store, message_id, ack_status="failed", error=f"ack: {exc}"[:1500])

    content = str(event.get("content") or "").strip()
    handler = ""
    try:
        if looks_like_fill_report(content):
            handler = "live-fill"
            result = render_fill_result(process_fill_report(
                content,
                message_id=message_id,
                message_at=message_at,
                store=stock_store,
            ))
        elif config.agent_enabled:
            handler = "codex"
            result = (agent or CodexMessageAgent(config, stock_store)).process(event)
        else:
            handler = "unsupported"
            result = "当前仅自动处理实盘成交回报；其他消息请在 Codex 桌面任务中处理。"
        processed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _update_event(
            stock_store,
            message_id,
            status="succeeded",
            handler=handler,
            result=result,
            error="",
            processed_at=processed_at,
        )
    except LiveFillError as exc:
        handler = "live-fill"
        result = f"❌ 成交回报未自动入账\n\n{exc}\n\n请核对代码、方向、价格、股数或补充建议单编号。"
        _update_event(
            stock_store,
            message_id,
            status="succeeded",
            handler=handler,
            result=result,
            error=str(exc),
            processed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as exc:
        result = f"❌ 消息处理失败：{str(exc)[:800]}"
        _update_event(
            stock_store,
            message_id,
            status="failed",
            handler=handler or "unknown",
            result=result,
            error=str(exc)[:1500],
            processed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    try:
        lark_client.reply(message_id, result)
        _update_event(
            stock_store,
            message_id,
            replied_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as exc:
        _update_event(
            stock_store,
            message_id,
            status="failed",
            error=f"reply: {exc}"[:1500],
        )
        return "reply_failed"
    return "processed"


def run_consumer(config: ListenerConfig) -> int:
    command = [
        config.binary,
        "--profile", config.profile,
        "event", "consume", "im.message.receive_v1", "--as", "bot",
    ]
    child = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
        env={
            **os.environ,
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        },
    )
    assert child.stdout is not None and child.stderr is not None
    ready = threading.Event()
    stopping = threading.Event()

    def drain_stderr() -> None:
        for line in child.stderr:
            print(line.rstrip(), file=sys.stderr, flush=True)
            if "[event] ready event_key=im.message.receive_v1" in line:
                ready.set()

    stderr_thread = threading.Thread(target=drain_stderr, name="lark-event-stderr", daemon=True)
    stderr_thread.start()

    def stop(_signum=None, _frame=None) -> None:
        stopping.set()
        if child.poll() is None:
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if not ready.wait(timeout=30):
        stop()
        child.wait(timeout=10)
        raise RuntimeError("Feishu event consumer did not become ready within 30s")

    store = StockStore()
    lark = LarkInboundClient(config)
    agent = CodexMessageAgent(config, store)
    with ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="feishu-inbound") as pool:
        for line in child.stdout:
            if stopping.is_set():
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"invalid event JSON: {line[:300]}", file=sys.stderr, flush=True)
                continue
            pool.submit(handle_event, event, config=config, store=store, lark=lark, agent=agent)
    returncode = child.wait()
    stderr_thread.join(timeout=2)
    if stopping.is_set():
        return 0
    return returncode or 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Stock Feishu inbound message listener")
    parser.add_argument(
        "--config",
        default=str(configurable_path("STOCK_RUNTIME_CONFIG", "config/runtime.local.json")),
    )
    parser.add_argument(
        "--event-json", default="", help="Process one flattened event JSON and exit",
    )
    args = parser.parse_args()
    config = load_listener_config(Path(args.config))
    if args.event_json:
        event = json.loads(args.event_json)
        print(handle_event(event, config=config))
        return 0
    return run_consumer(config)


if __name__ == "__main__":
    raise SystemExit(main())
