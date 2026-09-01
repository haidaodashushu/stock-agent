"""
data/store/sqlite_store.py - SQLite 数据库存储模块
提供：建表、批量插入/更新行情的统一接口
"""
from __future__ import annotations

import os
import sqlite3
import logging
import json
from typing import Optional, List, Dict, Any
from datetime import datetime

try:
    import pandas as pd
except ModuleNotFoundError:  # Lightweight services only use direct SQLite APIs.
    pd = None

from .schema import DB_NAME, get_schema_statements

logger = logging.getLogger(__name__)


def _pandas():
    if pd is None:
        raise RuntimeError("this StockStore DataFrame method requires pandas")
    return pd


def get_db_path() -> str:
    """获取数据库文件路径"""
    configured = os.environ.get("STOCK_DB_PATH", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..")
    return os.path.join(data_dir, "data", DB_NAME)


class StockStore:
    """股票数据存储"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or get_db_path()
        # 确保目录存在
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        """初始化数据库表结构"""
        conn = self._get_conn()
        try:
            for stmt in get_schema_statements():
                conn.execute(stmt)
            conn.commit()
            logger.info(f"数据库初始化完成: {self.db_path}")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")
            raise
        finally:
            conn.close()

    def _table_exists(self, table_name: str) -> bool:
        """检查表是否存在"""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
            return cursor.fetchone() is not None
        finally:
            conn.close()

    # ========== 股票基本信息 ==========

    def upsert_stock(self, code: str, name: str, exchange: str = "",
                     industry: str = "", list_date: str = ""):
        """更新或插入股票信息"""
        sql = """INSERT OR REPLACE INTO stocks (code, name, exchange, industry, list_date, updated_at)
                 VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))"""
        conn = self._get_conn()
        try:
            conn.execute(sql, (code, name, exchange, industry, list_date))
            conn.commit()
        finally:
            conn.close()

    def upsert_stocks_batch(self, df: pd.DataFrame):
        """批量更新股票列表（从实时行情中提取）"""
        if df.empty:
            return
        conn = self._get_conn()
        try:
            data = []
            for _, row in df.iterrows():
                code = str(row.get("代码", "")).zfill(6)
                name = row.get("名称", "")
                exchange = row.get("交易所", "")
                # 该股票是否已在库中（保留已有行业等信息）
                cur = conn.execute("SELECT code FROM stocks WHERE code=?", (code,))
                if not cur.fetchone():
                    conn.execute(
                        "INSERT OR IGNORE INTO stocks (code, name, exchange) VALUES (?, ?, ?)",
                        (code, name, exchange)
                    )
            conn.commit()
            logger.info(f"批量更新股票: {len(df)} 条")
        except Exception as e:
            logger.error(f"批量更新股票失败: {e}")
        finally:
            conn.close()

    def get_all_stocks(self, *, include_inactive: bool = False) -> pd.DataFrame:
        """获取股票列表；默认仅返回活跃标的。"""
        conn = self._get_conn()
        try:
            where = "" if include_inactive else " WHERE is_active=1"
            df = _pandas().read_sql(f"SELECT * FROM stocks{where} ORDER BY code", conn)
            return df
        finally:
            conn.close()

    # ========== 日K线数据 ==========

    def save_daily_prices(self, df: pd.DataFrame, code: str, adjust_flag: str = "qfq"):
        """批量保存日K线数据"""
        if df.empty:
            return
        conn = self._get_conn()
        try:
            sql = """INSERT OR REPLACE INTO daily_prices
                     (code, date, open, close, high, low, volume, amount, adjust_flag)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            records = []
            for _, row in df.iterrows():
                records.append((
                    code,
                    str(row.get("date", row.get("日期", "")))[:10],
                    float(row.get("open", row.get("开盘", 0))),
                    float(row.get("close", row.get("收盘", 0))),
                    float(row.get("high", row.get("最高", 0))),
                    float(row.get("low", row.get("最低", 0))),
                    int(row.get("volume", row.get("成交量", 0))),
                    float(row.get("amount", row.get("成交额", 0)) or 0),
                    adjust_flag,
                ))
            conn.executemany(sql, records)
            conn.commit()
            logger.info(f"保存 {code} 日K: {len(records)} 条")
        except Exception as e:
            logger.error(f"保存日K失败 [{code}]: {e}")
        finally:
            conn.close()

    def get_daily_prices(self, code: str, start_date: str = "",
                         end_date: str = "") -> pd.DataFrame:
        """获取日K线数据"""
        conn = self._get_conn()
        try:
            where = "code=? AND adjust_flag='qfq'"
            params = [code]
            if start_date:
                where += " AND date>=?"
                params.append(start_date)
            if end_date:
                where += " AND date<=?"
                params.append(end_date)

            df = _pandas().read_sql(
                f"SELECT * FROM daily_prices WHERE {where} ORDER BY date",
                conn, params=params
            )
            if not df.empty:
                df["date"] = _pandas().to_datetime(df["date"])
            return df
        finally:
            conn.close()

    # ========== 实时行情快照 ==========

    def save_realtime_snapshot(self, df: pd.DataFrame, source: str = "sina"):
        """保存实时行情快照"""
        if df.empty:
            return
        conn = self._get_conn()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            sql = """INSERT OR REPLACE INTO realtime_snapshots
                     (code, name, price, open, high, low, prev_close, change_pct, volume, amount, snapshot_at, source)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            records = []
            for _, row in df.iterrows():
                records.append((
                    str(row.get("代码", "")).zfill(6),
                    str(row.get("名称", "")),
                    float(row.get("最新价", row.get("price", 0))),
                    float(row.get("今开", row.get("open", 0))),
                    float(row.get("最高", row.get("high", 0))),
                    float(row.get("最低", row.get("low", 0))),
                    float(row.get("昨收", row.get("prev_close", 0))),
                    float(row.get("涨跌幅", row.get("change_pct", 0))),
                    int(float(row.get("成交量", row.get("volume", 0)))),
                    float(row.get("成交额", row.get("amount", 0))),
                    now,
                    source,
                ))
            conn.executemany(sql, records)
            conn.commit()
            logger.info(f"保存快照: {len(records)} 条")
        except Exception as e:
            logger.error(f"保存快照失败: {e}")
        finally:
            conn.close()

    # ========== 信号 ==========

    def save_signal(self, code: str, signal_type: str, strategy: str,
                    price: float = 0, reason: str = "", strength: float = 0):
        """保存交易信号"""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO signals (code, signal_type, strategy, price, reason, strength)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (code, signal_type, strategy, price, reason, strength)
            )
            conn.commit()
        finally:
            conn.close()

    def get_signals(self, code: str = "", limit: int = 50) -> pd.DataFrame:
        """获取交易信号"""
        conn = self._get_conn()
        try:
            if code:
                df = _pandas().read_sql(
                    "SELECT * FROM signals WHERE code=? ORDER BY created_at DESC LIMIT ?",
                    conn, params=(code, limit)
                )
            else:
                df = _pandas().read_sql(
                    "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",
                    conn, params=(limit,)
                )
            return df
        finally:
            conn.close()

    # ========== 持仓 ==========

    def get_portfolio(self) -> pd.DataFrame:
        """获取当前持仓"""
        conn = self._get_conn()
        try:
            return _pandas().read_sql("SELECT * FROM portfolio WHERE volume>0 ORDER BY code", conn)
        finally:
            conn.close()

    def update_portfolio(self, code: str, name: str, cost: float,
                         volume: int, available: int = 0):
        """更新持仓"""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO portfolio
                   (code, name, cost, volume, available, updated_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
                (code, name, cost, volume, available or volume)
            )
            conn.commit()
        finally:
            conn.close()

    # ========== 订单 ==========

    def save_order(self, code: str, direction: str, price: float,
                   volume: int, amount: float, status: str = "done",
                   strategy: str = "", reason: str = ""):
        """保存交易订单"""
        conn = self._get_conn()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """INSERT INTO orders (code, direction, price, volume, amount,
                   status, strategy, reason, executed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (code, direction, price, volume, amount, status, strategy, reason, now)
            )
            conn.commit()
        finally:
            conn.close()

    def get_orders(self, code: str = "", limit: int = 100) -> pd.DataFrame:
        """获取订单记录"""
        conn = self._get_conn()
        try:
            if code:
                df = _pandas().read_sql(
                    "SELECT * FROM orders WHERE code=? ORDER BY created_at DESC LIMIT ?",
                    conn, params=(code, limit)
                )
            else:
                df = _pandas().read_sql(
                    "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
                    conn, params=(limit,)
                )
            return df
        finally:
            conn.close()

    # ========== 回测结果 ==========

    def save_backtest_result(self, strategy: str, code: str,
                             start_date: str, end_date: str,
                             total_return: float, annual_return: float,
                             max_drawdown: float, win_rate: float,
                             sharpe: float, trade_count: int,
                             detail: dict = None):
        """保存回测结果"""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO backtest_results
                   (strategy, code, start_date, end_date, total_return, annual_return,
                    max_drawdown, win_rate, sharpe, trade_count, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (strategy, code, start_date, end_date, total_return,
                 annual_return, max_drawdown, win_rate, sharpe,
                 trade_count, json.dumps(detail or {}))
            )
            conn.commit()
        finally:
            conn.close()

    # ========== 账户状态 ==========

    def save_account_state(self, state: dict):
        """保存账户状态"""
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT id FROM account_state WHERE id=1"
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE account_state SET available_cash=?, total_equity=?,
                       total_profit=?, total_commission=?, total_tax=?,
                       updated_at=datetime('now','localtime') WHERE id=1""",
                    (state.get("available_cash", 0), state.get("total_equity", 0),
                     state.get("total_profit", 0), state.get("total_commission", 0),
                     state.get("total_tax", 0))
                )
            else:
                conn.execute(
                    """INSERT INTO account_state (available_cash, total_equity, total_profit,
                       total_commission, total_tax) VALUES (?, ?, ?, ?, ?)""",
                    (state.get("available_cash", 0), state.get("total_equity", 0),
                     state.get("total_profit", 0), state.get("total_commission", 0),
                     state.get("total_tax", 0))
                )
            conn.commit()
        finally:
            conn.close()

    def get_account_state(self) -> Optional[dict]:
        """获取账户状态"""
        conn = self._get_conn()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM account_state WHERE id=1").fetchone()
            if row:
                return dict(row)
            return None
        finally:
            conn.close()

    # ========== 持仓 ==========

    def save_positions(self, positions: List[dict]):
        """保存持仓列表（全量替换）"""
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM portfolio")
            for p in positions:
                conn.execute(
                    """INSERT INTO portfolio
                       (code, name, volume, cost_price, current_price, market_value, profit,
                        profit_pct, high_since_entry, available)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (p.get("code", ""), p.get("name", ""), int(p.get("volume", 0)),
                     p.get("cost_price", 0), p.get("current_price", p.get("cost_price", 0)),
                     p.get("market_value", 0), p.get("profit", 0),
                     p.get("profit_pct", 0),
                     p.get("high_since_entry", p.get("cost_price", 0)),
                     int(p.get("volume", 0)))
                )
            conn.commit()
        finally:
            conn.close()

    def get_positions(self) -> List[dict]:
        """获取持仓列表"""
        conn = self._get_conn()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM portfolio").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # ========== 订单 ==========

    def save_order(self, order: dict):
        """保存一笔订单"""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO orders
                   (order_id, code, name, direction, price, volume, amount,
                    commission, tax, status, reason, strategy, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order.get("order_id", ""), order.get("code", ""),
                 order.get("name", ""), order.get("action", ""),
                 order.get("price", 0), int(order.get("volume", 0)),
                 order.get("amount", 0), order.get("commission", 0),
                 order.get("tax", 0), order.get("status", "filled"),
                 order.get("reason", ""), order.get("strategy", ""),
                 order.get("created_at", ""))
            )
            conn.commit()
        finally:
            conn.close()

    def get_orders(self, limit: int = 100) -> List[dict]:
        """获取订单列表"""
        conn = self._get_conn()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_order_count(self) -> int:
        """获取数据库中的累计订单数，不受最近订单加载上限影响。"""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT COUNT(*) AS count FROM orders").fetchone()
            return int(row["count"] or 0)
        finally:
            conn.close()

    def get_active_stocks(self) -> pd.DataFrame:
        """获取活跃股票列表"""
        return self.get_all_stocks()

    def get_backtest_results(self, limit: int = 20) -> pd.DataFrame:
        """获取回测结果历史"""
        conn = self._get_conn()
        try:
            return _pandas().read_sql(
                "SELECT * FROM backtest_results ORDER BY created_at DESC LIMIT ?",
                conn, params=(limit,)
            )
        finally:
            conn.close()
