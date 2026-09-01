"""Hermes-free Feishu delivery through the authenticated lark-cli profile."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def load_feishu_settings(path: Path) -> dict[str, str]:
    raw = {}
    if path.exists():
        parsed = json.loads(path.read_text(encoding="utf-8"))
        raw = parsed.get("feishu") if isinstance(parsed, dict) else {}
        raw = raw if isinstance(raw, dict) else {}
    user_id = os.environ.get("STOCK_FEISHU_USER_ID") or ""
    chat_id = os.environ.get("STOCK_FEISHU_CHAT_ID") or ""
    kind = "user" if user_id else "chat" if chat_id else str(raw.get("target_kind") or "chat")
    target = user_id or chat_id or str(raw.get("target_id") or "")
    expected = "ou_" if kind == "user" else "oc_"
    if kind not in {"chat", "user"} or not target.startswith(expected):
        raise RuntimeError(
            "Feishu target is not configured; set STOCK_FEISHU_CHAT_ID/USER_ID "
            "or STOCK_RUNTIME_CONFIG (defaults to config/runtime.local.json)"
        )
    requested_binary = os.environ.get("LARK_CLI_BIN") or "lark-cli"
    binary = shutil.which(requested_binary)
    if not binary and requested_binary == "lark-cli":
        local_binary = Path.home() / ".local/bin/lark-cli"
        binary = str(local_binary) if local_binary.exists() else None
    if not binary:
        raise RuntimeError("lark-cli is unavailable")
    return {
        "binary": binary,
        "kind": kind,
        "target": target,
        "profile": os.environ.get("STOCK_LARK_PROFILE") or str(raw.get("profile") or "stock"),
    }


def send_feishu(
    *, settings: dict[str, str], content: str, message_type: str,
    idempotency_key: str, dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        settings["binary"], "--profile", settings["profile"], "im", "+messages-send",
        "--chat-id" if settings["kind"] == "chat" else "--user-id",
        settings["target"], "--idempotency-key", idempotency_key[:50],
    ]
    if message_type == "interactive":
        command.extend(["--msg-type", "interactive", "--content", content])
    else:
        command.extend(["--markdown", content])
    if dry_run:
        command.append("--dry-run")
    return subprocess.run(
        command, text=True, capture_output=True, timeout=60, check=False,
    )
