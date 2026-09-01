"""统一新闻服务：标准化新闻事件并打分。"""
from __future__ import annotations

from typing import List

from data.contracts import NewsEvent
from data.fetcher.ak_news import NewsFetcher
from data.news_analyzer import analyze_news


class NewsService:
    def __init__(self):
        self.fetcher = NewsFetcher()

    def get_stock_events(self, code: str, name: str = "", limit: int = 10) -> List[NewsEvent]:
        events: List[NewsEvent] = []
        df = self.fetcher.fetch_stock_news(code, num=limit)
        for _, row in df.iterrows():
            title = str(row.get("新闻标题", "") or "").strip()
            if not title:
                continue
            content = str(row.get("新闻内容", "") or "")
            scored = analyze_news(title, content, category="news")
            events.append(NewsEvent(
                code=str(code).zfill(6), name=name, title=title, content=content,
                source=str(row.get("文章来源", "") or "东方财富"),
                publish_at=str(row.get("发布时间", "") or "")[:19],
                url=str(row.get("新闻链接", "") or ""), category="news",
                sentiment=scored.sentiment, score=scored.score,
                risk_level=scored.risk_level, tags=scored.tags,
            ))
        hot = self.fetcher.fetch_stock_hot_keywords(code)
        for _, row in hot.iterrows():
            concept = str(row.get("概念名称", "") or "").strip()
            if not concept:
                continue
            heat = float(row.get("热度", 0) or 0)
            title = f"热词: {concept} 热度{heat:g}"
            scored = analyze_news(title, "", category="hot_keyword", heat=heat)
            events.append(NewsEvent(
                code=str(code).zfill(6), name=name, title=title, source="东方财富热词",
                publish_at=str(row.get("时间", "") or "")[:19], category="hot_keyword",
                sentiment=scored.sentiment, score=scored.score,
                risk_level=scored.risk_level, tags=scored.tags,
            ))
        return events
