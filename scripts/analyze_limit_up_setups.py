#!/usr/bin/env python3
"""Study prior-close features of recent limit-up stocks without look-ahead."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "stock_data.db"


FEATURES = (
    "prior_ret1", "prior_ret5", "prior_ret20", "prior_volume_ratio20",
    "prior_to20high", "prior_position60", "prior_compression",
)


def _load(db_path: Path, start: str, end: str) -> pd.DataFrame:
    history_start = (date.fromisoformat(start) - timedelta(days=140)).isoformat()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        frame = pd.read_sql_query(
            """SELECT p.code,p.date,p.open,p.high,p.low,p.close,p.volume,s.name
                 FROM daily_prices p JOIN stocks s ON s.code=p.code
                WHERE p.adjust_flag='qfq' AND p.date BETWEEN ? AND ?
                  AND s.is_active=1 AND p.code NOT LIKE '688%'
                  AND p.code NOT LIKE '8%' AND p.code NOT LIKE '4%'
                ORDER BY p.code,p.date""",
            conn, params=(history_start, end),
        )
    finally:
        conn.close()
    return frame[~frame["name"].str.upper().str.contains("ST", regex=False, na=False)].copy()


def build_sample(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    grouped = frame.groupby("code", group_keys=False)
    frame["ret1"] = grouped["close"].pct_change() * 100
    frame["ret5"] = grouped["close"].pct_change(5) * 100
    frame["ret20"] = grouped["close"].pct_change(20) * 100
    frame["volume_ratio20"] = frame["volume"] / grouped["volume"].transform(
        lambda values: values.shift(1).rolling(20).mean()
    )
    frame["prior20high_raw"] = grouped["high"].transform(lambda values: values.shift(1).rolling(20).max())
    high60 = grouped["high"].transform(lambda values: values.rolling(60).max())
    low60 = grouped["low"].transform(lambda values: values.rolling(60).min())
    frame["to20high"] = (frame["close"] / frame["prior20high_raw"] - 1) * 100
    frame["position60"] = (frame["close"] - low60) / (high60 - low60) * 100
    ranges = (frame["high"] - frame["low"]) / grouped["close"].shift(1) * 100
    atr5 = ranges.groupby(frame["code"]).transform(lambda values: values.rolling(5).mean())
    atr20 = ranges.groupby(frame["code"]).transform(lambda values: values.rolling(20).mean())
    frame["compression"] = atr5 / atr20
    growth = frame["code"].str.startswith("30")
    # Use a narrow band around the board limit.  Returns far above the legal
    # band usually indicate an unadjusted corporate action, a missing bar, or
    # an unrestricted listing day and must not train the launch detector.
    frame["limit_up"] = np.where(
        growth,
        frame["ret1"].between(19.5, 20.5),
        frame["ret1"].between(9.7, 10.5),
    )
    grouped = frame.groupby("code", group_keys=False)
    previous_limits = pd.concat([grouped["limit_up"].shift(i) for i in range(1, 6)], axis=1)
    frame["first_limit"] = frame["limit_up"] & ~previous_limits.fillna(False).any(axis=1)
    for source in ("ret1", "ret5", "ret20", "volume_ratio20", "to20high", "position60", "compression"):
        frame[f"prior_{source}"] = grouped[source].shift(1)
    return frame[(frame["date"] >= start) & (frame["date"] <= end)].dropna(subset=list(FEATURES)).copy()


def _matched_controls(sample: pd.DataFrame, target: str) -> pd.DataFrame:
    positives = sample[sample[target]].copy()
    negatives = sample[~sample[target]].copy()
    controls = []
    match_features = ["prior_ret20", "prior_position60", "prior_volume_ratio20"]
    scales = negatives[match_features].std().replace(0, 1)
    for _, positive in positives.iterrows():
        pool = negatives[
            (negatives["date"] == positive["date"])
            & (negatives["code"].str.startswith("30") == str(positive["code"]).startswith("30"))
        ].copy()
        if pool.empty:
            continue
        distance = sum(((pool[key] - positive[key]) / scales[key]) ** 2 for key in match_features)
        controls.append(pool.loc[distance.nsmallest(3).index])
    return pd.concat(controls, ignore_index=True) if controls else negatives.iloc[0:0]


def analyze(db_path: Path, start: str, end: str) -> dict:
    sample = build_sample(_load(db_path, start, end), start, end)
    result = {"start": start, "end": end, "trading_days": int(sample["date"].nunique()), "targets": {}}
    for target in ("limit_up", "first_limit"):
        positives = sample[sample[target]]
        controls = _matched_controls(sample, target)
        rows = {}
        for feature in FEATURES:
            rows[feature] = {
                "positive_median": round(float(positives[feature].median()), 3),
                "matched_control_median": round(float(controls[feature].median()), 3),
            }
        result["targets"][target] = {
            "events": int(len(positives)),
            "matched_controls": int(len(controls)),
            "base_rate_pct": round(float(sample[target].mean() * 100), 3),
            "feature_medians": rows,
        }
    result["conclusion"] = [
        "前一日高位多头或接近20日高点不是充分条件，不能据此直接追涨。",
        "首板前常见低位/中低位准备形态，但分辨力有限，必须等待当日点火确认。",
        "雷达应把实时相对强度、成交速度、平台突破与VWAP承接作为主要晋级依据。",
        "资金流、板块和新闻只作为独立确认或风险证据，缺失时不得推断。",
    ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="最近涨停股票启动前特征研究")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--start", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    start = args.start or (date.fromisoformat(args.end) - timedelta(days=14)).isoformat()
    report = analyze(args.db, start, args.end)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
