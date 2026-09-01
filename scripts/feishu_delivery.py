#!/usr/bin/env python3
"""Resolve a stable Feishu recipient from Hermes channel-directory JSON."""

from __future__ import annotations

import json
import sys
from typing import Any


def resolve_delivery_target(directory: dict[str, Any]) -> tuple[str, str] | None:
    channels = (directory.get("platforms") or {}).get("feishu") or []

    chat_ids: list[str] = []
    user_ids: list[str] = []
    for channel in channels:
        raw_id = str(channel.get("id") or "").strip()
        base_id = raw_id.split(":", 1)[0]
        if base_id.startswith("oc_"):
            chat_ids.append(base_id)
        elif base_id.startswith("ou_"):
            user_ids.append(base_id)

    if chat_ids:
        return "chat", chat_ids[0]
    if user_ids:
        return "user", user_ids[0]
    return None


def main() -> int:
    target = resolve_delivery_target(json.load(sys.stdin))
    if target is None:
        return 1
    print("\t".join(target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
