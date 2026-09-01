#!/usr/bin/env python3
"""政策热点雷达：用问财新闻搜索主动扫描产业政策。

不同于候选池新闻扫描，本脚本不围绕单只股票查询，而是围绕十五五科技
主线、部委联合发文和产业政策关键词查询，避免顶层政策没有股票名时漏采。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, Iterable, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.news_analyzer import analyze_news
from data.services.intelligence_service import IntelligenceService
from data.store.sqlite_store import StockStore


POLICY_CODE = "POLICY"
POLICY_NAME = "政策热点"

DEFAULT_QUERIES = [
    "今日 A股 热点政策 工信部 发改委 证监会 十五五",
    "工信部 工业互联网 高质量发展 八部门",
    "推动工业互联网高质量发展 实施意见 八部门",
    "工业互联网 高质量发展 行动方案 2026 2028",
    "十五五 人工智能 算力 工业互联网 政策",
    "半导体 国产替代 高端制造 政策 工信部",
    "工业软件 数据要素 网络安全 政策",
    "低空经济 机器人 专精特新 政策",
]

IMPORTANT_KEYWORDS = (
    "八部门", "工信部", "发改委", "证监会", "国资委", "央行",
    "行动方案", "实施意见", "印发", "联合发文", "政策", "十五五",
    "工业互联网", "工业5G", "人工智能", "算力", "半导体", "工业软件",
    "数据要素", "网络安全", "高端制造", "低空经济", "机器人",
)


def _parse_time(value: str) -> str:
    if not value:
        return ""
    s = str(value).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return f"{s} 00:00:00"
    return s[:19]


def _recent_enough(publish_at: str, hours: int) -> bool:
    if not publish_at:
        return True
    try:
        dt = datetime.strptime(publish_at[:19], "%Y-%m-%d %H:%M:%S")
        return dt >= datetime.now() - timedelta(hours=hours)
    except Exception:
        return True


def _policy_tags(text: str, scored_tags: Iterable[str]) -> list[str]:
    tags = list(scored_tags)
    for kw in IMPORTANT_KEYWORDS:
        if kw in text:
            tags.append(kw)
    tags.append("policy_hotspot")
    return list(dict.fromkeys(tags))


def _insert_event(conn, event: Dict) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO news_events
           (code, name, title, content, source, publish_at, url, category,
            sentiment, score, risk_level, tags)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event["code"], event.get("name", ""), event["title"], event.get("content", ""),
            event.get("source", ""), event.get("publish_at", ""), event.get("url", ""),
            event.get("category", "policy_hotspot"), event.get("sentiment", "neutral"),
            float(event.get("score", 0) or 0), event.get("risk_level", "low"),
            json.dumps(event.get("tags", []), ensure_ascii=False),
        ),
    )
    return cur.rowcount > 0


def scan_policy_hotspots(
    *,
    queries: list[str] | None = None,
    hours: int = 18,
    limit_per_query: int = 8,
) -> Dict:
    svc = IntelligenceService()
    store = StockStore()
    conn = store._get_conn()
    queries = queries or DEFAULT_QUERIES

    seen = 0
    inserted = 0
    errors: list[str] = []
    high: list[Dict] = []
    dedup: set[tuple[str, str]] = set()

    try:
        for query in queries:
            try:
                events = svc.search_news(query, limit=limit_per_query)
            except Exception as e:
                errors.append(f"{query}: {e}")
                continue
            for ev in events:
                title = (ev.title or "").strip()
                if not title:
                    continue
                publish_at = _parse_time(ev.publish_at)
                if not _recent_enough(publish_at, hours):
                    continue
                key = (title, publish_at)
                if key in dedup:
                    continue
                dedup.add(key)

                text = f"{title} {ev.content or ''}"
                if not any(kw in text for kw in IMPORTANT_KEYWORDS):
                    continue

                scored = analyze_news(title, ev.content or "", category="policy_hotspot")
                score = min(5.0, scored.score + 0.8)
                risk_level = "high" if score >= 3 else ("medium" if score >= 1.5 else "low")
                event = {
                    "code": POLICY_CODE,
                    "name": POLICY_NAME,
                    "title": title[:200],
                    "content": ev.content or "",
                    "source": ev.source or "iwencai_news",
                    "publish_at": publish_at,
                    "url": ev.url,
                    "category": "policy_hotspot",
                    "sentiment": "positive" if score >= 1 else scored.sentiment,
                    "score": round(score, 2),
                    "risk_level": risk_level,
                    "tags": _policy_tags(text, scored.tags),
                    "query": query,
                }
                seen += 1
                if _insert_event(conn, event):
                    inserted += 1
                if risk_level == "high":
                    high.append(event)
        conn.commit()
    finally:
        conn.close()

    high.sort(key=lambda x: (x.get("publish_at", ""), x.get("score", 0)), reverse=True)
    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "queries": len(queries),
        "events_seen": seen,
        "inserted": inserted,
        "source_errors": len(errors),
        "errors": errors[:5],
        "high": [
            {
                "title": e["title"],
                "publish_at": e["publish_at"],
                "source": e["source"],
                "score": e["score"],
                "tags": e["tags"][:8],
                "url": e["url"],
            }
            for e in high[:10]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan policy hotspot news via iwencai news-search")
    parser.add_argument("--hours", type=int, default=18)
    parser.add_argument("--limit-per-query", type=int, default=8)
    parser.add_argument("--query", action="append", help="Override default query; repeatable")
    args = parser.parse_args()

    result = scan_policy_hotspots(
        queries=args.query,
        hours=args.hours,
        limit_per_query=args.limit_per_query,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
