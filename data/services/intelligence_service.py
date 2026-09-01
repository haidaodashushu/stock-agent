"""信息增强数据服务。

用于接入问财等智能数据源：板块、财报、新闻、事件。策略层和脚本层只依赖
本服务返回的标准契约，避免直接绑定某个第三方接口。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from data.adapters.iwencai_intelligence_adapter import IwenCaiIntelligenceAdapter
from data.contracts import FinancialFactor, NewsEvent, SectorHeat

logger = logging.getLogger(__name__)


class IntelligenceService:
    """板块/财报/新闻/事件统一入口。"""

    def __init__(self, adapter: Optional[IwenCaiIntelligenceAdapter] = None):
        self.adapter = adapter or IwenCaiIntelligenceAdapter()

    # ------------------------------------------------------------------
    # 通用原始查询
    # ------------------------------------------------------------------
    def query_raw(self, query: str, skill_id: str, limit: int = 10) -> Dict[str, Any]:
        try:
            return self.adapter.query_raw(query, skill_id=skill_id, limit=limit)
        except Exception as e:
            logger.warning(f"信息增强原始查询失败 skill={skill_id} query={query}: {e}")
            return {}

    # ------------------------------------------------------------------
    # 板块 / 概念
    # ------------------------------------------------------------------
    def get_hot_sectors(self, limit: int = 20) -> List[SectorHeat]:
        try:
            return self.adapter.hot_sectors(limit=limit)
        except Exception as e:
            logger.warning(f"热板块查询失败: {e}")
            return []

    def query_sectors(self, query: str, limit: int = 20) -> List[SectorHeat]:
        try:
            return self.adapter.query_sectors(query, limit=limit)
        except Exception as e:
            logger.warning(f"板块查询失败 query={query}: {e}")
            return []

    # ------------------------------------------------------------------
    # 财务
    # ------------------------------------------------------------------
    def get_stock_financials(self, code_or_name: str) -> List[FinancialFactor]:
        try:
            return self.adapter.stock_financials(code_or_name)
        except Exception as e:
            logger.warning(f"个股财务查询失败 code_or_name={code_or_name}: {e}")
            return []

    def query_financials(self, query: str, limit: int = 20) -> List[FinancialFactor]:
        try:
            return self.adapter.query_financials(query, limit=limit)
        except Exception as e:
            logger.warning(f"财务查询失败 query={query}: {e}")
            return []

    # ------------------------------------------------------------------
    # 事件 / 风险
    # ------------------------------------------------------------------
    def get_stock_events(self, code_or_name: str, limit: int = 10) -> List[NewsEvent]:
        try:
            return self.adapter.stock_events(code_or_name, limit=limit)
        except Exception as e:
            logger.warning(f"个股事件查询失败 code_or_name={code_or_name}: {e}")
            return []

    def query_events(self, query: str, limit: int = 20) -> List[NewsEvent]:
        try:
            return self.adapter.query_events(query, limit=limit)
        except Exception as e:
            logger.warning(f"事件查询失败 query={query}: {e}")
            return []

    # ------------------------------------------------------------------
    # 新闻
    # ------------------------------------------------------------------
    def search_news(self, query: str, limit: int = 10) -> List[NewsEvent]:
        try:
            return self.adapter.search_news(query, limit=limit)
        except Exception as e:
            logger.warning(f"新闻搜索失败 query={query}: {e}")
            return []

    # ------------------------------------------------------------------
    # 面向策略的综合评分辅助
    # ------------------------------------------------------------------
    def build_intelligence_snapshot(self, code_or_name: str) -> Dict[str, Any]:
        """构建个股信息增强快照，供盯盘/复盘/策略解释使用。"""
        financials = self.get_stock_financials(code_or_name)
        events = self.get_stock_events(code_or_name, limit=10)
        news = self.search_news(f"{code_or_name} 最新消息", limit=5)
        return {
            "code_or_name": code_or_name,
            "financials": [x.to_dict() for x in financials],
            "events": [x.to_dict() for x in events],
            "news": [x.to_dict() for x in news],
            "event_risk_score": self._event_risk_score(events),
            "source": "iwencai",
        }

    @staticmethod
    def _event_risk_score(events: List[NewsEvent]) -> float:
        score = 0.0
        for e in events:
            if e.risk_level == "high":
                score -= 2.0
            elif e.risk_level == "medium":
                score -= 1.0
        return score
