#!/usr/bin/env python3
"""Render a stock cron report JSON into Feishu-friendly text and presentation JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


VALID_TONES = {"info", "success", "warning", "danger", "neutral"}
MAX_REASON = 48

TONE_STYLES = {
    "info": {"template": "blue", "accent": "blue", "background": "blue-50", "tag": "信息"},
    "success": {"template": "green", "accent": "green", "background": "green-50", "tag": "完成"},
    "warning": {"template": "yellow", "accent": "yellow", "background": "yellow-50", "tag": "关注"},
    "danger": {"template": "red", "accent": "red", "background": "red-50", "tag": "异常"},
    "neutral": {"template": "grey", "accent": "grey", "background": "grey-50", "tag": "报告"},
}


def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value).strip()


def clip(value: Any, limit: int = MAX_REASON) -> str:
    text = as_text(value, "-")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def table_cell(value: Any) -> str:
    return clip(value).replace("\n", " ").replace("|", " / ")


def normalize_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def normalize_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [as_text(item) for item in value if as_text(item)]


def split_warning_notes(items: list[str]) -> tuple[list[str], list[str]]:
    """Separate real warnings from data-source notes such as optional reliability."""
    warnings: list[str] = []
    notes: list[str] = []
    hard_tokens = ("401", "失败", "无数据", "未取到", "异常", "错误", "error", "HTTP", "超时", "timeout")
    for item in items:
        text = as_text(item)
        if not text:
            continue
        if "optional" in text.lower() and not any(token in text for token in hard_tokens):
            notes.append(text)
        else:
            warnings.append(text)
    return warnings, notes


def table(headers: list[str], rows: list[list[Any]], max_rows: int | None = 12) -> str:
    if not rows:
        return ""
    visible = rows if max_rows is None else rows[:max_rows]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in visible:
        lines.append("| " + " | ".join(table_cell(cell) for cell in row) + " |")
    if max_rows is not None and len(rows) > max_rows:
        extra = ["..."] * max(0, len(headers) - 1) + [f"另有 {len(rows) - max_rows} 条"]
        lines.append("| " + " | ".join(extra) + " |")
    return "\n".join(lines)


def money(value: Any) -> str:
    try:
        return f"{float(value):,.0f}"
    except Exception:
        return as_text(value, "-")


def signed_money(value: Any) -> str:
    try:
        return f"{float(value):+,.0f}"
    except Exception:
        return as_text(value, "-")


def pct(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):+.{digits}f}%"
    except Exception:
        return as_text(value, "-")


def status_line(status: dict[str, Any]) -> str:
    if not status:
        return ""
    parts = []
    if "executed" in status:
        parts.append(f"执行：{as_text(status.get('executed'))}")
    if "skipped" in status:
        parts.append(f"跳过：{as_text(status.get('skipped'))}")
    if status.get("market"):
        parts.append(f"市场：{as_text(status.get('market'))}")
    if status.get("reason"):
        parts.append(f"原因：{as_text(status.get('reason'))}")
    return "；".join(parts)


def metrics_lines(metrics: list[dict[str, Any]]) -> list[str]:
    lines = []
    for item in metrics[:10]:
        label = as_text(item.get("label") or item.get("name"))
        value = as_text(item.get("value"))
        if label and value:
            lines.append(f"- {label}: {value}")
    return lines


def file_lines(files: list[dict[str, Any]]) -> list[str]:
    lines = []
    for item in files[:8]:
        label = as_text(item.get("label") or item.get("name") or "文件")
        path = as_text(item.get("path") or item.get("file"))
        if path:
            lines.append(f"- {label}: {path}")
    return lines


def report_title(report: dict[str, Any]) -> str:
    title = as_text(report.get("title"), "股票 cron 报告")
    files = normalize_items(report.get("files"))
    file_text = " ".join(as_text(item.get("path") or item.get("file")) for item in files)
    source = as_text(report.get("source"))
    evidence = f"{file_text} {source}"
    is_sim_intraday = (
        "intraday_snapshot" in evidence
        or "ai_trade_signal" in evidence
        or "execute_trade_signal.py" in evidence
    ) and "live_trade" not in evidence
    if is_sim_intraday and "实盘操盘" in title:
        return title.replace("实盘操盘半小时筛选", "模拟盘盘中盯盘").replace("实盘操盘", "模拟盘盘中盯盘")
    return title


def action_rows(actions: list[dict[str, Any]]) -> list[list[str]]:
    rows = []
    for item in actions:
        rows.append(
            [
                as_text(item.get("code"), "-"),
                as_text(item.get("name"), "-"),
                as_text(item.get("action"), "-"),
                as_text(item.get("result"), "-"),
                as_text(item.get("reason") or item.get("risk"), "-"),
            ]
        )
    return rows


def build_close_review_blocks(report: dict[str, Any]) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    summary = as_text(report.get("summary"))
    if summary:
        blocks.append({"type": "text", "text": f"**收盘结论**\n{summary}"})

    account = report.get("account") if isinstance(report.get("account"), dict) else {}
    if account:
        lines = [
            f"- 总资产: {money(account.get('total_equity'))}",
            f"- 现金: {money(account.get('available_cash'))}",
            f"- 持仓市值: {money(account.get('position_market_value'))}",
            f"- 持仓: {as_text(account.get('position_count'), '0')}只",
            f"- 总盈亏: {signed_money(account.get('total_profit'))} ({pct(account.get('total_profit_pct'), 2)})",
        ]
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**账户总览**\n" + "\n".join(lines)})

    positions = normalize_items(report.get("positions"))
    if positions:
        rows = []
        for item in positions:
            concepts = item.get("concepts") if isinstance(item.get("concepts"), list) else []
            concept_text = ",".join(as_text(x) for x in concepts[:2] if as_text(x))
            rows.append(
                [
                    as_text(item.get("code")),
                    as_text(item.get("name")),
                    as_text(item.get("volume")),
                    money(item.get("market_value")),
                    pct(item.get("today_chg_pct")),
                    pct(item.get("pnl_pct")),
                    concept_text or "-",
                ]
            )
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "text",
                "text": "**持仓表现**\n"
                + table(["代码", "名称", "股数", "市值", "今日", "持仓盈亏", "标签"], rows, max_rows=None),
            }
        )
    else:
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**持仓表现**\n空仓"})

    orders = normalize_items(report.get("orders"))
    if orders:
        rows = []
        for item in orders[-12:]:
            action = "买" if item.get("action") == "buy" else "卖"
            rows.append(
                [
                    as_text(item.get("time")),
                    action,
                    as_text(item.get("code")),
                    as_text(item.get("name")),
                    as_text(item.get("volume")),
                    as_text(item.get("price")),
                    as_text(item.get("reason")),
                ]
            )
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "text",
                "text": "**今日交易**\n"
                + table(["时间", "方向", "代码", "名称", "股数", "价格", "理由"], rows, max_rows=12),
            }
        )
    else:
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**今日交易**\n无"})

    fund_flows = normalize_items(report.get("fund_flows"))
    if fund_flows:
        rows = [
            [as_text(item.get("code")), as_text(item.get("name")), as_text(item.get("summary"))]
            for item in fund_flows[:8]
        ]
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**收盘资金流向**\n" + table(["代码", "名称", "资金流"], rows, max_rows=8)})

    warnings, notes = split_warning_notes(normalize_strings(report.get("warnings")))
    if warnings:
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**异常 / 说明**\n" + "\n".join(f"- {w}" for w in warnings[:6])})
    data_notes = notes + normalize_strings(report.get("data_notes"))
    if data_notes:
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**数据说明**\n" + "\n".join(f"- {w}" for w in data_notes[:6])})

    source = as_text(report.get("source"))
    if source:
        blocks.append({"type": "context", "text": f"- 来源: {source}"})
    return blocks


def build_screening_report_blocks(report: dict[str, Any]) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    summary = as_text(report.get("summary"))
    if summary:
        blocks.append({"type": "text", "text": f"**选股结论**\n{summary}"})

    run = report.get("run") if isinstance(report.get("run"), dict) else {}
    if run:
        lines = [
            f"- 目标交易日: {as_text(run.get('run_date'))}",
            f"- 候选: {as_text(run.get('count'), '0')}只",
            f"- buy/watch: {as_text(run.get('buy_count'), '0')} / {as_text(run.get('watch_count'), '0')}",
            f"- 科技主线: {as_text(run.get('tech_count'), '0')}只",
            f"- 强逻辑变化: {as_text(run.get('strong_logic_count'), '0')}只",
            f"- 主线: {as_text(run.get('mainline_text'), '无明显集中主线')}",
        ]
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**本轮概览**\n" + "\n".join(lines)})

    candidates = normalize_items(report.get("candidates"))
    if candidates:
        rows = []
        for item in candidates[:10]:
            concepts = item.get("concepts") if isinstance(item.get("concepts"), list) else []
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            # Keep strategy/mainline identity visible instead of letting
            # concept names hide dynamically added theme labels.
            label_parts = [as_text(x) for x in tags if "主线" in as_text(x)][:2]
            label_parts.extend(as_text(x) for x in concepts[:2] if as_text(x))
            label_parts = list(dict.fromkeys(label_parts))[:3]
            if not label_parts:
                label_parts = [as_text(x) for x in tags[:2] if as_text(x)]
            logic = as_text(item.get("logic_level"))
            if logic:
                label_parts.append(f"逻辑:{logic}")
            rows.append(
                [
                    as_text(item.get("code")),
                    as_text(item.get("name")),
                    as_text(item.get("score")),
                    as_text(item.get("trend")),
                    pct(item.get("pct_change")),
                    as_text(item.get("vol_ratio")),
                    ",".join(label_parts) or "-",
                ]
            )
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "text",
                "text": "**TOP 候选**\n"
                + table(["代码", "名称", "评分", "趋势", "涨跌", "量比", "标签"], rows, max_rows=10),
            }
        )

        ai_rows = []
        for item in candidates[:10]:
            reason = as_text(item.get("ai_reason"))
            if not reason:
                continue
            ai_rows.append(
                f"- **{as_text(item.get('code'))} {as_text(item.get('name'))}** "
                f"({as_text(item.get('ai_confidence'), 'weak')})：{reason}；"
                f"风险：{as_text(item.get('ai_risk'), '-')}"
            )
        if ai_rows:
            blocks.append({"type": "divider"})
            blocks.append({"type": "text", "text": "**AI 入选依据**\n" + "\n".join(ai_rows)})

        tech_rows = []
        for item in candidates:
            concepts = item.get("concepts") if isinstance(item.get("concepts"), list) else []
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            text = ",".join(as_text(x) for x in (concepts[:3] or tags[:3]) if as_text(x))
            if any(word in text for word in ("算力", "芯片", "半导体", "AI", "数据中心", "先进封装")):
                tech_rows.append([as_text(item.get("code")), as_text(item.get("name")), as_text(item.get("score")), text])
        if tech_rows:
            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "text",
                    "text": "**科技 / 主线候选**\n" + table(["代码", "名称", "评分", "方向"], tech_rows, max_rows=8),
                }
            )

    warnings, notes = split_warning_notes(normalize_strings(report.get("warnings")))
    if warnings:
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**说明**\n" + "\n".join(f"- {w}" for w in warnings[:6])})
    data_notes = notes + normalize_strings(report.get("data_notes"))
    if data_notes:
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**数据说明**\n" + "\n".join(f"- {w}" for w in data_notes[:6])})

    source = as_text(report.get("source"))
    if source:
        blocks.append({"type": "context", "text": f"- 来源: {source}"})
    return blocks


def build_blocks(report: dict[str, Any]) -> list[dict[str, str]]:
    if report.get("profile") == "close_review":
        return build_close_review_blocks(report)
    if report.get("profile") == "screening_report":
        return build_screening_report_blocks(report)

    blocks: list[dict[str, str]] = []

    summary = as_text(report.get("summary") or report.get("conclusion"))
    status = report.get("status") if isinstance(report.get("status"), dict) else {}
    status_text = status_line(status)
    if summary or status_text:
        text = []
        if summary:
            text.append(f"**结论**\n{summary}")
        if status_text:
            text.append(f"**状态**\n{status_text}")
        blocks.append({"type": "text", "text": "\n\n".join(text)})

    metrics = metrics_lines(normalize_items(report.get("metrics")))
    if metrics:
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**账户 / 风控**\n" + "\n".join(metrics)})

    actions = action_rows(normalize_items(report.get("actions")))
    if actions:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "text",
                "text": "**操作结果**\n" + table(["代码", "名称", "动作", "结果", "理由"], actions),
            }
        )

    warnings, notes = split_warning_notes(normalize_strings(report.get("warnings") or report.get("exceptions")))
    if warnings:
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**异常 / 数据新鲜度**\n" + "\n".join(f"- {w}" for w in warnings[:8])})

    data_notes = notes + normalize_strings(report.get("data_notes"))
    if data_notes:
        blocks.append({"type": "divider"})
        blocks.append({"type": "text", "text": "**数据说明**\n" + "\n".join(f"- {w}" for w in data_notes[:8])})

    files = file_lines(normalize_items(report.get("files")))
    source = as_text(report.get("source"))
    context = files[:]
    if source:
        context.append(f"- 来源: {source}")
    if context:
        blocks.append({"type": "context", "text": "\n".join(context)})

    if not blocks:
        blocks.append({"type": "text", "text": json.dumps(report, ensure_ascii=False, indent=2)[:3000]})

    return blocks


def _summary_element(text: str, style: dict[str, str]) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "margin": "0px 0px 12px 0px",
        "columns": [{
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "background_style": style["background"],
            "padding": "12px",
            "vertical_spacing": "4px",
            "elements": [
                {"tag": "markdown", "content": text, "text_size": "normal"},
            ],
        }],
    }


def render_presentation(report: dict[str, Any]) -> dict[str, Any]:
    tone = as_text(report.get("tone"), "info")
    if tone not in VALID_TONES:
        tone = "info"
    style = TONE_STYLES[tone]
    body: list[dict[str, Any]] = []

    for block in build_blocks(report):
        block_type = as_text(block.get("type"))
        if block_type == "divider":
            continue
        text = as_text(block.get("text"))
        if not text:
            continue
        if not body:
            body.append(_summary_element(text, style))
        else:
            body.append({
                "tag": "markdown",
                "content": text,
                "text_size": "notation" if block_type == "context" else "normal",
                "margin": "0px 0px 12px 0px",
            })

    if not body:
        body.append(_summary_element("**报告**\n暂无可展示内容", style))

    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "enable_forward": True,
            "summary": {"content": report_title(report)},
        },
        "header": {
            "title": {"tag": "plain_text", "content": report_title(report)},
            "template": style["template"],
            "icon": {"tag": "standard_icon", "token": "chart_colorful"},
            "text_tag_list": [{
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": style["tag"]},
                "color": style["template"],
            }],
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "4px",
            "elements": body,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--presentation-out", required=True)
    args = parser.parse_args()

    report_path = Path(args.report)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise SystemExit("report root must be a JSON object")

    Path(args.presentation_out).write_text(
        json.dumps(render_presentation(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
