"""
engine/backtest.py - 回测引擎
核心功能：
1. 加载历史日K
2. 按日期逐日推进
3. 通过回调在每一天生成交易动作
4. 记录交易和净值曲线
5. 输出回测报告
"""
import logging
from typing import List, Optional, Callable

import pandas as pd
import numpy as np

from account.models import Portfolio
from account.trader import SimTrader
from data.loader import DataLoader
from data.store.sqlite_store import StockStore

logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    回测引擎
    支持全流程：选股 → 建仓 → 信号 → 风控 → 记录
    """

    def __init__(self, initial_capital: float = 1_000_000):
        self.loader = DataLoader()
        self.store = StockStore()
        self.trader = SimTrader(Portfolio(
            total_equity=initial_capital,
            available_cash=initial_capital,
        ))
        self._daily_nav: List[dict] = []  # 每日净值记录
        self._all_trades: List[dict] = []
        self._initial_capital = initial_capital
        self._current_date: str = ""

    def run(self, codes: List[str], start_date: str, end_date: str,
            trade_callback: Optional[Callable] = None) -> dict:
        """
        执行回测
        Args:
            codes: 关注的股票列表
            start_date: 开始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD
            trade_callback: 每日交易回调，
                            签名: fn(date, positions, market_prices, daily_data) -> List[TradeAction]
        Returns:
            dict: 回测报告
        """
        logger.info(f"🚀 回测启动: {len(codes)}只股票, {start_date} ~ {end_date}")

        # 1. 预加载所有日K数据
        daily_data = {}
        for code in codes:
            df = self.loader.get_daily(code, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty and len(df) > 20:
                df = df.sort_values("date").reset_index(drop=True)
                daily_data[code] = df

        if not daily_data:
            logger.error("没有可用日K数据")
            return {"error": "no_data", "message": "没有可用日K数据"}

        # 2. 获取所有交易日期
        all_dates = set()
        for df in daily_data.values():
            all_dates.update(df["date"].astype(str).tolist())
        sorted_dates = sorted(all_dates)

        logger.info(f"共 {len(sorted_dates)} 个交易日")

        # 3. 逐日回测
        for i, date in enumerate(sorted_dates):
            self._current_date = date
            day_data = self._get_day_data(daily_data, date)

            if day_data.empty:
                continue

            # 更新持仓市价
            market_prices = {row["code"]: row["close"] for _, row in day_data.iterrows()}
            self.trader.portfolio.update_prices(market_prices)

            # 调用交易回调
            if trade_callback:
                context = {
                    "daily_data": {code: df[df["date"].astype(str) <= date] for code, df in daily_data.items()},
                    "portfolio": [p.to_dict() for p in self.trader.portfolio.positions],
                    "market_prices": market_prices,
                    "available_cash": self.trader.portfolio.available_cash,
                    "total_equity": self.trader.portfolio.total_equity,
                    "current_date": date,
                }
                actions = trade_callback(date, context)
                if actions:
                    for action in actions:
                        self._execute_action(action, market_prices)

            # 记录每日净值
            self._record_nav(date, day_data)

        # 4. 生成报告
        report = self._generate_report(codes)
        logger.info(f"✅ 回测完成: 收益{report['total_return_pct']:.2f}%, "
                    f"胜率{report['win_rate']:.1f}%, "
                    f"最大回撤{report['max_drawdown']:.2f}%")
        return report

    def _get_day_data(self, daily_data: dict, date: str) -> pd.DataFrame:
        """获取某一天所有股票的数据"""
        rows = []
        for code, df in daily_data.items():
            day = df[df["date"].astype(str) == date]
            if not day.empty:
                row = day.iloc[0].to_dict()
                row["code"] = code
                rows.append(row)
        return pd.DataFrame(rows)

    def _execute_action(self, action: dict, market_prices: dict):
        """执行一个交易动作"""
        code = action.get("code", "")
        action_type = action.get("action", "")
        price = action.get("price", market_prices.get(code, 0))
        volume = action.get("volume", 0)
        reason = action.get("reason", "")
        strategy = action.get("strategy", "")

        if price <= 0:
            return

        name = action.get("name", code)
        action_type = action.get("action", "")

        if action_type == "buy":
            if volume <= 0:
                # 如果没有指定数量，按金额计算
                amount = action.get("amount", 0)
                if amount > 0:
                    volume = int(amount / price / 100) * 100
                if volume < 100:
                    return
            order = self.trader.buy(code, name, price, volume, reason, strategy)
            if order:
                self._all_trades.append(order.to_dict())

        elif action_type == "sell":
            sell_vol = volume if volume > 0 else 0
            order = self.trader.sell(code, price, sell_vol, reason, strategy)
            if order:
                self._all_trades.append(order.to_dict())

    def _record_nav(self, date: str, day_data: pd.DataFrame):
        """记录每日净值"""
        summary = self.trader.portfolio.summary()
        self._daily_nav.append({
            "date": date,
            "total_equity": summary["total_equity"],
            "available_cash": summary["available_cash"],
            "position_market_value": summary["position_market_value"],
            "position_count": summary["position_count"],
        })

    def _generate_report(self, codes: List[str]) -> dict:
        """生成回测报告"""
        if not self._daily_nav:
            return {"error": "no_data"}

        nav_df = pd.DataFrame(self._daily_nav)
        start_equity = nav_df.iloc[0]["total_equity"]
        end_equity = nav_df.iloc[-1]["total_equity"]

        # 总收益率
        total_return = (end_equity - start_equity) / start_equity

        # 年化收益率
        days = len(nav_df)
        years = days / 252
        annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

        # 最大回撤
        nav_df["peak"] = nav_df["total_equity"].cummax()
        nav_df["drawdown"] = (nav_df["total_equity"] - nav_df["peak"]) / nav_df["peak"]
        max_drawdown = abs(nav_df["drawdown"].min())

        # 胜率
        trades = [t for t in self._all_trades if t.get("action") == "sell"]
        if not trades and len(self._all_trades) > 0:
            trades = self._all_trades

        winning_trades = sum(1 for t in trades if t.get("profit", 0) > 0)
        win_rate = winning_trades / len(trades) if trades else 0

        # 夏普比率（简化版）
        if len(nav_df) > 1:
            nav_df["daily_return"] = nav_df["total_equity"].pct_change()
            avg_return = nav_df["daily_return"].mean()
            std_return = nav_df["daily_return"].std()
            sharpe = (avg_return * 252) / (std_return * np.sqrt(252)) if std_return > 0 else 0
        else:
            sharpe = 0

        return {
            "start_date": nav_df.iloc[0]["date"],
            "end_date": nav_df.iloc[-1]["date"],
            "initial_capital": self._initial_capital,
            "final_equity": round(end_equity, 2),
            "total_return": round(total_return, 4),
            "total_return_pct": round(total_return * 100, 2),
            "annual_return_pct": round(annual_return * 100, 2),
            "max_drawdown": round(max_drawdown * 100, 2),
            "max_drawdown_pct": round(max_drawdown * 100, 2),
            "win_rate": round(win_rate * 100, 1),
            "sharpe_ratio": round(sharpe, 2),
            "total_trades": len(trades),
            "total_buys": sum(1 for t in self._all_trades if t.get("action") == "buy"),
            "total_sells": sum(1 for t in self._all_trades if t.get("action") == "sell"),
            "daily_nav": nav_df.to_dict("records"),
            "trades": self._all_trades,
            "codes": codes,
        }
