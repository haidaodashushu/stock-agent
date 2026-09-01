"""证券时报网/人民财讯新闻适配器。"""
from __future__ import annotations

from datetime import datetime
from typing import List
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import json

from data.contracts import NewsEvent
from data.news_analyzer import analyze_news


class STCNAdapter:
    name = "证券时报网"
    base_url = "https://www.stcn.com"

    def fetch_latest(self, news_type: str = "kx", limit: int = 50) -> List[NewsEvent]:
        """抓取证券时报列表接口。

        news_type 常用：kx=快讯, yw=要闻, gsxw=公司新闻。
        """
        url = f"{self.base_url}/article/list.html?type={news_type}"
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"{self.base_url}/article/list/{news_type}.html",
            "X-Requested-With": "XMLHttpRequest",
        })
        data = urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        js = json.loads(data)
        rows = js.get("data", []) if isinstance(js, dict) else []
        events: List[NewsEvent] = []
        for item in rows[:limit]:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            content = str(item.get("content") or "")
            publish_at = self._parse_time(item)
            url = urljoin(self.base_url, item.get("web_url") or item.get("url") or "")
            tags = []
            for group in item.get("tags") or []:
                if isinstance(group, list):
                    for tag in group:
                        if isinstance(tag, dict) and tag.get("name"):
                            tags.append(str(tag["name"]))
            scored = analyze_news(title, content, category="news")
            all_tags = list(dict.fromkeys(scored.tags + tags[:5]))
            events.append(NewsEvent(
                code="", name="", title=title, content=content,
                source=str(item.get("source") or self.name), publish_at=publish_at,
                url=url, category=f"stcn_{news_type}", sentiment=scored.sentiment,
                score=scored.score, risk_level=scored.risk_level, tags=all_tags,
            ))
        return events

    @staticmethod
    def _parse_time(item: dict) -> str:
        raw = item.get("time") or item.get("show_time") or ""
        try:
            ts = float(raw)
            if ts > 10_000_000_000:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""
