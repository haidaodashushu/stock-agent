"""
account/models.py - 模拟账户数据模型
"""
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime, date
from decimal import Decimal
import json


@dataclass
class Position:
    """持仓"""
    code: str
    name: str
    volume: int                 # 持仓股数
    cost_price: float           # 持仓成本价
    current_price: float = 0.0  # 当前价
    market_value: float = 0.0   # 市值
    profit: float = 0.0         # 盈亏
    profit_pct: float = 0.0     # 盈亏%
    high_since_entry: float = 0.0  # 持仓期间最高价
    updated_at: str = ""

    def update_market(self, price: float):
        self.current_price = price
        self.market_value = round(self.volume * price, 2)
        self.profit = round(self.market_value - self.volume * self.cost_price, 2)
        self.profit_pct = round((price - self.cost_price) / self.cost_price * 100, 2)
        if price > self.high_since_entry:
            self.high_since_entry = price
        self.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "volume": self.volume,
            "cost_price": self.cost_price,
            "current_price": self.current_price,
            "market_value": self.market_value,
            "profit": self.profit,
            "profit_pct": self.profit_pct,
            "high_since_entry": self.high_since_entry,
            "updated_at": self.updated_at,
        }


@dataclass
class Order:
    """订单"""
    order_id: str
    code: str
    name: str
    action: str          # buy / sell
    price: float
    volume: int
    amount: float          # 成交金额
    commission: float = 0.0
    tax: float = 0.0
    status: str = "filled"
    reason: str = ""
    strategy: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "code": self.code,
            "name": self.name,
            "action": self.action,
            "price": self.price,
            "volume": self.volume,
            "amount": self.amount,
            "commission": self.commission,
            "tax": self.tax,
            "status": self.status,
            "reason": self.reason,
            "strategy": self.strategy,
            "created_at": self.created_at,
        }


@dataclass
class Portfolio:
    """账户组合"""
    total_equity: float = 1_000_000.0  # 总资产
    available_cash: float = 1_000_000.0  # 可用资金
    positions: List[Position] = field(default_factory=list)
    orders: List[Order] = field(default_factory=list)
    total_profit: float = 0.0
    total_commission: float = 0.0
    total_tax: float = 0.0
    # ``orders`` is only a recent in-memory window. Keep the persisted total
    # separately so summaries do not mistake that window size for all orders.
    total_order_count: int = 0

    def get_position(self, code: str) -> Optional[Position]:
        for pos in self.positions:
            if pos.code == code:
                return pos
        return None

    def has_position(self, code: str) -> bool:
        return self.get_position(code) is not None

    def position_count(self) -> int:
        return len(self.positions)

    def update_prices(self, market_prices: dict):
        """批量更新持仓市价"""
        for pos in self.positions:
            price = market_prices.get(pos.code, 0)
            if price > 0:
                pos.update_market(price)
        self._recalc()

    def _recalc(self):
        """重新计算总资产"""
        total_market = sum(p.market_value for p in self.positions)
        self.total_equity = round(self.available_cash + total_market, 2)
        self.total_profit = round(self.total_equity - 1_000_000, 2)

    def summary(self) -> dict:
        return {
            "total_equity": self.total_equity,
            "available_cash": self.available_cash,
            "position_market_value": round(self.total_equity - self.available_cash, 2),
            "total_profit": self.total_profit,
            "total_profit_pct": round(self.total_profit / 1_000_000 * 100, 2),
            "position_count": len(self.positions),
            "total_orders": max(self.total_order_count, len(self.orders)),
        }
