"""Logic-change evidence used by selectors and trading decisions.

"Logic change" must be incremental evidence: policy/industry/company/news,
sector heat, or funds. Static theme membership is only "mainline match".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from data.store.sqlite_store import StockStore
from data.news_evidence import build_news_evidence


@dataclass(frozen=True)
class LogicChangeEvidence:
    code: str
    level: str = "none"  # none / weak / medium / strong
    boost: float = 0.0
    tags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    event_count: int = 0
    max_score: float = 0.0
    penalty: float = 0.0
    risk_tags: list[str] = field(default_factory=list)
    risk_reasons: list[str] = field(default_factory=list)
    tradeable_positive: bool = False


def get_logic_change_evidence(
    codes: Iterable[str],
    *,
    lookback_days: int = 7,
    as_of: str | None = None,
    store: StockStore | None = None,
) -> dict[str, LogicChangeEvidence]:
    """Return recent incremental logic evidence for a batch of stocks.

    Uses the local news_events table only. Network-heavy sector/fund evidence is
    added later in the unified screener as separate factors.
    """
    normalized = [str(c).zfill(6) for c in codes if str(c or "").strip()]
    if not normalized:
        return {}

    try:
        reference = datetime.fromisoformat(str(as_of)) if as_of else datetime.now()
    except ValueError:
        reference = datetime.now()
    reference_text = reference.strftime("%Y-%m-%d %H:%M:%S")
    since = (reference - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join("?" for _ in normalized)
    sql = f"""
        SELECT code,title,content,source,url,category,sentiment,score,risk_level,tags,publish_at
        FROM news_events
        WHERE code IN ({placeholders})
          AND created_at <= ?
          AND (
            CASE
              WHEN length(publish_at)=8 AND publish_at GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
              THEN substr(publish_at,1,4) || '-' || substr(publish_at,5,2) || '-' || substr(publish_at,7,2) || ' 00:00:00'
              ELSE publish_at
            END
          ) BETWEEN ? AND ?
        ORDER BY publish_at DESC
    """

    store = store or StockStore()
    conn = store._get_conn()
    try:
        rows = conn.execute(sql, [*normalized, reference_text, since, reference_text]).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[dict]] = {c: [] for c in normalized}
    for row in rows:
        grouped.setdefault(str(row["code"]).zfill(6), []).append(dict(row))

    result: dict[str, LogicChangeEvidence] = {}
    for code, events in grouped.items():
        ev = _score_events(code, events)
        if ev.level != "none" or ev.penalty < 0:
            result[code] = ev
    return result


def _score_events(code: str, events: list[dict]) -> LogicChangeEvidence:
    if not events:
        return LogicChangeEvidence(code=code)

    # The intelligence job can poll the same keyword/financial period many
    # times.  Treat those rows as one piece of evidence; polling frequency is
    # not additional information.
    events = _dedupe_events(events)

    company_hits = []
    finance_hits = []
    hot_hits = []
    related_hits = []
    strong_hits = []
    negative_hits = []
    max_score = 0.0
    max_confirmed_score = 0.0
    reason_titles: list[str] = []
    risk_titles: list[str] = []

    for ev in events:
        title = str(ev.get("title") or "")
        content = str(ev.get("content") or "")
        category = str(ev.get("category") or "")
        evidence = build_news_evidence(ev)
        score = _safe_float(evidence.get("score"))
        ev = {
            **ev,
            "score": score,
            "sentiment": evidence.get("sentiment"),
            "risk_level": evidence.get("risk"),
        }
        max_score = max(max_score, score)
        sentiment = str(evidence.get("sentiment") or "")
        tags = list(dict.fromkeys(_parse_tags(ev.get("tags")) + list(evidence.get("tags") or [])))
        reason = str(evidence.get("summary") or title)
        search_text = f"{title} {content}"
        is_positive = sentiment == "positive" or score >= 1.0
        is_negative = sentiment == "negative" or score <= -1.0
        if is_negative:
            negative_hits.append(ev)
            risk_titles.append(_compact_title(reason))
        if not is_positive:
            continue

        if category == "hot_keyword":
            heat = _heat_from_title(title)
            # Low-heat concept labels are background metadata, not an
            # incremental catalyst.  Requiring real heat also prevents every
            # concept member from becoming a logic-change candidate.
            if heat >= 100:
                hot_hits.append(ev)
                reason_titles.append(_compact_title(reason))
            continue
        elif category == "iwencai_related_news":
            # Search results that mention the stock only in the body are useful
            # context, but are not company-confirmed catalysts.  They cannot
            # independently create a tradeable positive signal.
            related_hits.append(ev)
            reason_titles.append(_compact_title(reason))
            continue
        elif category == "iwencai_finance":
            # A scraped financial snapshot is useful confirmation, but should
            # not become a strong catalyst by itself before an announcement or
            # another independent source corroborates it.
            finance_hits.append(ev)
            reason_titles.append(_compact_title(reason))
        else:
            company_hits.append(ev)
            reason_titles.append(_compact_title(reason))
            max_confirmed_score = max(max_confirmed_score, score)

        if category not in {"hot_keyword", "iwencai_finance"} and (
            score >= 3.0 or ev.get("risk_level") == "high"
        ):
            strong_hits.append(ev)
        elif category not in {"hot_keyword", "iwencai_finance"} and any(
            k in search_text for k in ("中标", "订单", "合同", "业绩预增", "重组", "注入", "量产", "试点", "政策")
        ):
            strong_hits.append(ev)
        elif category not in {"hot_keyword", "iwencai_finance"} and any(
            k in "".join(tags) for k in ("政策", "订单", "业绩", "产业")
        ) and score >= 1.5:
            strong_hits.append(ev)

    level = "none"
    boost = 0.0
    tags: list[str] = []

    if strong_hits or len(company_hits) >= 2 or max_confirmed_score >= 3.0:
        level = "strong"
        boost = 2.5
        tags.append("强逻辑变化")
    elif company_hits or finance_hits or len(hot_hits) >= 3 or len(related_hits) >= 2:
        level = "medium"
        boost = 1.5
        tags.append("中逻辑变化")
    elif hot_hits or related_hits:
        level = "weak"
        boost = 0.8
        tags.append("弱逻辑变化")

    penalty = 0.0
    risk_tags: list[str] = []
    if any(_safe_float(x.get("score")) <= -3 for x in negative_hits) or len(negative_hits) >= 2:
        penalty = -2.0
        risk_tags.append("重大负面逻辑")
    elif negative_hits:
        penalty = -1.0
        risk_tags.append("负面逻辑")

    if level == "none" and penalty == 0:
        return LogicChangeEvidence(code=code)

    reasons = list(dict.fromkeys(x for x in reason_titles if x))[:3]
    tradeable_positive = bool(
        company_hits
        or hot_hits
        or any(_safe_float(x.get("score")) >= 3.0 for x in finance_hits)
    )
    return LogicChangeEvidence(
        code=code,
        level=level,
        boost=boost,
        tags=tags,
        reasons=reasons,
        event_count=(
            len(company_hits) + len(finance_hits) + len(hot_hits)
            + len(related_hits) + len(negative_hits)
        ),
        max_score=round(max_score, 2),
        penalty=penalty,
        risk_tags=risk_tags,
        risk_reasons=list(dict.fromkeys(x for x in risk_titles if x))[:3],
        tradeable_positive=tradeable_positive,
    )


def _dedupe_events(events: list[dict]) -> list[dict]:
    """Collapse repeated polling rows into distinct evidence items."""
    selected: dict[tuple[str, str], dict] = {}
    for ev in events:
        category = str(ev.get("category") or "news")
        title = str(ev.get("title") or "").strip()
        if not title:
            continue
        key_title = _event_fingerprint_title(category, title)
        key = (category, key_title)
        current = selected.get(key)
        if current is None:
            selected[key] = ev
            continue
        current_rank = (
            abs(_safe_float(current.get("score"))),
            str(current.get("publish_at") or ""),
        )
        candidate_rank = (
            abs(_safe_float(ev.get("score"))),
            str(ev.get("publish_at") or ""),
        )
        if candidate_rank > current_rank:
            selected[key] = ev
    return list(selected.values())


def _event_fingerprint_title(category: str, title: str) -> str:
    compact = re.sub(r"\s+", "", title).lower()
    if category == "hot_keyword":
        # Heat changes during the day; the keyword itself is the evidence.
        compact = re.sub(r"热度[-+]?\d+(?:\.\d+)?", "", compact)
    elif category == "iwencai_finance":
        # One financial observation per report period.
        match = re.search(r"(20\d{6})", compact)
        compact = f"finance:{match.group(1)}" if match else compact
    return compact


def _safe_float(value) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_tags(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(str(raw))
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        pass
    return [str(raw)]


def _heat_from_title(title: str) -> int:
    m = re.search(r"热度\s*(\d+)", title)
    return int(m.group(1)) if m else 0


def _compact_title(title: str) -> str:
    title = title.strip()
    return title[:40]
