"""
engine/screener.py — 统一选股引擎

以 TechnicalScoringSelector 作为唯一的盘前综合评分引擎。

流程：
  1. 加载日K数据
  2. TechnicalScoringSelector 跑全市场综合评分
  3. 叠加资金流、板块轮动与公司行动等独立数据因子
  4. 排序并输出 TOP N
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import pandas as pd

from strategy.selector.technical_scoring import TechnicalScoringSelector
from data.loader import DataLoader
from data.store.sqlite_store import StockStore
from data.tradeability import assess_tradeability

logger = logging.getLogger(__name__)

FINAL_BUY_THRESHOLD = 7.0
FINAL_WATCH_THRESHOLD = 4.0
DEFAULT_ENRICHMENT_POOL_SIZE = 100
THEME_FREE_SLOTS = 2
THEME_CONCENTRATION_STEP = 0.8
THEME_CONCENTRATION_CAP = 2.4

# 禁止交易的板块前缀
BLOCKED_CODE_PREFIXES = (
    "688",   # 科创板
    "8",     # 北交所（83xxxx / 87xxxx 等）
    "4",     # 北交所（43xxxx / 83xxxx 等老代码）
)


def is_tradeable(code: str) -> bool:
    """判断股票是否可交易（非科创板、非北证）"""
    return not code.startswith(BLOCKED_CODE_PREFIXES)


def filter_tradeable(codes: list) -> list:
    """过滤掉不可交易的股票代码"""
    return [c for c in codes if is_tradeable(c)]


class StockScreener:
    """统一选股筛选器"""

    def __init__(self, selector: Optional[TechnicalScoringSelector] = None):
        self.selector = selector or TechnicalScoringSelector()
        self.loader = DataLoader()
        self.store = StockStore()
        self._last_sector_rotation_contexts: Dict[str, dict] = {}

    # ----------------------------------------------------------------
    # 主入口
    # ----------------------------------------------------------------

    def screen(
        self,
        codes: Optional[List[str]] = None,
        date: str = "",
        top_n: int = 30,
        enrichment_pool_size: int = DEFAULT_ENRICHMENT_POOL_SIZE,
        refresh_intelligence: bool = False,
    ) -> pd.DataFrame:
        """执行选股"""
        logger.info("🔍 统一选股引擎启动...")

        # 1. 加载日K
        daily_data = self._load_daily_data(codes, expected_date=date)
        if not daily_data:
            logger.warning("无可用日K数据")
            return pd.DataFrame()

        logger.info(f"已加载 {len(daily_data)} 只股票日K数据")

        # 2. 先只用统一覆盖的本地日K生成较宽的技术初筛池。主题、新闻、
        # 财务和外部行情增强均不得影响进入初筛池。
        pool_size = max(int(top_n), int(enrichment_pool_size))
        technical_result = self._run_primary(
            daily_data,
            pool_size,
            include_enrichment=False,
        )
        if technical_result.empty:
            logger.info("主力评分引擎未选出股票")
            return pd.DataFrame()

        logger.info(
            "纯技术初筛池: %s 只，最终目标: %s 只",
            len(technical_result),
            top_n,
        )
        codes_for_boost = technical_result["code"].tolist()
        if refresh_intelligence:
            self._refresh_candidate_intelligence(codes_for_boost)

        enrichment_data = {
            code: daily_data[code]
            for code in codes_for_boost
            if code in daily_data
        }
        primary_result = self._run_primary(
            enrichment_data,
            pool_size,
            include_enrichment=True,
            score_floor=None,
        )
        if primary_result.empty:
            logger.info("增强评分阶段无可用结果")
            return pd.DataFrame()

        # Candidate enrichment refreshes current exchange names.  Re-apply the
        # hard gate so a newly renamed ST stock cannot survive merely because
        # the preloaded metadata was stale at the start of the scan.
        if date:
            primary_result = self._filter_enriched_tradeability(
                primary_result,
                daily_data,
                expected_date=date,
            )
            if primary_result.empty:
                logger.info("增强后交易资格复核无可用结果")
                return pd.DataFrame()

        technical_scores = technical_result.set_index("code")["score"].to_dict()
        primary_result["base_score"] = primary_result["code"].map(technical_scores).fillna(0.0)
        primary_result["enrichment_score"] = round(
            primary_result["score"] - primary_result["base_score"],
            1,
        )
        logger.info(
            "增强证据命中: 逻辑变化 %s/%s，基本面 %s/%s",
            int(primary_result.get("logic_available", pd.Series(dtype=bool)).sum()),
            len(primary_result),
            int(primary_result.get("fundamental_available", pd.Series(dtype=bool)).sum()),
            len(primary_result),
        )

        # 3. 资金流向加分（可选，失败时静默跳过）
        fund_flow_boost = self._compute_fund_flow_boost(codes_for_boost)
        primary_result["fund_flow_score"] = primary_result["code"].apply(
            lambda c: fund_flow_boost.get(c, 0)
        )

        # 4. 板块轮动加分（可选，失败时静默跳过）
        sector_rotation_boost = self._compute_sector_rotation_boost(codes_for_boost)
        primary_result["sector_rotation_score"] = primary_result["code"].apply(
            lambda c: sector_rotation_boost.get(c, (0, []))[0]
        )
        primary_result["sector_rotation_tags"] = primary_result["code"].apply(
            lambda c: "|".join(sector_rotation_boost.get(c, (0, []))[1])
        )
        primary_result["sector_context"] = primary_result["code"].apply(
            lambda c: self._last_sector_rotation_contexts.get(str(c).zfill(6), {})
        )

        # 5. 次一交易日除权除息短线扰动扣分（可选，失败时静默跳过）
        corporate_action_risks = self._compute_corporate_action_risks(codes_for_boost)
        primary_result["corporate_action_penalty"] = primary_result["code"].apply(
            lambda c: corporate_action_risks.get(c).penalty if c in corporate_action_risks else 0
        )
        primary_result["corporate_action_tags"] = primary_result["code"].apply(
            lambda c: corporate_action_risks.get(c).tag if c in corporate_action_risks else ""
        )
        if "signal_tags" in primary_result.columns:
            primary_result["signal_tags"] = primary_result.apply(
                lambda r: "|".join(
                    x for x in [str(r.get("signal_tags") or ""), str(r.get("corporate_action_tags") or "")]
                    if x
                ),
                axis=1,
            )

        primary_result["final_score"] = round(
            primary_result["score"]
            + primary_result["fund_flow_score"]
            + primary_result["sector_rotation_score"]
            + primary_result["corporate_action_penalty"],
            1,
        )

        primary_result = self._apply_theme_concentration_penalty(primary_result)

        # signal_type must reflect the final score and the entry/risk gate from
        # the primary selector.  Previously every TOP10 could remain "buy"
        # even after downstream penalties reduced its final score.
        primary_result["signal_type"] = primary_result.apply(
            self._classify_final_signal,
            axis=1,
        )

        result = primary_result[primary_result["signal_type"] != "pass"].copy()
        result = result.sort_values(
            ["final_score", "base_score", "code"],
            ascending=[False, False, True],
            kind="stable",
        )
        result = result.head(top_n).reset_index(drop=True)

        logger.info(
            f"✅ 选股完成: {len(result)} 只 "
            f"(buy {len(result[result['signal_type']=='buy'])}, "
            f"watch {len(result[result['signal_type']=='watch'])})"
        )
        return result

    @staticmethod
    def _apply_theme_concentration_penalty(result: pd.DataFrame) -> pd.DataFrame:
        """Softly diversify the candidate list without forcing weak names in.

        The first two names in a theme are unaffected.  Additional names remain
        eligible but need progressively stronger stock-specific evidence.
        """
        if result.empty or "theme_group" not in result.columns:
            result["theme_concentration_penalty"] = 0.0
            return result

        sort_columns = ["final_score"]
        ascending = [False]
        if "score" in result.columns:
            sort_columns.append("score")
            ascending.append(False)
        if "code" in result.columns:
            sort_columns.append("code")
            ascending.append(True)
        ranked = result.sort_values(
            sort_columns,
            ascending=ascending,
            kind="stable",
        ).copy()
        counts: Dict[str, int] = {}
        penalties: list[float] = []
        ranks: list[int] = []
        for group in ranked["theme_group"].fillna("").astype(str):
            if not group:
                ranks.append(0)
                penalties.append(0.0)
                continue
            counts[group] = counts.get(group, 0) + 1
            theme_rank = counts[group]
            ranks.append(theme_rank)
            excess = max(0, theme_rank - THEME_FREE_SLOTS)
            penalties.append(-min(THEME_CONCENTRATION_CAP, excess * THEME_CONCENTRATION_STEP))
        ranked["theme_rank"] = ranks
        ranked["theme_concentration_penalty"] = penalties
        ranked["final_score"] = round(
            ranked["final_score"] + ranked["theme_concentration_penalty"],
            1,
        )
        return ranked

    @staticmethod
    def _classify_final_signal(row: pd.Series) -> str:
        final_score = float(row.get("final_score") or 0)
        buy_eligible = bool(row.get("buy_eligible", False))
        if buy_eligible and final_score >= FINAL_BUY_THRESHOLD:
            return "buy"
        if final_score >= FINAL_WATCH_THRESHOLD:
            return "watch"
        return "pass"

    # ----------------------------------------------------------------
    # 内部
    # ----------------------------------------------------------------

    def _load_daily_data(
        self,
        codes: Optional[List[str]],
        *,
        expected_date: str = "",
    ) -> Dict[str, pd.DataFrame]:
        """加载日K数据"""
        daily_data: Dict[str, pd.DataFrame] = {}
        target_codes = codes or self._get_all_stock_codes()

        metadata: Dict[str, dict] = {}
        try:
            stocks = self.store.get_all_stocks(include_inactive=True)
            metadata = {
                str(row["code"]).zfill(6): row.to_dict()
                for _, row in stocks.iterrows()
            }
        except Exception as exc:
            logger.warning("股票交易资格元数据不可用: %s", exc)

        skipped = 0
        rejected: Dict[str, int] = {}
        for code in target_codes:
            if not is_tradeable(code):
                skipped += 1
                continue
            df = self.loader.get_daily(code, start_date="")
            meta = metadata.get(str(code).zfill(6), {})
            decision = assess_tradeability(
                code,
                str(meta.get("name") or ""),
                df,
                expected_date=expected_date,
                list_date=str(meta.get("list_date") or ""),
                is_active=bool(meta.get("is_active", True)),
            )
            if decision.eligible and len(df) > 30:
                daily_data[code] = df
                continue
            for reason in decision.reasons or ("insufficient_daily_history",):
                rejected[reason] = rejected.get(reason, 0) + 1
        if skipped:
            logger.info(f"已过滤 {skipped} 只不可交易股票（科创板/北证）")
        if rejected:
            summary = "、".join(f"{key}={value}" for key, value in sorted(rejected.items()))
            logger.info(
                "硬交易资格过滤（原因可重叠）%s：%s",
                len(target_codes) - skipped - len(daily_data),
                summary,
            )

        return daily_data

    def _run_primary(
        self,
        daily_data: dict,
        top_n: int,
        *,
        include_enrichment: bool,
        score_floor: float | None = FINAL_WATCH_THRESHOLD,
    ) -> pd.DataFrame:
        """主力评分引擎"""
        self.selector.set_param("top_n", top_n)
        ctx = {
            "daily_data": daily_data,
            "include_enrichment": include_enrichment,
            "score_floor": score_floor,
        }
        try:
            result = self.selector.evaluate(ctx)
            if isinstance(result, pd.DataFrame):
                return result
        except Exception as e:
            logger.error(f"主力评分引擎异常: {e}")

        return pd.DataFrame()

    def _refresh_candidate_intelligence(self, codes: list[str]) -> None:
        """批量刷新技术初筛池的公告与财务变化，失败时使用现有数据库。"""
        try:
            from data.services.candidate_enrichment_service import (
                CandidateEnrichmentService,
            )

            result = CandidateEnrichmentService(store=self.store).refresh(codes)
            logger.info(
                "候选信息刷新: 批次=%s 资料=%s 概念映射=%s "
                "新闻查询=%s 缓存命中=%s 延后=%s 事件=%s 财务=%s 新增=%s 错误=%s",
                result["batches"],
                result.get("profiles_seen", 0),
                result.get("concept_memberships", 0),
                result.get("news_queries", 0),
                result.get("news_skipped_fresh", 0),
                result.get("news_deferred", 0),
                result["events_seen"],
                result["financials_seen"],
                result["inserted"],
                len(result["errors"]),
            )
        except Exception as e:
            logger.warning("候选信息刷新跳过，继续使用数据库已有证据: %s", e)

    def _filter_enriched_tradeability(
        self,
        result: pd.DataFrame,
        daily_data: Dict[str, pd.DataFrame],
        *,
        expected_date: str,
    ) -> pd.DataFrame:
        try:
            stocks = self.store.get_all_stocks(include_inactive=True)
            metadata = {
                str(row["code"]).zfill(6): row.to_dict()
                for _, row in stocks.iterrows()
            }
        except Exception as exc:
            logger.warning("增强后交易资格复核跳过: %s", exc)
            return result

        eligible: list[str] = []
        rejected: Dict[str, int] = {}
        for code in result["code"].astype(str):
            normalized = code.zfill(6)
            meta = metadata.get(normalized, {})
            decision = assess_tradeability(
                normalized,
                str(meta.get("name") or ""),
                daily_data.get(normalized, pd.DataFrame()),
                expected_date=expected_date,
                list_date=str(meta.get("list_date") or ""),
                is_active=bool(meta.get("is_active", True)),
            )
            if decision.eligible:
                eligible.append(normalized)
                continue
            for reason in decision.reasons:
                rejected[reason] = rejected.get(reason, 0) + 1
        if rejected:
            logger.info(
                "增强后硬过滤 %s 只：%s",
                len(result) - len(eligible),
                "、".join(f"{key}={value}" for key, value in sorted(rejected.items())),
            )
        return result[result["code"].astype(str).str.zfill(6).isin(eligible)].copy()

    def _compute_fund_flow_boost(self, codes: list) -> Dict[str, float]:
        """调用资金流向服务计算加分，失败返回空。"""
        try:
            from data.fund_flow_filter import FundFlowFilter
            # 全市场筛选使用批量结果，不对缺失股票逐只补查。这样扩大
            # 初筛池后仍能控制问财请求量；持仓盯盘等小批量调用不受影响。
            fff = FundFlowFilter(fill_missing=False)
            boost = fff.compute_boost(codes)
            if boost:
                boosted = sum(1 for v in boost.values() if v != 0)
                logger.info(f"  资金流向: {boosted}/{len(codes)} 只有效")
            return boost
        except Exception as e:
            logger.warning(f"  资金流向查询跳过: {e}")
            return {}

    def _compute_sector_rotation_boost(self, codes: list) -> Dict[str, tuple]:
        """调用板块轮动服务计算加分，失败返回空。"""
        self._last_sector_rotation_contexts = {}
        try:
            from data.services.sector_rotation_service import SectorRotationService
            svc = SectorRotationService(store=self.store)
            snapshot = svc.get_snapshot(refresh=False)
            contexts = svc.get_stock_contexts(codes, snapshot=snapshot)
            self._last_sector_rotation_contexts = contexts
            boost = {
                code: (
                    float(context.get("rotation_score") or 0),
                    [str(value) for value in context.get("tags") or []],
                )
                for code, context in contexts.items()
                if context.get("matches")
            }
            if boost:
                boosted = sum(1 for v, _ in boost.values() if v != 0)
                logger.info(f"  板块轮动: {boosted}/{len(codes)} 只有效")
            return boost
        except Exception as e:
            logger.warning(f"  板块轮动查询跳过: {e}")
            return {}

    def _compute_corporate_action_risks(self, codes: list) -> Dict[str, object]:
        """查询次一交易日除权除息，返回短线扰动扣分。"""
        try:
            from data.corporate_actions import get_next_day_corporate_action_risks
            risks = get_next_day_corporate_action_risks(codes)
            if risks:
                logger.info(f"  除权除息风控: {len(risks)}/{len(codes)} 只次一交易日除权除息")
            return risks
        except Exception as e:
            logger.warning(f"  除权除息查询跳过: {e}")
            return {}

    def _get_all_stock_codes(self) -> List[str]:
        """获取全部可交易的活跃股票代码（已过滤科创板/北证）"""
        try:
            stocks = self.store.get_active_stocks()
            if stocks.empty:
                return []
            return filter_tradeable(stocks["code"].tolist())
        except Exception:
            return []
