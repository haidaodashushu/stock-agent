#!/usr/bin/env python3
"""Send one prepared stock-system message without Hermes."""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.feishu_client import load_feishu_settings, send_feishu  # noqa: E402
from config.runtime_paths import configurable_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--message-type", choices=("text", "interactive"), default="text")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument(
        "--config",
        default=str(configurable_path("STOCK_RUNTIME_CONFIG", "config/runtime.local.json")),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    content = args.file.read_text(encoding="utf-8")
    key = args.idempotency_key or hashlib.sha256(content.encode()).hexdigest()[:32]
    completed = send_feishu(
        settings=load_feishu_settings(Path(args.config)), content=content,
        message_type=args.message_type, idempotency_key=key, dry_run=args.dry_run,
    )
    if completed.stdout:
        print(completed.stdout.strip())
    if completed.returncode and completed.stderr:
        print(completed.stderr.strip(), file=sys.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
