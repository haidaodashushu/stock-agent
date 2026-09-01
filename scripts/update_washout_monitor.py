#!/usr/bin/env python3
"""update_washout_monitor.py - 更新洗盘监控 JSON，供 Web 自选监控展示。

默认更新 monitoring/{code}_washout.json。
数据来源：腾讯实时行情 + 腾讯/本地日K。仅生成量价监控信号，不直接交易。
"""
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.fetcher.tencent_quote import TencentQuoteFetcher  # noqa: E402


STOCK_NAMES = {
    "601012": "隆基绿能",
    "300803": "指南针",
}


def _float(v, default=0.0):
    try:
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def _int(v, default=0):
    try:
        if pd.isna(v):
            return default
        return int(float(v))
    except Exception:
        return default


def _round(v, n=3):
    return round(_float(v), n)


def load_monitor(path: Path, code: str) -> dict:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "stock": code,
        "name": STOCK_NAMES.get(code, ""),
        "label": "洗盘监控",
        "position": {},
        "trade_log": [],
        "key_levels": {},
        "daily_reviews": {},
        "washed_out_pattern": {},
    }


def fetch_realtime_quote(code: str) -> pd.Series:
    """腾讯实时行情快照。避免依赖 fetcher 内部接口变动。"""
    prefix = "sh" if str(code).startswith("6") else "sz"
    url = f"http://qt.gtimg.cn/q={prefix}{code}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        text = resp.read().decode("gbk", errors="ignore")
        if '="' not in text:
            return pd.Series()
        fields = text.split('="', 1)[1].rstrip('";\n').split("~")
        if len(fields) < 40:
            return pd.Series()
        return pd.Series({
            "代码": code,
            "名称": fields[1],
            "最新价": _float(fields[3]),
            "昨收": _float(fields[4]),
            "今开": _float(fields[5]),
            "成交量": _int(fields[6]),
            "成交额": _float(fields[37]) if len(fields) > 37 else 0,
            "最高": _float(fields[33]) if len(fields) > 33 else 0,
            "最低": _float(fields[34]) if len(fields) > 34 else 0,
            "涨跌额": _float(fields[31]) if len(fields) > 31 else 0,
            "涨跌幅": _float(fields[32]) if len(fields) > 32 else 0,
            "数据源": "tencent-qt",
        })
    except Exception:
        return pd.Series()


def normalize_daily(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if "date" not in df.columns:
        df = df.reset_index().rename(columns={"index": "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    for col in ["open", "close", "high", "low", "volume"]:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df[["date", "open", "close", "high", "low", "volume"]].sort_values("date").reset_index(drop=True)


def merge_realtime(df: pd.DataFrame, quote: pd.Series) -> pd.DataFrame:
    if quote is None or quote.empty:
        return df
    today = datetime.now().strftime("%Y-%m-%d")
    price = _float(quote.get("最新价"))
    if price <= 0:
        return df
    row = {
        "date": today,
        "open": _float(quote.get("今开"), price) or price,
        "close": price,
        "high": _float(quote.get("最高"), price) or price,
        "low": _float(quote.get("最低"), price) or price,
        "volume": _int(quote.get("成交量")),
    }
    df = df[df["date"] != today]
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    return df.sort_values("date").reset_index(drop=True)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["close"]
    for n in [5, 10, 20, 60]:
        df[f"ma{n}"] = close.rolling(n, min_periods=1).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd_bar"] = (df["dif"] - df["dea"]) * 2
    df["macd_golden_cross"] = (df["dif"] > df["dea"]) & (df["dif"].shift(1) <= df["dea"].shift(1))

    low_n = df["low"].rolling(9, min_periods=1).min()
    high_n = df["high"].rolling(9, min_periods=1).max()
    rsv = ((close - low_n) / (high_n - low_n).replace(0, pd.NA) * 100).fillna(50)
    k_vals, d_vals = [], []
    k = d = 50.0
    for v in rsv:
        k = 2 / 3 * k + 1 / 3 * float(v)
        d = 2 / 3 * d + 1 / 3 * k
        k_vals.append(k)
        d_vals.append(d)
    df["k"] = k_vals
    df["d"] = d_vals
    df["j"] = 3 * df["k"] - 2 * df["d"]
    return df


def ensure_washout_levels(monitor: dict, df: pd.DataFrame) -> dict:
    """为新股票自动建立洗盘/启动监控关键位。

    逻辑：优先找最近 90 日的大阳线作为“主力启动/试盘阳线”；若没有明显大阳线，
    则退化为最近 20 日波段高低点。已有人工关键位不覆盖。
    """
    levels = dict(monitor.get("key_levels") or {})
    if all(levels.get(k) for k in ["stop_loss", "support_78_6", "big_yang_vol"]):
        return levels

    d = df.copy().tail(120).reset_index(drop=True)
    if d.empty:
        return levels

    d["prev_close"] = d["close"].shift(1)
    d["pct"] = (d["close"] - d["prev_close"]) / d["prev_close"].replace(0, pd.NA) * 100
    d["body_pct"] = (d["close"] - d["open"]) / d["open"].replace(0, pd.NA) * 100
    d["vol_ma5"] = d["volume"].rolling(5, min_periods=1).mean()
    candidates = d[(d["pct"] >= 4.0) & (d["body_pct"] >= 2.0) & (d["volume"] >= d["vol_ma5"] * 1.05)]
    if candidates.empty:
        candidates = d[(d["pct"] >= 3.0) & (d["close"] > d["open"])]

    if not candidates.empty:
        base = candidates.iloc[-1]
    else:
        recent = d.tail(20)
        base = recent.loc[recent["high"].idxmax()]

    high = _float(base["high"])
    low = _float(base["low"])
    rng = high - low
    if rng <= 0:
        recent = d.tail(20)
        high = _float(recent["high"].max())
        low = _float(recent["low"].min())
        rng = max(high - low, 0)

    levels.setdefault("stop_loss", _round(low, 2))
    levels.setdefault("stop_loss_note", "跌破基准阳线/波段低点=结构失败，优先风控")
    levels.setdefault("support_78_6", _round(high - rng * 0.786, 2) if rng else _round(low, 2))
    levels.setdefault("support_note", "基于最近大阳线/波段的78.6%回撤支撑")
    levels.setdefault("half_retrace", _round(high - rng * 0.5, 2) if rng else _round((high + low) / 2, 2))
    levels.setdefault("big_yang_date", str(base["date"]))
    levels.setdefault("big_yang_open", _round(base["open"], 2))
    levels.setdefault("big_yang_close", _round(base["close"], 2))
    levels.setdefault("big_yang_high", _round(high, 2))
    levels.setdefault("big_yang_low", _round(low, 2))
    levels.setdefault("big_yang_vol", _int(base["volume"]))
    levels.setdefault("all_time_low", _round(d["low"].min(), 2))
    return levels


def build_monitor_cards(monitor: dict, review: dict) -> list[dict]:
    levels = monitor.get("key_levels") or {}
    pattern = monitor.get("washed_out_pattern") or {}
    return [{
        "id": "washout_start",
        "type": "washout",
        "title": "洗盘/启动监控",
        "thesis": "从庄家视角观察：前期拉升/试盘后是否缩量洗盘结束，并等待放量启动确认。",
        "phase": pattern.get("current_phase") or review.get("phase") or "跟踪中",
        "advice": pattern.get("next_trigger") or "等待关键位触发",
        "summary": review.get("summary", ""),
        "updated_at": review.get("updated_at") or monitor.get("last_updated_at"),
        "key_levels": [
            {"name": "止损/结构失败", "value": levels.get("stop_loss"), "note": levels.get("stop_loss_note", "")},
            {"name": "78.6%支撑", "value": levels.get("support_78_6"), "note": levels.get("support_note", "")},
            {"name": "半分位", "value": levels.get("half_retrace"), "note": "强弱分界参考"},
            {"name": "MA20", "value": levels.get("ma20"), "note": levels.get("ma20_note", "放量站上=启动确认")},
            {"name": "近期高点", "value": levels.get("recent_high"), "note": "突破确认参考"},
        ],
        "metrics": [
            {"name": "收盘/现价", "value": review.get("close")},
            {"name": "涨跌幅", "value": review.get("change_pct"), "unit": "%"},
            {"name": "大阳量占比", "value": review.get("vol_vs_big_yang_pct"), "unit": "%"},
            {"name": "前日量占比", "value": review.get("vol_vs_prev_pct"), "unit": "%"},
            {"name": "MACD", "value": "金叉" if review.get("macd_golden_cross") else "多头" if review.get("dif", 0) > review.get("dea", 0) else "空头"},
        ],
        "signals": [
            {"name": "结束信号", "value": pattern.get("end_signal")},
            {"name": "失败信号", "value": pattern.get("fail_signal")},
        ],
        "criteria": pattern.get("criteria") or [],
    }]


def build_review(df: pd.DataFrame, monitor: dict, mode: str) -> tuple[str, dict, dict]:
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    levels = dict(monitor.get("key_levels") or {})

    stop = _float(levels.get("stop_loss"))
    support = _float(levels.get("support_78_6"))
    big_yang_vol = _float(levels.get("big_yang_vol"))

    close = _float(last["close"])
    open_ = _float(last["open"])
    low = _float(last["low"])
    high = _float(last["high"])
    vol = _float(last["volume"])
    prev_close = _float(prev["close"])
    prev_vol = _float(prev["volume"])

    vol_vs_big = vol / big_yang_vol * 100 if big_yang_vol else 0
    vol_vs_prev = vol / prev_vol * 100 if prev_vol else 0
    change_pct = (close - prev_close) / prev_close * 100 if prev_close else 0

    ma5, ma10, ma20, ma60 = (_float(last.get(f"ma{n}")) for n in [5, 10, 20, 60])
    dif, dea = _float(last.get("dif")), _float(last.get("dea"))
    macd_cross = bool(last.get("macd_golden_cross"))

    # 阶段与建议：规则化监控，不替代人工/策略决策。
    if stop and low < stop and vol_vs_prev >= 100:
        phase = "洗盘失败风险"
        next_trigger = f"放量跌破止损位{stop:.2f} → 优先风控/止损确认"
    elif ma20 and close > ma20 and vol_vs_prev >= 120:
        phase = "启动确认"
        next_trigger = f"放量站上MA20({ma20:.2f}) → 拉升确认，观察回踩不破"
    elif close >= open_ and close >= ma5 and (macd_cross or dif > dea) and vol_vs_big <= 70:
        phase = "准备启动"
        next_trigger = "缩量止跌转小阳 + MACD改善 → 准备启动，等放量确认"
    elif stop and low >= stop and vol_vs_big <= 75:
        phase = "洗盘进行中"
        next_trigger = f"继续观察缩量止跌；放量跌破{stop:.2f}则洗盘失败"
    else:
        phase = "跟踪中"
        next_trigger = "等待缩量止跌、放量突破或跌破关键位的确认信号"

    support_note = ""
    if support:
        if low <= support * 1.01:
            support_note = f"今日低点{low:.2f}逼近/触及{support:.2f}支撑，需警惕"
        elif low > support:
            support_note = f"今日低点{low:.2f}仍在{support:.2f}支撑上方"
        else:
            support_note = f"今日低点{low:.2f}已低于{support:.2f}支撑"

    summary = (
        f"{phase}：收{close:.2f}，涨跌{change_pct:+.2f}%，量为大阳量{vol_vs_big:.1f}%、前日{vol_vs_prev:.1f}%。"
        f"MA5/10/20={ma5:.2f}/{ma10:.2f}/{ma20:.2f}，MACD {'金叉' if macd_cross else ('多头' if dif > dea else '空头')}。"
        f"{next_trigger}。"
    )

    review = {
        "mode": mode,
        "open": _round(open_, 2),
        "high": _round(high, 2),
        "low": _round(low, 2),
        "close": _round(close, 2),
        "volume": _int(vol),
        "change_pct": _round(change_pct, 2),
        "ma5": _round(ma5, 2),
        "ma10": _round(ma10, 2),
        "ma20": _round(ma20, 2),
        "ma60": _round(ma60, 2),
        "dif": _round(dif, 3),
        "dea": _round(dea, 3),
        "macd_bar": _round(last.get("macd_bar"), 3),
        "macd_golden_cross": macd_cross,
        "k": _round(last.get("k"), 1),
        "d": _round(last.get("d"), 1),
        "j": _round(last.get("j"), 1),
        "vol_vs_big_yang_pct": _round(vol_vs_big, 1),
        "vol_vs_prev_pct": _round(vol_vs_prev, 1),
        "above_ma5": close >= ma5 if ma5 else False,
        "above_ma10": close >= ma10 if ma10 else False,
        "above_ma20": close >= ma20 if ma20 else False,
        "phase": phase,
        "summary": summary,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    levels.update({
        "support_note": support_note or levels.get("support_note", ""),
        "ma5": _round(ma5, 2),
        "ma10": _round(ma10, 2),
        "ma20": _round(ma20, 2),
        "ma60": _round(ma60, 2),
        "ma20_note": "动态MA20，放量站上=启动确认；跌破后缩量震荡=仍需等待",
        "recent_high": _round(df["high"].tail(20).max(), 2),
    })

    pattern = dict(monitor.get("washed_out_pattern") or {})
    pattern.update({
        "criteria": [
            f"量能相对大阳线：{vol_vs_big:.1f}%（越缩越像洗盘，放量下跌需警惕）",
            f"最低价 vs 止损位：{low:.2f} / {stop:.2f} {'✅ 未破' if (not stop or low >= stop) else '⚠️ 已破'}",
            f"价格 vs MA20：{close:.2f} / {ma20:.2f} {'✅ 站上' if ma20 and close >= ma20 else '⏳ 未站上'}",
            f"MACD：DIF {dif:.3f} / DEA {dea:.3f} {'✅ 金叉/多头' if dif > dea else '⏳ 空头'}",
        ],
        "end_signal": "缩量止跌十字星/小阳线，随后放量站上MA20",
        "fail_signal": f"放量跌破{stop:.2f}" if stop else "放量跌破关键止损位",
        "current_phase": phase,
        "next_trigger": next_trigger,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

    return str(last["date"]), review, {"key_levels": levels, "washed_out_pattern": pattern}


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else "601012"
    mode = "auto"
    if "--mode" in sys.argv:
        i = sys.argv.index("--mode")
        if i + 1 < len(sys.argv):
            mode = sys.argv[i + 1]

    path = ROOT / "monitoring" / f"{code}_washout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monitor = load_monitor(path, code)

    fetcher = TencentQuoteFetcher()
    quote = fetch_realtime_quote(code)
    daily = normalize_daily(fetcher.fetch_daily(code))
    if daily.empty:
        raise SystemExit(f"无日K数据，无法更新 {code} 监控")
    daily = merge_realtime(daily, quote)
    daily = add_indicators(daily)

    if quote is not None and not quote.empty:
        monitor["name"] = str(quote.get("名称") or monitor.get("name") or STOCK_NAMES.get(code, ""))
    else:
        monitor["name"] = monitor.get("name") or STOCK_NAMES.get(code, "")

    monitor["key_levels"] = ensure_washout_levels(monitor, daily)

    review_date, review, updates = build_review(daily, monitor, mode)
    monitor["stock"] = code
    monitor.setdefault("label", "洗盘监控")
    monitor["key_levels"] = updates["key_levels"]
    monitor.setdefault("daily_reviews", {})[review_date] = review
    monitor["washed_out_pattern"] = updates["washed_out_pattern"]
    monitor["last_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    monitor["monitors"] = build_monitor_cards(monitor, review)

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(monitor, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)

    print(f"已更新 {path.relative_to(ROOT)}")
    print(f"日期: {review_date} 模式: {mode}")
    print(f"阶段: {review['phase']}")
    print(f"建议: {monitor['washed_out_pattern']['next_trigger']}")
    print(f"摘要: {review['summary']}")


if __name__ == "__main__":
    main()
