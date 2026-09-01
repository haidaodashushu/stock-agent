#!/usr/bin/env python3
"""持仓新闻监控：抓新闻/热词 → 规则打分 → 入库去重。

第一版只扫当前持仓，避免全市场新闻源过慢。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.fetcher.ak_news import NewsFetcher
from data.news_analyzer import analyze_news
from data.adapters.stcn_adapter import STCNAdapter
from data.store.sqlite_store import StockStore
from data.market_calendar import ensure_market_open


def _parse_time(v: str) -> str:
    if not v:
        return ""
    s = str(v).strip()
    # akshare 返回通常已是 YYYY-MM-DD HH:MM:SS
    return s[:19]


def _recent_enough(publish_at: str, hours: int) -> bool:
    if not publish_at:
        return True
    try:
        dt = datetime.strptime(publish_at[:19], "%Y-%m-%d %H:%M:%S")
        return dt >= datetime.now() - timedelta(hours=hours)
    except Exception:
        return True


def insert_event(conn, event: Dict) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO news_events
           (code, name, title, content, source, publish_at, url, category,
            sentiment, score, risk_level, tags)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event["code"], event.get("name", ""), event["title"], event.get("content", ""),
            event.get("source", ""), event.get("publish_at", ""), event.get("url", ""),
            event.get("category", "news"), event["sentiment"], event["score"],
            event["risk_level"], json.dumps(event.get("tags", []), ensure_ascii=False),
        )
    )
    return cur.rowcount > 0


def scan_news(hours: int = 48, per_stock: int = 10) -> Dict:
    store = StockStore()
    conn = store._get_conn()
    fetcher = NewsFetcher()
    try:
        positions = conn.execute("SELECT code, name FROM portfolio WHERE volume>0 ORDER BY code").fetchall()
        inserted = 0
        events: List[Dict] = []

        for p in positions:
            code, name = p["code"], p["name"]

            # 个股新闻
            df = fetcher.fetch_stock_news(code, num=per_stock)
            for _, row in df.iterrows():
                title = str(row.get("新闻标题", "") or "").strip()
                if not title:
                    continue
                content = str(row.get("新闻内容", "") or "")
                publish_at = _parse_time(row.get("发布时间", ""))
                if not _recent_enough(publish_at, hours):
                    continue
                scored = analyze_news(title, content, category="news")
                event = {
                    "code": code, "name": name, "title": title, "content": content,
                    "source": str(row.get("文章来源", "") or "东方财富"),
                    "publish_at": publish_at,
                    "url": str(row.get("新闻链接", "") or ""),
                    "category": "news",
                    "sentiment": scored.sentiment, "score": scored.score,
                    "risk_level": scored.risk_level, "tags": scored.tags,
                }
                if insert_event(conn, event):
                    inserted += 1
                events.append(event)

            # 热词/概念热度
            hot = fetcher.fetch_stock_hot_keywords(code)
            for _, row in hot.iterrows():
                concept = str(row.get("概念名称", "") or "").strip()
                if not concept:
                    continue
                heat = float(row.get("热度", 0) or 0)
                publish_at = _parse_time(row.get("时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                title = f"热词: {concept} 热度{heat:g}"
                scored = analyze_news(title, "", category="hot_keyword", heat=heat)
                event = {
                    "code": code, "name": name, "title": title, "content": "",
                    "source": "东方财富热词", "publish_at": publish_at, "url": "",
                    "category": "hot_keyword",
                    "sentiment": scored.sentiment, "score": scored.score,
                    "risk_level": scored.risk_level, "tags": scored.tags,
                }
                if insert_event(conn, event):
                    inserted += 1
                events.append(event)

        # 证券时报/人民财讯：抓全量快讯，按当前持仓代码/名称匹配
        try:
            stcn_events = STCNAdapter().fetch_latest("kx", limit=80)
            position_terms = [(p["code"], p["name"] or "") for p in positions]
            for ev in stcn_events:
                text = f"{ev.title} {ev.content}"
                matched = None
                for code, name in position_terms:
                    if code in text or (name and name in text):
                        matched = (code, name)
                        break
                if not matched:
                    continue
                code, name = matched
                if not _recent_enough(ev.publish_at, hours):
                    continue
                event = {
                    "code": code, "name": name, "title": ev.title, "content": ev.content,
                    "source": ev.source or "证券时报网", "publish_at": ev.publish_at,
                    "url": ev.url, "category": ev.category,
                    "sentiment": ev.sentiment, "score": ev.score,
                    "risk_level": ev.risk_level, "tags": ev.tags,
                }
                if insert_event(conn, event):
                    inserted += 1
                events.append(event)
        except Exception as e:
            # 新闻源增强失败不能阻塞主监控
            events.append({
                "code": "", "name": "证券时报", "title": f"证券时报源抓取失败: {e}",
                "category": "source_error", "sentiment": "neutral", "score": 0,
                "risk_level": "low", "tags": ["source_error"],
            })

        conn.commit()
        high = [e for e in events if e["risk_level"] == "high"]
        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "positions": len(positions),
            "events_seen": len(events),
            "inserted": inserted,
            "high": high[:10],
        }
    finally:
        conn.close()


def main() -> int:
    result = scan_news()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if not ensure_market_open(task="持仓新闻扫描"):
        raise SystemExit(0)
    raise SystemExit(main())
