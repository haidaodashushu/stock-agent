from __future__ import annotations

from datetime import datetime
from typing import Optional

from data.adapters.base import DataSourceAdapter
from data.contracts import MarketQuote
from data.fetcher.sina_quote import SinaQuoteFetcher


class SinaAdapter(DataSourceAdapter):
    name = "sina"

    def __init__(self):
        self.fetcher = SinaQuoteFetcher()

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
