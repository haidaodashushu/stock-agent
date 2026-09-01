#!/usr/bin/env python3
"""候选池新闻/公告/事件扫描。

扫描范围：
- 当前模拟盘持仓
- 自选监控池
- 最新 screen_records TOP 候选
- 十五五核心科技池

结果统一写入 news_events，供逻辑变化评分使用。脚本只做数据入库，
不直接给交易建议。

当前公告/重大事项结论源：同花顺问财 OAPI。巨潮/交易所公告原文以后只作为
二次校验增强，不是当前策略前置依赖。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.fetcher.ak_news import NewsFetcher
from data.fifteen_five_pool import FIFTEEN_FIVE_STOCKS
from data.logic_change import get_logic_change_evidence
from data.market_calendar import ensure_market_open, market_day
from data.news_analyzer import analyze_news
from data.services.intelligence_service import IntelligenceService
from data.services.candidate_enrichment_service import (
    insert_event as _insert_event,
    parse_time as _parse_time,
    recent_enough as _recent_enough,
)
from data.store.sqlite_store import StockStore
from data.watchlist_config import list_items as list_watchlist_items

BLOCKED_CODE_PREFIXES = ("688", "8", "4")


def _name_map(conn) -> Dict[str, str]:
    names: Dict[str, str] = {
        code: info.get("name", "") for code, info in FIFTEEN_FIVE_STOCKS.items()
    }
    for row in conn.execute("SELECT code,name FROM stocks WHERE name IS NOT NULL AND name!=''"):
        names.setdefault(str(row["code"]).zfill(6), row["name"])
    for row in conn.execute("SELECT code,name FROM portfolio WHERE name IS NOT NULL AND name!=''"):
        names[str(row["code"]).zfill(6)] = row["name"]
    for item in list_watchlist_items(enabled_only=False):
        code = str(item.get("code", "")).zfill(6)
        if code and code != "000000" and item.get("name"):
            names[code] = str(item.get("name"))
    return names


def _previous_trading_day(value: datetime) -> str:
    d = value.date() - timedelta(days=1)
    while not market_day(d).is_open:
        d -= timedelta(days=1)
    return d.isoformat()


def _expected_complete_daily_date(now: datetime | None = None) -> str:
    """Return the latest complete trading day that should have daily bars."""
    now = now or datetime.now()
    today = now.date()
    if market_day(today).is_open and now.hour >= 18:
        return today.isoformat()
    return _previous_trading_day(now)


def collect_candidate_codes(max_screen: int = 50, include_fifteen_five: bool = True) -> List[Tuple[str, str]]:
    """Return ordered (code, source) candidates without duplicates."""
    store = StockStore()
    conn = store._get_conn()
    selected: List[Tuple[str, str]] = []
    required_daily_date = _expected_complete_daily_date()
    latest_daily: Dict[str, str] = {}
    active_codes: set[str] = set()
    allow_stale_sources = {"portfolio", "watchlist"}

    def add(code: str, source: str):
        code = str(code or "").zfill(6)
        if not code or code == "000000" or not _is_tradeable(code):
            return
        if source not in allow_stale_sources:
            if active_codes and code not in active_codes:
                return
            if latest_daily.get(code, "") < required_daily_date:
                return
        selected.append((code, source))

    try:
        active_codes = {
            str(row["code"]).zfill(6)
            for row in conn.execute("SELECT code FROM stocks WHERE is_active=1")
        }
        latest_daily = {
            str(row["code"]).zfill(6): str(row["latest_date"] or "")
            for row in conn.execute(
                "SELECT code, MAX(date) AS latest_date FROM daily_prices GROUP BY code"
            )
        }

        for row in conn.execute("SELECT code FROM portfolio WHERE volume>0 ORDER BY code"):
            add(row["code"], "portfolio")

        for item in list_watchlist_items(enabled_only=True):
            add(item.get("code"), "watchlist")

        latest = conn.execute("SELECT MAX(run_date || ' ' || run_time) AS latest FROM screen_records").fetchone()
        latest_key = latest["latest"] if latest else ""
        if latest_key:
            for row in conn.execute(
                """SELECT code FROM screen_records
                   WHERE run_date || ' ' || run_time=?
                   ORDER BY score DESC LIMIT ?""",
                (latest_key, max_screen),
            ):
                add(row["code"], "screen_top")
    finally:
        conn.close()

    if include_fifteen_five:
        for code in FIFTEEN_FIVE_STOCKS:
            add(code, "fifteen_five")

    dedup: List[Tuple[str, str]] = []
    seen = set()
    for code, source in selected:
        if code not in seen:
            dedup.append((code, source))
            seen.add(code)
    return dedup


def _events_from_eastmoney(fetcher: NewsFetcher, code: str, name: str, hours: int, per_stock: int) -> List[Dict]:
    events: List[Dict] = []
    try:
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
            events.append({
                "code": code, "name": name, "title": title, "content": content,
                "source": str(row.get("文章来源", "") or "东方财富"),
                "publish_at": publish_at, "url": str(row.get("新闻链接", "") or ""),
                "category": "news", "sentiment": scored.sentiment, "score": scored.score,
                "risk_level": scored.risk_level, "tags": scored.tags,
            })
    except Exception as e:
        events.append(_source_error(code, name, f"东方财富新闻失败: {e}"))

    try:
        hot = fetcher.fetch_stock_hot_keywords(code)
        for _, row in hot.iterrows():
            concept = str(row.get("概念名称", "") or "").strip()
            if not concept:
                continue
            heat = float(row.get("热度", 0) or 0)
            publish_at = _parse_time(row.get("时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            title = f"热词: {concept} 热度{heat:g}"
            scored = analyze_news(title, "", category="hot_keyword", heat=heat)
            events.append({
                "code": code, "name": name, "title": title, "content": "",
                "source": "东方财富热词", "publish_at": publish_at, "url": "",
                "category": "hot_keyword", "sentiment": scored.sentiment,
                "score": scored.score, "risk_level": scored.risk_level, "tags": scored.tags,
            })
    except Exception as e:
        events.append(_source_error(code, name, f"东方财富热词失败: {e}"))

    return events


def _events_from_iwencai(svc: IntelligenceService, code: str, name: str, concepts: List[str], limit: int, hours: int) -> List[Dict]:
    events: List[Dict] = []
    key = name or code

    for ev in svc.get_stock_events(key, limit=limit):
        d = ev.to_dict()
        d["code"] = code
        d["name"] = name
        d["category"] = "iwencai_event"
        d["publish_at"] = _parse_time(d.get("publish_at", ""))
        if not _recent_enough(d["publish_at"], hours):
            continue
        events.append(d)

    # 行业/政策新闻搜索：对相关概念拼一个保守查询。
    if concepts:
        query = f"{' '.join(concepts[:3])} 政策 订单 景气 试点 规划 {name}".strip()
        for ev in svc.search_news(query, limit=5):
            d = ev.to_dict()
            publish_at = _parse_time(d.get("publish_at", ""))
            if not _recent_enough(publish_at, hours):
                continue
            title = d.get("title", "")
            content = d.get("content", "")
            scored = analyze_news(title, content, category="news")
            d.update({
                "code": code,
                "name": name,
                "category": "iwencai_policy",
                "publish_at": publish_at,
                "sentiment": scored.sentiment,
                "score": scored.score,
                "risk_level": scored.risk_level,
                "tags": list(dict.fromkeys(scored.tags + ["iwencai_policy"])),
            })
            events.append(d)

    return events


def _source_error(code: str, name: str, title: str) -> Dict:
    return {
        "code": code, "name": name, "title": title, "content": "",
        "source": "candidate_intelligence", "publish_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "url": "", "category": "source_error", "sentiment": "neutral",
        "score": 0, "risk_level": "low", "tags": ["source_error"],
    }


def run_scan(
    *,
    max_codes: int = 80,
    max_screen: int = 50,
    hours: int = 72,
    per_stock: int = 5,
    include_fifteen_five: bool = True,
    use_iwencai: bool = True,
) -> Dict:
    store = StockStore()
    conn = store._get_conn()
    names = _name_map(conn)
    candidates = collect_candidate_codes(max_screen=max_screen, include_fifteen_five=include_fifteen_five)[:max_codes]
    fetcher = NewsFetcher()
    svc = IntelligenceService() if use_iwencai else None

    inserted = 0
    seen = 0
    errors = 0
    try:
        for code, source in candidates:
            name = names.get(code) or FIFTEEN_FIVE_STOCKS.get(code, {}).get("name", "") or code
            concepts = FIFTEEN_FIVE_STOCKS.get(code, {}).get("concepts", [])
            events = _events_from_eastmoney(fetcher, code, name, hours=hours, per_stock=per_stock)
            if svc:
                try:
                    events.extend(_events_from_iwencai(svc, code, name, concepts, limit=8, hours=hours))
                except Exception as e:
                    events.append(_source_error(code, name, f"问财事件/财务失败: {e}"))
            for event in events:
                seen += 1
                if event.get("category") == "source_error":
                    errors += 1
                    continue
                if _insert_event(conn, event):
                    inserted += 1
        conn.commit()
    finally:
        conn.close()

    codes = [c for c, _ in candidates]
    logic = get_logic_change_evidence(codes)
    changed = [
        {"code": c, **asdict(ev)}
        for c, ev in sorted(logic.items(), key=lambda x: (x[1].boost, x[1].event_count), reverse=True)
    ][:10]
    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "candidate_count": len(candidates),
        "events_seen": seen,
        "inserted": inserted,
        "source_errors": errors,
        "top_logic_changes": changed,
    }


def _is_tradeable(code: str) -> bool:
    return not str(code).startswith(BLOCKED_CODE_PREFIXES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-codes", type=int, default=80)
    ap.add_argument("--max-screen", type=int, default=50)
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--per-stock", type=int, default=5)
    ap.add_argument("--no-fifteen-five", action="store_true")
    ap.add_argument("--no-iwencai", action="store_true")
    args = ap.parse_args()

    result = run_scan(
        max_codes=args.max_codes,
        max_screen=args.max_screen,
        hours=args.hours,
        per_stock=args.per_stock,
        include_fifteen_five=not args.no_fifteen_five,
        use_iwencai=not args.no_iwencai,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if not ensure_market_open(task="候选池新闻公告扫描"):
        raise SystemExit(0)
    raise SystemExit(main())
