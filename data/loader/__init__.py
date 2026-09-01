"""
data/loader/__init__.py - 数据加载/查询统一接口
对上层屏蔽数据源差异
"""
from typing import Optional, List, Dict, Any
import pandas as pd

from data.store.sqlite_store import StockStore
from data.fetcher.sina_quote import SinaQuoteFetcher
from data.fetcher.tencent_quote import TencentQuoteFetcher
from data.fetcher.ak_news import NewsFetcher
from data.services.market_data_service import MarketDataService


class DataLoader:
    """
    数据加载器 - 统一的数据访问入口
    上层代码（策略、引擎、Web）通过此类获取数据，无需关心底层细节
    """

    def __init__(self):
        self.store = StockStore()
        self.sina = SinaQuoteFetcher()
        self.tencent = TencentQuoteFetcher()
        self.news = NewsFetcher()
        self.market = MarketDataService(self.store)

    def get_realtime(self, symbol: str = "") -> pd.DataFrame:
        """
        获取实时行情
        如果指定 symbol 则单只，否则取全部 A 股
        """
        if symbol:
            q = self.market.get_realtime(symbol)
            if q:
                return pd.DataFrame([{
                    "代码": q.code, "名称": q.name, "最新价": q.close,
                    "昨收": q.prev_close, "今开": q.open, "最高": q.high, "最低": q.low,
                    "成交量": q.volume, "成交额": q.amount, "涨跌幅": q.change_pct,
                    "数据源": q.source,
                }])
            return pd.DataFrame()
        return self.sina.fetch_all_stocks()

    def get_daily(self, symbol: str, start_date: str = "",
                  end_date: str = "") -> pd.DataFrame:
        """
        获取日K线数据（优先数据库，没有再拉取）
        """
        return self.market.get_daily_df(symbol, start_date, end_date)

    def refresh_daily(self, symbols: List[str], days: int = 365) -> Dict[str, int]:
        """
        批量刷新日K线数据
        Args:
            symbols: 股票代码列表
            days: 拉取过去多少天数据
        Returns:
            {code: 拉取条数}
        """
        from datetime import datetime, timedelta
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        result = {}
        for sym in symbols:
            df = self.tencent.fetch_daily(sym, start, end)
            if df.empty:
                df = self.sina.fetch_daily(sym, start, end)
            if not df.empty:
                self.store.save_daily_prices(df, sym)
                result[sym] = len(df)
        return result

    def get_all_stocks(self) -> pd.DataFrame:
        """获取全部股票列表"""
        return self.store.get_all_stocks()

    def refresh_all_stocks(self) -> pd.DataFrame:
        """刷新全部股票列表和快照"""
        df = self.sina.fetch_all_stocks()
        if not df.empty:
            self.store.upsert_stocks_batch(df)
            self.store.save_realtime_snapshot(df, "sina")
        return df

    def get_hot_news(self, num: int = 20) -> pd.DataFrame:
        """获取热点新闻"""
        return self.news.fetch_hot_news(num)

    def get_concept_boards(self) -> pd.DataFrame:
        """获取概念板块"""
        return self.news.fetch_concept_boards()

    def get_industry_boards(self) -> pd.DataFrame:
        """获取行业板块"""
        return self.news.fetch_industry_boards()

    def get_portfolio(self) -> pd.DataFrame:
        """获取持仓"""
        return self.store.get_portfolio()

    def get_orders(self, limit: int = 100) -> pd.DataFrame:
        """获取订单"""
        return self.store.get_orders(limit=limit)

    def get_signals(self, limit: int = 50) -> pd.DataFrame:
        """获取交易信号"""
        return self.store.get_signals(limit=limit)
