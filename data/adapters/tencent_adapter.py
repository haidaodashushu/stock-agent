from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from data.adapters.base import DataSourceAdapter
from data.contracts import DailyBar, MarketQuote, MinuteBar
from data.fetcher.tencent_quote import TencentQuoteFetcher


class TencentAdapter(DataSourceAdapter):
    name = "tencent"

    def __init__(self):
        self.fetcher = TencentQuoteFetcher()

    def get_realtime(self, code: str) -> Optional[MarketQuote]:
        row = self.fetcher.fetch_realtime(code)
        if row is None or row.empty:
            return None
        return MarketQuote(
            code=str(row.get("代码", code)).zfill(6),
            name=str(row.get("名称", "")),
            datetime=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            open=float(row.get("今开", 0) or 0),
            high=float(row.get("最高", 0) or 0),
            low=float(row.get("最低", 0) or 0),
            close=float(row.get("最新价", 0) or 0),
            prev_close=float(row.get("昨收", 0) or 0),
            volume=float(row.get("成交量", 0) or 0),
            amount=float(row.get("成交额", 0) or 0),
            change_pct=float(row.get("涨跌幅", 0) or 0),
            source=self.name,
        )

    def get_daily(self, code: str, start_date: str = "", end_date: str = "") -> List[DailyBar]:
        df = self.fetcher.fetch_daily(code, start_date, end_date)
        if df is None or df.empty:
            return []
        bars = []
        for _, r in df.iterrows():
            bars.append(DailyBar(
                code=str(code).zfill(6), date=str(r.get("date"))[:10],
                open=float(r.get("open", 0)), high=float(r.get("high", 0)),
                low=float(r.get("low", 0)), close=float(r.get("close", 0)),
                volume=float(r.get("volume", 0)), amount=float(r.get("amount", 0) or 0),
                adjust_flag="qfq", source=self.name,
            ))
        return bars

    def get_minute(self, code: str) -> List[MinuteBar]:
        df = self.fetcher.fetch_minute(code)
        if df is None or df.empty:
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        bars = []
        for _, r in df.iterrows():
            t = str(r.get("time", ""))
            dt = f"{today} {t[:2]}:{t[2:4]}:00" if len(t) >= 4 else today
            bars.append(MinuteBar(
                code=str(code).zfill(6), datetime=dt,
                price=float(r.get("price", 0)), avg_price=float(r.get("avg_price", 0) or 0),
                volume=float(r.get("volume", 0) or 0), amount=float(r.get("amount", 0) or 0),
                source=self.name,
            ))
        return bars
