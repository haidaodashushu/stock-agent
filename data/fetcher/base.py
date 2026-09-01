"""
data/fetcher/base.py - 数据获取基类
所有数据源实现统一接口
"""
from abc import ABC, abstractmethod
from typing import Any

class BaseFetcher(ABC):
    """数据获取器基类"""

    @abstractmethod
    def name(self) -> str:
        """数据源名称"""
        ...

    @abstractmethod
    def fetch_daily(self, symbol: str, start_date: str = "", end_date: str = "") -> Any:
        """获取日K线数据"""
        ...

    @abstractmethod
    def fetch_realtime(self, symbol: str) -> Any:
        """获取实时行情"""
        ...

    @abstractmethod
    def fetch_all_stocks(self) -> Any:
        """获取全部股票列表及实时行情"""
        ...

    def normalize_symbol(self, symbol: str) -> str:
        """
        统一股票代码格式，返回 A 股 6 位代码
        如: '000001' -> '000001', '600000' -> '600000'
        """
        return str(symbol).zfill(6)
