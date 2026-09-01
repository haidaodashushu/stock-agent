#!/usr/bin/env python3
"""Append a non-operative cultural reading after a live trade suggestion.

This module is deliberately downstream of live-intent creation.  It reads the
immutable execution result and cannot alter the decision, account, or intent.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.agent_runtime import CodexCliProvider  # noqa: E402


LIUREN_ENGINE = ROOT / "skills" / "daliuren" / "scripts" / "liuren.py"
DISCLAIMER = (
    "术数属传统文化范畴，无科学依据，仅供娱乐与文化参考；"
    "不构成投资建议，也不得据此改变上方操作建议。"
)
FORTUNE_BEGIN = "FORTUNE_BEGIN"
FORTUNE_END = "FORTUNE_END"
PLAIN_BEGIN = "PLAIN_BEGIN"
PLAIN_END = "PLAIN_END"
DETAIL_BEGIN = "DETAIL_BEGIN"
DETAIL_END = "DETAIL_END"
SUITABILITY_LEVELS = ("偏适合", "中性", "偏不适合")
FORBIDDEN_OPERATIVE_PHRASES = (
    "建议买入",
    "建议卖出",
    "宜买",
    "宜卖",
    "加仓",
    "减仓",
    "止损",
    "止盈",
    "追涨",
    "抄底",
    "立即执行",
    "暂缓执行",
    "不宜执行",
)


@dataclass(frozen=True)
class FortuneContext:
    as_of: str
    actions: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class FortuneReading:
    suitability: str
    plain: str
    detail: str


def _minute_timestamp(value: str) -> str:
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    raise ValueError(f"invalid fortune as_of: {value!r}")


def load_context(result_path: Path, fallback_as_of: str = "") -> FortuneContext | None:
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if payload.get("mode") != "live" or payload.get("dry_run"):
        return None
    execution = payload.get("execution")
    if not isinstance(execution, dict) or execution.get("error"):
        return None
    rows = execution.get("results")
    if not isinstance(rows, list):
        return None
    actions = tuple(
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("created_intent") is True
        and row.get("action") in {"buy", "sell"}
    )
    if not actions:
        return None
    as_of = execution.get("as_of") or fallback_as_of
    return FortuneContext(as_of=_minute_timestamp(str(as_of)), actions=actions)


def generate_chart(as_of: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(LIUREN_ENGINE), "--datetime", as_of, "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    chart = json.loads(completed.stdout)
    if not isinstance(chart, dict) or not chart.get("三传"):
        raise ValueError("daliuren engine returned an incomplete chart")
    return chart


def build_prompt(context: FortuneContext, chart: dict[str, Any]) -> str:
    action_lines = []
    action_names = {"buy": "买入", "sell": "卖出"}
    for row in context.actions:
        action_lines.append(
            "- {action} {code} {name}，{volume}股，参考价{price:.2f}元".format(
                action=action_names[str(row["action"])],
                code=str(row.get("code") or ""),
                name=str(row.get("name") or ""),
                volume=int(row.get("volume") or 0),
                price=float(row.get("price") or 0),
            )
        )
    actions = "\n".join(action_lines)
    chart_json = json.dumps(chart, ensure_ascii=False, separators=(",", ":"))
    return f"""请依据下方课盘和随附的 daliuren 断课指南，对下面这批已经生成完毕的实盘建议做一次传统文化附注。

起课时间固定为 {context.as_of}（Asia/Shanghai），排盘已由 Skill 自带脚本生成：
{chart_json}

已完成、不可被本次解读修改的建议：
{actions}

严格边界：
1. 这不是第二次投资分析，不查询也不使用行情、资金、新闻、账户或其他证券数据。
2. 不评价原建议是否正确，不改变方向、价格、数量、有效期或是否执行。
3. 不输出买入、卖出、加减仓、追涨、止损、执行或延后的指令。
4. 只依据课盘和 references/duanke-guide.md，不把“适合度”包装成科学或金融判断。
5. 白话部分先从“偏适合 / 中性 / 偏不适合”三档中选择一档，再用普通人能读懂的语言解释开头、过程和结果；不使用六亲、三传、旬空、旺衰等术语。
6. 专业部分沿用六壬术语，按求财用神简短说明课体总象、初中末传和运势定调，控制在 300 个中文字符以内。
7. 不重复免责声明，不输出排盘原文，不使用 Markdown 标题。
8. 最终输出必须严格使用下面的结构，标记外不要输出任何内容：
{FORTUNE_BEGIN}
{PLAIN_BEGIN}
适配度：偏适合 / 中性 / 偏不适合（三选一，只保留一个）
解释：一到三句白话
{PLAIN_END}
{DETAIL_BEGIN}
专业课象原文
{DETAIL_END}
{FORTUNE_END}
"""


def _clean_disclaimer(text: str) -> str:
    cleaned = text.strip()
    for marker in (
        "术数属传统文化范畴",
        "术数无科学依据",
        "以上仅供文化参考",
        "以上仅供传统文化参考",
    ):
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[0].rstrip("；;。 \n") + "。"
    return cleaned


def _extract_section(value: str, begin: str, end: str) -> str:
    start = value.find(begin)
    finish = value.find(end, start + len(begin))
    if start < 0 or finish < 0:
        raise ValueError(f"daliuren interpretation is missing {begin}/{end}")
    return value[start + len(begin):finish].strip()


def clean_reading(raw_response: str) -> FortuneReading:
    start = raw_response.rfind(FORTUNE_BEGIN)
    end = raw_response.find(FORTUNE_END, start + len(FORTUNE_BEGIN))
    if start < 0 or end < 0:
        raise ValueError("daliuren interpretation did not return the required markers")
    reading = raw_response[start + len(FORTUNE_BEGIN):end].strip()
    plain_section = _extract_section(reading, PLAIN_BEGIN, PLAIN_END)
    detail = _clean_disclaimer(_extract_section(reading, DETAIL_BEGIN, DETAIL_END))
    plain_lines = [line.strip() for line in plain_section.splitlines() if line.strip()]
    if not plain_lines:
        raise ValueError("daliuren interpretation contained no cultural reading")
    suitability_line = plain_lines.pop(0).replace("**", "")
    if "：" in suitability_line:
        suitability = suitability_line.split("：", 1)[1].strip()
    elif ":" in suitability_line:
        suitability = suitability_line.split(":", 1)[1].strip()
    else:
        suitability = ""
    if suitability not in SUITABILITY_LEVELS:
        raise ValueError("daliuren interpretation returned an invalid suitability level")
    plain = _clean_disclaimer("\n".join(plain_lines))
    if plain.startswith("解释：") or plain.startswith("解释:"):
        plain = plain[3:].strip()
    if not plain or not detail:
        raise ValueError("daliuren interpretation contained an incomplete cultural reading")
    combined = f"{plain}\n{detail}"
    if any(phrase in combined for phrase in FORBIDDEN_OPERATIVE_PHRASES):
        raise ValueError("daliuren interpretation crossed the non-operative boundary")
    return FortuneReading(
        suitability=suitability,
        plain=plain[:500],
        detail=detail[:900],
    )


def ask_agent(prompt: str, timeout_seconds: int, codex_bin: str = "") -> FortuneReading:
    guide = (ROOT / "skills/daliuren/references/duanke-guide.md").read_text(encoding="utf-8")
    full_prompt = (
        prompt
        + "\n\n以下是本次唯一允许使用的断课指南；无需读取任何文件或调用工具：\n\n"
        + guide
    )
    with tempfile.TemporaryDirectory(prefix="stock-fortune-agent-") as tmp:
        outcome = CodexCliProvider(
            executable=codex_bin or None, timeout_seconds=timeout_seconds,
        ).run(prompt=full_prompt, workspace=ROOT, run_dir=Path(tmp))
    if outcome.returncode:
        raise RuntimeError(outcome.stderr.strip() or "daliuren agent failed")
    response = outcome.final_message.strip()
    if not response:
        raise ValueError("daliuren interpretation returned no text")
    return clean_reading(response)


def _action_subject(context: FortuneContext) -> str:
    actions = {str(row.get("action") or "") for row in context.actions}
    if actions == {"buy"}:
        return "本次买入"
    if actions == {"sell"}:
        return "本次卖出"
    return "本批买卖操作"


def _quote(text: str) -> str:
    return "\n".join(">" if not line else f"> {line}" for line in text.splitlines())


def render_block(context: FortuneContext, reading: FortuneReading) -> str:
    quoted_detail = _quote(f"**课象原文**\n\n{reading.detail}")
    return (
        "\n🔮 **传统文化附注（不影响实盘建议）**\n\n"
        f"起课时间：{context.as_of}（Asia/Shanghai）\n\n"
        f"**白话结论**：仅从传统课象看，{_action_subject(context)}"
        f"的适配度为 **{reading.suitability}**。\n\n"
        f"{reading.plain}\n\n"
        f"{quoted_detail}\n\n"
        f"> {DISCLAIMER}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--as-of", default="")
    parser.add_argument("--codex-bin", default="")
    # Deprecated compatibility flags keep a rollback on this branch parseable;
    # they are intentionally ignored by the Codex implementation.
    parser.add_argument("--hermes-bin", default="", help=argparse.SUPPRESS)
    parser.add_argument("--profile", default="", help=argparse.SUPPRESS)
    parser.add_argument("--skill", default="daliuren", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    context = load_context(args.result, args.as_of)
    if context is None:
        return 3
    chart = generate_chart(context.as_of)
    reading = ask_agent(
        build_prompt(context, chart), args.timeout, args.codex_bin,
    )
    block = render_block(context, reading)
    if args.output:
        args.output.write_text(block + "\n", encoding="utf-8")
    print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
