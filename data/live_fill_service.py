"""Deterministic, atomic processing for manual live-trade fill reports."""
from __future__ import annotations

import re
from hashlib import sha256
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
INTENT_ID_RE = re.compile(r"\bL\d{14}-[A-Z0-9]{6}\b", re.IGNORECASE)
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
    for raw_line in re.split(r"[\n;；]+", str(text or "").split("[企业微信引用消息]", 1)[0]):
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
            intent_match = INTENT_ID_RE.search(line)
            commands.append(FillCommand(
                action=VERB_ACTION[match.group("verb")],
                code=match.group("code").zfill(6),
                intent_id=intent_match.group(0).upper() if intent_match else "",
                price=round(float(match.group("price")), 2),
                volume=int(match.group("volume")),
                raw=line,
            ))
    return commands


def looks_like_fill_report(text: str) -> bool:
    return bool(re.search(rf"(?:{VERBS}).*(?:[036]\d{{5}}|L\d{{14}}-)", str(text or "").split("[企业微信引用消息]", 1)[0], re.S))


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
    same_day_unfilled = []
    for row in rows:
        if row["status"] == "filled":
            filled_at = _parse_local(row["filled_at"])
            if (
                filled_at
                and abs((filled_at - message_at).total_seconds()) <= 1800
                and round(float(row["filled_price"] or 0), 2) == command.price
                and int(row["filled_volume"] or 0) == command.volume
            ):
                return row
            continue
        created_at = _parse_local(row["created_at"])
        if (
            created_at
            and created_at <= message_at
            and created_at.date() == message_at.date()
        ):
            same_day_unfilled.append(row)
    if not same_day_unfilled:
        return None
    # Rows are newest first. A report without an explicit ID belongs to the
    # latest preceding unfilled decision for the same code and direction.
    # Expiry controls whether a suggestion is actionable, not whether a real
    # execution can be recorded after the fact.
    return same_day_unfilled[0]


def _manual_intent(
    conn,
    command: FillCommand,
    *,
    source_row,
    message_id: str,
    message_at: datetime,
    command_index: int,
):
    """Create a fillable manual record when a suggestion is only a reference."""
    source_action = str(source_row["action"]) if source_row is not None else ""
    source_code = str(source_row["code"]).zfill(6) if source_row is not None else ""
    action = command.action or source_action
    code = command.code or source_code
    if action not in {"buy", "sell"}:
        raise LiveFillError("手工成交回报必须明确写买入或卖出")
    if not re.fullmatch(r"[036]\d{5}", code):
        raise LiveFillError("手工成交回报必须包含有效的六位股票代码")

    digest = sha256(
        f"{message_id}|{command_index}|{command.raw}".encode("utf-8")
    ).hexdigest()[:6].upper()
    intent_id = f"L{message_at.strftime('%Y%m%d%H%M%S')}-{digest}"
    source_id = command.intent_id or (
        str(source_row["intent_id"]) if source_row is not None else ""
    )
    source_matches_code = source_row is not None and source_code == code
    name = str(source_row["name"] or code) if source_matches_code else code
    reason = "管理员手工成交回报"
    if source_id:
        reason += f"；参考建议单 {source_id}，实际成交信息以回报为准"
    conn.execute(
        """INSERT OR IGNORE INTO live_trade_intents
           (intent_id,code,name,action,suggested_price,suggested_volume,
            suggested_amount,limit_price,reason,strategy,risk_note,status,
            created_at,expires_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            intent_id, code, name, action, command.price, command.volume,
            round(command.price * command.volume, 2), 0.0, reason,
            "manual_execution", "", "proposed",
            message_at.strftime("%Y-%m-%d %H:%M:%S"), "",
        ),
    )
    return conn.execute(
        "SELECT * FROM live_trade_intents WHERE intent_id=?",
        (intent_id,),
    ).fetchone()


def process_fill_report(
    text: str,
    *,
    message_id: str,
    message_at: datetime,
    store: StockStore | None = None,
) -> dict[str, Any]:
    """Atomically fill every command contained in one Feishu message."""
    commands = parse_fill_commands(text)
    return process_fill_commands(
        commands, message_id=message_id, message_at=message_at, store=store,
    )


def process_fill_commands(
    commands: list[FillCommand],
    *,
    message_id: str,
    message_at: datetime,
    store: StockStore | None = None,
) -> dict[str, Any]:
    """Execute validated structured fills without classifying message text."""
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
        for command_index, command in enumerate(commands):
            if command.price <= 0 or command.volume <= 0:
                raise LiveFillError("成交价格和数量必须大于0")
            source_row = _matching_intent(conn, command, message_at)
            row = source_row
            if row is not None:
                source_action = str(row["action"])
                source_code = str(row["code"]).zfill(6)
                actual_action = command.action or source_action
                actual_code = command.code or source_code
                filled_differs = row["status"] == "filled" and (
                    round(float(row["filled_price"] or 0), 2) != command.price
                    or int(row["filled_volume"] or 0) != command.volume
                )
                if (
                    actual_action != source_action
                    or actual_code != source_code
                    or row["status"] in {"cancelled", "rejected"}
                    or filled_differs
                ):
                    row = None
            if row is None:
                row = _manual_intent(
                    conn,
                    command,
                    source_row=source_row,
                    message_id=message_id,
                    message_at=message_at,
                    command_index=command_index,
                )
            action = str(row["action"])
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
                note = f"机器人消息 {message_id}：{command.raw}"
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
