"""Configuration and loopback delivery bridge for WeCom intelligent robots."""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WeComAiBotSettings:
    bot_id: str
    secret: str
    allowed_user_ids: frozenset[str]
    admin_user_ids: frozenset[str]
    allow_all_users: bool = True
    agent_enabled: bool = True
    agent_model: str = "gpt-5.6-sol"
    agent_timeout_seconds: int = 600
    bridge_url: str = "http://127.0.0.1:8898"


def _env_bool(name: str, fallback: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return fallback if not raw else raw in {"1", "true", "yes", "on"}


def load_wecom_aibot_settings(path: Path) -> WeComAiBotSettings:
    if not path.is_file():
        raise RuntimeError(f"WeCom AI bot runtime config does not exist: {path}")
    parsed = json.loads(path.read_text(encoding="utf-8"))
    raw = parsed.get("wecom_aibot") if isinstance(parsed, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    agent = parsed.get("agent") if isinstance(parsed, dict) else {}
    agent = agent if isinstance(agent, dict) else {}

    bot_id = os.environ.get("STOCK_WECOM_AIBOT_ID") or str(raw.get("bot_id") or "")
    secret = os.environ.get("STOCK_WECOM_AIBOT_SECRET") or str(raw.get("secret") or "")
    allow_all = _env_bool(
        "STOCK_WECOM_AIBOT_ALLOW_ALL_USERS",
        bool(raw.get("allow_all_users", True)),
    )
    env_users = os.environ.get("STOCK_WECOM_AIBOT_ALLOWED_USER_IDS", "")
    users = {
        str(value).strip()
        for value in [*(raw.get("allowed_user_ids") or []), *env_users.split(",")]
        if str(value).strip()
    }
    env_admins = os.environ.get("STOCK_WECOM_AIBOT_ADMIN_USER_IDS", "")
    admins = {
        str(value).strip()
        for value in [*(raw.get("admin_user_ids") or []), *env_admins.split(",")]
        if str(value).strip()
    }
    if not bot_id or not secret:
        raise RuntimeError("WeCom intelligent robot bot_id or secret is not configured")
    if not admins:
        raise RuntimeError("WeCom intelligent robot requires at least one admin_user_id")
    if not allow_all and not users:
        raise RuntimeError("WeCom intelligent robot requires allowed_user_ids in restricted mode")
    if not allow_all and not admins.issubset(users):
        raise RuntimeError("WeCom intelligent robot admins must be allowed users")
    return WeComAiBotSettings(
        bot_id=bot_id,
        secret=secret,
        allowed_user_ids=frozenset(users),
        admin_user_ids=frozenset(admins),
        allow_all_users=allow_all,
        agent_enabled=bool(raw.get("agent_enabled", True)),
        agent_model=str(agent.get("model") or "gpt-5.6-sol"),
        agent_timeout_seconds=max(30, int(raw.get("agent_timeout_seconds") or 600)),
        bridge_url=str(raw.get("bridge_url") or "http://127.0.0.1:8898").rstrip("/"),
    )


def send_wecom_aibot_message(
    settings: WeComAiBotSettings,
    content: str,
    *,
    idempotency_key: str = "",
    mention_all: bool = False,
) -> None:
    payload = json.dumps(
        {
            "content": content,
            "idempotency_key": idempotency_key,
            "mention_all": mention_all,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{settings.bridge_url}/send",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "stock-agent/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"WeCom intelligent robot bridge failed: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "WeCom intelligent robot send failed"))
