"""统一行情服务：DB优先，多源兜底，返回标准契约。"""
from __future__ import annotations

from typing import List, Optional
import pandas as pd

from data.adapters.baostock_adapter import BaoStockAdapter
from data.adapters.sina_adapter import SinaAdapter
from data.adapters.tencent_adapter import TencentAdapter
from data.contracts import DailyBar, MarketQuote, MinuteBar
from data.store.sqlite_store import StockStore


class MarketDataService:
    def __init__(self, store: StockStore | None = None):
        self.store = store or StockStore()
        self.tencent = TencentAdapter()
        self.sina = SinaAdapter()
        self.baostock = BaoStockAdapter()

    def get_realtime(self, code: str) -> Optional[MarketQuote]:
        """实时行情：腾讯优先，新浪兜底。"""
        for adapter in (self.tencent, self.sina):
            try:
                q = adapter.get_realtime(code)
                if q and q.close > 0:
                    return q
            except Exception:
                continue
        return None

    def get_daily_bars(self, code: str, start_date: str = "", end_date: str = "", refresh: bool = False) -> List[DailyBar]:
        """日K：DB优先；腾讯拉取；BaoStock兜底。"""
        if not refresh:
            df = self.store.get_daily_prices(code, start_date, end_date)
            if df is not None and not df.empty:
                return self._df_to_bars(code, df, source="db")

        for adapter in (self.tencent, self.baostock):
            try:
                bars = adapter.get_daily(code, start_date, end_date)
                if bars:
                    # 兼容现有存储接口
                    df = pd.DataFrame([b.to_dict() for b in bars])
                    self.store.save_daily_prices(df, code)
                    return bars
            except Exception:
                continue
        return []

    def get_daily_df(self, code: str, start_date: str = "", end_date: str = "", refresh: bool = False) -> pd.DataFrame:
        bars = self.get_daily_bars(code, start_date, end_date, refresh=refresh)
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame([b.to_dict() for b in bars])
        df["date"] = pd.to_datetime(df["date"])
        return df

    def get_minute_bars(self, code: str) -> List[MinuteBar]:
        """分时：腾讯 ifzq，避免东方财富 push2。"""
        try:
            return self.tencent.get_minute(code)
        except Exception:
            return []

    @staticmethod
    def _df_to_bars(code: str, df: pd.DataFrame, source: str) -> List[DailyBar]:
        bars: List[DailyBar] = []
        for _, r in df.iterrows():
            bars.append(DailyBar(
                code=str(code).zfill(6), date=str(r.get("date"))[:10],
                open=float(r.get("open", 0)), high=float(r.get("high", 0)),
                low=float(r.get("low", 0)), close=float(r.get("close", 0)),
                volume=float(r.get("volume", 0)), amount=float(r.get("amount", 0) or 0),
                adjust_flag=str(r.get("adjust_flag", "qfq") or "qfq"), source=source,
            ))
        return bars
