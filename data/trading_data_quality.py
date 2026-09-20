"""Source-time and minute-unit contracts, independent of model reasoning."""
from __future__ import annotations

from datetime import datetime
import math
import pandas as pd


def source_datetime(value):
    text = str(value or "").strip()
    for pattern in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            pass
    return None


def valid_quote(row: dict, now: datetime, max_age: int = 240) -> bool:
    stamp = source_datetime(row.get("source_time"))
    try:
        price = float(row.get("price") or 0)
    except (ValueError, TypeError):
        return False
    return bool(stamp and stamp.date() == now.date() and
                -5 <= (now - stamp).total_seconds() <= max_age and
                math.isfinite(price) and price > 0 and not row.get("error"))


def summarize_minutes(frame: pd.DataFrame, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    missing = {"available": False, "lookback": "30_trading_minutes"}
    result = {"source": "tencent_ifzq", "half_hour": missing}
    if frame is None or frame.empty:
        return {**result, "error": "minute series missing"}
    day = str(frame.attrs.get("trading_date") or "").replace("-", "")
    if day != now.strftime("%Y%m%d"):
        return {**result, "error": "minute source trading date missing or stale", "source_trade_date": day}
    try:
        rows = frame.copy()
        rows["time"] = rows["time"].astype(str).str.zfill(4)
        rows = rows[rows["time"].between("0930", "1130") | rows["time"].between("1300", "1500")]
        if rows.empty or rows["time"].duplicated().any() or not rows["time"].is_monotonic_increasing:
            raise ValueError("minute timestamps empty, duplicate or unordered")
        stamps = [datetime.strptime(day + t, "%Y%m%d%H%M") for t in rows["time"]]
        if any((t - now).total_seconds() > 60 for t in stamps):
            raise ValueError("minute timestamp is in the future")
        if now.hour < 15 and (now - stamps[-1]).total_seconds() > 300:
            raise ValueError("minute source is stale")
        for key in ("price", "volume", "amount"):
            rows[key] = pd.to_numeric(rows[key], errors="raise")
            if not rows[key].map(math.isfinite).all() or (rows[key] < 0).any():
                raise ValueError("invalid minute numbers")
        if (rows["price"] <= 0).any():
            raise ValueError("invalid minute price")
        # Tencent's volume (lots) AND amount (yuan) are cumulative. Never guess
        # each column independently from a short monotonic sample.
        for key in ("volume", "amount"):
            diff = rows[key].diff()
            if (diff.dropna() < 0).any():
                raise ValueError("cumulative minute value regressed")
            rows[key + "_delta"] = diff.fillna(rows[key].iloc[0])
        indices = [t.hour * 60 + t.minute - 570 - (90 if t.hour >= 13 else 0) for t in stamps]
        rows["minute"] = indices
        last = float(rows["price"].iloc[-1])
        high, low = float(rows["price"].max()), float(rows["price"].min())
        volume, amount = float(rows["volume"].iloc[-1]), float(rows["amount"].iloc[-1])
        vwap = amount / (volume * 100) if volume else None
        vwap_error = None
        if vwap is not None and not low * .99 <= vwap <= high * 1.01:
            vwap, vwap_error = None, "VWAP outside observed price range"
        end = indices[-1]
        gaps = any(b-a > 1 for a,b in zip(indices, indices[1:]))
        def change(n):
            base = rows.loc[rows["minute"] == end-n, "price"]
            return round((last / float(base.iloc[-1]) - 1) * 100, 2) if len(base) else None
        half = {**missing, "price_change_pct": change(30)}
        if end >= 60 and not gaps and indices[0] <= end - 60:
            recent = rows[rows["minute"] > end-30]
            previous = rows[(rows["minute"] > end-60) & (rows["minute"] <= end-30)]
            def ratio(key):
                den = float(previous[key + "_delta"].sum())
                return round(float(recent[key + "_delta"].sum())/den, 2) if den > 0 else None
            vr, ar = ratio("volume"), ratio("amount")
            pct = change(30)
            activity = max(vr or 0, ar or 0)
            signal = "neutral"
            if pct is not None and activity >= 1.5:
                signal = "volume_price_up" if pct >= 1 else "volume_price_down" if pct <= -1 else "volume_stall" if abs(pct) <= .3 else "neutral"
            half.update(available=True, volume_last30_vs_prev30=vr, amount_last30_vs_prev30=ar,
                        above_vwap_now=last >= vwap if vwap else None, volume_price_signal=signal)
        return {**result, "source_trade_date": day, "source_time": stamps[-1].isoformat(sep=" "),
                "last_time": rows["time"].iloc[-1], "points": len(rows), "has_gaps": gaps,
                "last_5m_pct": change(5), "last_15m_pct": change(15),
                "pullback_from_high_pct": round((last/high-1)*100, 2),
                "vwap": round(vwap, 3) if vwap else None, "vwap_error": vwap_error,
                "above_vwap": last >= vwap if vwap else None, "half_hour": half}
    except (ValueError, KeyError, TypeError) as exc:
        return {**result, "source_trade_date": day, "error": str(exc)}
