#!/usr/bin/env python3
"""Prepare annual/semiannual report text for LLM scoring.

The script downloads the latest CNINFO report PDF, extracts text around MD&A,
and writes a prompt file whose expected output can be imported by
upsert_fundamental_llm_score.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.store.sqlite_store import StockStore


DEFAULT_OUT_DIR = Path("data/reports/fundamental_llm")


def collect_latest_screen_codes(limit: int) -> list[str]:
    store = StockStore()
    conn = store._get_conn()
    try:
        latest = conn.execute("SELECT MAX(run_date || ' ' || run_time) AS latest FROM screen_records").fetchone()
        latest_key = latest["latest"] if latest else ""
        if not latest_key:
            return []
        rows = conn.execute(
            """SELECT code FROM screen_records
               WHERE run_date || ' ' || run_time=?
               ORDER BY score DESC LIMIT ?""",
            (latest_key, limit),
        ).fetchall()
        return [str(r["code"]).zfill(6) for r in rows]
    finally:
        conn.close()


def find_latest_report(code: str, start_date: str, end_date: str, categories: list[str]) -> dict | None:
    import akshare as ak

    frames = []
    for category in categories:
        try:
            df = ak.stock_zh_a_disclosure_report_cninfo(
                symbol=code,
                market="沪深京",
                category=category,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as e:
            print(f"⚠️ {code} {category} 查询失败: {e}", file=sys.stderr)
            continue
        if df is not None and not df.empty:
            frames.extend(df.to_dict("records"))

    reports = []
    for row in frames:
        title = str(row.get("公告标题", "") or "")
        if "摘要" in title or "英文" in title:
            continue
        link = str(row.get("公告链接", "") or "")
        aid = _announcement_id(link)
        date = str(row.get("公告时间", "") or "")[:10]
        if not aid or not date:
            continue
        reports.append({
            "code": str(row.get("代码", code)).zfill(6),
            "name": str(row.get("简称", "") or ""),
            "title": title,
            "announcement_id": aid,
            "announcement_date": date,
            "period": infer_period(title),
            "report_type": infer_report_type(title),
            "detail_url": link,
            "pdf_url": f"http://static.cninfo.com.cn/finalpage/{date}/{aid}.PDF",
        })
    reports.sort(key=lambda x: (x["announcement_date"], x["period"], x["announcement_id"]), reverse=True)
    return reports[0] if reports else None


def download_pdf(url: str, path: Path) -> None:
    import requests

    path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    if "pdf" not in r.headers.get("content-type", "").lower() and not r.content.startswith(b"%PDF"):
        raise ValueError(f"not a PDF response: {r.headers.get('content-type')}")
    path.write_bytes(r.content)


def extract_pdf_text(path: Path, max_pages: int = 160) -> str:
    from PyPDF2 import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts)


def extract_mda_text(text: str, max_chars: int) -> str:
    compact = re.sub(r"[ \t]+", " ", text)
    starts = [
        "管理层讨论与分析",
        "经营情况讨论与分析",
        "董事会报告",
        "Management Discussion and Analysis",
    ]
    start_positions = [compact.find(s) for s in starts if compact.find(s) >= 0]
    start = min(start_positions) if start_positions else 0
    end_candidates = []
    for marker in ("公司治理", "重要事项", "环境和社会责任", "股份变动", "财务报告"):
        pos = compact.find(marker, start + 20)
        if pos > start:
            end_candidates.append(pos)
    end = min(end_candidates) if end_candidates else len(compact)
    return compact[start:end][:max_chars].strip()


def write_prompt(report: dict, mda_text: str, path: Path) -> None:
    prompt = {
        "task": "请只基于给定财报/MD&A文本，输出可导入量化系统的JSON。不要输出解释。",
        "stock": {
            "code": report["code"],
            "name": report["name"],
            "period": report["period"],
            "report_type": report["report_type"],
            "report_date": report["announcement_date"],
            "source": "cninfo",
        },
        "scoring_schema": {
            "industry_demand_score": "1-5，报告期行业需求/景气，5=明确上行且有数据支持，2.5=无信息",
            "future_demand_score": "1-5，管理层对未来行业需求判断，低分表示下行/谨慎",
            "product_penetration_score": "1-5，产品渗透率/市场空间变化；无明确文本给2.5",
            "strategy_score": "0-3，战略规划合理性",
            "candor_score": "0或1，盈利归因和风险披露是否坦诚均衡",
            "composite_score": "0-5，综合评分；不要因为单一亮点给满分",
            "confidence": "0-1，文本证据置信度",
        },
        "required_output_json": {
            "code": report["code"],
            "name": report["name"],
            "period": report["period"],
            "report_type": report["report_type"],
            "report_date": report["announcement_date"],
            "industry_demand_score": 2.5,
            "future_demand_score": 2.5,
            "product_penetration_score": 2.5,
            "strategy_score": 1.5,
            "candor_score": 0.5,
            "composite_score": 2.5,
            "confidence": 0.0,
            "summary": "一句话说明",
            "evidence": {
                "industry_demand": ["引用原文片段"],
                "future_demand": ["引用原文片段"],
                "risks": ["引用原文片段"],
            },
            "source": "cninfo",
            "model": "填入模型名",
        },
        "report_text": mda_text,
    }
    path.write_text(json.dumps(prompt, ensure_ascii=False, indent=2), encoding="utf-8")


def infer_period(title: str) -> str:
    m = re.search(r"(20\d{2})年(年度|半年度|第一季度|第三季度)", title)
    if not m:
        return ""
    year, kind = m.groups()
    return {
        "年度": f"{year}A",
        "半年度": f"{year}H1",
        "第一季度": f"{year}Q1",
        "第三季度": f"{year}Q3",
    }.get(kind, year)


def infer_report_type(title: str) -> str:
    if "半年度" in title:
        return "semiannual"
    if "季度" in title:
        return "quarterly"
    if "年度" in title or "年报" in title:
        return "annual"
    return ""


def _announcement_id(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    return (query.get("announcementId") or [""])[0]


def _dedup(codes: Iterable[str]) -> list[str]:
    seen = set()
    out = []
    for code in codes:
        code = str(code).strip().zfill(6)
        if code and code != "000000" and code not in seen:
            out.append(code)
            seen.add(code)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="准备 LLM 财报/MD&A 评分材料")
    p.add_argument("--codes", default="", help="逗号分隔股票代码")
    p.add_argument("--from-latest-screen", action="store_true", help="使用最新盘前候选")
    p.add_argument("--limit", type=int, default=10, help="latest screen 数量")
    p.add_argument("--start-date", default="20250101")
    p.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"))
    p.add_argument("--categories", default="年报,半年报")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--max-text-chars", type=int, default=24000)
    args = p.parse_args()

    codes = []
    if args.codes:
        codes.extend(args.codes.split(","))
    if args.from_latest_screen:
        codes.extend(collect_latest_screen_codes(args.limit))
    codes = _dedup(codes)
    if not codes:
        raise SystemExit("没有待处理代码；使用 --codes 或 --from-latest-screen")

    out_dir = Path(args.out_dir)
    categories = [x.strip() for x in args.categories.split(",") if x.strip()]
    manifest = []

    for code in codes:
        report = find_latest_report(code, args.start_date, args.end_date, categories)
        if not report:
            print(f"⚠️ {code} 未找到年报/半年报")
            continue
        stem = f"{report['code']}_{report['period'] or report['announcement_date']}_{report['announcement_id']}"
        pdf_path = out_dir / f"{stem}.pdf"
        text_path = out_dir / f"{stem}.mda.txt"
        prompt_path = out_dir / f"{stem}.prompt.json"
        download_pdf(report["pdf_url"], pdf_path)
        text = extract_mda_text(extract_pdf_text(pdf_path), args.max_text_chars)
        text_path.write_text(text, encoding="utf-8")
        write_prompt(report, text, prompt_path)
        report.update({
            "pdf_path": str(pdf_path),
            "mda_text_path": str(text_path),
            "prompt_path": str(prompt_path),
            "mda_chars": len(text),
        })
        manifest.append(report)
        print(f"✅ {code} {report['name']} {report['period']} -> {prompt_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成：{len(manifest)}/{len(codes)}，manifest={out_dir / 'manifest.json'}")
    print("下一步：把 *.prompt.json 交给 LLM，保存输出 JSON 后用 scripts/upsert_fundamental_llm_score.py --json 导入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
