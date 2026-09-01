"""Deterministic, atomic processing for manual live-trade fill reports."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from data.live_manual_account import execution_deviation_warnings, load_config
from data.store.sqlite_store import StockStore


class LiveFillError(ValueError):
    """A user fill report could not be matched safely."""


@dataclass(frozen=True)
class FillCommand:
    action: str
    code: str
    intent_id: str
    price: float
    volume: int
    raw: str


VERB_ACTION = {
    "买入": "buy",
    "已买": "buy",
    "卖出": "sell",
    "已卖": "sell",
    "成交": "",
    "修改成交": "",
}
VERBS = "修改成交|成交|已买|已卖|买入|卖出"
INTENT_RE = re.compile(
    rf"(?P<verb>{VERBS})\s+(?P<intent>L\d{{14}}-[A-Z0-9]{{6}})\s+"
    r"(?P<price>\d+(?:\.\d+)?)\s+(?P<volume>\d+)",
    re.IGNORECASE,
)
NATURAL_RE = re.compile(
    rf"(?P<verb>{VERBS})\s+(?P<code>[036]\d{{5}})"
    r"(?:\s+[\u4e00-\u9fffA-Za-z*]+)?\s*[：:]?\s*"
    r"(?P<volume>\d+)\s*股(?:\s*[，,]\s*|\s+)"
    r"(?:(?:参考价|成交价|价格|价)\s*[：:]?\s*)?"
    r"(?P<price>\d+(?:\.\d+)?)",
)
COMPACT_RE = re.compile(
    rf"(?P<verb>{VERBS})\s+(?P<code>[036]\d{{5}})\s+"
    r"(?P<price>\d+(?:\.\d+)?)\s+(?P<volume>\d+)",
)


def parse_fill_commands(text: str) -> list[FillCommand]:
    """Parse one or more fill lines, including the natural Feishu report form."""
    commands: list[FillCommand] = []
    for raw_line in re.split(r"[\n;；]+", str(text or "")):
        line = raw_line.strip().lstrip("-*• ").strip()
        if not line:
            continue
        match = INTENT_RE.search(line)
        if match:
            commands.append(FillCommand(
                action=VERB_ACTION[match.group("verb")],
                code="",
                intent_id=match.group("intent").upper(),
                price=round(float(match.group("price")), 2),
                volume=int(match.group("volume")),
                raw=line,
            ))
            continue
        match = NATURAL_RE.search(line) or COMPACT_RE.search(line)
        if match:
            commands.append(FillCommand(
                action=VERB_ACTION[match.group("verb")],
                code=match.group("code").zfill(6),
                intent_id="",
                price=round(float(match.group("price")), 2),
                volume=int(match.group("volume")),
                raw=line,
            ))
    return commands


def looks_like_fill_report(text: str) -> bool:
    return bool(re.search(rf"(?:{VERBS}).*(?:[036]\d{{5}}|L\d{{14}}-)", str(text or ""), re.S))


def _parse_local(value: str) -> datetime | None:
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _matching_intent(conn, command: FillCommand, message_at: datetime):
    if command.intent_id:
        row = conn.execute(
            "SELECT * FROM live_trade_intents WHERE intent_id=?",
            (command.intent_id,),
        ).fetchone()
        if not row:
            raise LiveFillError(f"未找到建议单 {command.intent_id}")
        if command.action and row["action"] != command.action:
            raise LiveFillError(f"建议单 {command.intent_id} 的买卖方向与成交回报不一致")
        return row

    params: list[Any] = [command.code]
    where = "code=? AND status IN ('proposed','expired','filled')"
    if command.action:
        where += " AND action=?"
        params.append(command.action)
    rows = conn.execute(
        f"SELECT * FROM live_trade_intents WHERE {where} ORDER BY id DESC LIMIT 20",
        params,
    ).fetchall()
    eligible = []
    for row in rows:
        if row["status"] == "filled":
            filled_at = _parse_local(row["filled_at"])
            if (
                filled_at
                and abs((filled_at - message_at).total_seconds()) <= 1800
                and round(float(row["filled_price"] or 0), 2) == command.price
                and int(row["filled_volume"] or 0) == command.volume
            ):
                eligible.append(row)
            continue
        created_at = _parse_local(row["created_at"])
        expires_at = _parse_local(row["expires_at"])
        if created_at and created_at <= message_at and (not expires_at or message_at <= expires_at):
            eligible.append(row)
    if not eligible:
        direction = {"buy": "买入", "sell": "卖出", "": "成交"}[command.action]
        raise LiveFillError(
            f"未找到 {command.code} 在消息时间有效的{direction}建议单，请带建议单编号"
        )
    active = [row for row in eligible if row["status"] != "filled"]
    candidates = active or eligible
    if len(candidates) > 1:
        ids = "、".join(str(row["intent_id"]) for row in candidates[:5])
        raise LiveFillError(f"{command.code} 匹配到多张建议单，请明确编号：{ids}")
    return candidates[0]


def process_fill_report(
    text: str,
    *,
    message_id: str,
    message_at: datetime,
    store: StockStore | None = None,
) -> dict[str, Any]:
    """Atomically fill every command contained in one Feishu message."""
    commands = parse_fill_commands(text)
    if not commands:
        raise LiveFillError(
            "未识别成交回报。示例：买入 600460 士兰微：300 股，成交价 41.20"
        )
    stock_store = store or StockStore()
    conn = stock_store._get_conn()
    cfg = load_config()
    results: list[dict[str, Any]] = []
    filled_at = message_at.strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for command in commands:
            if command.price <= 0 or command.volume <= 0:
                raise LiveFillError("成交价格和数量必须大于0")
            row = _matching_intent(conn, command, message_at)
            action = str(row["action"])
            if row["status"] in {"cancelled", "rejected"}:
                raise LiveFillError(f"建议单 {row['intent_id']} 已{row['status']}，不能自动回填")
            if row["status"] == "filled":
                if (
                    round(float(row["filled_price"] or 0), 2) != command.price
                    or int(row["filled_volume"] or 0) != command.volume
                ):
                    raise LiveFillError(f"建议单 {row['intent_id']} 已有不同成交记录，请人工核对")
                warnings: list[str] = []
                already_filled = True
            else:
                warnings = execution_deviation_warnings(row, command.price, command.volume, cfg)
                note = f"飞书消息 {message_id}：{command.raw}"
                if warnings:
                    note += "；执行偏离警告：" + "；".join(warnings)
                conn.execute(
                    """UPDATE live_trade_intents
                       SET status='filled', filled_price=?, filled_volume=?, filled_amount=?,
                           filled_at=?, user_note=? WHERE intent_id=?""",
                    (
                        command.price,
                        command.volume,
                        round(command.price * command.volume, 2),
                        filled_at,
                        note,
                        row["intent_id"],
                    ),
                )
                already_filled = False
            results.append({
                "intent_id": str(row["intent_id"]),
                "action": action,
                "code": str(row["code"]),
                "name": str(row["name"] or ""),
                "price": command.price,
                "volume": command.volume,
                "amount": round(command.price * command.volume, 2),
                "warnings": warnings,
                "already_filled": already_filled,
            })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"fills": results, "filled_at": filled_at}


def render_fill_result(result: dict[str, Any]) -> str:
    lines = ["✅ 实盘成交已回填"]
    for item in result.get("fills", []):
        action = "买入" if item["action"] == "buy" else "卖出"
        marker = "已存在，确认无重复记账" if item.get("already_filled") else "已记录"
        lines.append(
            f"- {action} {item['code']} {item['name']}：{item['volume']}股 @ "
            f"¥{item['price']:.2f}（{marker}，编号 {item['intent_id']}）"
        )
        for warning in item.get("warnings", []):
            lines.append(f"  ⚠️ {warning}")
    lines.append(f"成交时间：{result.get('filled_at', '')}")
    lines.append("影子实盘账户已按真实成交重建；系统不会向券商重复下单。")
    return "\n".join(lines)
