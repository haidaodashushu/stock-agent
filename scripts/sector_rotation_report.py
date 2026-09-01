#!/usr/bin/env python3
"""板块轮动报告。

输出：当前强主线、资金潜伏/低位补涨、短线过热，以及候选股轮动加分。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.services.sector_rotation_service import SectorRotationService  # noqa: E402
from engine.screener import filter_tradeable  # noqa: E402
from data.store.sqlite_store import StockStore  # noqa: E402


def _fmt_money(v: float) -> str:
    if abs(v) >= 100_000_000:
        return f"{v/100_000_000:.1f}亿"
    return f"{v/10000:.0f}万"


def main() -> int:
    refresh = "--refresh" in sys.argv
    svc = SectorRotationService()
    signals = svc.get_signals(refresh=refresh)
    print("━━━ 板块轮动报告 ━━━")
    print(f"数据源: 同花顺问财  板块数: {len(signals)}")
    print()

    groups = [
        ("强主线/延续", lambda s: s.stage == "leader"),
        ("资金潜伏/补涨候选", lambda s: s.stage in ("laggard", "accelerating")),
        ("短线过热/谨慎追高", lambda s: s.stage == "overheat"),
    ]
    for title, pred in groups:
        rows = [s for s in signals if pred(s)][:12]
        if not rows:
            continue
        print(f"【{title}】")
        for s in rows:
            print(
                f"  {s.name:<12} score={s.score:>5.2f} "
                f"3m={s.pct_3m:>6.1f}% 1m={s.pct_1m:>6.1f}% 5d={s.pct_5d:>5.1f}% "
                f"资金={_fmt_money(s.fund_inflow)} {'/'.join(s.tags)}"
            )
        print()

    # 当前股票池轮动加分预览
    try:
        store = StockStore()
        stocks = store.get_active_stocks()
        codes = filter_tradeable(stocks["code"].tolist())[:300] if not stocks.empty else []
        boosts = svc.get_stock_boosts(codes)
        top = sorted(boosts.items(), key=lambda kv: kv[1][0], reverse=True)[:20]
        if top:
            print("【股票轮动加分预览 Top20】")
            for code, (score, tags) in top:
                name = ""
                try:
                    row = stocks[stocks["code"] == code]
                    if not row.empty:
                        name = str(row.iloc[0].get("name", ""))
                except Exception:
                    pass
                print(f"  {code} {name:<8} +{score:.2f}  {' | '.join(tags[:2])}")
    except Exception as e:
        print(f"股票轮动加分预览跳过: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
