#!/usr/bin/env python3
"""Deliver committed agent reports directly through lark-cli."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.store.sqlite_store import StockStore  # noqa: E402
from data.feishu_client import load_feishu_settings, send_feishu  # noqa: E402
from scripts.render_cron_report import render_presentation  # noqa: E402
from config.runtime_paths import configurable_path  # noqa: E402


def _claim_next(store: StockStore) -> dict[str, Any] | None:
    conn = store._get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT * FROM agent_message_outbox
                WHERE status IN ('pending','failed','sending') AND attempts < 5
                ORDER BY id LIMIT 1"""
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            """UPDATE agent_message_outbox
                  SET status='sending', attempts=attempts+1, last_error=''
                WHERE id=?""",
            (row["id"],),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _finish(store: StockStore, row_id: int, *, error: str = "") -> None:
    conn = store._get_conn()
    try:
        if error:
            conn.execute(
                "UPDATE agent_message_outbox SET status='failed', last_error=? WHERE id=?",
                (error[:1000], row_id),
            )
        else:
            conn.execute(
                """UPDATE agent_message_outbox
                      SET status='sent', last_error='', sent_at=datetime('now','localtime')
                    WHERE id=?""",
                (row_id,),
            )
        conn.commit()
    finally:
        conn.close()


def _card_content(content: str) -> str:
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("interactive outbox content must be a JSON object")
    card = parsed if parsed.get("schema") == "2.0" else render_presentation(parsed)
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Send pending stock-agent Feishu messages")
    parser.add_argument(
        "--config",
        default=str(configurable_path("STOCK_RUNTIME_CONFIG", "config/runtime.local.json")),
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    settings = load_feishu_settings(Path(args.config))
    store = StockStore()
    sent = failed = 0
    for _ in range(max(0, args.limit)):
        row = _claim_next(store)
        if row is None:
            break
        key = hashlib.sha256(str(row["dedupe_key"]).encode()).hexdigest()[:32]
        if row["message_type"] == "interactive":
            content = _card_content(row["content"])
        else:
            content = str(row["content"])
        completed = send_feishu(
            settings=settings, content=content, message_type=str(row["message_type"]),
            idempotency_key=key, dry_run=args.dry_run,
        )
        if completed.returncode == 0:
            _finish(store, int(row["id"]))
            sent += 1
        else:
            error = completed.stderr.strip() or completed.stdout.strip() or "lark-cli failed"
            _finish(store, int(row["id"]), error=error)
            failed += 1
    print(json.dumps({"status": "ok" if not failed else "partial", "sent": sent, "failed": failed}))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
