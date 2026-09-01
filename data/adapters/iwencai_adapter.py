"""同花顺问财 OpenAPI 适配器。
提供行情数据 + 资金流向数据查询能力。
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.client import RemoteDisconnected
from typing import Dict, List, Optional

from data.adapters.base import DataSourceAdapter
from data.adapters.iwencai_credentials import IwenCaiKeyring, is_quota_http_error
from data.contracts import DailyBar, FundFlow, MarketQuote


class IwenCaiAdapter(DataSourceAdapter):
    """问财行情 & 资金流向适配器。

    使用同花顺问财 OpenAPI (/v1/query2data)。
    认证：Bearer Token，从环境变量 IWENCAI_API_KEY 读取。
    """

    name = "iwencai"
    BASE_URL = "https://openapi.iwencai.com"
    QUERY_ENDPOINT = "/v1/query2data"

    def __init__(
        self,
        api_key: str = "",
        *,
        request_timeout: int = 30,
        retries: int = 2,
        fill_missing: bool = True,
        batch_size: int = 10,
    ):
        self._api_key = api_key or self._load_api_key()
        self._last_call: float = 0.0
        self._min_interval: float = 0.3  # 最小调用间隔（秒）
        self._request_timeout = max(1, int(request_timeout))
        self._retries = max(0, int(retries))
        self._fill_missing = bool(fill_missing)
        self._batch_size = max(1, min(10, int(batch_size)))
        self.last_code_errors: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # 资金流向
    # ------------------------------------------------------------------
    def get_fund_flow(self, codes: List[str], date: str = "") -> List[FundFlow]:
        """获取个股主力资金流向。

        对每个 code 发一次查询，合并返回。
        可通过 batch_query 一次性查多只（如「贵州茅台,同花顺 主力资金流向」）。
        """
        if not codes:
            return []

        self.last_code_errors = {}
        results: List[FundFlow] = []
        date_str = date or datetime.now().strftime("%Y%m%d")

        seen: set[str] = set()

        # Each batch is isolated: a transient failure must not discard data
        # already returned for other stocks.
        for i in range(0, len(codes), self._batch_size):
            batch = codes[i : i + self._batch_size]
            names = ",".join(batch)
            query = f"{names} 主力资金流向 大单净买入额 dde大单净额 小单净流入 资金流入 资金流出"
            try:
                raw = self._call_api(query, limit=len(batch))
            except Exception as exc:
                self._record_code_error(batch, str(exc))
                continue

            datas = raw.get("datas", [])
            for item in datas:
                try:
                    ff = self._parse_fund_flow(item, date_str)
                    if ff and ff.code not in seen:
                        results.append(ff)
                        seen.add(ff.code)
                        self.last_code_errors.pop(ff.code, None)
                except Exception:
                    continue

            missing_codes = [code for code in batch if self._normalize_code(code) not in seen]
            for code in missing_codes if self._fill_missing else []:
                norm = self._normalize_code(code)
                try:
                    raw_one = self._call_api(
                        f"{code} 主力资金流向 大单净买入额 dde大单净额 小单净流入 资金流入 资金流出",
                        limit=1,
                    )
                    for item in raw_one.get("datas", []):
                        ff = self._parse_fund_flow(item, date_str)
                        if ff and ff.code == norm and ff.code not in seen:
                            results.append(ff)
                            seen.add(ff.code)
                            self.last_code_errors.pop(ff.code, None)
                            break
                except Exception as exc:
                    self.last_code_errors[norm] = str(exc)

            for code in missing_codes:
                norm = self._normalize_code(code)
                if norm not in seen and norm not in self.last_code_errors:
                    self.last_code_errors[norm] = "问财未返回该标的资金流"

        return results

    def _record_code_error(self, codes: List[str], message: str) -> None:
        for code in codes:
            self.last_code_errors[self._normalize_code(code)] = message

    def get_fund_flow_top(self, n: int = 20, date: str = "") -> List[FundFlow]:
        """获取主力资金净流入排名前 N 的股票。"""
        date_str = date or datetime.now().strftime("%Y%m%d")
        query = f"主力资金净流入排名 最新主力资金流向"
        raw = self._call_api(query, limit=min(n, 50))

        results: List[FundFlow] = []
        for item in raw.get("datas", []):
            try:
                ff = self._parse_fund_flow(item, date_str)
                if ff:
                    results.append(ff)
            except Exception:
                continue
        return results

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------
    def get_realtime(self, code: str) -> Optional[MarketQuote]:
        """获取个股实时行情。"""
        query = f"{code} 最新价 涨跌幅 成交量 成交额 换手率"
        raw = self._call_api(query, limit=1)

        datas = raw.get("datas", [])
        if not datas:
            return None

        item = datas[0]
        return MarketQuote(
            code=str(item.get("股票代码", code)),
            name=str(item.get("股票简称", "")),
            datetime=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            close=float(item.get("最新价", 0) or 0),
            change_pct=float(item.get("最新涨跌幅", 0) or 0),
            volume=float(item.get("成交量", 0) or 0),
            amount=float(item.get("成交额", 0) or 0),
            source=self.name,
        )

    # ------------------------------------------------------------------
    # 核心 API 调用
    # ------------------------------------------------------------------
    def _call_api(
        self, query: str, page: int = 1, limit: int = 10, retries: int | None = None,
    ) -> dict:
        """调用问财 query2data 接口，带节流与错误处理。"""
        retries = self._retries if retries is None else max(0, int(retries))
        payload = json.dumps(
            {
                "query": query,
                "page": str(page),
                "limit": str(limit),
                "is_cache": "1",
                "expand_index": "true",
            }
        ).encode("utf-8")

        url = self.BASE_URL + self.QUERY_ENDPOINT
        quota_errors: list[str] = []
        for slot, key in IwenCaiKeyring.candidates(self._api_key):
            self._api_key = key
            last_error: Exception | None = None
            for attempt in range(retries + 1):
                self._throttle()
                trace_id = secrets.token_hex(32)
                req = urllib.request.Request(url, data=payload, method="POST")
                req.add_header("Content-Type", "application/json")
                req.add_header("Authorization", f"Bearer {self._api_key}")
                req.add_header("X-Claw-Call-Type", "normal")
                req.add_header("X-Claw-Skill-Id", "hithink-market-query")
                req.add_header("X-Claw-Skill-Version", "1.0.0")
                req.add_header("X-Claw-Plugin-Id", "none")
                req.add_header("X-Claw-Plugin-Version", "none")
                req.add_header("X-Claw-Trace-Id", trace_id)

                try:
                    with urllib.request.urlopen(req, timeout=self._request_timeout) as resp:
                        body = resp.read().decode("utf-8")
                    IwenCaiKeyring.promote(key)
                    return json.loads(body)
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8", errors="replace")
                    if is_quota_http_error(e.code, err_body):
                        quota_errors.append(f"{slot}: HTTP {e.code} {err_body[:120]}")
                        break
                    raise RuntimeError(f"问财 API HTTP {e.code}: {err_body[:200]}") from e
                except (urllib.error.URLError, TimeoutError, RemoteDisconnected, ConnectionResetError) as e:
                    last_error = e
                    if attempt >= retries:
                        raise RuntimeError(f"问财 API 请求失败: {last_error}") from last_error
                    time.sleep(0.5 * (attempt + 1))
                except Exception as e:
                    raise RuntimeError(f"问财 API 请求失败: {e}") from e

        detail = "; ".join(quota_errors) if quota_errors else "无可用 key"
        raise RuntimeError(f"问财 API HTTP 401: 全部已配置 key 额度不可用: {detail}")

    def _throttle(self):
        """简单节流，避免频率限制。"""
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def _parse_fund_flow(self, item: dict, date: str) -> Optional[FundFlow]:
        """将问财返回的字段映射到 FundFlow 契约。"""
        code = item.get("股票代码", "")
        if not code:
            return None

        # 主力资金净流入（L1）
        main_net = self._safe_float(item, "主力资金流向")

        # DDE 大单净额
        big_net = self._safe_float(item, "dde大单净额", "DDE大单净额", "大单净买入额", "大单净额")

        # 小单净买入额
        retail_net = self._safe_float(item, "小单净买入额")

        # 资金流入/流出（用于计算流向占比）
        inflow = self._safe_float(item, "资金流入")
        outflow = self._safe_float(item, "资金流出")

        # 估算主力净占比
        total = inflow + outflow
        main_pct = (abs(main_net) / total * 100) if total > 0 and main_net != 0 else 0.0

        return FundFlow(
            code=self._normalize_code(str(code)),  # 600519.SH → 600519
            date=date,
            main_net_inflow=main_net,
            big_net_inflow=big_net,
            retail_net_inflow=retail_net,
            main_net_pct=round(main_pct, 2),
            source=self.name,
            reliability="optional",
        )

    @staticmethod
    def _safe_float(item: dict, *keys: str) -> float:
        """安全取浮点数，支持带日期后缀的 key，如 资金流入[20260618]。"""
        for key in keys:
            val = item.get(key)
            if val is not None and val != "":
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        # 尝试匹配带日期后缀的 key
        for k, v in item.items():
            for key in keys:
                if k.startswith(key) and "[" in k:
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        continue
        return 0.0

    @staticmethod
    def _normalize_code(code: str) -> str:
        return str(code).split(".")[0].strip()

    # ------------------------------------------------------------------
    # Key 加载
    # ------------------------------------------------------------------
    @staticmethod
    def _load_api_key() -> str:
        """从环境变量或 shell profile 加载 API Key。"""
        return IwenCaiKeyring.load_profile_key()
