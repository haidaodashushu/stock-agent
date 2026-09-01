"""Turn persisted news bodies into compact, reproducible decision evidence.

The database remains the source of truth.  Selection and trading receive short
extractive summaries and snippets instead of an unbounded raw article body.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping

from data.news_analyzer import analyze_news


TOPIC_KEYWORDS = (
    "人工智能", "AI", "算力", "数据要素", "工业互联网", "工业5G", "工业软件",
    "半导体", "芯片", "氮化镓", "国产替代", "机器人", "低空经济", "高端制造",
    "网络安全", "储能", "新能源", "光伏", "风电", "军工", "商业航天", "大飞机",
)
EVIDENCE_MARKERS = (
    "印发", "发布", "实施", "支持", "补贴", "试点", "目标", "规划", "意见", "方案",
    "合同", "中标", "订单", "合作", "量产", "回购", "增持", "减持", "预增", "增长",
    "下滑", "亏损", "立案", "监管函", "问询函", "处罚", "终止", "风险提示",
)
POLICY_AUTHORITIES = (
    "国务院", "中央", "工信部", "工业和信息化部", "发改委", "国家发展改革委",
    "财政部", "证监会", "央行", "中国人民银行", "国资委", "商务部", "科技部",
    "市场监管总局", "国家能源局", "住建部", "交通运输部", "农业农村部",
)
POLICY_ACTIONS = (
    "印发", "发布通知", "发布意见", "出台", "实施方案", "行动方案", "规划", "办法",
    "指导意见", "征求意见", "部署", "决定", "政策支持", "试点",
)
MARKET_INTERPRETATION_MARKERS = (
    "证券：", "证券称", "券商", "研报", "机构认为", "分析师", "解读", "点评",
)


def _value(row: Mapping[str, Any], key: str, default: Any = "") -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError):
        value = default
    return default if value is None else value


def parse_tags(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return list(dict.fromkeys(str(item) for item in value if item))
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return list(dict.fromkeys(str(item) for item in parsed if item))
    except (TypeError, ValueError):
        pass
    return list(dict.fromkeys(item for item in str(value).split("|") if item))


def clean_article_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    return [part.strip(" ，,；;") for part in parts if len(part.strip()) >= 8]


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _policy_provenance(title: str, content: str, category: str) -> tuple[str, str, str]:
    if category != "policy_hotspot":
        return "news", "reported", "medium"
    title_has_authority = any(word in title for word in POLICY_AUTHORITIES)
    title_has_action = any(word in title for word in POLICY_ACTIONS)
    full_text = f"{title} {content}"
    body_has_policy = (
        any(word in full_text for word in POLICY_AUTHORITIES)
        and any(word in full_text for word in POLICY_ACTIONS)
    )
    interpreted = any(word in title for word in MARKET_INTERPRETATION_MARKERS)
    if title_has_authority and title_has_action and not interpreted:
        # The upstream news index is still a relay, not the ministry's original
        # web page, so do not label this as a primary official source.
        return "policy_document", "official_relay", "high"
    if interpreted:
        return "policy_interpretation", "market_analysis", "low"
    if body_has_policy:
        return "policy_reference", "secondary_report", "medium"
    return "policy_related", "secondary_report", "low"


def build_news_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build a bounded evidence object from one stored ``news_events`` row."""
    title = clean_article_text(_value(row, "title"))
    content = clean_article_text(_value(row, "content"))
    category = str(_value(row, "category", "news") or "news")
    stored_tags = parse_tags(_value(row, "tags"))
    rescored = analyze_news(title, content, category=category)
    tags = list(dict.fromkeys(stored_tags + rescored.tags))
    full_text = f"{title} {content}"
    topics = [topic for topic in TOPIC_KEYWORDS if topic in full_text]

    keywords = tuple(dict.fromkeys([*topics, *tags, *EVIDENCE_MARKERS]))
    sentences = _sentences(content)
    relevant = [sentence for sentence in sentences if any(word and word in sentence for word in keywords)]
    selected = relevant[:2] or sentences[:2]
    snippets = [_clip(sentence, 140) for sentence in selected]
    summary = _clip(" ".join(selected), 260) if selected else title
    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16] if content else ""
    evidence_type, source_tier, confidence = _policy_provenance(title, content, category)

    # Re-analysis corrects legacy rows that were originally scored from title
    # only.  Keep stronger hot-keyword heat scores because their heat value is
    # an explicit input not recoverable from an empty article body.
    try:
        stored_score = float(_value(row, "score", 0) or 0)
    except (TypeError, ValueError):
        stored_score = 0.0
    use_stored = not content or (
        category == "hot_keyword" and abs(stored_score) > abs(rescored.score)
    )
    score = stored_score if use_stored else rescored.score
    sentiment = str(_value(row, "sentiment", rescored.sentiment)) if use_stored else rescored.sentiment
    risk_level = str(_value(row, "risk_level", rescored.risk_level)) if use_stored else rescored.risk_level
    return {
        "title": _clip(title, 200),
        "summary": summary,
        "evidence_snippets": snippets,
        "content_available": bool(content),
        "analysis_basis": "title_content" if content else "title_only",
        "content_digest": content_digest,
        "source": str(_value(row, "source")),
        "published_at": str(_value(row, "publish_at")),
        "url": str(_value(row, "url")),
        "category": category,
        "sentiment": sentiment,
        "score": round(score, 2),
        "risk": risk_level,
        "tags": tags[:16],
        "mentioned_topics": topics,
        "evidence_type": evidence_type,
        "source_tier": source_tier,
        "confidence": confidence,
    }


def recent_policy_evidence(
    conn: sqlite3.Connection, as_of: str, *, lookback_days: int = 7, limit: int = 10,
) -> list[dict[str, Any]]:
    """Read global policy evidence frozen at the caller's snapshot time."""
    try:
        reference = datetime.fromisoformat(str(as_of))
    except ValueError:
        reference = datetime.now()
    since = (reference - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """SELECT title,content,source,publish_at,url,category,sentiment,
                  score,risk_level,tags,created_at
             FROM news_events
            WHERE code='POLICY' AND category='policy_hotspot'
              AND created_at<=?
              AND COALESCE(NULLIF(publish_at,''),created_at) BETWEEN ? AND ?
            ORDER BY publish_at DESC,id DESC LIMIT ?""",
        (as_of, since, as_of, max(int(limit) * 5, 30)),
    ).fetchall()
    evidence = [build_news_evidence(row) for row in rows]
    type_priority = {
        "policy_document": 0,
        "policy_reference": 1,
        "policy_interpretation": 2,
        "policy_related": 3,
    }
    evidence.sort(key=lambda item: (
        type_priority.get(str(item.get("evidence_type")), 9),
        -float(item.get("score") or 0),
    ))
    return evidence[: int(limit)]


def match_policy_evidence(
    evidence: list[dict[str, Any]], sector_context: Mapping[str, Any], *, limit: int = 3,
) -> list[dict[str, Any]]:
    """Match policies to a stock only through explicit industry/concept text."""
    memberships = [
        str(value).strip()
        for key in ("primary_industry", "industries", "concepts")
        for value in (
            sector_context.get(key, [])
            if isinstance(sector_context.get(key), list)
            else [sector_context.get(key)]
        )
        if str(value or "").strip()
    ]
    matched: list[dict[str, Any]] = []
    for item in evidence:
        if item.get("evidence_type") not in {"policy_document", "policy_reference"}:
            continue
        topics = [str(value) for value in item.get("mentioned_topics", []) if value]
        hits = list(dict.fromkeys(
            topic for topic in topics
            if any(topic == member or (len(topic) >= 3 and topic in member) or (len(member) >= 3 and member in topic)
                   for member in memberships)
        ))
        if hits:
            matched.append({**item, "matched_topics": hits})
        if len(matched) >= limit:
            break
    return matched
