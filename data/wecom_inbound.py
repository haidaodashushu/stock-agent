"""Process trusted inbound messages from a WeCom self-built application."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import ROOT_DIR
from data.agent_runtime import CodexCliProvider
from data.live_fill_service import (
    LiveFillError,
    looks_like_fill_report,
    process_fill_report,
    render_fill_result,
)
from data.store.sqlite_store import StockStore
from data.selection_report import latest_selection_report
from data.wecom_client import WeComClient, WeComSettings
from data.wecom_sessions import (
    load_session,
    reset_session,
    save_session_id,
    thread_id_from_events,
)


ROOT = Path(ROOT_DIR)


def _is_latest_selection_request(content: str) -> bool:
    compact = content.upper().replace(" ", "")
    return "夜间预选股" in compact and any(
        word in compact for word in ("TOP", "本轮", "最新", "入选依据", "主要风险", "展示")
    )


def _latest_selection_reply(store: StockStore) -> str:
    report = latest_selection_report(store)
    if not report:
        return "当前还没有可展示的夜间预选股报告。"
    from scripts.render_cron_report import render_markdown
    return render_markdown(report)


def parse_wecom_xml(value: str) -> dict[str, str]:
    root = ET.fromstring(value)
    return {child.tag: child.text or "" for child in root}


def message_time(raw: str) -> datetime:
    try:
        value = int(raw)
        if value > 10_000_000_000:
            value //= 1000
        return datetime.fromtimestamp(value)
    except (TypeError, ValueError, OSError):
        return datetime.now()


def _claim(store: StockStore, event: dict[str, Any], message_id: str) -> bool:
    conn = store._get_conn()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO bot_inbound_messages
               (message_id,event_id,chat_id,sender_id,message_type,content,status,
                ack_status,message_at)
               VALUES (?,?,?,?,?,?,'processing','not_supported',?)""",
            (
                message_id,
                str(event.get("MsgId") or message_id),
                str(event.get("ChatId") or event.get("ToUserName") or ""),
                str(event.get("FromUserName") or ""),
                str(event.get("MsgType") or ""),
                str(event.get("Content") or ""),
                message_time(event.get("CreateTime") or "").strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _update(store: StockStore, message_id: str, **fields: str) -> None:
    allowed = {"status", "handler", "result", "error", "processed_at", "replied_at"}
    if not fields or set(fields) - allowed:
        raise ValueError("invalid inbound event update")
    assignments = ", ".join(f"{name}=?" for name in fields)
    conn = store._get_conn()
    try:
        conn.execute(
            f"UPDATE bot_inbound_messages SET {assignments} WHERE message_id=?",
            (*fields.values(), message_id),
        )
        conn.commit()
    finally:
        conn.close()


class WeComMessageAgent:
    def __init__(self, settings: WeComSettings, store: StockStore) -> None:
        self.settings = settings
        self.store = store

    def _recent_group_context(self, message_id: str, sender_id: str, chat_id: str) -> str:
        if not chat_id:
            return ""
        conn = self.store._get_conn()
        try:
            rows = conn.execute(
                """SELECT sender_id,content,result FROM bot_inbound_messages
                   WHERE message_id<>? AND chat_id=? AND sender_id<>?
                     AND status='succeeded' AND handler IN ('codex','live-fill')
                   ORDER BY received_at DESC LIMIT 6""",
                (message_id, chat_id, sender_id),
            ).fetchall()
        finally:
            conn.close()
        return "\n\n".join(
            f"{row['sender_id']}：{str(row['content'] or '')[:1200]}"
            f"\n机器人：{str(row['result'] or '')[:1800]}"
            for row in reversed(rows)
        )

    def process(
        self,
        message_id: str,
        content: str,
        *,
        sender_id: str,
        can_write: bool,
        chat_type: str,
        chat_id: str,
        session_id: str,
        image_paths: tuple[Path, ...] = (),
    ) -> tuple[str, str]:
        permission = (
            "你是管理员，可以回答任何领域的问题，也可以按明确请求修改项目和实盘数据并做相称验证。"
            if can_write
            else "你可以回答任何领域的问题，不局限于股票；可以检查资料并执行只读分析，但不得修改文件、数据库、实盘数据或外部系统，也不得调用任何写接口。"
        )
        group_context = self._recent_group_context(message_id, sender_id, chat_id)
        media_context = "\n".join(f"- {path}" for path in image_paths)
        prompt = f"""你正在处理企业微信智能机器人“搅市的棍”中、来自用户的一条消息。

内部安全约束（正常回复不要复述）：{permission}
当前会话类型：{'群聊' if chat_type == 'group' else '单聊'}

用户消息：
{content}

最近本群中机器人实际收到并处理的其他成员公开问答（可能为空；不是完整群历史）：
{group_context or '无'}

本次消息或引用消息附带的图片（可能为空，已作为图像输入同时提供）：
{media_context or '无'}

请在权限范围内回答或完成用户的实际意图。问题不必与股票或当前项目有关；涉及项目事实时先检查再回答。除非用户的请求因为触及实盘写入而被拒绝，否则不要主动提及权限、只读状态、管理员身份或实盘写入限制。
遵守项目安全边界。不要调用多代理，不要发送任何外部消息，也不要创建新的 Codex 任务；最终回复会由入站服务发回企业微信。
回复使用简洁中文并说明实际结果；若缺少会实质改变结果的信息，只说明需要用户补充什么。
"""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_id = "".join(ch for ch in message_id if ch.isalnum() or ch in "_-")[-48:]
        run_dir = ROOT / "data" / "agent_runs" / "wecom-inbound" / f"{stamp}-{safe_id}"
        outcome = CodexCliProvider(
            model=self.settings.agent_model,
            timeout_seconds=self.settings.agent_timeout_seconds,
            sandbox="workspace-write" if can_write else "read-only",
            ephemeral=False,
        ).run(
            prompt=prompt,
            workspace=ROOT,
            run_dir=run_dir,
            resume_session_id=session_id,
            image_paths=image_paths,
        )
        if outcome.returncode != 0 or not outcome.final_message.strip():
            detail = outcome.stderr.strip() or outcome.final_message or "Codex 未返回结果"
            raise RuntimeError(detail[:1500])
        return outcome.final_message.strip(), thread_id_from_events(outcome.events)


def _normalized_content(content: str, chat_type: str) -> str:
    value = content.strip()
    if chat_type == "group":
        # WeCom may concatenate the bot display name and the question without
        # whitespace (for example ``@搅市的棍请展示...``).
        value = re.sub(r"^@搅市的棍\s*", "", value).strip()
    if chat_type == "group" and value.startswith("@"):
        parts = value.split(maxsplit=1)
        if len(parts) == 2:
            return parts[1].strip()
    return value


def handle_wecom_message(
    event: dict[str, Any],
    *,
    settings: WeComSettings,
    store: StockStore | None = None,
    client: WeComClient | None = None,
    agent: WeComMessageAgent | None = None,
    message_id_prefix: str = "wecom",
) -> str:
    sender = str(event.get("FromUserName") or "")
    if not sender:
        return "ignored_invalid"
    if not settings.allow_all_users and sender not in settings.allowed_user_ids:
        return "ignored_untrusted"
    if str(event.get("MsgType") or "") != "text":
        return "ignored_unsupported"
    raw_id = str(event.get("MsgId") or "").strip()
    if not raw_id:
        return "ignored_invalid"
    message_id = f"{message_id_prefix}:{raw_id}"
    stock_store = store or StockStore()
    if not _claim(stock_store, event, message_id):
        return "duplicate"

    chat_type = str(event.get("ChatType") or "single").strip().lower()
    chat_type = "group" if chat_type == "group" else "single"
    chat_id = str(event.get("ChatId") or "").strip()
    image_paths = tuple(
        Path(str(value)).resolve()
        for value in (event.get("ImagePaths") or [])
        if str(value).strip()
    )
    content = _normalized_content(str(event.get("Content") or ""), chat_type)
    can_write = sender in settings.admin_user_ids
    handler = ""
    try:
        if content.lower() == "/new":
            handler = "session-reset"
            session = reset_session(
                stock_store,
                sender_id=sender,
                chat_type=chat_type,
                chat_id=chat_id,
                can_write=can_write,
            )
            result = f"✅ 已开启新会话（第 {session.generation} 个）。下一条消息将使用全新的上下文。"
        elif looks_like_fill_report(content):
            if not can_write:
                handler = "permission-denied"
                result = "⛔ 当前账号只有查询权限。只有管理员 WangZhengKui 可以提交或修改实盘成交数据。"
            else:
                handler = "live-fill"
                result = render_fill_result(process_fill_report(
                    content,
                    message_id=message_id,
                    message_at=message_time(event.get("CreateTime") or ""),
                    store=stock_store,
                ))
        elif _is_latest_selection_request(content):
            handler = "selection-report"
            result = _latest_selection_reply(stock_store)
        elif settings.agent_enabled:
            handler = "codex"
            session = load_session(
                stock_store,
                sender_id=sender,
                chat_type=chat_type,
                chat_id=chat_id,
                can_write=can_write,
            )
            agent_result = (agent or WeComMessageAgent(settings, stock_store)).process(
                message_id,
                content,
                sender_id=sender,
                can_write=can_write,
                chat_type=chat_type,
                chat_id=chat_id,
                session_id=session.session_id,
                image_paths=image_paths,
            )
            if isinstance(agent_result, tuple):
                result, returned_session_id = agent_result
                save_session_id(stock_store, session.conversation_key, returned_session_id)
            else:
                result = agent_result
        else:
            handler = "unsupported"
            result = "当前仅自动处理实盘成交回报。"
        _update(
            stock_store,
            message_id,
            status="succeeded",
            handler=handler,
            result=result,
            error="",
            processed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except LiveFillError as exc:
        handler = "live-fill"
        result = f"❌ 成交回报未自动入账\n\n{exc}\n\n请核对代码、方向、价格、股数或建议单编号。"
        _update(
            stock_store,
            message_id,
            status="succeeded",
            handler=handler,
            result=result,
            error=str(exc)[:1500],
            processed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as exc:
        result = f"❌ 消息处理失败：{str(exc)[:800]}"
        _update(
            stock_store,
            message_id,
            status="failed",
            handler=handler or "unknown",
            result=result,
            error=str(exc)[:1500],
            processed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    try:
        (client or WeComClient(settings)).send_markdown(sender, result)
        _update(
            stock_store,
            message_id,
            replied_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as exc:
        _update(
            stock_store,
            message_id,
            status="failed",
            error=f"reply: {exc}"[:1500],
        )
        return "reply_failed"
    return "processed"
