"""统一资金流向服务。

架构：
  fund_flow_service
    ├── IwenCaiAdapter (主力) — 同花顺问财 OpenAPI
    └── 预留其他适配器插槽 (EastMoney / Sina / 自定义)

调用方（策略、盯盘、复盘）只依赖 FundFlow 契约和服务接口，
数据源可随时替换或扩展。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from data.adapters.base import DataSourceAdapter
from data.contracts import FundFlow

logger = logging.getLogger(__name__)


class FundFlowProvider:
    """资金流向数据提供者接口。

    封装一个 DataSourceAdapter，统一定义查询能力。
    后续增加新数据源只需实现相同接口并注册到 FundFlowService。
    """

    adapter: DataSourceAdapter

    def __init__(self, adapter: DataSourceAdapter):
        self.adapter = adapter

    def get_fund_flow(self, codes: List[str], date: str = "") -> List[FundFlow]:
        return self.adapter.get_fund_flow(codes, date)

    def get_fund_flow_top(self, n: int = 20, date: str = "") -> List[FundFlow]:
        if hasattr(self.adapter, "get_fund_flow_top"):
            return self.adapter.get_fund_flow_top(n, date)
        return []

    @property
    def name(self) -> str:
        return self.adapter.name


class FundFlowService:
    """资金流向服务 — 多源聚合与降级。

    使用方式:
        svc = FundFlowService()
        svc.register("iwencai", IwenCaiAdapter())
        flows = svc.get_fund_flow(["600519", "300033"])
        # 或查主力净流入排名
        top20 = svc.get_fund_flow_top(20)
    """

    def __init__(self):
        self._providers: Dict[str, FundFlowProvider] = {}
        self._priority: List[str] = []  # 优先级顺序
        self.last_errors: List[str] = []
        self.last_code_errors: Dict[str, str] = {}

    def register(self, name: str, adapter: DataSourceAdapter, priority: int = 0):
        """注册一个数据源。priority 越小越优先（0 最高）。"""
        self._providers[name] = FundFlowProvider(adapter)
        if name not in self._priority:
            self._priority.append(name)

    def get_provider(self, name: str) -> Optional[FundFlowProvider]:
        return self._providers.get(name)

    @property
    def default_provider(self) -> Optional[FundFlowProvider]:
        """返回第一个可用的 provider。"""
        for name in self._priority:
            if name in self._providers:
                return self._providers[name]
        return None

    def get_fund_flow(
        self,
        codes: List[str],
        date: str = "",
        provider: Optional[str] = None,
        fallback: bool = True,
    ) -> List[FundFlow]:
        """获取个股资金流向。

        Args:
            codes: 股票代码列表
            date: 日期 YYYYMMDD，默认今天
            provider: 指定数据源名称，None 则按优先级自动选择
            fallback: 主源失败时是否降级到备选源
        """
        date = date or datetime.now().strftime("%Y%m%d")
        self.last_errors = []
        self.last_code_errors = {}

        if provider:
            p = self._providers.get(provider)
            if p:
                try:
                    result = p.get_fund_flow(codes, date)
                    self._collect_code_errors(provider, p)
                    self._clear_recovered_errors(result)
                    return result
                except Exception as e:
                    self.last_errors = [f"{provider}: {e}"]
                    self.last_code_errors = {
                        str(code).zfill(6): f"{provider}: {e}" for code in codes
                    }
                    raise
            return []

        # 按优先级尝试
        errors = []
        for name in self._priority:
            p = self._providers.get(name)
            if not p:
                continue
            try:
                result = p.get_fund_flow(codes, date)
                self._collect_code_errors(name, p)
                self._clear_recovered_errors(result)
                if result:
                    if self.last_code_errors:
                        logger.warning(
                            "[FundFlow] provider=%s 部分缺失: %s/%s",
                            name,
                            len(self.last_code_errors),
                            len(codes),
                        )
                    return result
                logger.warning(f"[FundFlow] provider={name} 返回空数据")
                if self.last_code_errors:
                    unique_errors = list(dict.fromkeys(self.last_code_errors.values()))
                    errors.append("; ".join(unique_errors[:3]))
            except Exception as e:
                logger.warning(f"[FundFlow] provider={name} 失败: {e}")
                errors.append(f"{name}: {e}")
                for code in codes:
                    self.last_code_errors.setdefault(str(code).zfill(6), f"{name}: {e}")
                self.last_errors = errors[:]
                if not fallback:
                    raise

        if errors:
            self.last_errors = errors[:]
            logger.error(f"[FundFlow] 全部 {len(errors)} 个 provider 失败: {'; '.join(errors)}")
        return []

    def _collect_code_errors(self, provider: str, source: FundFlowProvider) -> None:
        raw = getattr(source.adapter, "last_code_errors", {}) or {}
        for code, message in raw.items():
            self.last_code_errors[str(code).zfill(6)] = f"{provider}: {message}"

    def _clear_recovered_errors(self, flows: List[FundFlow]) -> None:
        for flow in flows:
            self.last_code_errors.pop(str(flow.code).zfill(6), None)

    def get_fund_flow_top(
        self,
        n: int = 20,
        date: str = "",
        provider: Optional[str] = None,
    ) -> List[FundFlow]:
        """获取主力资金净流入排名前 N 的股票。"""
        p = self._providers.get(provider) if provider else self.default_provider
        if not p:
            return []
        try:
            return p.get_fund_flow_top(n, date)
        except Exception as e:
            logger.error(f"[FundFlow] top={n} 查询失败: {e}")
            return []

    def list_providers(self) -> List[str]:
        """列出所有已注册的数据源。"""
        return list(self._providers.keys())
