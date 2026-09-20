#!/usr/bin/env python3
"""One-off screening health check.

Answers three yes/no questions about the deterministic screening layer only:

  Q1  Does the candidate pool beat the market baseline over the next 5/10/20 days?
  Q2  Is forward return monotonic in ``score``?
  Q3  Does the static theme bonus (AI compute / 15-5 pool) add anything?

This is NOT a backtest and NOT a quant pipeline. It does not simulate a
portfolio, model position sizing, or evaluate AI trading decisions -- those are
non-deterministic and cannot be replayed. It reads the screening layer's own
recorded history (``screen_records``, produced before any AI involvement) and
joins it to realized forward prices.

Run once, read the verdicts, then delete this file if you like. It writes
nothing to the database.

Usage:
    .venv/bin/python scripts/screening_health_check.py
    .venv/bin/python scripts/screening_health_check.py --horizons 5,10,20 --json-out /tmp/h.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "stock_data.db"
DEFAULT_HORIZONS = (5, 10, 20)
# Below this many observations a bucket's mean is noise, not a finding.
MIN_BUCKET_N = 20
# Score buckets are quantile-based, so an arbitrary 7.0/9.0 cut is not assumed.
SCORE_BUCKETS = 5


# ---------------------------------------------------------------- data loading


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _trading_days(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["date"])
        for row in conn.execute("SELECT DISTINCT date FROM daily_prices ORDER BY date")
    ]


def _price_panel(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Return ``{date: {code: close}}`` for the whole price table."""
    panel: dict[str, dict[str, float]] = {}
    for row in conn.execute(
        "SELECT date, code, close FROM daily_prices WHERE close > 0"
    ):
        panel.setdefault(str(row["date"]), {})[str(row["code"]).zfill(6)] = float(
            row["close"]
        )
    return panel


def _full_market_dates(panel: dict[str, dict[str, float]], min_codes: int) -> set[str]:
    """Dates whose cross-section is wide enough to act as a market baseline.

    Early history in this database covers only a ~115-name watchlist. Using
    those dates as a "whole market" baseline would silently compare the pool
    against itself, so they are excluded from Q1.
    """
    return {date for date, quotes in panel.items() if len(quotes) >= min_codes}


def _selections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load recorded screening output, one row per (run_date, code).

    ``screen_records`` is written by the deterministic selector before any AI
    step, which is exactly the layer under test. Where a run produced several
    rows for one code (multiple runs per day), the earliest is kept so the
    forward window always starts from the first time the name was surfaced.
    """
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(
        """SELECT run_date, run_time, code, name, score, signal_type, extra
             FROM screen_records
            WHERE run_date IS NOT NULL AND code IS NOT NULL
            ORDER BY run_date, run_time"""
    ):
        code = str(row["code"]).zfill(6)
        key = (str(row["run_date"]), code)
        if key in rows:
            continue
        extra: dict[str, Any] = {}
        if row["extra"]:
            try:
                parsed = json.loads(row["extra"])
                if isinstance(parsed, dict):
                    extra = parsed
            except (json.JSONDecodeError, TypeError):
                extra = {}
        selector = extra.get("selector") if isinstance(extra.get("selector"), dict) else {}
        rows[key] = {
            "run_date": str(row["run_date"]),
            "code": code,
            "name": str(row["name"] or ""),
            "score": float(row["score"] or 0.0),
            "signal_type": str(row["signal_type"] or ""),
            "theme_bonus": float(selector.get("theme_bonus") or 0.0),
            "entry_route": str(selector.get("entry_route") or "") or None,
            "position_pct": selector.get("position_pct"),
        }
    return list(rows.values())


# ------------------------------------------------------------------- mechanics


def _forward_return(
    code: str,
    entry_date: str,
    horizon: int,
    panel: dict[str, dict[str, float]],
    days: list[str],
    day_index: dict[str, int],
) -> float | None:
    """Realized return from the close AFTER the signal to ``horizon`` days later.

    The screening run for ``run_date`` is produced from bars up to and including
    the prior session, and the earliest a position could be taken is the next
    session. Entry is therefore ``run_date``'s close (the first close an actor
    could transact on), never the bar the score was computed from. This keeps
    the measurement free of look-ahead.
    """
    start = day_index.get(entry_date)
    if start is None:
        return None
    end = start + horizon
    if end >= len(days):
        return None
    entry_px = panel.get(days[start], {}).get(code)
    exit_px = panel.get(days[end], {}).get(code)
    if not entry_px or not exit_px or entry_px <= 0:
        return None
    return (exit_px / entry_px - 1) * 100


def _market_return(
    entry_date: str,
    horizon: int,
    panel: dict[str, dict[str, float]],
    days: list[str],
    day_index: dict[str, int],
    cache: dict[tuple[str, int], float | None],
) -> float | None:
    """Equal-weighted mean return of every name quoted on both dates.

    Equal weighting is deliberate: the pool is equal-weighted too, so an
    index-weighted baseline would compare different things.
    """
    key = (entry_date, horizon)
    if key in cache:
        return cache[key]
    start = day_index.get(entry_date)
    result: float | None = None
    if start is not None and start + horizon < len(days):
        begin, finish = panel.get(days[start], {}), panel.get(days[start + horizon], {})
        rets = [
            (finish[code] / begin[code] - 1) * 100
            for code in begin.keys() & finish.keys()
            if begin[code] > 0
        ]
        if rets:
            result = statistics.fmean(rets)
    cache[key] = result
    return result


def _prior_gain(
    code: str,
    entry_date: str,
    lookback: int,
    panel: dict[str, dict[str, float]],
    days: list[str],
    day_index: dict[str, int],
) -> float | None:
    """Return already-realized gain over the ``lookback`` sessions before entry."""
    start = day_index.get(entry_date)
    if start is None or start < lookback:
        return None
    then = panel.get(days[start - lookback], {}).get(code)
    now = panel.get(days[start], {}).get(code)
    if not then or not now or then <= 0:
        return None
    return (now / then - 1) * 100


def _describe(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "win_rate": round(sum(1 for v in values if v > 0) / len(values) * 100, 1),
    }


def _buckets(values: list[float], count: int) -> list[float]:
    """Quantile cut points, deduplicated so ties do not create empty buckets."""
    ordered = sorted(values)
    if not ordered:
        return []
    cuts = []
    for i in range(1, count):
        idx = int(len(ordered) * i / count)
        cuts.append(ordered[min(idx, len(ordered) - 1)])
    return sorted(set(cuts))


def _bucket_of(value: float, cuts: list[float]) -> int:
    for i, cut in enumerate(cuts):
        if value <= cut:
            return i
    return len(cuts)


# ------------------------------------------------------------------- questions


def q1_pool_vs_market(
    rows: list[dict[str, Any]], horizons: Iterable[int], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Q1: does being in the pool beat the equal-weighted market at all?"""
    out: dict[str, Any] = {}
    for h in horizons:
        pool, excess = [], []
        for row in rows:
            if row["run_date"] not in ctx["baseline_dates"]:
                continue
            r = _forward_return(
                row["code"], row["run_date"], h, ctx["panel"], ctx["days"], ctx["day_index"]
            )
            if r is None:
                continue
            m = _market_return(
                row["run_date"], h, ctx["panel"], ctx["days"], ctx["day_index"], ctx["mcache"]
            )
            if m is None:
                continue
            pool.append(r)
            excess.append(r - m)
        stats = {"pool": _describe(pool), "excess": _describe(excess)}
        if excess and len(excess) >= MIN_BUCKET_N:
            mean_x = statistics.fmean(excess)
            # Paired t-like screen: excess is per-signal vs its own date's market,
            # so dispersion across signals is the relevant error scale.
            sd = statistics.pstdev(excess) or 0.0
            stats["mean_excess"] = round(mean_x, 3)
            stats["t_stat"] = round(mean_x / (sd / len(excess) ** 0.5), 2) if sd else None
            stats["beats_market"] = mean_x > 0
        out[f"{h}d"] = stats
    return out


def q2_score_monotonic(
    rows: list[dict[str, Any]], horizons: Iterable[int], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Q2: do higher scores actually earn more?"""
    scored = [r for r in rows if r["score"] is not None]
    cuts = _buckets([r["score"] for r in scored], SCORE_BUCKETS)
    out: dict[str, Any] = {"score_cuts": [round(c, 2) for c in cuts]}
    for h in horizons:
        grouped: dict[int, list[float]] = {}
        for row in scored:
            r = _forward_return(
                row["code"], row["run_date"], h, ctx["panel"], ctx["days"], ctx["day_index"]
            )
            if r is None:
                continue
            grouped.setdefault(_bucket_of(row["score"], cuts), []).append(r)
        table = {
            f"Q{b + 1}": {**_describe(v), "usable": len(v) >= MIN_BUCKET_N}
            for b, v in sorted(grouped.items())
        }
        usable = [
            (b, statistics.fmean(v))
            for b, v in sorted(grouped.items())
            if len(v) >= MIN_BUCKET_N
        ]
        verdict: dict[str, Any] = {"buckets": table}
        if len(usable) >= 2:
            means = [m for _, m in usable]
            verdict["monotonic_increasing"] = all(
                a <= b for a, b in zip(means, means[1:])
            )
            verdict["top_minus_bottom"] = round(means[-1] - means[0], 3)
        out[f"{h}d"] = verdict
    return out


def q3_theme_bonus(
    rows: list[dict[str, Any]], horizons: Iterable[int], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Q3: is the static theme bonus earning its ~31% share of the buy threshold?"""
    out: dict[str, Any] = {}
    tagged = [r for r in rows if r["theme_bonus"] > 0]
    plain = [r for r in rows if r["theme_bonus"] <= 0]
    out["counts"] = {"with_theme_bonus": len(tagged), "without": len(plain)}
    for h in horizons:
        def rets(subset: list[dict[str, Any]]) -> list[float]:
            vals = []
            for row in subset:
                r = _forward_return(
                    row["code"], row["run_date"], h, ctx["panel"], ctx["days"], ctx["day_index"]
                )
                if r is not None:
                    vals.append(r)
            return vals

        a, b = rets(tagged), rets(plain)
        entry: dict[str, Any] = {"with_theme": _describe(a), "without_theme": _describe(b)}
        if len(a) >= MIN_BUCKET_N and len(b) >= MIN_BUCKET_N:
            diff = statistics.fmean(a) - statistics.fmean(b)
            entry["difference"] = round(diff, 3)
            entry["theme_helps"] = diff > 0
        out[f"{h}d"] = entry
    return out


def q4_prior_gain(
    rows: list[dict[str, Any]], horizons: Iterable[int], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Q4 (diagnostic): does the pool buy names that already ran up?

    Q1-Q3 measure whether the layer works. This one explains why. If forward
    return degrades monotonically as prior gain rises, the scorer is buying
    late-stage moves rather than identifying them early -- which is a weighting
    problem, not a market problem.
    """
    edges = ((None, 0.0, "<0%"), (0.0, 10.0, "0-10%"), (10.0, 25.0, "10-25%"), (25.0, None, ">25%"))

    def label(gain: float) -> str:
        for lo, hi, name in edges:
            if (lo is None or gain >= lo) and (hi is None or gain < hi):
                return name
        return ">25%"

    out: dict[str, Any] = {"lookback_days": 20}
    for h in horizons:
        grouped: dict[str, list[float]] = {}
        for row in rows:
            prior = _prior_gain(
                row["code"], row["run_date"], 20, ctx["panel"], ctx["days"], ctx["day_index"]
            )
            fwd = _forward_return(
                row["code"], row["run_date"], h, ctx["panel"], ctx["days"], ctx["day_index"]
            )
            if prior is None or fwd is None:
                continue
            grouped.setdefault(label(prior), []).append(fwd)
        table = {
            name: {**_describe(grouped.get(name, [])), "usable": len(grouped.get(name, [])) >= 15}
            for _, _, name in edges
        }
        entry: dict[str, Any] = {"buckets": table}
        usable = [
            statistics.fmean(grouped[name])
            for _, _, name in edges
            if len(grouped.get(name, [])) >= 15
        ]
        if len(usable) >= 3:
            entry["degrades_with_prior_gain"] = all(
                a >= b for a, b in zip(usable, usable[1:])
            )
        out[f"{h}d"] = entry
    return out


# ------------------------------------------------------------------- reporting


def _fmt(stats: dict[str, Any]) -> str:
    if not stats.get("n"):
        return "样本不足"
    return (
        f"n={stats['n']:<5d} 均值={stats['mean']:+7.2f}%  "
        f"中位={stats['median']:+7.2f}%  胜率={stats['win_rate']:.1f}%"
    )


def render(report: dict[str, Any]) -> str:
    m = report["meta"]
    L: list[str] = []
    L.append("=" * 74)
    L.append("选股第一层体检（确定性打分层，不含 AI 决策）")
    L.append("=" * 74)
    L.append(f"数据库          : {m['db']}")
    L.append(f"行情覆盖        : {m['price_range']}  共 {m['trading_days']} 个交易日")
    L.append(f"全市场基准可用日: {m['baseline_days']} 天（截面 ≥ {m['min_codes']} 只才计入）")
    L.append(f"选股记录        : {m['selection_range']}  {m['selection_days']} 天 / {m['selections']} 条")
    L.append(f"入场口径        : run_date 收盘买入 → N 交易日后收盘（无前视）")
    L.append("")

    L.append("-" * 74)
    L.append("Q1  候选池是否跑赢等权全市场")
    L.append("-" * 74)
    for h, s in report["q1"].items():
        L.append(f"[{h}]")
        L.append(f"  候选池   {_fmt(s['pool'])}")
        L.append(f"  超额     {_fmt(s['excess'])}")
        if "mean_excess" in s:
            t = s.get("t_stat")
            L.append(
                f"  → 平均超额 {s['mean_excess']:+.2f}%   t≈{t if t is not None else 'n/a'}   "
                f"{'跑赢' if s['beats_market'] else '跑输'}"
            )
        else:
            L.append("  → 样本不足，不下结论")
        L.append("")

    L.append("-" * 74)
    L.append("Q2  分数越高是否收益越高（分位分桶）")
    L.append("-" * 74)
    L.append(f"分位切点: {report['q2'].get('score_cuts')}")
    for h, s in report["q2"].items():
        if not h.endswith("d"):
            continue
        L.append(f"[{h}]")
        for b, v in s["buckets"].items():
            flag = "" if v.get("usable") else "  (样本不足)"
            L.append(f"  {b}  {_fmt(v)}{flag}")
        if "monotonic_increasing" in s:
            L.append(
                f"  → 单调递增: {'是' if s['monotonic_increasing'] else '否'}   "
                f"最高桶-最低桶 = {s['top_minus_bottom']:+.2f}%"
            )
        else:
            L.append("  → 可用桶不足，不下结论")
        L.append("")

    L.append("-" * 74)
    L.append("Q3  静态题材加分是否有效")
    L.append("-" * 74)
    c = report["q3"]["counts"]
    L.append(f"有题材加分 {c['with_theme_bonus']} 条 / 无 {c['without']} 条")
    for h, s in report["q3"].items():
        if not h.endswith("d"):
            continue
        L.append(f"[{h}]")
        L.append(f"  有题材   {_fmt(s['with_theme'])}")
        L.append(f"  无题材   {_fmt(s['without_theme'])}")
        if "difference" in s:
            L.append(
                f"  → 差异 {s['difference']:+.2f}%   "
                f"{'题材加分有正贡献' if s['theme_helps'] else '题材加分无正贡献'}"
            )
        else:
            L.append("  → 样本不足，不下结论")
        L.append("")

    L.append("-" * 74)
    L.append("Q4  诊断：候选股是否在被选中前已经大涨（追高检验）")
    L.append("-" * 74)
    L.append(f"回看窗口: 选股日前 {report['q4']['lookback_days']} 个交易日")
    for h, s in report["q4"].items():
        if not h.endswith("d"):
            continue
        L.append(f"[{h}]")
        for name, v in s["buckets"].items():
            flag = "" if v.get("usable") else "  (样本不足)"
            L.append(f"  前期涨幅 {name:<8s} {_fmt(v)}{flag}")
        if "degrades_with_prior_gain" in s:
            if s["degrades_with_prior_gain"]:
                L.append("  → 前期涨幅越大，后续收益越差：存在系统性追高")
            else:
                L.append("  → 未见单调恶化")
        else:
            L.append("  → 可用桶不足，不下结论")
        L.append("")

    L.append("=" * 74)
    L.append("读法：这是观察性统计，不是回测。窗口短、样本少时，任何单一数字都")
    L.append("不足以推翻或确立一个因子；出现『样本不足』即表示该结论不成立。")
    L.append("=" * 74)
    return "\n".join(L)

def main() -> int:
    p = argparse.ArgumentParser(description="One-off screening layer health check")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS))
    p.add_argument(
        "--min-market-codes",
        type=int,
        default=1000,
        help="Minimum cross-section width for a date to serve as market baseline",
    )
    p.add_argument("--json-out", default="")
    args = p.parse_args()

    horizons = [int(x) for x in str(args.horizons).split(",") if x.strip()]
    db_path = Path(args.db)
    conn = _connect(db_path)
    try:
        panel = _price_panel(conn)
        days = _trading_days(conn)
        rows = _selections(conn)
    finally:
        conn.close()

    if not rows:
        print("screen_records 为空，无法体检。", file=sys.stderr)
        return 1

    day_index = {d: i for i, d in enumerate(days)}
    baseline_dates = _full_market_dates(panel, args.min_market_codes)
    ctx = {
        "panel": panel,
        "days": days,
        "day_index": day_index,
        "baseline_dates": baseline_dates,
        "mcache": {},
    }

    sel_dates = sorted({r["run_date"] for r in rows})
    report = {
        "meta": {
            "db": str(db_path),
            "price_range": f"{days[0]} ~ {days[-1]}" if days else "-",
            "trading_days": len(days),
            "baseline_days": len(baseline_dates),
            "min_codes": args.min_market_codes,
            "selection_range": f"{sel_dates[0]} ~ {sel_dates[-1]}",
            "selection_days": len(sel_dates),
            "selections": len(rows),
            "horizons": horizons,
        },
        "q1": q1_pool_vs_market(rows, horizons, ctx),
        "q2": q2_score_monotonic(rows, horizons, ctx),
        "q3": q3_theme_bonus(rows, horizons, ctx),
        "q4": q4_prior_gain(rows, horizons, ctx),
    }

    print(render(report))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nJSON: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
