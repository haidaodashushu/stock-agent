"""
data/fetcher/ak_news.py - 新闻热点获取器
通过 akshare 获取财经新闻、热点概念等
"""
import logging
from typing import Optional
import pandas as pd

logger = logging.getLogger(__name__)


class NewsFetcher:
    """新闻/热点获取器"""

    def __init__(self):
        self._ak = None

    @property
    def ak(self):
        if self._ak is None:
            import akshare as ak
            self._ak = ak
        return self._ak

    def fetch_hot_news(self, num: int = 20) -> pd.DataFrame:
        """
        获取热点头条新闻
        返回: date, time, title, content, url
        """
        try:
            df = self.ak.stock_hot_rank_em()
            if df is not None and not df.empty:
                return df.head(num)
        except Exception as e:
            logger.warning(f"获取热点新闻失败: {e}")
        return pd.DataFrame()

    def fetch_concept_boards(self) -> pd.DataFrame:
        """
        获取概念板块列表
        返回: 板块名称, 涨跌幅, 领涨股等
        """
        try:
            # akshare 概念板块
            df = self.ak.stock_board_concept_name_em()
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.warning(f"获取概念板块失败: {e}")
        return pd.DataFrame()

    def fetch_industry_boards(self) -> pd.DataFrame:
        """获取行业板块"""
        try:
            df = self.ak.stock_board_industry_name_em()
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.warning(f"获取行业板块失败: {e}")
        return pd.DataFrame()

    def fetch_concept_stocks(self, concept_name: str) -> pd.DataFrame:
        """
        获取某概念板块下的成分股
        Args:
            concept_name: 概念板块名称
        """
        try:
            df = self.ak.stock_board_concept_cons_em(symbol=concept_name)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.warning(f"获取概念 {concept_name} 成分股失败: {e}")
        return pd.DataFrame()

    def fetch_news_by_date(self, date: str = "") -> pd.DataFrame:
        """
        获取某日财经新闻
        Args:
            date: YYYYMMDD，空则取最新
        """
        try:
            df = self.ak.stock_info_global_em(symbol="财经")
            if df is not None and not df.empty:
                return df.head(30)
        except:
            pass
        return pd.DataFrame()

    def fetch_stock_news(self, code: str, num: int = 20) -> pd.DataFrame:
        """获取单只股票新闻（东方财富）。"""
        try:
            df = self.ak.stock_news_em(symbol=str(code).zfill(6))
            if df is not None and not df.empty:
                return df.head(num)
        except Exception as e:
            logger.warning(f"获取 {code} 个股新闻失败: {e}")
        return pd.DataFrame()

    def fetch_stock_hot_keywords(self, code: str) -> pd.DataFrame:
        """获取单只股票热词/概念热度（东方财富）。"""
        s = str(code).zfill(6)
        symbol = ("SH" if s.startswith("6") else "SZ") + s
        try:
            df = self.ak.stock_hot_keyword_em(symbol=symbol)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.warning(f"获取 {code} 热词失败: {e}")
        return pd.DataFrame()
