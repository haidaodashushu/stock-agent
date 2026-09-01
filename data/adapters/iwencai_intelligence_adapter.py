"""问财信息增强数据适配器。

覆盖板块、财务、事件、新闻等非硬行情数据。上层调用只依赖标准契约，
问财自然语言查询的字段差异在本 adapter 内吸收。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from data.adapters.iwencai_client import IwenCaiClient
from data.contracts import FinancialFactor, NewsEvent, SectorHeat
from data.news_analyzer import analyze_news


class IwenCaiIntelligenceAdapter:
    name = "iwencai_intelligence"

    def __init__(self, client: IwenCaiClient | None = None):
        self.client = client or IwenCaiClient()

    # ------------------------------------------------------------------
    # 原始查询
    # ------------------------------------------------------------------
    def query_raw(self, query: str, *, skill_id: str, page: int = 1, limit: int = 10) -> Dict[str, Any]:
        return self.client.query2data(query, skill_id=skill_id, page=page, limit=limit)

    # ------------------------------------------------------------------
    # 板块 / 行业 / 概念
    # ------------------------------------------------------------------
    def query_sectors(self, query: str, limit: int = 20) -> List[SectorHeat]:
        raw = self.client.query2data(
            query,
            skill_id="hithink-sector-selector",
            limit=limit,
        )
        return [self._parse_sector(x) for x in raw.get("datas", [])]

    def hot_sectors(self, limit: int = 20) -> List[SectorHeat]:
        return self.query_sectors("主力资金净流入排名靠前的概念板块和行业板块", limit=limit)

    def query_stock_profiles(self, codes: List[str]) -> List[Dict[str, Any]]:
        """Return current names and concept/industry memberships in one batch."""
        normalized = list(dict.fromkeys(
            str(code).split(".")[0].zfill(6)
            for code in codes
            if str(code or "").strip()
        ))
        if not normalized:
            return []
        raw = self.client.query2data(
            f"{','.join(normalized)} 所属同花顺概念 所属行业",
            skill_id="hithink-stock-selector",
            limit=len(normalized),
        )
        profiles: List[Dict[str, Any]] = []
        for item in raw.get("datas", []):
            code = self._first_str(item, "股票代码", "代码")
            code = code.split(".")[0].zfill(6) if code else ""
            if code not in normalized:
                continue
            profiles.append({
                "code": code,
                "name": self._first_str(item, "股票简称", "名称"),
                "concepts": self._first_list(item, "所属概念", "概念"),
                "industries": self._first_list(
                    item,
                    "所属同花顺行业",
                    "所属行业",
                    "行业",
                ),
                "listing_board": self._first_str(item, "上市板块"),
                "listing_place": self._first_str(item, "上市地点"),
            })
        return profiles

    # ------------------------------------------------------------------
    # 财务
    # ------------------------------------------------------------------
    def query_financials(self, query: str, limit: int = 20) -> List[FinancialFactor]:
        raw = self.client.query2data(
            query,
            skill_id="hithink-finance-query",
            limit=limit,
        )
        return [self._parse_financial(x) for x in raw.get("datas", [])]

    def stock_financials(self, code_or_name: str) -> List[FinancialFactor]:
        q = f"{code_or_name} 最新营业收入 净利润 ROE 毛利率 净利率 资产负债率 经营现金流"
        return self.query_financials(q, limit=3)

    # ------------------------------------------------------------------
    # 事件 / 风险
    # ------------------------------------------------------------------
    def query_events(self, query: str, limit: int = 20) -> List[NewsEvent]:
        raw = self.client.query2data(
            query,
            skill_id="hithink-event-query",
            limit=limit,
        )
        return [self._parse_event(x, category="event") for x in raw.get("datas", [])]

    def stock_events(self, code_or_name: str, limit: int = 10) -> List[NewsEvent]:
        q = (
            f"{code_or_name} 最近公告 重大事项 重大合同 中标 订单 "
            f"战略合作 量产 业绩预告 业绩快报 机构调研 龙虎榜 "
            f"回购 增持 减持 重组 资产注入 限售解禁 质押 监管函 问询函"
        )
        return self.query_events(q, limit=limit)

    # ------------------------------------------------------------------
    # 新闻
    # ------------------------------------------------------------------
    def search_news(self, query: str, limit: int = 10) -> List[NewsEvent]:
        raw = self.client.search_news(query)
        events: List[NewsEvent] = []
        for item in raw.get("data", [])[:limit]:
            events.append(self._parse_news(item))
        return events

    # ------------------------------------------------------------------
    # 解析工具
    # ------------------------------------------------------------------
    def _parse_sector(self, item: Dict[str, Any]) -> SectorHeat:
        name = self._first_str(
            item,
            "板块名称", "概念名称", "行业名称", "指数简称", "股票简称", "名称",
        )
        sector_type = "concept"
        raw_type = self._first_str(item, "板块类型", "类型", "所属类别")
        if "行业" in raw_type or "申万" in raw_type:
            sector_type = "industry"
        elif "地域" in raw_type:
            sector_type = "region"

        change_pct = self._first_float(item, "最新涨跌幅", "涨跌幅", "板块涨跌幅")
        amount = self._first_float(item, "成交额", "板块成交额")
        volume = self._first_float(item, "成交量", "板块成交量")
        main_net = self._first_float(item, "主力资金流向", "主力净流入", "资金流向")
        leader_code = self._first_str(item, "领涨股代码", "龙头股代码", "股票代码")
        leader_name = self._first_str(item, "领涨股", "龙头股", "股票简称")
        heat_score = change_pct + (main_net / 100_000_000) * 0.5

        return SectorHeat(
            sector_name=name,
            sector_type=sector_type,
            change_pct=change_pct,
            volume=volume,
            amount=amount,
            leader_code=leader_code.split(".")[0] if leader_code else "",
            leader_name=leader_name,
            heat_score=round(heat_score, 2),
            source=self.name,
        )

    def _parse_financial(self, item: Dict[str, Any]) -> FinancialFactor:
        code = self._first_str(item, "股票代码", "代码")
        period = self._extract_period(item) or datetime.now().strftime("%Y")
        return FinancialFactor(
            code=code.split(".")[0] if code else "",
            period=period,
            roe=self._first_float(item, "ROE", "净资产收益率", "加权净资产收益率"),
            roa=self._first_float(item, "ROA", "总资产收益率"),
            gross_margin=self._first_float(item, "毛利率", "销售毛利率"),
            net_margin=self._first_float(item, "净利率", "销售净利率"),
            eps=self._first_float(item, "EPS", "每股收益"),
            revenue_yoy=self._first_float(item, "营业收入同比增长率", "营收同比", "营业收入增长率"),
            profit_yoy=self._first_float(item, "净利润同比增长率", "净利润增长率", "归母净利润同比增长率"),
            debt_ratio=self._first_float(item, "资产负债率", "负债率"),
            source=self.name,
        )

    def _parse_event(self, item: Dict[str, Any], category: str = "event") -> NewsEvent:
        code = self._first_str(item, "股票代码", "代码")
        name = self._first_str(item, "股票简称", "名称")
        title = self._first_str(
            item,
            "事件标题", "公告标题", "标题", "事件名称", "摘要",
        ) or self._compact_item(item)
        publish_at = self._first_str(item, "公告日期", "发布日期", "发生日期", "日期", "时间")
        risk_level = "low"
        text = f"{title} {self._compact_item(item)}"
        scored = analyze_news(title, text, category=category)
        if any(x in text for x in ("监管函", "问询函", "警示", "立案", "处罚", "质押", "解禁", "减持")):
            risk_level = "high"
        elif any(x in text for x in ("业绩预减", "亏损", "下滑")):
            risk_level = "medium"
        else:
            risk_level = scored.risk_level
        return NewsEvent(
            code=code.split(".")[0] if code else "",
            name=name,
            title=title[:200],
            content=self._compact_item(item),
            source=self.name,
            publish_at=publish_at,
            category=category,
            sentiment=scored.sentiment,
            score=scored.score,
            risk_level=risk_level,
            tags=list(dict.fromkeys(scored.tags + [category, "iwencai"])),
        )

    def _parse_news(self, item: Dict[str, Any]) -> NewsEvent:
        return NewsEvent(
            code="",
            name="",
            title=str(item.get("title", ""))[:200],
            content=str(item.get("summary", "") or item.get("source_original", "")),
            source="iwencai_news",
            publish_at=str(item.get("publish_date", "") or item.get("publish_time", "")),
            url=str(item.get("url", "")),
            category="news",
            sentiment="neutral",
            tags=["news", "iwencai"],
        )

    @staticmethod
    def _first_str(item: Dict[str, Any], *keys: str) -> str:
        for key in keys:
            if key in item and item[key] not in (None, ""):
                return str(item[key])
        for k, v in item.items():
            if v in (None, ""):
                continue
            for key in keys:
                if str(k).startswith(key) or key in str(k):
                    return str(v)
        return ""

    @staticmethod
    def _first_list(item: Dict[str, Any], *keys: str) -> List[str]:
        value: Any = None
        for key in keys:
            if key in item and item[key] not in (None, ""):
                value = item[key]
                break
        if value is None:
            for raw_key, raw_value in item.items():
                if raw_value in (None, ""):
                    continue
                if any(str(raw_key).startswith(key) or key in str(raw_key) for key in keys):
                    value = raw_value
                    break
        if isinstance(value, list):
            values = value
        elif isinstance(value, str):
            text = value.replace("；", ",").replace("、", ",").replace("|", ",")
            values = text.split(",")
        else:
            values = []
        return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))

    @staticmethod
    def _first_float(item: Dict[str, Any], *keys: str) -> float:
        for key in keys:
            if key in item and item[key] not in (None, ""):
                try:
                    return float(str(item[key]).replace("%", ""))
                except Exception:
                    pass
        for k, v in item.items():
            if v in (None, ""):
                continue
            for key in keys:
                if str(k).startswith(key) or key in str(k):
                    try:
                        return float(str(v).replace("%", ""))
                    except Exception:
                        pass
        return 0.0

    @staticmethod
    def _extract_period(item: Dict[str, Any]) -> str:
        for k in item.keys():
            s = str(k)
            if "[" in s and "]" in s:
                return s.split("[", 1)[1].split("]", 1)[0]
        return ""

    @staticmethod
    def _compact_item(item: Dict[str, Any], max_len: int = 500) -> str:
        text = "；".join(f"{k}: {v}" for k, v in item.items() if v not in (None, ""))
        return text[:max_len]
