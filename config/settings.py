"""
config/settings.py - 全局配置
"""
import os
from typing import Dict, Any

# 项目根目录
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 数据存储
DATA_DIR = os.path.join(ROOT_DIR, "data")
CACHE_DIR = os.path.join(ROOT_DIR, ".cache")

# 数据库
DB_PATH = os.path.join(DATA_DIR, "stock_data.db")

# 市场指数必须保留交易所前缀，不能与六位股票代码共用命名空间。
MARKET_INDEX_SYMBOLS = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
    "sh000688": "科创50",
}

# 默认关注的股票代码（主板+沪深300核心标的）
DEFAULT_WATCH_LIST = [
    # 银行
    "601398",  # 工商银行
    "601939",  # 建设银行
    "600036",  # 招商银行
    # 保险/券商
    "601318",  # 中国平安
    "600030",  # 中信证券
    # 白酒
    "600519",  # 贵州茅台
    "000858",  # 五粮液
    # 科技
    "000063",  # 中兴通讯
    "002415",  # 海康威视
    "300750",  # 宁德时代
    # 医药
    "600276",  # 恒瑞医药
    "300760",  # 迈瑞医疗
    # 新能源
    "601012",  # 隆基绿能
    "600900",  # 长江电力
    # 消费
    "600887",  # 伊利股份
    "000333",  # 美的集团
    "002594",  # 比亚迪
    "600809",  # 山西汾酒
    "300059",  # 东方财富
]

# 模拟账户初始资金
INITIAL_CAPITAL = 1_000_000  # 100万

# 交易费率（万分之）
COMMISSION_RATE = 0.0003  # 万3佣金
STAMP_TAX_RATE = 0.001    # 千1印花税（卖出时）

# 数据更新配置
FETCH_CONFIG: Dict[str, Any] = {
    "sina_batch_size": 80,        # 新浪每次请求数量
    "sina_delay": 0.15,          # 请求间隔(秒)
    "refresh_interval_min": 5,   # 盘中行情刷新间隔(分钟)
}

# 行情时间（A股交易时间）
TRADING_HOURS = {
    "morning_start": "09:30",
    "morning_end": "11:30",
    "afternoon_start": "13:00",
    "afternoon_end": "15:00",
}

# 日志配置
LOG_CONFIG = {
    "level": "INFO",
    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    "file": os.path.join(ROOT_DIR, "logs", "stock.log"),
}
