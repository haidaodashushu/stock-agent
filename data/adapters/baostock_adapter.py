from __future__ import annotations

from typing import List

from data.adapters.base import DataSourceAdapter
from data.contracts import DailyBar, FinancialFactor, StockBasic


def _bs_code(code: str) -> str:
    s = str(code).zfill(6)
    return ("sh." if s.startswith("6") else "sz.") + s


def _raw_code(bs_code: str) -> str:
    return str(bs_code).split(".")[-1].zfill(6)


class BaoStockAdapter(DataSourceAdapter):
    name = "baostock"

    def __init__(self):
        self._bs = None

    @property
    def bs(self):
        if self._bs is None:
            import baostock as bs
            self._bs = bs
        return self._bs

    def _login(self):
        rs = self.bs.login()
        if getattr(rs, "error_code", "0") != "0":
            raise RuntimeError(f"baostock login failed: {rs.error_msg}")

    def _logout(self):
        try:
            self.bs.logout()
        except Exception:
            pass

    def get_daily(self, code: str, start_date: str = "", end_date: str = "") -> List[DailyBar]:
        self._login()
        try:
            fields = "date,code,open,high,low,close,volume,amount"
            rs = self.bs.query_history_k_data_plus(
                _bs_code(code), fields,
                start_date=start_date or "1990-01-01",
                end_date=end_date or "",
                frequency="d", adjustflag="2"  # 2=前复权
            )
            bars: List[DailyBar] = []
            while rs.next():
                row = dict(zip(rs.fields, rs.get_row_data()))
                try:
                    bars.append(DailyBar(
                        code=_raw_code(row["code"]), date=row["date"],
                        open=float(row.get("open") or 0), high=float(row.get("high") or 0),
                        low=float(row.get("low") or 0), close=float(row.get("close") or 0),
                        volume=float(row.get("volume") or 0), amount=float(row.get("amount") or 0),
                        adjust_flag="qfq", source=self.name,
                    ))
                except ValueError:
                    continue
            return bars
        finally:
            self._logout()

    def get_stock_basic(self, code: str = "") -> List[StockBasic]:
        self._login()
        try:
            rs = self.bs.query_stock_basic(code=_bs_code(code) if code else "")
            rows: List[StockBasic] = []
            while rs.next():
                row = dict(zip(rs.fields, rs.get_row_data()))
                c = _raw_code(row.get("code", ""))
                rows.append(StockBasic(
                    code=c, name=row.get("code_name", ""), exchange=row.get("code", "")[:2],
                    market=row.get("type", ""), industry="", list_date=row.get("ipoDate", ""),
                    is_active=1 if row.get("outDate", "") == "" else 0,
                    source=self.name,
                ))
            return rows
        finally:
            self._logout()

    def get_industries(self, date: str = "") -> List[StockBasic]:
        """获取证监会行业分类。"""
        self._login()
        try:
            rs = self.bs.query_stock_industry(date=date) if date else self.bs.query_stock_industry()
            rows: List[StockBasic] = []
            while rs.next():
                row = dict(zip(rs.fields, rs.get_row_data()))
                code = _raw_code(row.get("code", ""))
                rows.append(StockBasic(
                    code=code,
                    name=row.get("code_name", ""),
                    exchange=row.get("code", "")[:2],
                    industry=row.get("industry", "") or row.get("industryClassification", ""),
                    source=self.name,
                ))
            return rows
        finally:
            self._logout()

    def get_financial_factors(self, code: str, year: str = "", quarter: int = 0) -> List[FinancialFactor]:
        if not year or not quarter:
            return []
        self._login()
        try:
            rs = self.bs.query_profit_data(code=_bs_code(code), year=int(year), quarter=int(quarter))
            rows: List[FinancialFactor] = []
            while rs.next():
                row = dict(zip(rs.fields, rs.get_row_data()))
                def f(k):
                    try: return float(row.get(k) or 0)
                    except ValueError: return 0.0
                rows.append(FinancialFactor(
                    code=_raw_code(row.get("code", code)), period=f"{year}Q{quarter}",
                    roe=f("roeAvg"), roa=f("npMargin"), gross_margin=f("gpMargin"),
                    net_margin=f("npMargin"), eps=f("epsTTM"),
                    source=self.name,
                ))
            return rows
        finally:
            self._logout()
