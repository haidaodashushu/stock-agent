"""统一财务/基础资料服务。"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, List

from data.adapters.baostock_adapter import BaoStockAdapter
from data.adapters.iwencai_intelligence_adapter import IwenCaiIntelligenceAdapter
from data.contracts import FinancialFactor, StockBasic
from data.store.sqlite_store import StockStore


class FinanceService:
    def __init__(
        self,
        store: StockStore | None = None,
        iwencai: IwenCaiIntelligenceAdapter | None = None,
        baostock: BaoStockAdapter | None = None,
    ):
        self.store = store or StockStore()
        self.iwencai = iwencai or IwenCaiIntelligenceAdapter()
        self.baostock = baostock or BaoStockAdapter()

    @staticmethod
    def recent_periods(now: datetime | None = None) -> list[tuple[str, int]]:
        """Return likely disclosed reporting periods, newest first."""
        now = now or datetime.now()
        year = now.year
        if now.month <= 4:
            return [(str(year - 1), 4), (str(year - 1), 3), (str(year - 1), 2)]
        if now.month <= 8:
            return [(str(year), 1), (str(year - 1), 4), (str(year - 1), 3)]
        if now.month <= 10:
            return [(str(year), 2), (str(year), 1), (str(year - 1), 4)]
        return [(str(year), 3), (str(year), 2), (str(year), 1)]

    @staticmethod
    def normalize_period(period: str) -> str:
        value = str(period or "").strip()
        compact = value.replace("-", "").replace("/", "")
        if len(compact) >= 8 and compact[:4].isdigit():
            suffix = {"0331": "Q1", "0630": "Q2", "0930": "Q3", "1231": "A"}.get(compact[4:8])
            if suffix:
                return f"{compact[:4]}{suffix}"
        return value

    @classmethod
    def normalize_factor(cls, factor: FinancialFactor) -> FinancialFactor:
        return FinancialFactor(
            code=str(factor.code).zfill(6),
            period=cls.normalize_period(factor.period),
            roe=factor.roe,
            roa=factor.roa,
            gross_margin=factor.gross_margin,
            net_margin=factor.net_margin,
            eps=factor.eps,
            revenue_yoy=factor.revenue_yoy,
            profit_yoy=factor.profit_yoy,
            debt_ratio=factor.debt_ratio,
            source=factor.source,
        )

    @staticmethod
    def is_informative(factor: FinancialFactor) -> bool:
        return any(
            abs(float(value or 0)) > 1e-9
            for value in (
                factor.roe,
                factor.roa,
                factor.gross_margin,
                factor.net_margin,
                factor.eps,
                factor.revenue_yoy,
                factor.profit_yoy,
                factor.debt_ratio,
            )
        )

    def upsert_factor(self, factor: FinancialFactor) -> FinancialFactor:
        factor = self.normalize_factor(factor)
        conn = self.store._get_conn()
        try:
            conn.execute(
                """INSERT INTO financial_factors
                   (code, period, roe, roa, gross_margin, net_margin, eps,
                    revenue_yoy, profit_yoy, debt_ratio, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
                   ON CONFLICT(code, period, source) DO UPDATE SET
                     roe=excluded.roe,
                     roa=excluded.roa,
                     gross_margin=excluded.gross_margin,
                     net_margin=excluded.net_margin,
                     eps=excluded.eps,
                     revenue_yoy=excluded.revenue_yoy,
                     profit_yoy=excluded.profit_yoy,
                     debt_ratio=excluded.debt_ratio,
                     updated_at=datetime('now','localtime')""",
                (
                    factor.code,
                    factor.period,
                    factor.roe,
                    factor.roa,
                    factor.gross_margin,
                    factor.net_margin,
                    factor.eps,
                    factor.revenue_yoy,
                    factor.profit_yoy,
                    factor.debt_ratio,
                    factor.source,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return factor

    def refresh_latest_factor(
        self,
        code: str,
        periods: Iterable[tuple[str, int]] | None = None,
    ) -> tuple[FinancialFactor | None, list[str]]:
        """Refresh one symbol using IwenCai first and BaoStock as fallback."""
        code = str(code).zfill(6)
        errors: list[str] = []
        try:
            factors = [
                self.normalize_factor(item)
                for item in self.iwencai.stock_financials(code)
                if str(item.code).split(".")[0].zfill(6) == code and self.is_informative(item)
            ]
            if factors:
                factor = sorted(factors, key=lambda item: item.period, reverse=True)[0]
                return self.upsert_factor(factor), errors
            errors.append("问财未返回有效财务数据")
        except Exception as exc:
            errors.append(f"问财: {exc}")

        for year, quarter in list(periods or self.recent_periods()):
            try:
                factors = [item for item in self.baostock.get_financial_factors(code, year, quarter) if self.is_informative(item)]
                if factors:
                    return self.upsert_factor(factors[0]), errors
            except Exception as exc:
                errors.append(f"BaoStock {year}Q{quarter}: {exc}")
                break
        if not any(text.startswith("BaoStock") for text in errors):
            errors.append("BaoStock 未返回有效财务数据")
        return None, errors

    def sync_stock_basic(self) -> dict:
        basics = self.baostock.get_stock_basic()
        industries = {x.code: x.industry for x in self.baostock.get_industries() if x.industry}
        conn = self.store._get_conn()
        try:
            updated = 0
            for b in basics:
                industry = industries.get(b.code, b.industry)
                conn.execute(
                    """INSERT INTO stocks (code, name, exchange, industry, list_date, is_active, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
                       ON CONFLICT(code) DO UPDATE SET
                         name=excluded.name,
                         exchange=excluded.exchange,
                         industry=COALESCE(NULLIF(excluded.industry,''), stocks.industry),
                         list_date=COALESCE(NULLIF(excluded.list_date,''), stocks.list_date),
                         is_active=excluded.is_active,
                         updated_at=datetime('now','localtime')""",
                    (b.code, b.name, b.exchange, industry, b.list_date, b.is_active)
                )
                updated += 1
            conn.commit()
            return {"updated": updated, "industries": len(industries)}
        finally:
            conn.close()

    def sync_profit_factors(self, codes: List[str], year: str, quarter: int) -> dict:
        conn = self.store._get_conn()
        try:
            inserted = 0
            for code in codes:
                factors = self.baostock.get_financial_factors(code, year=year, quarter=quarter)
                for f in factors:
                    conn.execute(
                        """INSERT OR REPLACE INTO financial_factors
                           (code, period, roe, roa, gross_margin, net_margin, eps,
                            revenue_yoy, profit_yoy, debt_ratio, source, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
                        (f.code, f.period, f.roe, f.roa, f.gross_margin, f.net_margin, f.eps,
                         f.revenue_yoy, f.profit_yoy, f.debt_ratio, f.source)
                    )
                    inserted += 1
            conn.commit()
            return {"codes": len(codes), "inserted": inserted, "period": f"{year}Q{quarter}"}
        finally:
            conn.close()

    def get_latest_factors(self, code: str) -> FinancialFactor | None:
        conn = self.store._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM financial_factors WHERE code=? ORDER BY period DESC LIMIT 1",
                (str(code).zfill(6),)
            ).fetchone()
            if not row:
                return None
            return FinancialFactor(
                code=row["code"], period=row["period"], roe=row["roe"], roa=row["roa"],
                gross_margin=row["gross_margin"], net_margin=row["net_margin"], eps=row["eps"],
                revenue_yoy=row["revenue_yoy"], profit_yoy=row["profit_yoy"],
                debt_ratio=row["debt_ratio"], source=row["source"],
            )
        finally:
            conn.close()
