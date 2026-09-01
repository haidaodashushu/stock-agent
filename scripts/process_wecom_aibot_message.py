#!/usr/bin/env python3
"""Process one intelligent-robot frame through the shared WeCom policy engine."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.runtime_paths import configurable_path  # noqa: E402
from data.store.sqlite_store import StockStore  # noqa: E402
from data.wecom_aibot import load_wecom_aibot_settings  # noqa: E402
from data.wecom_inbound import handle_wecom_message  # noqa: E402


class CaptureClient:
    def __init__(self) -> None:
        self.reply = ""

    def send_markdown(self, user_id: str, content: str) -> dict:
        self.reply = content
        return {"errcode": 0}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(configurable_path("STOCK_RUNTIME_CONFIG", "config/runtime.local.json")),
    )
    args = parser.parse_args()
    frame = json.load(sys.stdin)
    settings = load_wecom_aibot_settings(Path(args.config))
    sender = str(frame.get("sender_id") or "")
    client = CaptureClient()
    status = handle_wecom_message(
        {
            "ToUserName": settings.bot_id,
            "FromUserName": sender,
            "CreateTime": str(frame.get("create_time") or ""),
            "MsgType": "text",
            "Content": str(frame.get("content") or ""),
            "MsgId": str(frame.get("message_id") or ""),
            "ChatType": str(frame.get("chat_type") or "single"),
            "ChatId": str(frame.get("chat_id") or ""),
            "ImagePaths": list(frame.get("image_paths") or []),
        },
        settings=settings,  # type: ignore[arg-type]
        store=StockStore(),
        client=client,  # type: ignore[arg-type]
        message_id_prefix="wecom-aibot",
    )
    reply = client.reply
    if status == "duplicate" and not reply:
        reply = "这条消息已经处理过了，请勿重复发送。"
    elif status.startswith("ignored") and not reply:
        reply = "这条消息暂不支持处理。"
    print("STOCK_AIBOT_RESULT=" + json.dumps(
        {"status": status, "reply": reply}, ensure_ascii=False, separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
