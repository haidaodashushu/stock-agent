"""Route operational notifications to the locally configured message provider."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from data.feishu_client import load_feishu_settings, send_feishu
from data.wecom_aibot import load_wecom_aibot_settings, send_wecom_aibot_message
from data.wecom_client import WeComClient, load_wecom_settings


def configured_provider(path: Path) -> str:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    messaging = parsed.get("messaging") if isinstance(parsed, dict) else {}
    messaging = messaging if isinstance(messaging, dict) else {}
    return str(messaging.get("provider") or "feishu").strip().lower()


def _messaging_options(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    messaging = parsed.get("messaging") if isinstance(parsed, dict) else {}
    return messaging if isinstance(messaging, dict) else {}


def _lark_card_markdown(card: dict[str, Any]) -> str:
    lines: list[str] = []
    header = card.get("header") if isinstance(card.get("header"), dict) else {}
    title = header.get("title") if isinstance(header.get("title"), dict) else {}
    if title.get("content"):
        lines.append(f"## {title['content']}")

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("tag") == "markdown" and value.get("content"):
                lines.append(str(value["content"]))
            for key, child in value.items():
                if key not in {"header", "title"}:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(card.get("body") or {})
    return "\n\n".join(dict.fromkeys(lines)) or "股票系统报告已生成。"


def portable_content(content: str, message_type: str) -> str:
    if message_type != "interactive":
        return content
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("interactive message content must be a JSON object")
    if parsed.get("schema") == "2.0":
        return _lark_card_markdown(parsed)
    from scripts.render_cron_report import render_markdown
    return render_markdown(parsed)


def feishu_content(content: str, message_type: str) -> str:
    """Convert a channel-neutral report into a native Feishu payload."""
    if message_type != "interactive":
        return content
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("interactive message content must be a JSON object")
    if parsed.get("schema") != "2.0":
        from scripts.render_cron_report import render_presentation
        parsed = render_presentation(parsed)
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def send_configured_message(
    *,
    config_path: Path,
    content: str,
    message_type: str,
    idempotency_key: str,
    dry_run: bool = False,
) -> None:
    provider = configured_provider(config_path)
    if provider == "wecom":
        if not dry_run:
            client = WeComClient(load_wecom_settings(config_path))
            client.send_to_allowed_users(portable_content(content, message_type))
        return
    if provider == "wecom_aibot":
        if not dry_run:
            options = _messaging_options(config_path)
            send_wecom_aibot_message(
                load_wecom_aibot_settings(config_path),
                portable_content(content, message_type),
                idempotency_key=idempotency_key,
                mention_all=bool(options.get("mention_all_on_push", True)),
            )
        return
    if provider == "feishu":
        completed = send_feishu(
            settings=load_feishu_settings(config_path),
            content=feishu_content(content, message_type),
            message_type=message_type,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip() or "lark-cli failed"
            raise RuntimeError(detail)
        return
    raise RuntimeError(f"unsupported messaging provider: {provider}")
