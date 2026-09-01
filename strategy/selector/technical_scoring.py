"""
strategy/selector/technical_scoring.py — 综合技术面评分选股策略

统一 daily_screen.py 的评分逻辑，作为唯一的盘前综合评分策略。

评分维度：
  趋势排列（多头/偏多/偏空/空头）
  均线金叉/死叉
  MACD 金叉
  KDJ 金叉
  成交量分析（放量/缩量/量比）
  超跌反转
  主线匹配与增量逻辑变化

核心操盘原则：
  低位多看逻辑变化，高位多看趋势量价。
  静态命中十五五/AI算力等方向只算主线匹配；
  真正逻辑变化必须有新闻、热词、公告、板块或资金等增量证据；
  高位若趋势量价不配合、放量滞涨或破位则降权。

输出 DataFrame: code, name, price, score, signal_type, signal_tags, trend, pct_change, vol_ratio
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from strategy.base import BaseStrategy
from data.fifteen_five_pool import FIFTEEN_FIVE_STOCKS
from data.ai_compute_pool import AI_COMPUTE_STOCKS, get_stock_bonus, get_stock_tags
from data.fundamental_llm import FundamentalLLMScore, get_fundamental_llm_scores
from data.financial_scoring import FinancialScore, get_financial_scores
from data.logic_change import LogicChangeEvidence, get_logic_change_evidence

logger = logging.getLogger(__name__)


class TechnicalScoringSelector(BaseStrategy):
    """综合技术面评分选股 — 主力评分引擎"""

    def __init__(self, name: str = "tech_scoring", params: Optional[Dict] = None):
        super().__init__(name, params or {
            "score_threshold_buy": 7.0,     # 最终分达到门槛且路线条件满足 → buy
            "score_threshold_watch": 4.0,   # >= 此分 → watch
            "fifteen_five_bonus": 1.2,      # 静态十五五主线匹配小幅加分
            "ai_compute_bonus_scale": 0.7,  # AI算力/科技赛道静态匹配加分缩放
            "theme_group_cap": 2.2,         # 高相关静态主题合并后的总加分上限
            "fundamental_llm_enabled": True,  # LLM财报/MD&A中期好赛道因子
            "fundamental_llm_boost_scale": 1.0,
            "weight_low_fundamental_confirmed": 0.3,  # 低位好赛道+技术确认
            "min_days": 30,                 # 最少需要多少天K线数据
            "top_n": 30,                    # 最多返回多少只
            # 各维度权重（可通过 params 热调）
            "weight_trend_bull": 3.0,       # 多头排列
            "weight_trend_bias_bull": 1.5,  # 偏多
            "weight_trend_bear": -1.0,      # 空头惩罚
            "weight_golden_cross": 2.0,     # 均线金叉
            "weight_macd_buy": 2.0,         # MACD 金叉
            "weight_kdj_buy": 1.0,          # KDJ 金叉
            "weight_vol_huge": 2.5,         # 巨量 (vol_ratio>2)
            "weight_vol_big": 1.5,          # 放量 (vol_ratio>1.5)
            "weight_vol_small": 0.5,        # 微放量 (vol_ratio>1.2)
            "weight_oversold_reversal": 2.5,  # 超跌+金叉反转
            "weight_oversold": 1.0,         # 超跌
            "weight_dead_cross": -2.0,      # 死叉惩罚
            "weight_limit_up_penalty": -1.5,  # 涨停追入风险
            "weight_dried_volume": -1.0,    # 缩量惩罚 (vol_ratio<0.3)
            "weight_low_mainline_match": 0.8,  # 低位静态主线匹配加分
            "weight_low_mainline_confirmed": 0.5,  # 低位主线匹配+技术确认
            "weight_low_logic_confirmed": 0.8,  # 低位增量逻辑变化+技术确认
            "weight_high_trend_volume_confirm": 1.0,  # 高位趋势量价确认
            "weight_high_no_confirmation": -1.5,  # 高位趋势/量价不确认惩罚
            "weight_high_stall_penalty": -2.0,  # 高位放量滞涨惩罚
            "weight_high_upper_shadow": -1.5,  # 高位长上影
            "weight_high_break_ma": -1.5,  # 高位跌破短均线
            "weight_high_macd_divergence": -1.5,  # 高位MACD顶背离
            "weight_good_news_absorption": 0.6,  # 利好后温和放量承接
            "weight_sell_news_penalty": -2.0,  # 高位利好兑现/借利好出货
            "weight_good_news_no_rise": -1.5,  # 正面逻辑出现后股价不涨
            "weight_bad_news_absorption": 0.8,  # 低位利空后出现承接，部分抵销负面
            "weight_early_start_setup": 1.8,  # 低位趋势萌芽，进入跨日观察而非等待急拉
            "weight_strong_continuation": 1.8,  # 主升延续质量，不因高位机械拒绝
        })

    # ---- evaluate ----

    def evaluate(self, context: Dict[str, Any]) -> pd.DataFrame:
        """主入口：跑全市场技术初筛或指定池子的增强评分。"""
        daily_data: Dict[str, pd.DataFrame] = context.get("daily_data", {})
        if not daily_data:
            logger.warning("TechnicalScoringSelector: 无日K数据")
            return pd.DataFrame()

        min_days = self.get_param("min_days", 30)
        threshold_watch = self.get_param("score_threshold_watch", 1.5)
        top_n = self.get_param("top_n", 30)
        include_enrichment = bool(context.get("include_enrichment", True))
        discovery_mode = not include_enrichment
        score_floor = context.get("score_floor", threshold_watch)

        logic_evidence: dict[str, LogicChangeEvidence] = {}
        fundamental_scores: dict[str, FundamentalLLMScore] = {}
        financial_scores: dict[str, FinancialScore] = {}
        if include_enrichment:
            try:
                logic_evidence = get_logic_change_evidence(daily_data.keys())
            except Exception as e:
                logger.warning("逻辑变化证据查询跳过: %s", e)
            if self.get_param("fundamental_llm_enabled", True):
                try:
                    fundamental_scores = get_fundamental_llm_scores(daily_data.keys())
                    if fundamental_scores:
                        logger.info(
                            "LLM财报评分: %s/%s 只有效",
                            len(fundamental_scores),
                            len(daily_data),
                        )
                except Exception as e:
                    logger.warning("LLM财报评分查询跳过: %s", e)
            try:
                financial_scores = get_financial_scores(daily_data.keys())
                if financial_scores:
                    logger.info(
                        "结构化财务评分: %s/%s 只有效",
                        len(financial_scores),
                        len(daily_data),
                    )
            except Exception as e:
                logger.warning("结构化财务评分查询跳过: %s", e)

        results: List[dict] = []
        for code, df in daily_data.items():
            if df is None or df.empty or len(df) < min_days:
                continue
            normalized = str(code).zfill(6)
            r = self._score_one(
                normalized,
                df,
                logic_evidence.get(normalized),
                fundamental_scores.get(normalized),
                financial_scores.get(normalized),
                include_enrichment=include_enrichment,
            )
            if r is None:
                continue
            qualifies_setup = (
                discovery_mode
                and r.get("setup_stage") in {"warming", "actionable"}
                and float(r.get("setup_score") or 0) >= 3.5
            )
            if score_floor is None or r["score"] >= float(score_floor) or qualifies_setup:
                results.append(r)

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results)
        sort_key = "discovery_score" if discovery_mode else "score"
        result_df.sort_values(
            [sort_key, "score", "code"],
            ascending=[False, False, True],
            kind="stable",
            inplace=True,
        )
        result_df.reset_index(drop=True, inplace=True)
        if not discovery_mode:
            return result_df.head(top_n)

        # Preserve both entry routes in the finite enrichment pool.  Without
        # route-aware slots, mature trends crowd out lower absolute-score
        # setups before their multi-day improvement can ever be observed.
        early_limit = max(1, int(top_n * 0.6))
        continuation_limit = max(1, int(top_n * 0.4))
        chosen = []
        chosen_codes: set[str] = set()
        for route, limit in (
            ("early_start", early_limit),
            ("strong_continuation", continuation_limit),
        ):
            rows = result_df[result_df["entry_route"] == route].head(limit)
            chosen.extend(rows.to_dict("records"))
            chosen_codes.update(rows["code"].astype(str))
        for row in result_df.to_dict("records"):
            if len(chosen) >= top_n:
                break
            if str(row["code"]) in chosen_codes:
                continue
            chosen.append(row)
            chosen_codes.add(str(row["code"]))
        selected = pd.DataFrame(chosen)
        selected.sort_values(
            [sort_key, "score", "code"],
            ascending=[False, False, True],
            kind="stable",
            inplace=True,
        )
        return selected.reset_index(drop=True)

    # ---- 单只股票评分 ----

    def _score_one(
        self,
        code: str,
        df: pd.DataFrame,
        logic: Optional[LogicChangeEvidence] = None,
        fundamental: Optional[FundamentalLLMScore] = None,
        financial: Optional[FinancialScore] = None,
        *,
        include_enrichment: bool = True,
    ) -> Optional[dict]:
        """对单只股票评分；技术初筛时不读取或应用外部增强因子。"""
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)

        price = float(close.iloc[-1])
        if price <= 0:
            return None

        # -- 均线 --
        ma5_arr = close.rolling(5).mean()
        ma10_arr = close.rolling(10).mean()
        ma20_arr = close.rolling(20).mean()
        ma5 = float(ma5_arr.iloc[-1])
        ma10 = float(ma10_arr.iloc[-1])
        ma20 = float(ma20_arr.iloc[-1])

        # -- 趋势 --
        if ma5 > ma10 > ma20:
            trend = "多头"
        elif ma5 < ma10 < ma20:
            trend = "空头"
        elif ma5 > ma20:
            trend = "偏多"
        else:
            trend = "偏空"

        # -- 涨跌幅 --
        pct = round(float(close.pct_change().iloc[-1] * 100), 2)
        pct_3d = round((close.iloc[-1] / close.iloc[-4] - 1) * 100, 2) if len(close) >= 4 else 0
        pct_5d = round((close.iloc[-1] / close.iloc[-6] - 1) * 100, 2) if len(close) >= 6 else 0
        prev_close = float(close.iloc[-2])
        today_open = float(df["open"].iloc[-1] if "open" in df.columns else price)
        gap_pct = (today_open / prev_close - 1) * 100 if prev_close > 0 else 0.0
        intraday_pct = (price / today_open - 1) * 100 if today_open > 0 else 0.0

        # -- 相对位置（低位/高位）--
        # 用近60日区间做位置判断。
        # 低位（≤35%分位）：更重视逻辑变化（产业/政策/订单/景气）。
        # 高位（≥75%分位）：更重视趋势量价，并检测长上影/破位/背离。
        window = min(len(close), 60)
        recent_high = float(high.tail(window).max())
        recent_low = float(low.tail(window).min())
        if recent_high > recent_low:
            position_pct = (price - recent_low) / (recent_high - recent_low)
        else:
            position_pct = 0.5
        low_zone = position_pct <= 0.35
        high_zone = position_pct >= 0.75

        # -- 成交量 --
        vol_ma10 = float(volume.rolling(10).mean().iloc[-1])
        vol_now = float(volume.iloc[-1])
        vol_ratio = round(vol_now / vol_ma10, 2) if vol_ma10 > 0 else 1.0

        # -- 入场准备结构 --
        # 日线只负责识别“正在变好”或“强势仍在延续”。真正买点仍由
        # 次日分时验证，避免把收盘快照直接当成交易指令。
        previous_20_high = float(high.iloc[-21:-1].max()) if len(high) >= 21 else recent_high
        breakout_distance_pct = (
            (price / previous_20_high - 1) * 100 if previous_20_high > 0 else 0.0
        )
        range_pct = (high - low) / close.shift(1).replace(0, float("nan")) * 100
        range_5 = float(range_pct.tail(5).mean())
        range_20 = float(range_pct.tail(20).mean())
        range_compression = range_5 / range_20 if range_20 > 0 else 1.0
        prior_floor = float(low.iloc[-20:-5].min()) if len(low) >= 20 else float(low.iloc[:-5].min())
        recent_floor = float(low.tail(5).min())
        floor_holding = recent_floor >= prior_floor * 0.985 if prior_floor > 0 else False
        ma5_rising = ma5 > float(ma5_arr.iloc[-3]) if len(ma5_arr) >= 3 else False
        ma10_rising = ma10 > float(ma10_arr.iloc[-3]) if len(ma10_arr) >= 3 else False
        daily_return = close.pct_change()
        up_volume = volume.tail(10)[daily_return.tail(10) > 0]
        down_volume = volume.tail(10)[daily_return.tail(10) < 0]
        up_down_volume_ratio = (
            float(up_volume.mean()) / float(down_volume.mean())
            if len(up_volume) and len(down_volume) and float(down_volume.mean()) > 0 else 1.0
        )
        today_range = float(high.iloc[-1] - low.iloc[-1])
        close_location = (
            (price - float(low.iloc[-1])) / today_range if today_range > 0 else 0.5
        )
        ret_20d = (
            (price / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else 0.0
        )

        # -- MACD --
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9).mean()
        macd_buy = bool(dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1])

        # -- 均线金叉/死叉 --
        prev_ma5 = float(close.rolling(5).mean().iloc[-2])
        prev_ma10 = float(close.rolling(10).mean().iloc[-2])
        golden = bool(prev_ma5 <= prev_ma10 and ma5 > ma10)
        dead = bool(prev_ma5 >= prev_ma10 and ma5 < ma10)

        # -- KDJ --
        low_5 = low.rolling(5).min()
        high_5 = high.rolling(5).max()
        rsv = (close - low_5) / (high_5 - low_5) * 100
        k = rsv.ewm(com=2).mean()
        d = k.ewm(com=2).mean()
        kdj_buy = bool(k.iloc[-2] <= d.iloc[-2] and k.iloc[-1] > d.iloc[-1])

        # ===== 评分 =====
        score = 0.0
        tags: List[str] = []
        risk_tags: List[str] = []
        theme_bonus = 0.0
        logic_score = 0.0
        fundamental_score = 0.0
        event_reaction_score = 0.0
        sell_news_risk = False
        high_stall = False
        high_upper_shadow = False
        high_break = False
        high_divergence = False
        setup_triggers: List[str] = []
        setup_risks: List[str] = []

        # 趋势
        if trend == "多头":
            score += self.get_param("weight_trend_bull", 3.0)
            tags.append("多头排列")
        elif trend == "偏多":
            score += self.get_param("weight_trend_bias_bull", 1.5)
        elif trend == "空头":
            score += self.get_param("weight_trend_bear", -1.0)

        # 金叉
        if golden:
            score += self.get_param("weight_golden_cross", 2.0)
            tags.append("均线金叉")
        if macd_buy:
            score += self.get_param("weight_macd_buy", 2.0)
            tags.append("MACD金叉")
        if kdj_buy:
            score += self.get_param("weight_kdj_buy", 1.0)
            tags.append("KDJ金叉")

        # 成交量
        if vol_ratio > 2.0:
            score += self.get_param("weight_vol_huge", 2.5)
            tags.append(f"巨量{vol_ratio}x")
        elif vol_ratio > 1.5:
            score += self.get_param("weight_vol_big", 1.5)
            tags.append(f"放量{vol_ratio}x")
        elif vol_ratio > 1.2:
            score += self.get_param("weight_vol_small", 0.5)
            tags.append(f"微放量{vol_ratio}x")

        # 超跌反转
        if pct_5d < -10 and macd_buy:
            score += self.get_param("weight_oversold_reversal", 2.5)
            tags.append("超跌反转")
        elif pct_3d < -8:
            score += self.get_param("weight_oversold", 1.0)
            tags.append("超跌")

        # 死叉 / 空头
        if dead:
            score += self.get_param("weight_dead_cross", -2.0)
            tags.append("死叉")
        if pct > 9.8:
            score += self.get_param("weight_limit_up_penalty", -1.5)
        if vol_ratio < 0.3:
            score += self.get_param("weight_dried_volume", -1.0)

        concepts = []

        # 静态主题只能作为一组证据。十五五、AI算力及其子概念高度
        # 相关，展示标签可以并存，但分数不再逐项累加。
        extra_info = ""
        fifteen_bonus = 0.0
        ai_bonus = 0.0
        if include_enrichment and code in FIFTEEN_FIVE_STOCKS:
            fifteen_bonus = self.get_param("fifteen_five_bonus", 1.2)
            tags.append("十五五主线")
            info = FIFTEEN_FIVE_STOCKS[code]
            concepts.extend(info["concepts"])

        # AI算力/科技细分赛道匹配
        if include_enrichment and code in AI_COMPUTE_STOCKS:
            ai_tags = get_stock_tags(code)
            ai_bonus = get_stock_bonus(code) * self.get_param("ai_compute_bonus_scale", 0.7)
            tags.append("AI算力主线")
            tags.extend(ai_tags[:3])
            concepts.extend(ai_tags)

        theme_bonus = max(fifteen_bonus, ai_bonus)
        theme_bonus = min(theme_bonus, self.get_param("theme_group_cap", 2.2))
        score += theme_bonus

        # 低位多看逻辑变化，高位多看趋势量价。
        has_mainline_match = bool(concepts)
        has_logic_change = bool(
            include_enrichment and logic and logic.level != "none"
        )
        has_basic_confirm = trend in {"多头", "偏多"} or golden or macd_buy or kdj_buy or vol_ratio > 1.2
        if include_enrichment and fundamental:
            fundamental_score = fundamental.boost * self.get_param(
                "fundamental_llm_boost_scale",
                1.0,
            )
            score += fundamental_score
            tags.extend(fundamental.tags)
            if low_zone and fundamental_score > 0 and has_basic_confirm:
                score += self.get_param("weight_low_fundamental_confirmed", 0.3)
                tags.append("低位好赛道确认")
        elif include_enrichment and financial:
            fundamental_score = financial.boost
            score += fundamental_score
            tags.extend(financial.tags)
            if low_zone and fundamental_score > 0 and has_basic_confirm:
                score += self.get_param("weight_low_fundamental_confirmed", 0.3)
                tags.append("低位基本面确认")

        if include_enrichment and logic:
            logic_score = logic.boost + logic.penalty
            score += logic_score
            tags.extend(logic.risk_tags)
            risk_tags.extend(logic.risk_tags)
        if include_enrichment and has_logic_change:
            tags.extend(logic.tags)

        if low_zone and has_mainline_match:
            score += self.get_param("weight_low_mainline_match", 0.8)
            tags.append("低位主线匹配")
            if has_basic_confirm:
                score += self.get_param("weight_low_mainline_confirmed", 0.5)
                tags.append("低位主线确认")

        if low_zone and has_logic_change and has_basic_confirm:
            score += self.get_param("weight_low_logic_confirmed", 0.8)
            tags.append("低位逻辑确认")

        # 低位出现增量逻辑后，只有温和放量、收盘不弱于开盘才视为
        # 启动确认；单纯有利好不直接生成买点。
        if (
            low_zone and has_logic_change and 0 <= pct <= 7
            and 1.2 <= vol_ratio <= 2.8 and intraday_pct >= -0.5
        ):
            reaction = self.get_param("weight_good_news_absorption", 0.6)
            score += reaction
            event_reaction_score += reaction
            tags.append("低位利好承接")

        # 利空后低位放量收回可视为承接，但只能部分抵销负面证据。
        if (
            low_zone and logic and logic.penalty < 0 and pct > 0
            and vol_ratio >= 1.2 and intraday_pct >= 0
        ):
            reaction = min(
                abs(logic.penalty) * 0.5,
                self.get_param("weight_bad_news_absorption", 0.8),
            )
            score += reaction
            event_reaction_score += reaction
            tags.append("低位利空承接")

        if high_zone:
            trend_ok = trend in {"多头", "偏多"}
            volume_ok = vol_ratio >= 1.2

            # 高位趋势量价确认
            if trend_ok and volume_ok and pct >= 0:
                score += self.get_param("weight_high_trend_volume_confirm", 0.8)
                tags.append("高位量价确认")
            else:
                score += self.get_param("weight_high_no_confirmation", -1.2)
                tags.append("高位待确认")

            # 高位放量滞涨
            if vol_ratio >= 1.8 and pct < 1.0:
                score += self.get_param("weight_high_stall_penalty", -1.5)
                tags.append("高位放量滞涨")
                risk_tags.append("高位放量滞涨")
                high_stall = True

            # 高位长上影：今日最高价显著高于收盘，且收盘在今日区间下半部
            today_high = float(high.iloc[-1])
            today_low = float(low.iloc[-1])
            upper_shadow = 0.0
            if today_high > today_low and today_high > price:
                upper_shadow = (today_high - max(price, today_open)) / (today_high - today_low)
                if upper_shadow > 0.4 and pct < 3.0:
                    score += self.get_param("weight_high_upper_shadow", -1.5)
                    tags.append("高位长上影")
                    risk_tags.append("高位长上影")
                    high_upper_shadow = True

            # 高位跌破短期均线
            broke_ma5 = price < ma5
            broke_ma10 = price < ma10
            if broke_ma5 or broke_ma10:
                score += self.get_param("weight_high_break_ma", -1.5)
                tags.append("高位破短均线")
                risk_tags.append("高位破短均线")
                high_break = True

            # 高位MACD顶背离：价格新高但DIF未新高
            if position_pct >= 0.85:
                dif_now = float(dif.iloc[-1])
                dif_20d_max = float(dif.tail(20).max())
                if dif_now < dif_20d_max * 0.85:
                    score += self.get_param("weight_high_macd_divergence", -1.5)
                    tags.append("高位MACD背离")
                    risk_tags.append("高位MACD背离")
                    high_divergence = True

            # 高位正面逻辑公布后，更看市场是否承接。高开低走、巨量
            # 滞涨或长上影属于“利好兑现”，不能继续按利好加分。
            if has_logic_change:
                sell_news_risk = (
                    (gap_pct >= 3.0 and intraday_pct <= -1.0)
                    or high_stall
                    or high_upper_shadow
                )
                if sell_news_risk:
                    reaction = self.get_param("weight_sell_news_penalty", -2.0)
                    score += reaction
                    event_reaction_score += reaction
                    tags.append("高位利好兑现")
                    risk_tags.append("高位利好兑现")
                elif pct < 0:
                    reaction = self.get_param("weight_good_news_no_rise", -1.5)
                    score += reaction
                    event_reaction_score += reaction
                    tags.append("利好不涨")
                    risk_tags.append("利好不涨")
                elif trend_ok and volume_ok and intraday_pct >= -0.5:
                    reaction = self.get_param("weight_good_news_absorption", 0.6)
                    score += reaction
                    event_reaction_score += reaction
                    tags.append("高位利好承接")

        major_risk = (
            sell_news_risk
            or high_stall
            or high_upper_shadow
            or (high_break and high_divergence)
            or bool(include_enrichment and logic and logic.penalty <= -2.0)
        )

        # ===== 两条独立入场路线 =====
        # 低位趋势萌芽：不是因为便宜加分，而是要求波动收敛、底部不再
        # 下移、短均线转向和上涨日量能更有效等结构逐步改善。
        early_start_score = 0.0
        if position_pct <= 0.55:
            early_start_score += 0.75
            setup_triggers.append("位置仍处低中位")
        if range_compression <= 0.9:
            early_start_score += 0.75
            setup_triggers.append("短期波动收敛")
        if floor_holding:
            early_start_score += 0.75
            setup_triggers.append("近期低点不再下移")
        if ma5_rising:
            early_start_score += 0.75
            setup_triggers.append("MA5开始上行")
        if ma10_rising:
            early_start_score += 0.5
            setup_triggers.append("MA10开始改善")
        if price >= ma5:
            early_start_score += 0.5
            setup_triggers.append("收盘站上MA5")
        if up_down_volume_ratio >= 1.1:
            early_start_score += 0.75
            setup_triggers.append("上涨日量能占优")
        if close_location >= 0.6:
            early_start_score += 0.5
            setup_triggers.append("日内收盘承接较强")
        if pct_5d > 12:
            early_start_score -= 1.0
            setup_risks.append("五日涨幅已集中")

        # 强势延续：高位本身不是风险，关键是平台突破后价格保持、趋势
        # 方向和供给消化。巨量滞涨、长上影和破均线才是否决证据。
        continuation_score = 0.0
        if trend == "多头":
            continuation_score += 1.0
        elif trend == "偏多":
            continuation_score += 0.5
        if position_pct >= 0.65:
            continuation_score += 0.5
        if breakout_distance_pct >= -2.0:
            continuation_score += 1.0
        if close_location >= 0.7:
            continuation_score += 0.75
        if 0.8 <= vol_ratio <= 2.8:
            continuation_score += 0.75
        if up_down_volume_ratio >= 1.1:
            continuation_score += 0.75
        if 0 < pct_5d <= 20 and ret_20d > 0:
            continuation_score += 0.75
        if high_stall or high_upper_shadow:
            continuation_score -= 1.5
            setup_risks.append("高位价格保持不足")
        if high_break or high_divergence:
            continuation_score -= 1.0
            setup_risks.append("强势结构出现衰减")

        early_actionable = (
            early_start_score >= 4.5
            and price >= ma5
            and (golden or macd_buy or 1.1 <= vol_ratio <= 2.8)
            and pct <= 7.0
            and not dead
        )
        continuation_actionable = (
            continuation_score >= 4.5
            and trend in {"多头", "偏多"}
            and price >= ma5
            and pct >= 0
            and not (high_stall or high_upper_shadow or high_break or high_divergence)
        )
        if continuation_score >= early_start_score and continuation_score >= 3.0:
            entry_route = "strong_continuation"
            setup_score = continuation_score
            route_triggers = [
                "多周期趋势保持", "接近或突破20日平台", "上涨量能效率较好",
            ]
            setup_triggers.extend(route_triggers)
            setup_stage = "actionable" if continuation_actionable else "warming"
            score += min(
                self.get_param("weight_strong_continuation", 1.8),
                max(0.0, continuation_score - 2.5) * 0.6,
            )
            tags.append("强势延续观察" if not continuation_actionable else "强势延续确认")
        elif early_start_score >= 2.5:
            entry_route = "early_start"
            setup_score = early_start_score
            setup_stage = (
                "actionable" if early_actionable
                else "warming" if early_start_score >= 3.5
                else "preparing"
            )
            score += min(
                self.get_param("weight_early_start_setup", 1.8),
                max(0.0, early_start_score - 2.0) * 0.6,
            )
            tags.append("低位启动准备" if setup_stage == "preparing" else "低位趋势升温")
        else:
            entry_route = "balanced"
            setup_score = max(early_start_score, continuation_score)
            setup_stage = "preparing"

        if major_risk or (dead and price < ma20 and setup_score < 3.0):
            setup_stage = "invalidated"
            setup_risks.extend(risk_tags or ["趋势结构失效"])

        extra_payload = {}
        if concepts:
            # 去重保序，便于 Web 展示和复盘检索
            dedup = list(dict.fromkeys(concepts))
            extra_payload["concepts"] = dedup
        if logic and logic.level != "none":
            extra_payload["logic_change"] = {
                "level": logic.level,
                "boost": logic.boost,
                "event_count": logic.event_count,
                "max_score": logic.max_score,
                "reasons": logic.reasons,
                "penalty": logic.penalty,
                "risk_tags": logic.risk_tags,
                "risk_reasons": logic.risk_reasons,
                "tradeable_positive": logic.tradeable_positive,
            }
        if include_enrichment and fundamental:
            extra_payload["fundamental_llm"] = fundamental.to_extra()
        elif include_enrichment and financial:
            extra_payload["financial_factor"] = {
                "period": financial.period,
                "boost": financial.boost,
                "tags": list(financial.tags),
                "source": financial.source,
            }
        if extra_payload:
            extra_info = json.dumps(extra_payload, ensure_ascii=False)

        score = round(score, 1)

        if include_enrichment and code in AI_COMPUTE_STOCKS:
            theme_group = "AI算力"
        elif concepts:
            theme_group = str(concepts[0])
        else:
            theme_group = ""
        if entry_route == "early_start":
            buy_eligible = (
                setup_stage == "actionable" and not major_risk
            )
        elif entry_route == "strong_continuation":
            buy_eligible = continuation_actionable and not major_risk
        else:
            # The experiment has two explicit entry routes.  A balanced daily
            # score may remain observable, but cannot bypass route-specific
            # setup and lifecycle confirmation.
            buy_eligible = False

        # 初步信号分类。统一引擎追加资金/板块/公司行动因子后会再次
        # 按 final_score 重算，避免基础分与最终信号不一致。
        threshold_buy = self.get_param("score_threshold_buy", 7.0)
        threshold_watch = self.get_param("score_threshold_watch", 4.0)
        if score >= threshold_buy and buy_eligible:
            signal_type = "buy"
        elif score >= threshold_watch:
            signal_type = "watch"
        else:
            signal_type = "pass"

        # 名称：优先十五五池，其次从 DataFrame 取
        name = FIFTEEN_FIVE_STOCKS.get(code, {}).get("name", "") or AI_COMPUTE_STOCKS.get(code, {}).get("name", "")
        if not name:
            name = str(df.iloc[-1].get("name", "")) if "name" in df.columns else ""

        ordered_tags = list(dict.fromkeys(tags))
        discovery_score = max(
            score,
            setup_score + (2.0 if setup_stage == "actionable" else 0.25 if setup_stage == "warming" else 0.0),
        )
        return {
            "code": code,
            "name": name,
            "price": price,
            "score": score,
            "discovery_score": round(discovery_score, 2),
            "signal_type": signal_type,
            "signal_tags": "|".join(ordered_tags),
            "trend": trend,
            "pct_change": pct,
            "vol_ratio": vol_ratio,
            "position_pct": round(position_pct, 2),
            "zone": "低位" if low_zone else ("高位" if high_zone else "中位"),
            "entry_route": entry_route,
            "setup_stage": setup_stage,
            "setup_score": round(setup_score, 2),
            "setup_triggers": "|".join(list(dict.fromkeys(setup_triggers))),
            "setup_risks": "|".join(list(dict.fromkeys(setup_risks))),
            "entry_metrics": {
                "range_compression_5v20": round(range_compression, 2),
                "floor_holding": bool(floor_holding),
                "ma5_rising": bool(ma5_rising),
                "ma10_rising": bool(ma10_rising),
                "up_down_volume_ratio_10d": round(up_down_volume_ratio, 2),
                "close_location": round(close_location, 2),
                "breakout_distance_pct": round(breakout_distance_pct, 2),
                "early_start_score": round(early_start_score, 2),
                "continuation_score": round(continuation_score, 2),
            },
            "theme_group": theme_group,
            "buy_eligible": bool(buy_eligible),
            "risk_tags": "|".join(list(dict.fromkeys(risk_tags))),
            "theme_bonus": round(theme_bonus, 2),
            "logic_score": round(logic_score, 2),
            "logic_available": bool(include_enrichment and logic),
            "fundamental_score": round(fundamental_score, 2),
            "fundamental_available": bool(
                include_enrichment and (fundamental or financial)
            ),
            "event_reaction_score": round(event_reaction_score, 2),
            "gap_pct": round(gap_pct, 2),
            "intraday_pct": round(intraday_pct, 2),
            "extra": extra_info,
            "strategy": self.name,
        }
