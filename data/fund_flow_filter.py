"""资金流向过滤器 — 选股/建仓/盯盘共用的资金流向辅助模块。

使用方式：
  from data.fund_flow_filter import FundFlowFilter

  fff = FundFlowFilter()
  # 对候选池打分
  boost = fff.compute_boost(["600519","300033"])
  # 获取单只流向
  flow = fff.get_flow("300033")
  # 主力净流入排名前20
  top20 = fff.get_top_net_inflows(20)
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from data.contracts import FundFlow
from data.services.fund_flow_service import FundFlowService

logger = logging.getLogger(__name__)


class FundFlowFilter:
    """资金流向辅助工具。

    封装 FundFlowService，提供面向选股/盯盘场景的快捷方法。
    所有网络调用都通过 FundFlowService 的多源降级机制。
    """

    def __init__(
        self,
        service: Optional[FundFlowService] = None,
        *,
        fill_missing: bool = True,
    ):
        if service is None:
            from data.adapters.iwencai_adapter import IwenCaiAdapter

            service = FundFlowService()
            try:
                service.register(
                    "iwencai",
                    IwenCaiAdapter(fill_missing=fill_missing),
                )
            except Exception as e:
                logger.warning(f"问财 adapter 注册失败: {e}")
        self._service = service

    # ------------------------------------------------------------------
    # 选股/建仓场景
    # ------------------------------------------------------------------
    def compute_boost(
        self, codes: List[str], date: str = ""
    ) -> Dict[str, float]:
        """对候选池计算资金流向加分。

        返回 {code: boost_score}，范围约 [-2.0, +3.0]。
        主力和大单同时净流入 +2.5，单一净流入 +1.0，双杀 -2.0。
        """
        if not codes:
            return {}

        try:
            flows = self._service.get_fund_flow(codes, date=date)
        except Exception as e:
            logger.warning(f"资金流向查询失败，跳过: {e}")
            return {}

        boost: Dict[str, float] = {}
        for f in flows:
            score = 0.0
            # 主力净流入
            if f.main_net_inflow > 0:
                score += 1.0
                # 大额流入再加码
                if abs(f.main_net_inflow) > 50_000_000:  # >5千万
                    score += 0.5
                if abs(f.main_net_inflow) > 200_000_000:  # >2亿
                    score += 0.5
            elif f.main_net_inflow < 0:
                score -= 0.5
                if abs(f.main_net_inflow) > 100_000_000:
                    score -= 1.0

            # 大单净流入（DDE）
            if f.big_net_inflow > 0:
                score += 0.5
            elif f.big_net_inflow < 0:
                score -= 0.3

            # 主力占比
            if f.main_net_pct > 5:
                score += 0.5

            boost[f.code] = round(score, 2)

        return boost

    def get_flow(self, code: str, date: str = "") -> Optional[FundFlow]:
        """获取单只股票资金流向。"""
        flows = self._service.get_fund_flow([code], date=date)
        return flows[0] if flows else None

    def get_top_net_inflows(self, n: int = 20, date: str = "") -> List[FundFlow]:
        """获取主力净流入排名前 N 的股票。"""
        return self._service.get_fund_flow_top(n, date)

    # ------------------------------------------------------------------
    # 盯盘场景
    # ------------------------------------------------------------------
    def summarize(self, code: str, date: str = "") -> str:
        """返回单只股票资金流向的文字摘要，用于盯盘输出。"""
        flow = self.get_flow(code, date)
        if not flow:
            return "无数据"

        parts = []
        # 主力
        if flow.main_net_inflow > 0:
            parts.append(f"主力净入{self._fmt(flow.main_net_inflow)}")
        elif flow.main_net_inflow < 0:
            parts.append(f"主力净出{self._fmt(abs(flow.main_net_inflow))}")

        # 大单
        if flow.big_net_inflow != 0:
            direction = "入" if flow.big_net_inflow > 0 else "出"
            parts.append(f"大单净{direction}{self._fmt(abs(flow.big_net_inflow))}")

        # 小单
        if flow.retail_net_inflow != 0:
            direction = "入" if flow.retail_net_inflow > 0 else "出"
            parts.append(f"小单净{direction}{self._fmt(abs(flow.retail_net_inflow))}")

        # 占比
        if flow.main_net_pct > 0:
            parts.append(f"占比{flow.main_net_pct:.1f}%")

        return " | ".join(parts)

    def batch_summarize(
        self, codes: List[str], date: str = ""
    ) -> Dict[str, Tuple[Optional[FundFlow], str]]:
        """批量获取多只股票的资金流向摘要。

        返回 {code: (FundFlow, summary_str)}
        """
        try:
            flows = self._service.get_fund_flow(codes, date=date)
        except Exception as e:
            logger.warning(f"批量资金流向查询失败: {e}")
            return {}

        flow_map = {f.code: f for f in flows}
        error_summary = ""
        if not flows and getattr(self._service, "last_errors", None):
            error_summary = "资金流未取到: " + "; ".join(self._service.last_errors)
            if len(error_summary) > 180:
                error_summary = error_summary[:177].rstrip() + "..."
        result = {}
        for code in codes:
            f = flow_map.get(code)
            if f:
                result[code] = (f, self._summarize_from_flow(f))
            else:
                code_error = getattr(self._service, "last_code_errors", {}).get(code)
                summary = (
                    f"资金流未取到: {code_error}"
                    if code_error
                    else error_summary or "无数据"
                )
                if len(summary) > 180:
                    summary = summary[:177].rstrip() + "..."
                result[code] = (None, summary)
        return result

    @staticmethod
    def _summarize_from_flow(flow: FundFlow) -> str:
        parts = []
        if flow.main_net_inflow > 0:
            parts.append(f"主力净入{_fmt(flow.main_net_inflow)}")
        elif flow.main_net_inflow < 0:
            parts.append(f"主力净出{_fmt(abs(flow.main_net_inflow))}")
        if flow.big_net_inflow != 0:
            d = "入" if flow.big_net_inflow > 0 else "出"
            parts.append(f"大单净{d}{_fmt(abs(flow.big_net_inflow))}")
        if flow.main_net_pct > 0:
            parts.append(f"占比{flow.main_net_pct:.1f}%")
        return " | ".join(parts) if parts else "持平"

    @staticmethod
    def _fmt(val: float) -> str:
        if abs(val) >= 100_000_000:
            return f"{val/100_000_000:.2f}亿"
        return f"{val/10000:.0f}万"


def _fmt(val: float) -> str:
    if abs(val) >= 100_000_000:
        return f"{val/100_000_000:.2f}亿"
    return f"{val/10000:.0f}万"
