"""Database-backed current facts for the half-hour trading decision.

The refresh side may call market data providers.  The read side never does:
AI context is reconstructed exclusively from the two current-state tables.
Rows are overwritten in place, so this module does not create cycle IDs or an
unbounded intraday history.
"""
from __future__ import annotations

import json
import math
import sqlite3
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from account.trader import SimTrader
from account.portfolio_policy import simulated_account_policy
from config.settings import MARKET_INDEX_SYMBOLS
from data.fetcher.tencent_quote import TencentQuoteFetcher
from data.fifteen_five_pool import FIFTEEN_FIVE_STOCKS
from data.live_manual_account import (
    account_snapshot,
    blocked_prefixes as live_blocked_prefixes,
    is_live_buy_allowed,
    load_config as load_live_config,
)
from data.market_regime import classify_market_regime
from data.candidate_board import candidate_board_status, load_active_candidate_board
from data.news_evidence import build_news_evidence, match_policy_evidence, recent_policy_evidence
from data.services.sector_rotation_service import SectorRotationService
from data.store.sqlite_store import StockStore

SECTOR_REFRESH_MINUTES = 25
MINUTE_FETCH_ATTEMPTS = 2
FUND_FLOW_CACHE_MAX_AGE_MINUTES = 45


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        return default if math.isnan(number) or math.isinf(number) else number
    except (TypeError, ValueError):
        return default


def _round(value: Any, digits: int = 2) -> float:
    return round(_float(value), digits)


def _symbols(codes: list[str]) -> list[str]:
    return [("sh" if str(code).zfill(6).startswith(("5", "6")) else "sz") + str(code).zfill(6) for code in codes]


def fetch_quotes(codes: list[str], timeout: int = 10) -> dict[str, dict[str, Any]]:
    """Fetch the decision universe in batches from Tencent."""
    result: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(codes), 80):
        batch = [str(code).zfill(6) for code in codes[offset : offset + 80]]
        if not batch:
            continue
        url = "http://qt.gtimg.cn/q=" + ",".join(_symbols(batch))
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            body = urllib.request.urlopen(request, timeout=timeout).read().decode("gbk", errors="ignore")
        except Exception as exc:
            for code in batch:
                result[code] = {"code": code, "source": "tencent", "error": str(exc)}
            continue
        for line in body.strip().split("\n"):
            if '="' not in line or "~" not in line:
                continue
            fields = line.split('="', 1)[1].rstrip('";').split("~")
            if len(fields) < 38:
                continue
            code = fields[2].strip().zfill(6)
            price = _float(fields[3])
            previous = _float(fields[4])
            result[code] = {
                "code": code,
                "name": fields[1],
                "price": _round(price),
                "prev_close": _round(previous),
                "open": _round(fields[5]),
                "high": _round(fields[33]),
                "low": _round(fields[34]),
                # Tencent qt reports volume in lots (手) and amount in 万元.
                # Persist the platform contract in shares and yuan.
                "volume": int(_float(fields[6]) * 100),
                "amount": _round(_float(fields[37]) * 10_000),
                "change_pct": round((price - previous) / previous * 100, 2) if price and previous else 0.0,
                "source": "tencent",
            }
    return result


def fetch_market_indices(timeout: int = 10) -> dict[str, dict[str, Any]]:
    symbols = MARKET_INDEX_SYMBOLS
    url = "http://qt.gtimg.cn/q=" + ",".join(symbols)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        body = urllib.request.urlopen(request, timeout=timeout).read().decode("gbk", errors="ignore")
    except Exception as exc:
        return {"error": {"source": "tencent", "message": str(exc)}}
    result: dict[str, dict[str, Any]] = {}
    for line in body.strip().split("\n"):
        if '="' not in line or "~" not in line:
            continue
        symbol = line.split("=", 1)[0].replace("v_", "").strip()
        fields = line.split('="', 1)[1].rstrip('";').split("~")
        if len(fields) < 38:
            continue
        price, previous = _float(fields[3]), _float(fields[4])
        result[symbol] = {
            "name": fields[1] or symbols.get(symbol, symbol),
            "price": _round(price),
            "change_pct": round((price - previous) / previous * 100, 2) if price and previous else 0.0,
            "amount": _round(fields[37]),
            "source": "tencent",
        }
    return result


def technical_state(code: str, store: StockStore) -> dict[str, Any]:
    """Compute daily technical facts strictly from persisted daily bars."""
    frame = store.get_daily_prices(code)
    if frame is None or frame.empty:
        return {"error": "daily_prices missing"}
    close = frame["close"].astype(float)
    high = frame["high"].astype(float) if "high" in frame else close
    volume = frame["volume"].astype(float) if "volume" in frame else None
    latest = float(close.iloc[-1])
    averages = {
        length: float(close.rolling(length).mean().iloc[-1]) if len(close) >= length else 0.0
        for length in (5, 10, 20, 60)
    }
    if averages[5] > averages[10] > averages[20]:
        trend = "bull"
    elif averages[5] < averages[10] < averages[20]:
        trend = "bear"
    elif latest >= averages[5] >= averages[10]:
        trend = "partial_bull"
    else:
        trend = "neutral"
    vol_ratio = 0.0
    if volume is not None and len(volume) >= 10:
        mean = float(volume.tail(10).mean())
        vol_ratio = float(volume.iloc[-1] / mean) if mean else 0.0
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    low_60 = float(close.tail(60).min())
    high_60 = float(high.tail(60).max())
    position = (latest - low_60) / (high_60 - low_60) * 100 if high_60 > low_60 else 50.0

    def period_return(days: int) -> float | None:
        if len(close) <= days:
            return None
        base = float(close.iloc[-days - 1])
        return round((latest / base - 1) * 100, 2) if base else None

    return {
        "daily_date": str(frame.iloc[-1].get("date", ""))[:10],
        "trend": trend,
        "ma5": _round(averages[5]),
        "ma10": _round(averages[10]),
        "ma20": _round(averages[20]),
        "ma60": _round(averages[60]),
        "above_ma5": bool(latest >= averages[5]) if averages[5] else False,
        "above_ma10": bool(latest >= averages[10]) if averages[10] else False,
        "above_ma20": bool(latest >= averages[20]) if averages[20] else False,
        "vol_ratio": round(vol_ratio, 2),
        "macd_cross": bool(len(dif) >= 2 and dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]),
        "macd_dead": bool(len(dif) >= 2 and dif.iloc[-2] >= dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1]),
        "position_60d_pct": round(position, 1),
        "return_5d_pct": period_return(5),
        "return_20d_pct": period_return(20),
        "return_60d_pct": period_return(60),
    }


def minute_state(code: str, fetcher: TencentQuoteFetcher) -> dict[str, Any]:
    """Persist only derived intraday facts, never the raw minute series."""
    try:
        frame = fetcher.fetch_minute(code)
    except Exception as exc:
        return {
            "source": "tencent_ifzq",
            "error": str(exc),
            "half_hour": {"available": False, "lookback": "30_trading_minutes"},
        }
    if frame is None or frame.empty:
        return {
            "source": "tencent_ifzq",
            "points": 0,
            "half_hour": {"available": False, "lookback": "30_trading_minutes"},
        }
    prices = frame["price"].astype(float)

    def incremental(series: pd.Series | None) -> pd.Series | None:
        if series is None or len(series) < 2:
            return series
        differences = series.diff()
        if int((differences.iloc[1:] >= 0).sum()) >= max(1, int((len(series) - 1) * 0.95)):
            return differences.fillna(series.iloc[0]).clip(lower=0)
        return series

    volume = incremental(frame["volume"].astype(float) if "volume" in frame else None)
    amount = incremental(frame["amount"].astype(float) if "amount" in frame else None)
    last, high, low = float(prices.iloc[-1]), float(prices.max()), float(prices.min())
    vwap = 0.0
    if volume is not None and amount is not None and float(volume.sum()) > 0:
        vwap = float(amount.sum() / (volume.sum() * 100))

    def window_pct(length: int) -> float:
        if len(prices) <= length:
            return 0.0
        base = float(prices.iloc[-length])
        return round((last - base) / base * 100, 2) if base else 0.0

    half_hour: dict[str, Any] = {"available": False, "lookback": "30_trading_minutes"}
    if len(prices) > 60:
        base = float(prices.iloc[-31])
        recent_volume = float(volume.iloc[-30:].sum()) if volume is not None else 0.0
        previous_volume = float(volume.iloc[-60:-30].sum()) if volume is not None else 0.0
        recent_amount = float(amount.iloc[-30:].sum()) if amount is not None else 0.0
        previous_amount = float(amount.iloc[-60:-30].sum()) if amount is not None else 0.0
        price_change = round((last - base) / base * 100, 2) if base else 0.0
        volume_ratio = round(recent_volume / previous_volume, 2) if previous_volume else 0.0
        amount_ratio = round(recent_amount / previous_amount, 2) if previous_amount else 0.0
        activity = max(volume_ratio, amount_ratio)
        if price_change >= 1.0 and activity >= 1.5:
            signal = "volume_price_up"
        elif price_change <= -1.0 and activity >= 1.5:
            signal = "volume_price_down"
        elif abs(price_change) <= 0.3 and activity >= 1.5:
            signal = "volume_stall"
        else:
            signal = "neutral"
        half_hour = {
            "available": True,
            "lookback": "30_trading_minutes",
            "price_change_pct": price_change,
            "volume_last30_vs_prev30": volume_ratio,
            "amount_last30_vs_prev30": amount_ratio,
            "above_vwap_now": bool(last >= vwap) if vwap else None,
            "volume_price_signal": signal,
        }
    return {
        "source": "tencent_ifzq",
        "points": int(len(frame)),
        "last_time": str(frame.iloc[-1].get("time", "")),
        "last_5m_pct": window_pct(5),
        "last_15m_pct": window_pct(15),
        "pullback_from_high_pct": round((last - high) / high * 100, 2) if high else 0.0,
        "vwap": _round(vwap),
        "above_vwap": bool(last >= vwap) if vwap else None,
        "half_hour": half_hour,
    }


def _minute_states(codes: list[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}

    def fetch_one(code: str) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for _ in range(MINUTE_FETCH_ATTEMPTS):
            state = minute_state(code, TencentQuoteFetcher())
            # A timestamp means the provider returned a valid current-day
            # minute series.  A short series near the open is still valid and
            # must not be retried merely because half-hour evidence is absent.
            if state.get("last_time"):
                return state
        if not state.get("error"):
            state["error"] = f"minute data unavailable after {MINUTE_FETCH_ATTEMPTS} attempts"
        return state

    result: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(codes))) as pool:
        futures = {
            pool.submit(fetch_one, code): code
            for code in codes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                result[code] = future.result()
            except Exception as exc:
                result[code] = {
                    "source": "tencent_ifzq",
                    "error": str(exc),
                    "half_hour": {"available": False, "lookback": "30_trading_minutes"},
                }
    return result


def _minute_scope(codes: list[str], limit: int | None = None) -> list[str]:
    """Return the complete decision scope unless a diagnostic limit is explicit."""
    return list(codes) if limit is None else list(codes[:max(0, limit)])


def _query_fund_flows(
    codes: list[str], *, request_timeout: int, retries: int, batch_size: int,
) -> tuple[dict[str, tuple[Any, str]], dict[str, str]]:
    """Query one provider pass and preserve failures at code granularity."""
    if not codes:
        return {}, {}
    try:
        from data.adapters.iwencai_adapter import IwenCaiAdapter
        from data.fund_flow_filter import FundFlowFilter
        from data.services.fund_flow_service import FundFlowService

        service = FundFlowService()
        service.register(
            "iwencai",
            IwenCaiAdapter(
                request_timeout=request_timeout,
                retries=retries,
                fill_missing=False,
                batch_size=batch_size,
            ),
        )
        summaries = FundFlowFilter(service=service).batch_summarize(codes)
    except Exception as exc:
        return {}, {code: str(exc) for code in codes}
    return summaries, dict(service.last_code_errors)


def _save_fund_flow_cache(
    store: StockStore, rows: dict[str, dict[str, Any]],
) -> None:
    successful = [
        (code, row)
        for code, row in rows.items()
        if row.get("status") == "available" and isinstance(row.get("detail"), dict)
    ]
    if not successful:
        return
    conn = store._get_conn()
    try:
        conn.executemany(
            """INSERT INTO fund_flow_cache
               (code, trade_date, payload, source, observed_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))
               ON CONFLICT(code) DO UPDATE SET
                 trade_date=excluded.trade_date,
                 payload=excluded.payload,
                 source=excluded.source,
                 observed_at=excluded.observed_at,
                 updated_at=excluded.updated_at""",
            [
                (
                    code,
                    str((row.get("detail") or {}).get("date") or ""),
                    json.dumps(row, ensure_ascii=False),
                    str(row.get("source") or ""),
                    str(row.get("observed_at") or ""),
                )
                for code, row in successful
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _load_fund_flow_cache(
    store: StockStore, codes: list[str], now: datetime,
) -> dict[str, tuple[dict[str, Any], int]]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    conn = store._get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT code, payload, observed_at FROM fund_flow_cache WHERE code IN ({placeholders})",
            codes,
        ).fetchall()
    finally:
        conn.close()
    result: dict[str, tuple[dict[str, Any], int]] = {}
    for row in rows:
        try:
            observed = datetime.fromisoformat(str(row["observed_at"]))
            age_seconds = max(0, int((now - observed).total_seconds()))
            payload = json.loads(str(row["payload"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if observed.date() != now.date():
            continue
        if age_seconds > FUND_FLOW_CACHE_MAX_AGE_MINUTES * 60:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("detail"), dict):
            result[str(row["code"]).zfill(6)] = (payload, age_seconds)
    return result


def _fund_flows(
    codes: list[str],
    store: StockStore | None = None,
    *,
    retry_missing: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fetch every in-scope flow, retry misses, then use a clearly marked cache."""
    normalized = list(dict.fromkeys(str(code).zfill(6) for code in codes))
    if not normalized:
        return {}
    summaries, errors = _query_fund_flows(
        normalized, request_timeout=12, retries=1, batch_size=5,
    )
    if retry_missing:
        missing = [
            code for code in normalized
            if not (summaries.get(code) or (None, ""))[0]
        ]
        if missing:
            retried, retry_errors = _query_fund_flows(
                missing, request_timeout=8, retries=0, batch_size=5,
            )
            for code in missing:
                if (retried.get(code) or (None, ""))[0]:
                    summaries[code] = retried[code]
                    errors.pop(code, None)
                elif retry_errors.get(code):
                    errors[code] = retry_errors[code]

    now = datetime.now()
    observed_at = now.strftime("%Y-%m-%d %H:%M:%S")
    result: dict[str, dict[str, Any]] = {}
    for code in normalized:
        flow, summary = summaries.get(code, (None, ""))
        error = errors.get(code) if not flow else None
        if not flow and not error:
            error = summary or "iwencai: 问财未返回该标的资金流"
        result[code] = {
            "status": "available" if flow else "unavailable",
            "freshness": "live" if flow else "unavailable",
            "summary": summary if flow else "",
            "detail": flow.to_dict() if flow else None,
            "source": "iwencai",
            "observed_at": observed_at,
            "cache_age_seconds": 0 if flow else None,
            "error": error,
        }

    if store is not None:
        _save_fund_flow_cache(store, result)
        missing = [code for code, row in result.items() if row["status"] == "unavailable"]
        cached = _load_fund_flow_cache(store, missing, now)
        for code, (payload, age_seconds) in cached.items():
            current_error = result[code].get("error")
            result[code] = {
                **payload,
                "status": "cached",
                "freshness": "cached",
                "cache_age_seconds": age_seconds,
                # Retain the current provider failure so the consumer knows
                # why a cached value was used.
                "error": current_error,
            }
    return result


def _screen_candidates(store: StockStore, run_date: str) -> list[dict[str, Any]]:
    # Trading consumes an immutable candidate-board snapshot. Composition,
    # ranking and replacement are exclusively owned by candidate_board.py.
    return load_active_candidate_board(store, trade_date=run_date)


def _validate_candidate_board_scope(
    board_status: dict[str, Any],
    candidates: list[dict[str, Any]],
    now: datetime,
) -> None:
    """Reject a silent empty scope when a ready board should be consumable."""
    minute = now.hour * 60 + now.minute
    in_candidate_window = 9 * 60 + 25 <= minute <= 15 * 60 + 5
    active_count = int(board_status.get("active_count") or 0)
    if (
        in_candidate_window
        and board_status.get("status") == "ready"
        and active_count > 0
        and not candidates
    ):
        raise RuntimeError(
            "candidate board is ready but its active scope is empty; "
            "refuse a holdings-only decision caused by candidate expiry/version mismatch"
        )


def _news(
    store: StockStore, codes: list[str], as_of: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    try:
        reference = datetime.fromisoformat(str(as_of))
    except ValueError:
        reference = datetime.now()
    since = (reference - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    conn = store._get_conn()
    conn.row_factory = sqlite3.Row
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        for code in codes:
            rows = conn.execute(
                """SELECT title,content,source,publish_at,url,category,sentiment,
                          score,risk_level,tags,created_at
                   FROM news_events WHERE code=? AND created_at<=?
                     AND COALESCE(NULLIF(publish_at,''),created_at) BETWEEN ? AND ?
                   ORDER BY publish_at DESC, id DESC LIMIT 2""",
                (code, as_of, since, as_of),
            ).fetchall()
            result[code] = [build_news_evidence(row) for row in rows]
        policy_context = recent_policy_evidence(conn, as_of)
    finally:
        conn.close()
    return result, policy_context


def _sector_state() -> dict[str, Any]:
    try:
        payload = SectorRotationService(
            cache_minutes=SECTOR_REFRESH_MINUTES,
        ).get_snapshot(refresh=False)
    except Exception as exc:
        return {
            "status": "unavailable",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "iwencai",
            "signals": [],
            "error": str(exc),
        }
    result = dict(payload)
    signals = payload.get("signals") if isinstance(payload.get("signals"), list) else []
    result["signals"] = sorted(
        signals,
        key=lambda row: _float(row.get("score")) if isinstance(row, dict) else 0.0,
        reverse=True,
    )[:30]
    result.setdefault("status", "available" if result["signals"] else "unavailable")
    return result


def _ensure_sector_memberships(codes: list[str], store: StockStore) -> dict[str, Any]:
    try:
        from data.services.stock_sector_membership_service import (
            StockSectorMembershipService,
        )
        return StockSectorMembershipService(store=store).ensure(codes)
    except Exception as exc:
        return {
            "requested": len(codes),
            "refreshed": 0,
            "memberships": 0,
            "missing": list(codes),
            "errors": [str(exc)],
        }


def _live_account_without_network(store: StockStore) -> dict[str, Any]:
    conn = store._get_conn()
    conn.row_factory = sqlite3.Row
    try:
        return account_snapshot(conn, quotes={})
    finally:
        conn.close()


def _sim_position_activity(
    store: StockStore, codes: list[str], as_of: str,
) -> dict[str, dict[str, Any]]:
    """Summarize evidence needed for thesis-decay and residual-position exits."""
    normalized = list(dict.fromkeys(str(code).zfill(6) for code in codes))
    if not normalized:
        return {}
    try:
        reference = datetime.fromisoformat(str(as_of))
    except ValueError:
        reference = datetime.now()
    cutoff = (reference - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join("?" for _ in normalized)
    conn = store._get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""SELECT code,direction,volume,reason,created_at
                  FROM orders
                 WHERE status='filled' AND code IN ({placeholders})
                   AND created_at<=?
                 ORDER BY created_at,id""",
            [*normalized, as_of],
        ).fetchall()
    finally:
        conn.close()
    result = {
        code: {
            "recent_buy_orders_7d": 0,
            "recent_sell_orders_7d": 0,
            "last_buy_at": None,
            "last_buy_reason": "",
            "last_sell_at": None,
            "last_sell_reason": "",
        }
        for code in normalized
    }
    for row in rows:
        code = str(row["code"]).zfill(6)
        direction = str(row["direction"] or "")
        created_at = str(row["created_at"] or "")
        if direction == "buy":
            result[code]["last_buy_at"] = created_at
            result[code]["last_buy_reason"] = str(row["reason"] or "")[:260]
            if created_at >= cutoff:
                result[code]["recent_buy_orders_7d"] += 1
        elif direction == "sell":
            result[code]["last_sell_at"] = created_at
            result[code]["last_sell_reason"] = str(row["reason"] or "")[:260]
            if created_at >= cutoff:
                result[code]["recent_sell_orders_7d"] += 1
    return result


def _annotate_relative_strength(items: list[dict[str, Any]]) -> None:
    """Add cross-sectional ranks so holdings and candidates compete directly."""
    for days in (5, 20, 60):
        key = f"return_{days}d_pct"
        ranked = sorted(
            (
                (_float((item.get("technical") or {}).get(key)), item)
                for item in items
                if (item.get("technical") or {}).get(key) is not None
            ),
            key=lambda pair: pair[0],
        )
        total = len(ranked)
        for index, (_, item) in enumerate(ranked, start=1):
            item["technical"][f"rs_{days}d_percentile"] = round(index / total * 100, 1)


def _industry_exposure(
    items: list[dict[str, Any]], total_equity: float, mode: str,
) -> list[dict[str, Any]]:
    holding_key = "is_sim_holding" if mode == "simulated" else "is_live_holding"
    position_key = "position" if mode == "simulated" else "live_position"
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        if not item.get(holding_key):
            continue
        sector = item.get("sector") if isinstance(item.get("sector"), dict) else {}
        industry = str(sector.get("primary_industry") or "未知行业")
        position = item.get(position_key) if isinstance(item.get(position_key), dict) else {}
        row = grouped.setdefault(
            industry, {"industry": industry, "codes": [], "market_value": 0.0},
        )
        row["codes"].append(str(item.get("code") or "").zfill(6))
        row["market_value"] += _float(position.get("market_value"))
    exposure = []
    for row in grouped.values():
        market_value = round(row["market_value"], 2)
        exposure.append({
            "industry": row["industry"],
            "position_count": len(row["codes"]),
            "codes": row["codes"],
            "market_value": market_value,
            "weight_pct": round(market_value / total_equity * 100, 2) if total_equity else 0.0,
        })
    return sorted(exposure, key=lambda row: (-row["weight_pct"], row["industry"]))


def _ensure_mode_state_schema(conn: sqlite3.Connection) -> None:
    stock_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(trading_stock_state)")
    }
    market_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(trading_market_state)")
    }
    if stock_columns and market_columns and "mode" in stock_columns and "mode" in market_columns:
        return
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_trading_stock_state_scope;
        DROP TABLE IF EXISTS trading_stock_state;
        DROP TABLE IF EXISTS trading_market_state;
        CREATE TABLE trading_stock_state (
            mode TEXT NOT NULL CHECK (mode IN ('simulated', 'live')),
            code TEXT NOT NULL,
            name TEXT DEFAULT '',
            is_candidate INTEGER DEFAULT 0,
            is_sim_holding INTEGER DEFAULT 0,
            is_live_holding INTEGER DEFAULT 0,
            screen_date TEXT DEFAULT '',
            screen_score REAL DEFAULT 0,
            screen_signal TEXT DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (mode, code)
        );
        CREATE INDEX idx_trading_stock_state_scope
        ON trading_stock_state(mode, is_candidate, is_sim_holding, is_live_holding);
        CREATE TABLE trading_market_state (
            mode TEXT PRIMARY KEY CHECK (mode IN ('simulated', 'live')),
            payload TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        """
    )


def persist_trading_state(
    snapshot: dict[str, Any], mode: str, store: StockStore | None = None,
) -> None:
    """Atomically replace one account's current decision state."""
    if mode not in {"simulated", "live"}:
        raise ValueError(f"invalid trading mode: {mode}")
    store = store or StockStore()
    items = [
        item
        for section in ("positions", "candidates", "tracked")
        for item in snapshot.get(section, [])
        if isinstance(item, dict) and item.get("code")
    ]
    conn = store._get_conn()
    try:
        _ensure_mode_state_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        codes = []
        for item in items:
            code = str(item["code"]).zfill(6)
            codes.append(code)
            screen = item.get("screen") if isinstance(item.get("screen"), dict) else {}
            conn.execute(
                """INSERT INTO trading_stock_state
                   (mode, code, name, is_candidate, is_sim_holding, is_live_holding,
                    screen_date, screen_score, screen_signal, payload, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mode, code) DO UPDATE SET
                     name=excluded.name,
                     is_candidate=excluded.is_candidate,
                     is_sim_holding=excluded.is_sim_holding,
                     is_live_holding=excluded.is_live_holding,
                     screen_date=excluded.screen_date,
                     screen_score=excluded.screen_score,
                     screen_signal=excluded.screen_signal,
                     payload=excluded.payload,
                     updated_at=excluded.updated_at""",
                (
                    mode,
                    code,
                    item.get("name") or code,
                    int(bool(item.get("is_candidate"))),
                    int(bool(item.get("is_sim_holding"))),
                    int(bool(item.get("is_live_holding"))),
                    screen.get("run_date") or "",
                    _float(screen.get("score")),
                    screen.get("signal_type") or "",
                    json.dumps(item, ensure_ascii=False),
                    snapshot["as_of"],
                ),
            )
        if codes:
            placeholders = ",".join("?" for _ in codes)
            conn.execute(
                f"DELETE FROM trading_stock_state WHERE mode=? AND code NOT IN ({placeholders})",
                [mode, *codes],
            )
        else:
            conn.execute("DELETE FROM trading_stock_state WHERE mode=?", (mode,))
        market = {
            "mode": mode,
            "stage": snapshot.get("stage"),
            "as_of": snapshot.get("as_of"),
            "market": snapshot.get("market") or {},
            "account": snapshot.get("account") or {},
            "account_policy": snapshot.get("account_policy") or {},
            "refresh": snapshot.get("refresh") or {},
        }
        conn.execute(
            """INSERT INTO trading_market_state (mode, payload, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(mode) DO UPDATE SET
                 payload=excluded.payload, updated_at=excluded.updated_at""",
            (mode, json.dumps(market, ensure_ascii=False), snapshot["as_of"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_trading_state(mode: str, store: StockStore | None = None) -> dict[str, Any]:
    """Read one account's AI facts exclusively from SQLite."""
    store = store or StockStore()
    conn = store._get_conn()
    conn.row_factory = sqlite3.Row
    try:
        market_row = conn.execute(
            "SELECT payload, updated_at FROM trading_market_state WHERE mode=?", (mode,)
        ).fetchone()
        rows = conn.execute(
            """SELECT * FROM trading_stock_state WHERE mode=?
               ORDER BY is_sim_holding DESC, is_live_holding DESC, screen_score DESC""",
            (mode,),
        ).fetchall()
    finally:
        conn.close()
    if not market_row:
        raise RuntimeError(f"trading_market_state[{mode}] is empty; refresh is required")
    market = json.loads(market_row["payload"])
    positions, candidates, tracked = [], [], []
    for row in rows:
        item = json.loads(row["payload"])
        item["updated_at"] = row["updated_at"]
        holding = row["is_sim_holding"] if mode == "simulated" else row["is_live_holding"]
        if holding:
            positions.append(item)
        elif row["is_candidate"]:
            candidates.append(item)
        else:
            tracked.append(item)
    return {
        "schema": f"{mode}_trading_state.v1",
        "mode": mode,
        "stage": market.get("stage"),
        "as_of": market_row["updated_at"],
        "data_source": "stock_data.db current-state tables",
        "market_context": market.get("market") or {},
        "account": market.get("account") or {},
        "risk_rules": market.get("account_policy") or {},
        "positions": positions,
        "candidates": candidates,
        "tracked": tracked,
        "refresh": market.get("refresh") or {},
    }


def refresh_trading_state(
    stage: str, mode: str, minute_limit: int | None = None,
) -> dict[str, Any]:
    """Refresh one account's holdings and candidate pool into its own DB partition."""
    if mode not in {"simulated", "live"}:
        raise ValueError(f"invalid trading mode: {mode}")
    now = datetime.now()
    as_of = now.strftime("%Y-%m-%d %H:%M:%S")
    store = StockStore()
    trader = SimTrader() if mode == "simulated" else None
    sim_by_code = {
        str(position.code).zfill(6): position
        for position in (trader.portfolio.positions if trader else [])
    }
    live_account = _live_account_without_network(store) if mode == "live" else {}
    live_by_code = {
        str(row.get("code") or "").zfill(6): row
        for row in live_account.get("positions") or []
    }
    live_cfg = load_live_config() if mode == "live" else None
    board_status = candidate_board_status(
        store, trade_date=now.date().isoformat(), now=now,
    )
    candidates = _screen_candidates(store, now.date().isoformat())
    _validate_candidate_board_scope(board_status, candidates, now)
    if live_cfg is not None:
        candidates = [
            row for row in candidates
            if is_live_buy_allowed(str(row.get("code") or ""), live_cfg)
        ]
    candidate_by_code = {row["code"]: row for row in candidates}
    holding_by_code = sim_by_code if mode == "simulated" else live_by_code
    codes = list(dict.fromkeys(list(holding_by_code) + list(candidate_by_code)))
    quotes = fetch_quotes(codes)
    if codes and not any(_float((quotes.get(code) or {}).get("price")) > 0 for code in codes):
        raise RuntimeError("Tencent realtime refresh failed for the entire decision universe")
    # Holdings and candidates form one bounded decision universe.  Every code
    # needs the same intraday evidence; limiting this list made later-ranked
    # candidates impossible to assess on VWAP and half-hour volume/price.
    minute_codes = _minute_scope(codes, minute_limit)
    with ThreadPoolExecutor(max_workers=5) as pool:
        minute_future = pool.submit(_minute_states, minute_codes)
        flow_future = pool.submit(
            _fund_flows, codes, store, retry_missing=True,
        )
        index_future = pool.submit(fetch_market_indices)
        sector_future = pool.submit(_sector_state)
        membership_future = pool.submit(_ensure_sector_memberships, codes, store)
        minutes = minute_future.result()
        flows = flow_future.result()
        indices = index_future.result()
        sectors = sector_future.result()
        membership_refresh = membership_future.result()
    try:
        sector_contexts = SectorRotationService(store=store).get_stock_contexts(
            codes, snapshot=sectors,
        )
    except Exception as exc:
        sector_contexts = {
            code: {
                "membership_status": "missing",
                "rotation_status": sectors.get("status", "unavailable"),
                "rotation_as_of": sectors.get("created_at"),
                "matches": [],
                "rotation_score": 0.0,
                "alignment": "unknown",
                "error": str(exc),
            }
            for code in codes
        }
    news, policy_context = _news(store, codes, as_of)
    if trader:
        trader.portfolio.update_prices({
            code: _float((quotes.get(code) or {}).get("price")) for code in sim_by_code
        })
        trader._save_portfolio()
        account = trader.portfolio.summary()
        account_policy = simulated_account_policy(len(sim_by_code))
        sim_activity = _sim_position_activity(store, list(sim_by_code), as_of)
    else:
        conn = store._get_conn()
        conn.row_factory = sqlite3.Row
        try:
            live_account = account_snapshot(conn, quotes=quotes, expire_pending=False)
        finally:
            conn.close()
        live_by_code = {
            str(row.get("code") or "").zfill(6): row
            for row in live_account.get("positions") or []
        }
        account = live_account.get("summary") or {}
        account_policy = {
            "manual_only": True,
            "creates_broker_orders": False,
            "blocked_prefixes": list(live_blocked_prefixes(live_cfg)),
            "max_positions": live_cfg.get("max_positions"),
            "buy_lot_size": 100,
        }
        sim_activity = {}

    def simulated_position_payload(code: str, position: Any) -> dict[str, Any] | None:
        if not position:
            return None
        weight_pct = (
            round(_float(position.market_value) / _float(account.get("total_equity")) * 100, 2)
            if _float(account.get("total_equity"))
            else 0.0
        )
        activity = sim_activity.get(code, {})
        return {
            **position.to_dict(),
            "available_to_sell": int(
                max(0, position.volume - trader._today_buy_volume(code))
            ),
            "portfolio_weight_pct": weight_pct,
            **activity,
            "residual_after_reductions": bool(
                weight_pct < 2
                and int(activity.get("recent_sell_orders_7d") or 0) >= 2
            ),
        }

    def item(code: str) -> dict[str, Any]:
        candidate = candidate_by_code.get(code) or {}
        position = sim_by_code.get(code) if mode == "simulated" else None
        live_position = live_by_code.get(code) if mode == "live" else None
        quote = quotes.get(code) or {"code": code, "error": "quote missing"}
        return {
            "code": code,
            "name": quote.get("name") or candidate.get("name") or (position.name if position else "") or (live_position or {}).get("name") or code,
            "is_candidate": code in candidate_by_code,
            "is_sim_holding": mode == "simulated" and code in sim_by_code,
            "is_live_holding": mode == "live" and code in live_by_code,
            "screen": {
                "run_date": candidate.get("run_date"),
                "run_time": candidate.get("run_time"),
                "score": candidate.get("score"),
                "signal_type": candidate.get("signal_type"),
                "trend": candidate.get("trend"),
                "tags": candidate.get("strategies"),
                "concepts": candidate.get("concepts"),
                "extra": candidate.get("extra") or {},
            } if candidate else {},
            "position": simulated_position_payload(code, position),
            "live_position": live_position,
            "quote": quote,
            "technical": technical_state(code, store),
            "intraday": minutes.get(code, {"half_hour": {"available": False}, "error": "minute limit"}),
            "fund_flow": flows.get(code, {"summary": "", "error": "fund flow missing"}),
            "sector": sector_contexts.get(code, {
                "membership_status": "missing",
                "rotation_status": sectors.get("status", "unavailable"),
                "rotation_as_of": sectors.get("created_at"),
                "matches": [],
                "rotation_score": 0.0,
                "alignment": "unknown",
            }),
            "news": news.get(code, []),
            "policy_evidence": match_policy_evidence(
                policy_context, sector_contexts.get(code, {}),
            ),
            "themes": FIFTEEN_FIVE_STOCKS.get(code, {}).get("concepts", []),
            "updated_at": as_of,
        }

    items = [item(code) for code in codes]
    _annotate_relative_strength(items)
    if mode == "simulated":
        account_policy.update(simulated_account_policy(
            len(sim_by_code),
            _industry_exposure(items, _float(account.get("total_equity")), mode),
        ))
    snapshot = {
        "stage": stage,
        "as_of": as_of,
        "market": {
            "indices": indices,
            "regime": classify_market_regime(indices),
            "sector_rotation": sectors,
            "policy_context": policy_context,
        },
        "account": account,
        "account_policy": account_policy,
        "positions": [row for row in items if row[f"is_{'sim' if mode == 'simulated' else 'live'}_holding"]],
        "candidates": [
            row for row in items
            if row["is_candidate"]
            and not row[f"is_{'sim' if mode == 'simulated' else 'live'}_holding"]
        ],
        "tracked": [],
        "refresh": {
            "mode": mode,
            "scope_count": len(codes),
            "candidate_count": len(candidate_by_code),
            "candidate_board": board_status,
            "holding_count": len(holding_by_code),
            "quote_ok": sum(1 for code in codes if _float((quotes.get(code) or {}).get("price")) > 0),
            "intraday_requested": len(minute_codes),
            "intraday_ok": sum(1 for code in minute_codes if (minutes.get(code) or {}).get("last_time")),
            "fund_flow_ok": sum(1 for code in codes if (flows.get(code) or {}).get("detail")),
            "fund_flow_fresh": sum(1 for code in codes if (flows.get(code) or {}).get("status") == "available"),
            "fund_flow_cached": sum(1 for code in codes if (flows.get(code) or {}).get("status") == "cached"),
            "sector_membership_ok": sum(
                1 for code in codes
                if (sector_contexts.get(code) or {}).get("membership_status") == "available"
            ),
            "sector_rotation_matched": sum(
                1 for code in codes if (sector_contexts.get(code) or {}).get("matches")
            ),
            "sector_membership_refreshed": membership_refresh.get("refreshed", 0),
            "sector_membership_errors": len(membership_refresh.get("errors") or []),
            "sector_status": sectors.get("status", "unavailable"),
        },
    }
    persist_trading_state(snapshot, mode, store)
    return load_trading_state(mode, store)
