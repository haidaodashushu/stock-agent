"""Persistent per-conversation Codex session mapping for WeCom AI bot messages."""
from __future__ import annotations

import json
from dataclasses import dataclass

from data.store.sqlite_store import StockStore


@dataclass(frozen=True)
class ConversationSession:
    conversation_key: str
    session_id: str
    generation: int
    can_write: bool


def conversation_key(*, sender_id: str, chat_type: str, chat_id: str) -> str:
    """Isolate every private user and every group participant from one another."""
    if chat_type == "group" and chat_id:
        return f"group:{chat_id}:{sender_id}"
    return f"single:{sender_id}"


def _ensure_table(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS wecom_agent_sessions (
               conversation_key TEXT PRIMARY KEY,
               sender_id TEXT NOT NULL,
               chat_type TEXT NOT NULL,
               chat_id TEXT NOT NULL DEFAULT '',
               session_id TEXT NOT NULL DEFAULT '',
               generation INTEGER NOT NULL DEFAULT 1,
               can_write INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
               updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
           )"""
    )


def load_session(
    store: StockStore,
    *,
    sender_id: str,
    chat_type: str,
    chat_id: str,
    can_write: bool,
) -> ConversationSession:
    key = conversation_key(sender_id=sender_id, chat_type=chat_type, chat_id=chat_id)
    conn = store._get_conn()
    try:
        _ensure_table(conn)
        row = conn.execute(
            "SELECT session_id,generation,can_write FROM wecom_agent_sessions WHERE conversation_key=?",
            (key,),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO wecom_agent_sessions
                   (conversation_key,sender_id,chat_type,chat_id,session_id,generation,can_write)
                   VALUES (?,?,?,?, '',1,?)""",
                (key, sender_id, chat_type, chat_id, int(can_write)),
            )
            conn.commit()
            return ConversationSession(key, "", 1, can_write)
        generation = int(row["generation"] or 1)
        session_id = str(row["session_id"] or "")
        if bool(row["can_write"]) != can_write:
            generation += 1
            session_id = ""
            conn.execute(
                """UPDATE wecom_agent_sessions
                   SET session_id='',generation=?,can_write=?,updated_at=datetime('now','localtime')
                   WHERE conversation_key=?""",
                (generation, int(can_write), key),
            )
            conn.commit()
        return ConversationSession(key, session_id, generation, can_write)
    finally:
        conn.close()


def save_session_id(store: StockStore, conversation_key: str, session_id: str) -> None:
    if not session_id:
        return
    conn = store._get_conn()
    try:
        _ensure_table(conn)
        conn.execute(
            """UPDATE wecom_agent_sessions
               SET session_id=?,updated_at=datetime('now','localtime')
               WHERE conversation_key=?""",
            (session_id, conversation_key),
        )
        conn.commit()
    finally:
        conn.close()


def reset_session(
    store: StockStore,
    *,
    sender_id: str,
    chat_type: str,
    chat_id: str,
    can_write: bool,
) -> ConversationSession:
    current = load_session(
        store,
        sender_id=sender_id,
        chat_type=chat_type,
        chat_id=chat_id,
        can_write=can_write,
    )
    conn = store._get_conn()
    try:
        _ensure_table(conn)
        generation = current.generation + 1
        conn.execute(
            """UPDATE wecom_agent_sessions
               SET session_id='',generation=?,can_write=?,updated_at=datetime('now','localtime')
               WHERE conversation_key=?""",
            (generation, int(can_write), current.conversation_key),
        )
        conn.commit()
        return ConversationSession(current.conversation_key, "", generation, can_write)
    finally:
        conn.close()


def thread_id_from_events(events: str) -> str:
    for raw in events.splitlines():
        try:
            event = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return ""
