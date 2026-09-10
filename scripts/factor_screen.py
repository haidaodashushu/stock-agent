#!/usr/bin/env python3
"""Cross-sectional single-factor screen over the whole market.

Purpose: decide which signals deserve weight in the screening layer, using
this database rather than textbook priors. Prompted by the health check
finding that trend/golden-cross/MACD are collinear "already moved" signals
and that the pool systematically buys extended names.

For each factor and horizon it ranks the full cross-section on each
rebalance date, splits it into quintiles, and measures forward returns. A
factor is only interesting if Q5-Q1 is large AND ordering is roughly
monotonic AND it survives across horizons.

Definitions are deliberately simple and computed from daily bars only:
    mom_20      20-day return                       (momentum / reversal)
    mom_60      60-day return                       (medium-term trend)
    rev_5       negative 5-day return               (short-term reversal)
    vol_20      20-day realized volatility          (low-vol anomaly)
    turnover    20-day mean of close*volume         (liquidity / size proxy)
    amihud      illiquidity: |ret| / turnover       (illiquidity premium)
    dist_high   distance below 60-day high          (position in range)
    vol_trend   5-day vs 20-day volume ratio        (volume expansion)

Read-only. Writes nothing.

Usage:
    .venv/bin/python scripts/factor_screen.py
    .venv/bin/python scripts/factor_screen.py --horizons 5,10,20 --json-out /tmp/f.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "stock_data.db"

QUANTILES = 5
MIN_NAMES_PER_DATE = 800          # a date must have a real cross-section
MIN_OBS_PER_BUCKET = 100          # below this a bucket mean is noise
REBALANCE_EVERY = 5               # trading days between sampling dates
MIN_PRICE = 2.0                   # drop near-delisting penny names
MIN_TURNOVER = 2e6                # ~2m CNY/day floor, in close*volume units


def _connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise SystemExit(f"database not found: {db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load(conn: sqlite3.Connection, start: str) -> tuple[list[str], dict[str, dict[str, tuple]]]:
    """Return (sorted dates, {code: {date: (close, volume)}})."""
    series: dict[str, dict[str, tuple]] = {}
    dates: set[str] = set()
    for row in conn.execute(
        "SELECT code, date, close, volume FROM daily_prices "
        "WHERE close > 0 AND date >= ? ORDER BY date",
        (start,),
    ):
        code = str(row["code"]).zfill(6)
        d = str(row["date"])
        series.setdefault(code, {})[d] = (float(row["close"]), float(row["volume"] or 0))
        dates.add(d)
    return sorted(dates), series


# ------------------------------------------------------------------- factors


def _ret(hist: list[tuple], back: int) -> float | None:
    if len(hist) <= back:
        return None
    a, b = hist[-1 - back][0], hist[-1][0]
    return (b / a - 1) * 100 if a > 0 else None


def _turnover(hist: list[tuple], window: int = 20) -> float | None:
    tail = hist[-window:]
    if len(tail) < window:
        return None
    vals = [c * v for c, v in tail]
    return statistics.fmean(vals) if vals else None


def _volatility(hist: list[tuple], window: int = 20) -> float | None:
    tail = hist[-window - 1:]
    if len(tail) < window + 1:
        return None
    rets = [
        (tail[i][0] / tail[i - 1][0] - 1)
        for i in range(1, len(tail))
        if tail[i - 1][0] > 0
    ]
    return statistics.pstdev(rets) * 100 if len(rets) > 1 else None


def _amihud(hist: list[tuple], window: int = 20) -> float | None:
    tail = hist[-window - 1:]
    if len(tail) < window + 1:
        return None
    vals = []
    for i in range(1, len(tail)):
        prev, cur = tail[i - 1], tail[i]
        turn = cur[0] * cur[1]
        if prev[0] > 0 and turn > 0:
            vals.append(abs(cur[0] / prev[0] - 1) / turn)
    return statistics.fmean(vals) * 1e9 if vals else None


def _dist_from_high(hist: list[tuple], window: int = 60) -> float | None:
    tail = hist[-window:]
    if len(tail) < 20:
        return None
    peak = max(c for c, _ in tail)
    return (hist[-1][0] / peak - 1) * 100 if peak > 0 else None


def _volume_trend(hist: list[tuple], short: int = 5, long: int = 20) -> float | None:
    if len(hist) < long:
        return None
    s = statistics.fmean([v for _, v in hist[-short:]])
    l = statistics.fmean([v for _, v in hist[-long:]])
    return s / l if l > 0 else None


FACTORS: dict[str, tuple[Callable[[list[tuple]], float | None], str]] = {
    "mom_20": (lambda h: _ret(h, 20), "20日动量（正=近期涨得多）"),
    "mom_60": (lambda h: _ret(h, 60), "60日动量（正=中期涨得多）"),
    "rev_5": (lambda h: (-_ret(h, 5)) if _ret(h, 5) is not None else None,
              "5日反转（正=近期跌得多）"),
    "vol_20": (_volatility, "20日波动率（正=波动大）"),
    "turnover": (_turnover, "20日成交额（正=流动性好）"),
    "amihud": (_amihud, "非流动性（正=越难成交）"),
    "dist_high": (_dist_from_high, "距60日高点（0=在高点，负=离高点远）"),
    "vol_trend": (_volume_trend, "量能扩张（>1=近期放量）"),
}


# ------------------------------------------------------------------ mechanics


def _quantile_buckets(pairs: list[tuple[str, float]], q: int) -> dict[str, int]:
    """Rank-based assignment; ties broken by order so buckets stay balanced."""
    ordered = sorted(pairs, key=lambda kv: kv[1])
    n = len(ordered)
    out: dict[str, int] = {}
    for i, (code, _) in enumerate(ordered):
        out[code] = min(q - 1, i * q // n)
    return out


def _describe(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": round(statistics.fmean(vals), 3),
        "median": round(statistics.median(vals), 3),
        "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
    }


def run(
    dates: list[str],
    series: dict[str, dict[str, tuple]],
    horizons: list[int],
) -> dict[str, Any]:
    idx = {d: i for i, d in enumerate(dates)}
    # Precompute each code's ordered history once.
    hist_by_code = {code: sorted(rec.items()) for code, rec in series.items()}

    results: dict[str, Any] = {}
    for fname, (fn, desc) in FACTORS.items():
        per_h: dict[str, Any] = {}
        for h in horizons:
            buckets: dict[int, list[float]] = {i: [] for i in range(QUANTILES)}
            used_dates = 0
            for di in range(60, len(dates) - h, REBALANCE_EVERY):
                d = dates[di]
                exposures: list[tuple[str, float]] = []
                fwd: dict[str, float] = {}
                for code, hist in hist_by_code.items():
                    upto = [(dt, v) for dt, v in hist if dt <= d]
                    if len(upto) < 61:
                        continue
                    vals = [v for _, v in upto]
                    px, vol = vals[-1]
                    if px < MIN_PRICE:
                        continue
                    turn = _turnover(vals)
                    if turn is None or turn < MIN_TURNOVER:
                        continue
                    x = fn(vals)
                    if x is None:
                        continue
                    exit_rec = series.get(code, {}).get(dates[di + h])
                    if not exit_rec:
                        continue
                    exposures.append((code, x))
                    fwd[code] = (exit_rec[0] / px - 1) * 100
                if len(exposures) < MIN_NAMES_PER_DATE:
                    continue
                used_dates += 1
                assign = _quantile_buckets(exposures, QUANTILES)
                for code, b in assign.items():
                    if code in fwd:
                        buckets[b].append(fwd[code])

            table = {
                f"Q{b + 1}": {
                    **_describe(v),
                    "usable": len(v) >= MIN_OBS_PER_BUCKET,
                }
                for b, v in sorted(buckets.items())
            }
            entry: dict[str, Any] = {"buckets": table, "rebalance_dates": used_dates}
            means = [
                statistics.fmean(buckets[b])
                for b in sorted(buckets)
                if len(buckets[b]) >= MIN_OBS_PER_BUCKET
            ]
            if len(means) == QUANTILES:
                spread = means[-1] - means[0]
                inc = sum(1 for a, b in zip(means, means[1:]) if b > a)
                entry["spread_q5_q1"] = round(spread, 3)
                # Monotonicity as a fraction rather than a strict all(): real
                # factors rarely order perfectly, but a genuine one should get
                # most steps right in the same direction.
                entry["monotonic_score"] = round(
                    max(inc, (QUANTILES - 1) - inc) / (QUANTILES - 1), 2
                )
                entry["direction"] = "high_better" if spread > 0 else "low_better"
            per_h[f"{h}d"] = entry
        results[fname] = {"description": desc, **per_h}
    return results


def stability(
    dates: list[str],
    series: dict[str, dict[str, tuple]],
    hist_by_code: dict[str, list],
    horizon: int,
    factor: str,
) -> dict[str, Any]:
    """Per-month Q5-Q1 for one factor, alongside that month's market return.

    A factor that only works because the market fell is not a factor, it is a
    market call. Splitting by month and showing the market's own return next to
    the spread makes that distinction checkable: if the spread keeps its sign in
    both up and down months, the effect is cross-sectional rather than
    directional.
    """
    fn = FACTORS[factor][0]
    by_month: dict[str, list[float]] = {}
    for di in range(60, len(dates) - horizon, REBALANCE_EVERY):
        d = dates[di]
        exposures: list[tuple[str, float]] = []
        fwd: dict[str, float] = {}
        for code, hist in hist_by_code.items():
            upto = [v for dt, v in hist if dt <= d]
            if len(upto) < 61:
                continue
            px, _ = upto[-1]
            if px < MIN_PRICE:
                continue
            turn = _turnover(upto)
            if turn is None or turn < MIN_TURNOVER:
                continue
            x = fn(upto)
            if x is None:
                continue
            exit_rec = series.get(code, {}).get(dates[di + horizon])
            if not exit_rec:
                continue
            exposures.append((code, x))
            fwd[code] = (exit_rec[0] / px - 1) * 100
        if len(exposures) < MIN_NAMES_PER_DATE:
            continue
        exposures.sort(key=lambda kv: kv[1])
        n = len(exposures)
        q1 = [fwd[c] for c, _ in exposures[: n // QUANTILES] if c in fwd]
        q5 = [fwd[c] for c, _ in exposures[-(n // QUANTILES):] if c in fwd]
        if q1 and q5:
            by_month.setdefault(d[:7], []).append(
                statistics.fmean(q5) - statistics.fmean(q1)
            )

    # Market return per month, equal-weighted over names quoted at both ends.
    month_last: dict[str, str] = {}
    for d in dates:
        month_last[d[:7]] = d
    market: dict[str, float] = {}
    months = sorted(month_last)
    for prev_m, cur_m in zip(months, months[1:]):
        a = {c: series[c][month_last[prev_m]][0] for c in series if month_last[prev_m] in series[c]}
        b = {c: series[c][month_last[cur_m]][0] for c in series if month_last[cur_m] in series[c]}
        common = a.keys() & b.keys()
        if common:
            market[cur_m] = statistics.fmean([(b[c] / a[c] - 1) * 100 for c in common])

    rows = {}
    for m in sorted(by_month):
        spreads = by_month[m]
        rows[m] = {
            "periods": len(spreads),
            "spread": round(statistics.fmean(spreads), 3),
            "market": round(market[m], 2) if m in market else None,
        }
    signs = {1 if r["spread"] > 0 else -1 for r in rows.values()}
    return {
        "factor": factor,
        "horizon": horizon,
        "months": rows,
        "sign_stable": len(signs) == 1 and len(rows) >= 3,
    }


def render(report: dict[str, Any]) -> str:
    m = report["meta"]
    L = []
    L.append("=" * 78)
    L.append("全市场单因子检验（横截面分位，用于决定该给哪些信号权重）")
    L.append("=" * 78)
    L.append(f"区间      : {m['range']}   调仓间隔 {m['rebalance_every']} 日")
    L.append(f"过滤      : 股价≥{m['min_price']}元, 20日成交额≥{m['min_turnover'] / 1e6:.0f}百万")
    L.append(f"判定门槛  : 每桶≥{m['min_obs']}观测, 每期≥{m['min_names']}只")
    L.append("")
    L.append("读法: Q5-Q1 为多空价差; 单调性1.00表示五档完全按序; 方向说明哪一端更好")
    L.append("")

    for h in m["horizons"]:
        key = f"{h}d"
        L.append("-" * 78)
        L.append(f"持有 {h} 个交易日")
        L.append("-" * 78)
        L.append(f"{'因子':<12}{'Q1':>9}{'Q3':>9}{'Q5':>9}{'Q5-Q1':>10}{'单调':>7}  说明")
        rows = []
        for fname, data in report["factors"].items():
            e = data.get(key, {})
            if "spread_q5_q1" not in e:
                continue
            b = e["buckets"]
            rows.append((
                abs(e["spread_q5_q1"]), fname,
                b["Q1"]["mean"], b["Q3"]["mean"], b["Q5"]["mean"],
                e["spread_q5_q1"], e["monotonic_score"], data["description"],
            ))
        for _, fname, q1, q3, q5, sp, mono, desc in sorted(rows, reverse=True):
            L.append(
                f"{fname:<12}{q1:>+9.2f}{q3:>+9.2f}{q5:>+9.2f}{sp:>+10.2f}{mono:>7.2f}  {desc}"
            )
        L.append("")

    if report.get("stability"):
        s = report["stability"]
        L.append("-" * 78)
        L.append(f"稳定性检验: {s['factor']} (持有{s['horizon']}日) 分月 Q5-Q1 vs 当月大盘")
        L.append("-" * 78)
        L.append(f"{'月份':<10}{'调仓期数':>9}{'Q5-Q1':>10}{'当月大盘':>11}  判定")
        for m, r in s["months"].items():
            mk = f"{r['market']:+.2f}%" if r["market"] is not None else "n/a"
            tag = "反转占优" if r["spread"] < 0 else "动量占优"
            L.append(f"{m:<10}{r['periods']:>9d}{r['spread']:>+10.2f}{mk:>11}  {tag}")
        if s["sign_stable"]:
            L.append("  → 各月方向一致；若涨跌月都成立，则非单纯的市场方向效应")
        else:
            L.append("  → 方向随月份翻转，该因子不稳定")
        L.append("")

    L.append("=" * 78)
    L.append("注意: 样本仅覆盖上述区间且为单一市场阶段，价差方向可能随行情反转。")
    L.append("单因子有效 ≠ 加入打分后仍有效，合并前需再查因子间相关性。")
    L.append("=" * 78)
    return "\n".join(L)


def main() -> int:
    p = argparse.ArgumentParser(description="Whole-market single-factor screen")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--start", default="2026-03-01", help="full-market coverage starts here")
    p.add_argument("--horizons", default="5,10,20")
    p.add_argument("--json-out", default="")
    p.add_argument(
        "--stability-factor",
        default="mom_20",
        help="factor to break down by month (use '' to skip)",
    )
    args = p.parse_args()

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    conn = _connect(Path(args.db))
    try:
        dates, series = _load(conn, args.start)
    finally:
        conn.close()
    if len(dates) < 80:
        print(f"only {len(dates)} trading days available; too short", file=sys.stderr)
        return 1

    factors = run(dates, series, horizons)
    report = {
        "meta": {
            "range": f"{dates[0]} ~ {dates[-1]}",
            "trading_days": len(dates),
            "codes": len(series),
            "horizons": horizons,
            "rebalance_every": REBALANCE_EVERY,
            "min_price": MIN_PRICE,
            "min_turnover": MIN_TURNOVER,
            "min_obs": MIN_OBS_PER_BUCKET,
            "min_names": MIN_NAMES_PER_DATE,
        },
        "factors": factors,
    }
    if args.stability_factor and args.stability_factor in FACTORS:
        hist_by_code = {code: sorted(rec.items()) for code, rec in series.items()}
        report["stability"] = stability(
            dates, series, hist_by_code, max(horizons), args.stability_factor
        )
    print(render(report))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nJSON: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
