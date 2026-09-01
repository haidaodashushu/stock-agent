"""数据源适配器基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from data.contracts import DailyBar, FinancialFactor, FundFlow, MarketQuote, MinuteBar, StockBasic


class DataSourceAdapter(ABC):
    name: str = "base"

    def get_realtime(self, code: str) -> Optional[MarketQuote]:
        return None

    def get_daily(self, code: str, start_date: str = "", end_date: str = "") -> List[DailyBar]:
        return []

    def get_minute(self, code: str) -> List[MinuteBar]:
        return []

    def get_stock_basic(self, code: str = "") -> List[StockBasic]:
        return []

    def get_financial_factors(self, code: str, year: str = "", quarter: int = 0) -> List[FinancialFactor]:
        return []

    def get_fund_flow(self, codes: List[str], date: str = "") -> List[FundFlow]:
        """获取个股资金流向。codes: 股票代码列表；date: 日期 YYYYMMDD，默认最新。"""
        return []
