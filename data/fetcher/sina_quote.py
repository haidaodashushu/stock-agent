"""
data/fetcher/sina_quote.py - 新浪财经行情接口
支持：全部A股实时行情、个股日K线（通过新浪免费接口）
"""
import time
import re
import logging
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

import pandas as pd

from .base import BaseFetcher

logger = logging.getLogger(__name__)

# 新浪A股列表代码分区
SINA_STOCK_CODES = {
    "sh": list(range(600000, 605000)),   # 沪市主板 600xxx
    "sh_b": list(range(688000, 689000)), # 科创板 688xxx
    "sz_a": list(range(1, 1000)),        # 深市主板 000xxx
    "sz_b": list(range(2000, 3000)),     # 中小板 002xxx
    "sz_c": list(range(3000, 3010)),     # 创业板 300xxx
}

# 新浪代码前缀映射
_EXCHANGE_MAP = {"sh": "sh", "sz": "sz", "bj": "bj"}
_SYMBOL_PREFIX = {
    "0": "sz", "3": "sz", "6": "sh", "8": "bj", "4": "bj",
}


def _get_prefix(symbol: str) -> str:
    """根据股票代码判断交易所前缀"""
    s = str(symbol).zfill(6)
    prefix = s[0]
    return _SYMBOL_PREFIX.get(prefix, "sz")


def _sina_symbol(symbol: str) -> str:
    """转换为新浪格式: sh600000, sz000001"""
    s = str(symbol).zfill(6)
    return f"{_get_prefix(s)}{s}"


class SinaQuoteFetcher(BaseFetcher):
    """新浪财经行情获取器"""

    def name(self) -> str:
        return "sina"

    def fetch_all_stocks(self) -> pd.DataFrame:
        """
        获取全部A股实时行情（新浪接口）
        返回 DataFrame: 代码, 名称, 最新价, 涨跌额, 涨跌幅, 今开, 昨收, 最高, 最低, 成交量, 成交额
        """
        # 分批获取，每次 50-80 个代码
        all_codes = []
        # 生成沪市+深市代码列表（精简版，仅常用代码）
        for prefix, codes in SINA_STOCK_CODES.items():
            step = 1 if prefix in ("sh_b", "sz_c") else 1
            for code in range(codes[0], codes[-1] + 1, step):
                all_codes.append(f"{prefix}{code:06d}")

        # 分批请求
        batch_size = 80
        results = []
        for i in range(0, len(all_codes), batch_size):
            batch = all_codes[i:i + batch_size]
            url = f"https://hq.sinajs.cn/list={','.join(batch)}"
            try:
                req = Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://finance.sina.com.cn"
                })
                resp = urlopen(req, timeout=10)
                text = resp.read().decode("gbk")
                for line in text.strip().split("\n"):
                    if not line.strip():
                        continue
                    results.append(self._parse_sina_line(line))
            except Exception as e:
                logger.warning(f"请求新浪行情失败 [batch {i//batch_size}]: {e}")
            time.sleep(0.15)  # 避免被限流

        # 过滤 None + 去重
        valid = [r for r in results if r is not None]
        seen = set()
        unique = []
        for r in valid:
            key = r["代码"]
            if key not in seen:
                seen.add(key)
                unique.append(r)

        if not unique:
            return pd.DataFrame()

        df = pd.DataFrame(unique)
        # 按成交量排序
        df["成交额"] = pd.to_numeric(df["成交额"], errors="coerce")
        df.sort_values("成交额", ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def fetch_realtime(self, symbol: str) -> pd.Series:
        """获取单只股票实时行情"""
        sym = _sina_symbol(symbol)
        url = f"https://hq.sinajs.cn/list={sym}"
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn"
            })
            resp = urlopen(req, timeout=10)
            text = resp.read().decode("gbk")
            for line in text.strip().split("\n"):
                row = self._parse_sina_line(line)
                if row and row["代码"] == str(symbol).zfill(6):
                    return pd.Series(row)
        except Exception as e:
            logger.error(f"获取 {symbol} 实时行情失败: {e}")
        return pd.Series()

    def fetch_daily(self, symbol: str, start_date: str = "", end_date: str = "") -> pd.DataFrame:
        """
        获取个股日K线（通过新浪历史接口）
        参数:
            symbol: 股票代码
            start_date: 开始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD
        返回:
            DataFrame: date, open, close, high, low, volume
        """
        sym = _sina_symbol(symbol)
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,qfq"

        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urlopen(req, timeout=15)
            data = resp.read().decode("utf-8")
        except Exception as e:
            logger.error(f"获取 {symbol} 日K失败: {e}")
            return pd.DataFrame()

        import json
        try:
            js = json.loads(data)
            data_section = js.get("data", {})
            if isinstance(data_section, dict):
                klines = data_section.get(sym, {}).get("day", [])
                if not klines:
                    klines = data_section.get(sym, {}).get("qfqday", [])
            else:
                klines = []
            if not klines:
                logger.warning(f"{symbol} 日K数据为空")
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
                        "volume": int(k[5]) if k[5] else 0,
                    })

            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            df.sort_values("date", inplace=True)
            df.reset_index(drop=True, inplace=True)

            # 日期过滤
            if start_date:
                df = df[df["date"] >= pd.Timestamp(start_date)]
            if end_date:
                df = df[df["date"] <= pd.Timestamp(end_date)]

            return df
        except json.JSONDecodeError:
            logger.error(f"{symbol} 日K返回格式异常")
            return pd.DataFrame()

    @staticmethod
    def _parse_sina_line(line: str) -> Optional[dict]:
        """解析新浪行情一行数据"""
        # 格式: var hq_str_sh600000="名称,今开,昨收,最新价,最高,最低,...";
        m = re.match(r'var hq_str_(\w+)="(.+)"', line.strip())
        if not m:
            return None

        code = m.group(1)  # e.g. sh600000
        fields = m.group(2).split(",")
        if len(fields) < 32:
            return None

        # 提取股票代码数字部分
        stock_code = code[2:] if len(code) > 6 else code
        exchange = code[:2] if len(code) > 6 else ""

        def safe_float(v, default=0.0):
            try:
                return float(v) if v else default
            except (ValueError, TypeError):
                return default

        def safe_int(v, default=0):
            try:
                return int(float(v)) if v else default
            except (ValueError, TypeError):
                return default

        try:
            return {
                "代码": stock_code,
                "交易所": exchange,
                "名称": fields[0],
                "今开": safe_float(fields[1]),
                "昨收": safe_float(fields[2]),
                "最新价": safe_float(fields[3]),
                "最高": safe_float(fields[4]),
                "最低": safe_float(fields[5]),
                "买入价": safe_float(fields[6]),
                "卖出价": safe_float(fields[7]),
                "成交量": safe_int(fields[8]),
                "成交额": safe_float(fields[9]) if len(fields) > 9 else 0,
                "涨跌额": safe_float(fields[3]) - safe_float(fields[2]) if fields[3] and fields[2] else 0,
                "涨跌幅": round(
                    ((safe_float(fields[3]) - safe_float(fields[2])) / safe_float(fields[2]) * 100)
                    if safe_float(fields[2]) != 0 else 0, 2
                ),
            }
        except (IndexError, ValueError):
            return None
