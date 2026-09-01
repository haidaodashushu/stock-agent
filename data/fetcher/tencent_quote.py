"""
data/fetcher/tencent_quote.py - 腾讯行情接口
提供：实时行情快照、分时数据、日K线（备选数据源）
"""
import time
import logging
from typing import Any, Optional
from urllib.request import Request, urlopen
import json

import pandas as pd

from .base import BaseFetcher

logger = logging.getLogger(__name__)

# 腾讯代码前缀
_SYMBOL_TENCENT = {
    "sh": "sh", "sz": "sz", "bj": "bj",
}


def _tencent_symbol(symbol: str) -> str:
    """转换为腾讯格式: sh600000, sz000001"""
    s = str(symbol).zfill(6)
    prefix = {"5": "sh", "6": "sh", "0": "sz", "1": "sz", "3": "sz", "8": "bj", "4": "bj"}
    return f"{prefix.get(s[0], 'sz')}{s}"


def _stock_type(symbol: str) -> tuple:
    """返回腾讯 secid 格式: (int market, str code)"""
    s = str(symbol).zfill(6)
    if s.startswith(("5", "6")):
        return (1, s)  # 上交所
    return (0, s)      # 深交所


class TencentQuoteFetcher(BaseFetcher):
    """腾讯行情获取器"""

    def name(self) -> str:
        return "tencent"

    def fetch_realtime(self, symbol: str) -> pd.Series:
        """获取单只股票实时行情快照"""
        sym = _tencent_symbol(symbol)
        url = f"https://hq.gtimg.cn/s?code={sym}"
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urlopen(req, timeout=10)
            text = resp.read().decode("gbk")
            return self._parse_tencent_line(text, symbol)
        except Exception as e:
            logger.error(f"腾讯获取 {symbol} 失败: {e}")
            return pd.Series()

    def fetch_daily(self, symbol: str, start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """
        获取个股日K线数据（腾讯）
        注意：腾讯日K数据量有限，适合近期行情
        """
        sym = _tencent_symbol(symbol)
        # 使用 /kline/kline 接口获取复权数据，尽可能多地拉取
        url = f"https://ifzq.gtimg.cn/appstock/app/kline/kline?param={sym},day,,,500"

        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urlopen(req, timeout=15)
            data = resp.read().decode("utf-8")
            js = json.loads(data)
        except Exception as e:
            logger.warning(f"腾讯获取 {symbol} 日K失败: {e}")
            return pd.DataFrame()

        data_section = js.get("data", {})
        if isinstance(data_section, list):
            klines = []
        else:
            klines = data_section.get(sym, {}).get("day", [])
            if not klines:
                klines = data_section.get(sym, {}).get("qfqday", [])

        if not klines:
            return pd.DataFrame()

        rows = []
        for k in klines:
            if len(k) >= 6:
                rows.append({
                    "date": k[0],
                    "open": float(k[1]),
                    "close": float(k[2]),
                    "high": float(k[3]),
                    "low": float(k[4]),
                    "volume": int(float(k[5])) if k[5] else 0,
                })

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df.sort_values("date", inplace=True)
        df.reset_index(drop=True, inplace=True)

        if start_date:
            df = df[df["date"] >= pd.Timestamp(start_date)]
        if end_date:
            df = df[df["date"] <= pd.Timestamp(end_date)]

        return df

    def fetch_all_stocks(self) -> pd.DataFrame:
        """腾讯不支持全部股票列表，调用新浪接口"""
        from .sina_quote import SinaQuoteFetcher
        return SinaQuoteFetcher().fetch_all_stocks()

    def fetch_minute(self, symbol: str) -> pd.DataFrame:
        """
        获取当日分时数据
        """
        sym = _tencent_symbol(symbol)
        # 不依赖东方财富 push2（当前环境经常 502/代理失败），统一走腾讯 ifzq 分时接口。
        url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://gu.qq.com/"
            })
            resp = urlopen(req, timeout=10)
            data = resp.read().decode("utf-8")
            js = json.loads(data)
            trends = js.get("data", {}).get(sym, {}).get("data", {}).get("data", [])
            rows = []
            for t in trends:
                parts = t.replace(",", " ").split()
                # 腾讯格式："0930 37.84 33898 128270032.32"
                if len(parts) >= 4:
                    rows.append({
                        "time": parts[0],
                        "price": float(parts[1]),
                        "avg_price": 0.0,
                        "volume": float(parts[2]),
                        "amount": float(parts[3]),
                    })
            frame = pd.DataFrame(rows)
            frame.attrs.update(
                {
                    "source": "tencent_ifzq",
                    "trading_date": str(
                        js.get("data", {})
                        .get(sym, {})
                        .get("data", {})
                        .get("date", "")
                    ),
                }
            )
            return frame
        except Exception as e:
            logger.warning(f"获取分时数据失败: {e}")
            return pd.DataFrame()

    @staticmethod
    def _parse_tencent_line(text: str, symbol: str) -> pd.Series:
        """解析腾讯行情一行"""
        # 格式: v_sh600000="1~名称~...很多字段..."
        try:
            # 找到第一个 = 后面的引号内容
            start = text.index('"') + 1
            end = text.rindex('"')
            fields = text[start:end].split("~")
            if len(fields) < 40:
                return pd.Series()

            def sf(v):
                try:
                    return float(v)
                except (ValueError, TypeError):
                    return 0.0

            row = {
                "代码": str(symbol).zfill(6),
                "名称": fields[1],
                "最新价": sf(fields[3]),
                "昨收": sf(fields[4]),
                "今开": sf(fields[5]),
                "成交量": sf(fields[6]),
                "成交额": sf(fields[37]) if len(fields) > 37 else 0,
                "最高": sf(fields[33]),
                "最低": sf(fields[34]),
                "涨跌额": sf(fields[31]),
                "涨跌幅": sf(fields[32]),
                "数据源": "tencent",
            }
            return pd.Series(row)
        except (ValueError, IndexError):
            return pd.Series()
