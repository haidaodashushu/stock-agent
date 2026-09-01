"""平台标准数据契约。

策略、交易、Web 层只依赖这些标准字段；数据源差异由 adapters/services 吸收。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List


@dataclass
class MarketQuote:
    code: str
    name: str = ""
    datetime: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    prev_close: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    change_pct: float = 0.0
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DailyBar:
    code: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0
    adjust_flag: str = "qfq"
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MinuteBar:
    code: str
    datetime: str
    price: float
    avg_price: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StockBasic:
    code: str
    name: str = ""
    exchange: str = ""
    market: str = ""
    industry: str = ""
    list_date: str = ""
    is_active: int = 1
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FinancialFactor:
    code: str
    period: str
    roe: float = 0.0
    roa: float = 0.0
    gross_margin: float = 0.0
    net_margin: float = 0.0
    eps: float = 0.0
    revenue_yoy: float = 0.0
    profit_yoy: float = 0.0
    debt_ratio: float = 0.0
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NewsEvent:
    code: str
    title: str
    name: str = ""
    content: str = ""
    source: str = ""
    publish_at: str = ""
    url: str = ""
    category: str = "news"
    sentiment: str = "neutral"
    score: float = 0.0
    risk_level: str = "low"
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SectorHeat:
    sector_name: str
    sector_type: str = "concept"  # concept/industry
    change_pct: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    leader_code: str = ""
    leader_name: str = ""
    heat_score: float = 0.0
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FundFlow:
    code: str
    date: str
    main_net_inflow: float = 0.0
    big_net_inflow: float = 0.0
    retail_net_inflow: float = 0.0
    main_net_pct: float = 0.0
    source: str = ""
    reliability: str = "optional"

    def to_dict(self) -> dict:
        return asdict(self)
