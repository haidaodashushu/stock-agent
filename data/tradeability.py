"""Deterministic hard gates for the premarket stock universe.

These checks answer whether a symbol is eligible to enter the scoring model at
all.  They deliberately do not contribute points: an ST stock, a suspended
stock, or an illiquid stock must not be rescued by a high score.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import re
from typing import Any

import pandas as pd


LIQUIDITY_WINDOW = 20
MIN_AVG_DAILY_NOTIONAL = 20_000_000.0
FALLBACK_VOLUME_LOT_SIZE = 100.0

_ST_NAME = re.compile(r"^S?\*?ST", re.IGNORECASE)


@dataclass(frozen=True)
class TradeabilityDecision:
    eligible: bool
    reasons: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)


def is_st_name(name: str) -> bool:
    """Return whether the exchange name carries an ST/*ST risk marker."""
    compact = re.sub(r"\s+", "", str(name or ""))
    return bool(_ST_NAME.match(compact))


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def assess_tradeability(
    code: str,
    name: str,
    bars: pd.DataFrame,
    *,
    expected_date: str = "",
    list_date: str = "",
    is_active: bool = True,
) -> TradeabilityDecision:
    """Apply non-negotiable listing, trading-state, and liquidity gates."""
    reasons: list[str] = []
    metrics: dict[str, Any] = {}
    code = str(code or "").zfill(6)

    if code.startswith(("688", "8", "4")):
        reasons.append("blocked_board")
    if not is_active:
        reasons.append("inactive")
    if is_st_name(name):
        reasons.append("st")
    if bars is None or bars.empty:
        return TradeabilityDecision(False, tuple(dict.fromkeys(reasons + ["no_daily_bars"])), metrics)

    frame = bars.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date")
    if frame.empty:
        return TradeabilityDecision(False, tuple(dict.fromkeys(reasons + ["no_dated_bars"])), metrics)

    expected = _as_date(expected_date)
    if expected:
        frame = frame[frame["date"].dt.date <= expected]
    if frame.empty:
        return TradeabilityDecision(False, tuple(dict.fromkeys(reasons + ["no_bar_as_of"])), metrics)

    latest = frame.iloc[-1]
    latest_date = latest["date"].date()
    metrics["latest_date"] = latest_date.isoformat()
    if expected and latest_date != expected:
        reasons.append("suspended_or_stale")

    numeric = {
        key: float(pd.to_numeric(pd.Series([latest.get(key, 0)]), errors="coerce").fillna(0).iloc[0])
        for key in ("open", "close", "high", "low", "volume")
    }
    if min(numeric[key] for key in ("open", "close", "high", "low")) <= 0 or numeric["volume"] <= 0:
        reasons.append("suspended_or_unpriced")

    observed_listing = _as_date(list_date) or frame.iloc[0]["date"].date()
    reference_date = expected or latest_date
    listing_days = (reference_date - observed_listing).days
    metrics["listing_date"] = observed_listing.isoformat()
    metrics["listing_days"] = listing_days

    liquid = frame.tail(LIQUIDITY_WINDOW).copy()
    close = pd.to_numeric(liquid.get("close"), errors="coerce").fillna(0.0)
    volume = pd.to_numeric(liquid.get("volume"), errors="coerce").fillna(0.0)
    fallback_notional = close * volume * FALLBACK_VOLUME_LOT_SIZE
    if "amount" in liquid.columns:
        amount = pd.to_numeric(liquid["amount"], errors="coerce").fillna(0.0)
        notional = amount.where(amount > 0, fallback_notional)
    else:
        notional = fallback_notional
    valid_notional = notional[(notional > 0) & close.gt(0) & volume.gt(0)]
    avg_notional = float(valid_notional.mean()) if not valid_notional.empty else 0.0
    metrics["liquidity_bars"] = int(len(valid_notional))
    metrics["avg_daily_notional"] = round(avg_notional, 2)
    if avg_notional < MIN_AVG_DAILY_NOTIONAL:
        reasons.append("illiquid")

    if len(frame) >= 2 and numeric["close"] > 0:
        previous_close = float(pd.to_numeric(pd.Series([frame.iloc[-2].get("close", 0)]), errors="coerce").fillna(0).iloc[0])
        pct = (numeric["close"] / previous_close - 1) * 100 if previous_close > 0 else 0.0
        one_price = max(numeric["high"], numeric["low"], numeric["open"], numeric["close"]) - min(
            numeric["high"], numeric["low"], numeric["open"], numeric["close"]
        ) <= numeric["close"] * 0.001
        # 创业板日常涨跌幅为 20%，主板为 10%。这里只识别有日常
        # 涨跌停限制的股票；上市时间本身不再作为交易资格硬门槛。
        limit_threshold = 19.5 if code.startswith(("300", "301")) else 9.5
        if one_price and abs(pct) >= limit_threshold:
            reasons.append("one_price_limit")
            metrics["one_price_change_pct"] = round(pct, 2)

    unique_reasons = tuple(dict.fromkeys(reasons))
    return TradeabilityDecision(not unique_reasons, unique_reasons, metrics)
