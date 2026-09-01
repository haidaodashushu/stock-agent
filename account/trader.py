"""
account/trader.py - 模拟交易执行模块
处理订单执行、成交计算、持仓更新、数据库持久化
"""
import logging
from typing import Optional
from datetime import datetime
import uuid

from account.models import Portfolio, Position, Order
from account.portfolio_policy import SIM_HARD_MAX_POSITIONS
from data.store.sqlite_store import StockStore
from account.reconcile import is_tradeable_a_share

logger = logging.getLogger(__name__)


class SimTrader:
    """
    模拟交易器
    负责：
    - 创建/执行订单
    - 管理账户组合
    - 持久化到数据库
    """

    def __init__(self, portfolio: Optional[Portfolio] = None):
        self.store = StockStore()
        self.portfolio = portfolio or Portfolio()
        self._load_from_db()

    # ========== 订单执行 ==========

    def buy(self, code: str, name: str, price: float, volume: int,
            reason: str = "", strategy: str = "") -> Optional[Order]:
        """
        买入
        Returns: Order 或 None（失败时）
        """
        code = str(code).zfill(6)
        if volume <= 0 or price <= 0:
            logger.warning(f"无效买入参数: {code} price={price} vol={volume}")
            return None
        if volume % 100 != 0:
            logger.warning(f"买入 {code} 拒绝: A股买入必须100股整数倍 vol={volume}")
            return None
        if not is_tradeable_a_share(code):
            logger.warning(f"买入 {code} 拒绝: 禁止科创板/北证")
            return None
        if (
            not self.portfolio.has_position(code)
            and self.portfolio.position_count() >= SIM_HARD_MAX_POSITIONS
        ):
            logger.warning(
                f"买入 {code} 拒绝: 模拟盘持仓已达硬上限"
                f"{SIM_HARD_MAX_POSITIONS}只"
            )
            return None

        amount = round(volume * price, 2)
        commission = round(amount * 0.0003, 2)  # 万3佣金
        total_cost = amount + commission

        # 检查资金
        if total_cost > self.portfolio.available_cash:
            logger.warning(f"买入 {code} 资金不足: 需{total_cost:.2f}, 仅{self.portfolio.available_cash:.2f}")
            return None

        # 创建订单
        order_id = self._gen_order_id("B")
        order = Order(
            order_id=order_id,
            code=code, name=name,
            action="buy",
            price=round(price, 2),
            volume=volume,
            amount=amount,
            commission=commission,
            tax=0.0,
            reason=reason,
            strategy=strategy,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        # 更新持仓
        pos = self.portfolio.get_position(code)
        if pos:
            # 加仓：加权平均成本
            old_value = pos.volume * pos.cost_price
            new_value = volume * price
            total_vol = pos.volume + volume
            pos.cost_price = round((old_value + new_value) / total_vol, 2)
            pos.volume = total_vol
            pos.update_market(price)  # 更新市价
        else:
            pos = Position(
                code=code, name=name,
                volume=volume,
                cost_price=round(price, 2),
                high_since_entry=round(price, 2),
            )
            pos.update_market(price)
            self.portfolio.positions.append(pos)

        # 更新资金
        self.portfolio.available_cash = round(self.portfolio.available_cash - total_cost, 2)
        self.portfolio.total_commission = round(self.portfolio.total_commission + commission, 2)

        # 记录订单
        self.portfolio.orders.append(order)

        # 持久化
        self._save_order(order)
        self._save_portfolio()

        logger.info(f"🟢 BUY {code} {name} {volume}股 @{price:.2f} (金额:{amount:.2f} 佣金:{commission:.2f})")
        return order

    def sell(self, code: str, price: float, volume: int = 0,
             reason: str = "", strategy: str = "") -> Optional[Order]:
        """
        卖出
        volume=0 表示全卖
        """
        code = str(code).zfill(6)
        # 禁止price=0的异常订单
        if price <= 0:
            logger.error(f"卖出 {code} 拒绝: price={price}无效")
            return None

        pos = self.portfolio.get_position(code)
        if not pos:
            logger.warning(f"卖出 {code} 但无持仓")
            return None

        # T+1 检查：当天买入的股份不可卖出，但允许卖出历史可用股份
        today_buy_vol = self._today_buy_volume(code)
        available_to_sell = max(0, pos.volume - today_buy_vol)
        sell_vol = volume if volume > 0 else available_to_sell
        if sell_vol <= 0:
            logger.error(f"卖出 {code} 拒绝: A股T+1，当日可卖0股")
            return None
        if sell_vol > available_to_sell:
            logger.error(f"卖出 {code} 拒绝: A股T+1，当日可卖{available_to_sell}股，试卖{sell_vol}股")
            return None
        if sell_vol > pos.volume:
            logger.error(f"卖出 {code} 拒绝: 试卖{sell_vol}股，持仓仅{pos.volume}股")
            return None

        amount = round(sell_vol * price, 2)
        commission = round(amount * 0.0003, 2)  # 万3佣金
        tax = round(amount * 0.001, 2)  # 千1印花税
        net_amount = round(amount - commission - tax, 2)

        # 计算收益（用于记录）
        cost_basis = round(sell_vol * pos.cost_price, 2)
        realized_profit = round(amount - cost_basis - commission - tax, 2)

        order_id = self._gen_order_id("S")
        order = Order(
            order_id=order_id,
            code=code, name=pos.name,
            action="sell",
            price=round(price, 2),
            volume=sell_vol,
            amount=amount,
            commission=commission,
            tax=tax,
            reason=reason,
            strategy=strategy,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        # 更新持仓
        if sell_vol >= pos.volume:
            self.portfolio.positions.remove(pos)
        else:
            pos.volume -= sell_vol
            pos.update_market(price)

        # 更新资金
        self.portfolio.available_cash = round(self.portfolio.available_cash + net_amount, 2)
        self.portfolio.total_commission = round(self.portfolio.total_commission + commission, 2)
        self.portfolio.total_tax = round(self.portfolio.total_tax + tax, 2)

        # 记录
        self.portfolio.orders.append(order)
        self._save_order(order)
        self._save_portfolio()

        action_emoji = "🔴" if realized_profit < 0 else "🟢"
        logger.info(f"{action_emoji} SELL {code} {pos.name} {sell_vol}股 @{price:.2f} "
                    f"(盈亏:{realized_profit:.2f} 佣金:{commission:.2f} 印花税:{tax:.2f})")
        return order

    # ========== 数据库持久化 ==========

    def _load_from_db(self):
        """从数据库恢复账户状态"""
        try:
            state = self.store.get_account_state()
            if state:
                self.portfolio.available_cash = float(state.get("available_cash", 1_000_000))
                self.portfolio.total_equity = float(state.get("total_equity", 1_000_000))
                self.portfolio.total_commission = float(state.get("total_commission", 0))
                self.portfolio.total_tax = float(state.get("total_tax", 0))
                self.portfolio.total_profit = float(state.get("total_profit", 0))

                # 恢复持仓
                positions_data = self.store.get_positions()
                for pd_ in positions_data:
                    cost_p = float(pd_.get("cost_price") or pd_.get("cost") or 0)
                    high_p = float(pd_.get("high_since_entry") or cost_p)
                    pos = Position(
                        code=pd_["code"],
                        name=pd_.get("name", ""),
                        volume=int(pd_["volume"]),
                        cost_price=cost_p,
                        high_since_entry=high_p,
                    )
                    # 从DB恢复计算字段；current_price无专门列，用cost_price兜底
                    pos.current_price = float(pd_.get("current_price", cost_p) or cost_p)
                    pos.market_value = float(pd_.get("market_value", 0) or cost_p * int(pd_["volume"]))
                    pos.profit = float(pd_.get("profit", 0) or 0)
                    pos.profit_pct = float(pd_.get("profit_pct", 0) or 0)
                    self.portfolio.positions.append(pos)

                # 恢复最近的订单（限最近50条）
                orders_data = self.store.get_orders(limit=50)
                for od in orders_data:
                    order = Order(
                        order_id=od.get("order_id", ""),
                        code=od["code"],
                        name=od.get("name", ""),
                        action=od.get("direction", od.get("action", "")),
                        price=float(od["price"]),
                        volume=int(od["volume"]),
                        amount=float(od["amount"]),
                        commission=float(od.get("commission", 0)),
                        tax=float(od.get("tax", 0)),
                        reason=od.get("reason", ""),
                        strategy=od.get("strategy", ""),
                        created_at=od.get("created_at", ""),
                    )
                    self.portfolio.orders.append(order)

                self.portfolio.total_order_count = self.store.get_order_count()

                logger.info(f"恢复账户: 现金{self.portfolio.available_cash:.2f}, "
                           f"{len(self.portfolio.positions)}只持仓, "
                           f"累计{self.portfolio.total_order_count}条订单")
        except Exception as e:
            logger.warning(f"账户恢复失败(首次启动): {e}")

    def _save_order(self, order: Order):
        try:
            self.store.save_order(order.to_dict())
            self.portfolio.total_order_count = self.store.get_order_count()
        except Exception as e:
            logger.error(f"保存订单失败: {e}")

    def _save_portfolio(self):
        try:
            state = self.portfolio.summary()
            state["available_cash"] = self.portfolio.available_cash
            state["total_equity"] = self.portfolio.total_equity
            state["total_commission"] = self.portfolio.total_commission
            state["total_tax"] = self.portfolio.total_tax
            state["total_profit"] = self.portfolio.total_profit
            self.store.save_account_state(state)

            # 更新持仓到数据库
            positions_data = [p.to_dict() for p in self.portfolio.positions]
            self.store.save_positions(positions_data)
        except Exception as e:
            logger.error(f"保存账户状态失败: {e}")

    @staticmethod
    def _gen_order_id(prefix: str = "") -> str:
        base = uuid.uuid4().hex[:12].upper()
        return f"{prefix}{base}"

    def _today_buy_volume(self, code: str) -> int:
        """查询当日买入股数，用于A股T+1可卖校验。"""
        from datetime import date
        today = date.today().isoformat()
        conn = self.store._get_conn()
        try:
            row = conn.execute(
                """SELECT COALESCE(SUM(volume), 0) AS vol
                   FROM orders
                   WHERE code=? AND direction='buy' AND date(created_at)=?""",
                (code, today)
            ).fetchone()
            return int(row["vol"] or 0)
        finally:
            conn.close()
