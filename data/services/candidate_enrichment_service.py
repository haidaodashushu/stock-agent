"""Batch-refresh optional evidence for the technical screening pool.

The full market is ranked from local daily bars first.  Only the resulting
technical pool reaches this service, which refreshes announcement/event and
financial-change evidence in bounded IwenCai batches before final ranking.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Dict, Iterable

from data.adapters.iwencai_intelligence_adapter import IwenCaiIntelligenceAdapter
from data.contracts import FinancialFactor
from data.news_analyzer import analyze_news
from data.services.stock_sector_membership_service import replace_stock_memberships
from data.store.sqlite_store import StockStore


NOISE_NEWS_MARKERS = (
    "融资买入", "融资融券余额", "估值：", "市盈率", "后市是否有机会",
    "股东户数", "周评：", "成交额", "主力资金", "龙虎榜数据",
    "概念行情数据", "个股资讯查询", "股市必读", "涨停雷达",
)

NEWS_REFRESH_HOURS = 12
# The enrichment pool is currently capped at 100.  Cover the whole pool on a
# stale run; the refresh ledger prevents the night and next-morning scans from
# issuing the same requests twice within 12 hours.
MAX_NEWS_QUERIES_PER_RUN = 100


def parse_time(value: str) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]} 00:00:00"
    return text[:19]


def recent_enough(publish_at: str, hours: int) -> bool:
    if not publish_at:
        return False
    try:
        value = datetime.strptime(parse_time(publish_at), "%Y-%m-%d %H:%M:%S")
        return value >= datetime.now() - timedelta(hours=hours)
    except (TypeError, ValueError):
        return False


def insert_event(conn, event: Dict) -> bool:
    publish_at = parse_time(event.get("publish_at", ""))
    cur = conn.execute(
        """INSERT INTO news_events
           (code, name, title, content, source, publish_at, url, category,
            sentiment, score, risk_level, tags)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(code,title,publish_at,category) DO UPDATE SET
             name=excluded.name,
             content=CASE WHEN excluded.content!='' THEN excluded.content ELSE news_events.content END,
             source=excluded.source,
             url=excluded.url,
             sentiment=excluded.sentiment,
             score=excluded.score,
             risk_level=excluded.risk_level,
             tags=excluded.tags
           WHERE excluded.content!=news_events.content
              OR excluded.sentiment!=news_events.sentiment
              OR excluded.score!=news_events.score
              OR excluded.risk_level!=news_events.risk_level
              OR excluded.tags!=news_events.tags""",
        (
            str(event["code"]).zfill(6),
            event.get("name", ""),
            event["title"],
            event.get("content", ""),
            event.get("source", ""),
            publish_at,
            event.get("url", ""),
            event.get("category", "news"),
            event.get("sentiment", "neutral"),
            float(event.get("score", 0) or 0),
            event.get("risk_level", "low"),
            json.dumps(event.get("tags", []), ensure_ascii=False),
        ),
    )
    return cur.rowcount > 0


class CandidateEnrichmentService:
    """Refresh IwenCai evidence for an explicit, bounded stock pool."""

    def __init__(
        self,
        *,
        store: StockStore | None = None,
        adapter: IwenCaiIntelligenceAdapter | None = None,
        batch_size: int = 10,
    ):
        self.store = store or StockStore()
        self.adapter = adapter or IwenCaiIntelligenceAdapter()
        self.batch_size = max(1, min(10, int(batch_size)))

    def refresh(
        self,
        codes: Iterable[str],
        *,
        hours: int = 168,
        events_per_stock: int = 3,
    ) -> dict:
        normalized = list(dict.fromkeys(
            str(code).split(".")[0].zfill(6)
            for code in codes
            if str(code or "").strip()
        ))
        result = {
            "codes": len(normalized),
            "batches": 0,
            "events_seen": 0,
            "financials_seen": 0,
            "profiles_seen": 0,
            "concept_memberships": 0,
            "news_queries": 0,
            "news_skipped_fresh": 0,
            "news_deferred": 0,
            "inserted": 0,
            "errors": [],
        }
        if not normalized:
            return result

        names = self._name_map(normalized)
        conn = self.store._get_conn()
        try:
            news_refresh_codes = set(self._news_refresh_codes(
                conn,
                normalized,
                refresh_hours=NEWS_REFRESH_HOURS,
                limit=MAX_NEWS_QUERIES_PER_RUN,
            ))
            stale_count = self._stale_news_refresh_count(
                conn,
                normalized,
                refresh_hours=NEWS_REFRESH_HOURS,
            )
            result["news_skipped_fresh"] = len(normalized) - stale_count
            result["news_deferred"] = max(0, stale_count - len(news_refresh_codes))
            for offset in range(0, len(normalized), self.batch_size):
                batch = normalized[offset:offset + self.batch_size]
                result["batches"] += 1
                requested = set(batch)
                key = ",".join(batch)

                try:
                    profiles = self.adapter.query_stock_profiles(batch)
                    profile_result = self._persist_profiles(conn, profiles, requested)
                    result["profiles_seen"] += profile_result["profiles"]
                    result["concept_memberships"] += profile_result["concept_memberships"]
                    names.update(profile_result["names"])
                except Exception as exc:
                    result["errors"].append(f"profiles[{key}]: {exc}")

                # hithink-event-query returns a stock snapshot without a dated
                # event for these queries.  Search the news index per stock so
                # every accepted record has a real publication timestamp and
                # an unambiguous code mapping.
                for code in batch:
                    if code not in news_refresh_codes:
                        continue
                    name = names.get(code, "")
                    result["news_queries"] += 1
                    try:
                        events = self.adapter.search_news(
                            (
                                f"{code} {name} 最近7天 公告 重大事项 重大合同 中标 订单 "
                                "战略合作 量产 业绩预告 业绩快报 回购 增持 减持 "
                                "重组 限售解禁 质押 监管函 问询函"
                            ),
                            limit=max(5, events_per_stock * 2),
                        )
                        accepted = 0
                        for event in events:
                            publish_at = parse_time(event.publish_at)
                            text = f"{event.title} {event.content}"
                            event_code = str(event.code or "").split(".")[0].zfill(6)
                            if event_code not in {"000000", code}:
                                continue
                            if not event_code.strip("0") and code not in text and (not name or name not in text):
                                continue
                            if not recent_enough(publish_at, hours) or self._is_noise_news(event.title):
                                continue
                            company_title = (
                                event_code == code
                                or self._title_identifies_company(event.title, code, name)
                            )
                            # The title identifies the company, while the body
                            # contains the actual contract/policy/risk terms.
                            scored = analyze_news(event.title, event.content or "", category="news")
                            payload = event.to_dict()
                            payload.update({
                                "code": code,
                                "name": name,
                                "publish_at": publish_at,
                                "category": (
                                    "iwencai_company_news"
                                    if company_title
                                    else "iwencai_related_news"
                                ),
                                "sentiment": scored.sentiment,
                                "score": scored.score,
                                "risk_level": scored.risk_level,
                                "tags": list(dict.fromkeys(scored.tags + ["iwencai_news"])),
                            })
                            result["events_seen"] += 1
                            if insert_event(conn, payload):
                                result["inserted"] += 1
                            accepted += 1
                            if accepted >= events_per_stock:
                                break
                        self._mark_news_refreshed(conn, code, status="ok")
                    except Exception as exc:
                        result["errors"].append(f"news[{code}]: {exc}")
                        self._mark_news_refreshed(conn, code, status="error", detail=str(exc))

                try:
                    factors = self.adapter.query_financials(
                        (
                            f"{key} 最新营业收入同比增长率 净利润同比增长率 "
                            "ROE 毛利率 净利率 资产负债率"
                        ),
                        limit=len(batch),
                    )
                    for factor in factors:
                        code = str(factor.code).split(".")[0].zfill(6)
                        if code not in requested:
                            continue
                        if not self._informative_factor(factor):
                            continue
                        result["financials_seen"] += 1
                        self._upsert_financial(conn, factor)
                except Exception as exc:
                    result["errors"].append(f"financials[{key}]: {exc}")

                conn.commit()
        finally:
            conn.close()
        return result

    @staticmethod
    def _is_noise_news(title: str) -> bool:
        text = str(title or "")
        return any(marker in text for marker in NOISE_NEWS_MARKERS)

    @staticmethod
    def _title_identifies_company(title: str, code: str, name: str) -> bool:
        compact = str(title or "").strip().replace(" ", "")
        normalized_name = str(name or "").strip().replace(" ", "")
        return bool(
            code in compact
            or (
                normalized_name
                and (
                    compact.startswith(normalized_name)
                    or compact.startswith(f"*ST{normalized_name}")
                )
            )
        )

    @staticmethod
    def _stale_news_refresh_count(conn, codes: list[str], *, refresh_hours: int) -> int:
        if not codes:
            return 0
        row = conn.execute(
            f"""SELECT COUNT(*) AS count FROM (
                    SELECT value AS code FROM json_each(?)
                 ) requested
                 LEFT JOIN candidate_intelligence_refresh refresh
                   ON refresh.code=requested.code AND refresh.source='iwencai_news'
                WHERE refresh.refreshed_at IS NULL
                   OR refresh.refreshed_at < datetime('now','localtime', ?)""",
            (json.dumps(codes), f"-{int(refresh_hours)} hours"),
        ).fetchone()
        return int(row["count"] or 0)

    @staticmethod
    def _news_refresh_codes(
        conn,
        codes: list[str],
        *,
        refresh_hours: int,
        limit: int,
    ) -> list[str]:
        if not codes or limit <= 0:
            return []
        rows = conn.execute(
            """SELECT requested.value AS code
                 FROM json_each(?) requested
                 LEFT JOIN candidate_intelligence_refresh refresh
                   ON refresh.code=requested.value AND refresh.source='iwencai_news'
                WHERE refresh.refreshed_at IS NULL
                   OR refresh.refreshed_at < datetime('now','localtime', ?)
                ORDER BY COALESCE(refresh.refreshed_at, ''), requested.key
                LIMIT ?""",
            (json.dumps(codes), f"-{int(refresh_hours)} hours", int(limit)),
        ).fetchall()
        return [str(row["code"]).zfill(6) for row in rows]

    @staticmethod
    def _mark_news_refreshed(conn, code: str, *, status: str, detail: str = "") -> None:
        conn.execute(
            """INSERT INTO candidate_intelligence_refresh
                   (code,source,refreshed_at,status,detail)
               VALUES (?, 'iwencai_news', datetime('now','localtime'), ?, ?)
               ON CONFLICT(code,source) DO UPDATE SET
                 refreshed_at=excluded.refreshed_at,
                 status=excluded.status,
                 detail=excluded.detail""",
            (str(code).zfill(6), status, str(detail)[:300]),
        )

    @staticmethod
    def _persist_profiles(conn, profiles: Iterable[Dict], requested: set[str]) -> dict:
        names: dict[str, str] = {}
        profile_count = 0
        membership_count = 0
        for profile in profiles:
            code = str(profile.get("code") or "").split(".")[0].zfill(6)
            if code not in requested:
                continue
            profile_count += 1
            name = str(profile.get("name") or "").strip()
            industries = [str(value).strip() for value in profile.get("industries", []) if str(value).strip()]
            concepts = [str(value).strip() for value in profile.get("concepts", []) if str(value).strip()]
            if name:
                names[code] = name
            conn.execute(
                """UPDATE stocks
                      SET name=CASE WHEN ?!='' THEN ? ELSE name END,
                          industry=CASE WHEN ?!='' THEN ? ELSE industry END,
                          updated_at=datetime('now','localtime')
                    WHERE code=?""",
                (name, name, industries[-1] if industries else "", industries[-1] if industries else "", code),
            )
            replace_stock_memberships(
                conn,
                code,
                industries=industries,
                concepts=concepts,
            )
            for concept in list(dict.fromkeys(concepts + industries)):
                row = conn.execute("SELECT stocks FROM concepts WHERE name=?", (concept,)).fetchone()
                members = {
                    str(value).strip().zfill(6)
                    for value in str(row["stocks"] if row else "").split(",")
                    if str(value).strip()
                }
                before = len(members)
                members.add(code)
                membership_count += int(len(members) > before)
                conn.execute(
                    """INSERT INTO concepts(name,category,stocks,updated_at)
                       VALUES (?, 'concept', ?, datetime('now','localtime'))
                       ON CONFLICT(name) DO UPDATE SET
                         stocks=excluded.stocks,
                         updated_at=datetime('now','localtime')""",
                    (concept, ",".join(sorted(members))),
                )
        return {
            "profiles": profile_count,
            "concept_memberships": membership_count,
            "names": names,
        }

    @staticmethod
    def _informative_factor(factor: FinancialFactor) -> bool:
        return any(
            abs(float(value or 0)) > 1e-9
            for value in (
                factor.roe,
                factor.roa,
                factor.gross_margin,
                factor.net_margin,
                factor.eps,
                factor.revenue_yoy,
                factor.profit_yoy,
                factor.debt_ratio,
            )
        )

    @staticmethod
    def _upsert_financial(conn, factor: FinancialFactor) -> None:
        conn.execute(
            """INSERT INTO financial_factors
               (code,period,roe,roa,gross_margin,net_margin,eps,
                revenue_yoy,profit_yoy,debt_ratio,source,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
               ON CONFLICT(code,period,source) DO UPDATE SET
                 roe=excluded.roe,
                 roa=excluded.roa,
                 gross_margin=excluded.gross_margin,
                 net_margin=excluded.net_margin,
                 eps=excluded.eps,
                 revenue_yoy=excluded.revenue_yoy,
                 profit_yoy=excluded.profit_yoy,
                 debt_ratio=excluded.debt_ratio,
                 updated_at=datetime('now','localtime')""",
            (
                str(factor.code).split(".")[0].zfill(6),
                str(factor.period or ""),
                factor.roe,
                factor.roa,
                factor.gross_margin,
                factor.net_margin,
                factor.eps,
                factor.revenue_yoy,
                factor.profit_yoy,
                factor.debt_ratio,
                factor.source or "iwencai_intelligence",
            ),
        )

    def _name_map(self, codes: list[str]) -> dict[str, str]:
        placeholders = ",".join("?" for _ in codes)
        conn = self.store._get_conn()
        try:
            return {
                str(row["code"]).zfill(6): str(row["name"] or "")
                for row in conn.execute(
                    f"SELECT code,name FROM stocks WHERE code IN ({placeholders})",
                    codes,
                )
            }
        finally:
            conn.close()
