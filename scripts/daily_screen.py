#!/usr/bin/env python3
"""
scripts/daily_screen.py — 每日盘前选股脚本（v3 数据感知版）

流程：
  1. 检查数据新鲜度，盘前/盘中允许使用上一交易日日K
  2. 通过 DataLoader 加载全市场已缓存日K数据
  3. 调用统一选股引擎 StockScreener
  4. 将量化候选池原子写入 screen_candidate_pool
  5. 最终 screen_records 由独立 AI 选股执行层校验后写入
"""
from __future__ import annotations

import sys
import os
import logging
import argparse
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.store.sqlite_store import StockStore
from data.stock_selection_repository import (
    stage_candidate_pool,
    update_selection_status,
)
from data.loader import DataLoader
from data.fifteen_five_pool import FIFTEEN_FIVE_STOCKS
from engine.screener import (
    DEFAULT_ENRICHMENT_POOL_SIZE,
    StockScreener,
    filter_tradeable,
)
from data.market_calendar import ensure_market_open, market_day

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 初始化统一引擎
# ---------------------------------------------------------------------------

def _build_engine() -> StockScreener:
    """构建统一综合评分引擎。"""
    return StockScreener()


def _previous_trading_day(value: datetime) -> str:
    """返回给定日期之前的最近一个交易日。"""
    d = value.date() - timedelta(days=1)
    while not market_day(d).is_open:
        d -= timedelta(days=1)
    return d.isoformat()


def _next_trading_day(value: datetime) -> str:
    """返回给定日期之后的最近一个交易日。"""
    d = value.date() + timedelta(days=1)
    while not market_day(d).is_open:
        d += timedelta(days=1)
    return d.isoformat()


def _expected_daily_date(now: datetime | None = None) -> str:
    """盘前选股使用已完整落库的最近交易日日K。"""
    now = now or datetime.now()
    today = now.date().isoformat()
    if market_day(today).is_open and now.hour >= 18:
        return today
    return _previous_trading_day(now)


def check_data_freshness(now: datetime | None = None) -> bool:
    """检查日K数据是否覆盖到本次选股所需的最近完整交易日。"""
    import subprocess
    expected = _expected_daily_date(now)
    store = StockStore()
    conn = store._get_conn()
    try:
        latest = conn.execute("SELECT MAX(date) FROM daily_prices").fetchone()[0]
        if latest and latest >= expected:
            logger.info(f"✅ 日K数据已覆盖至 {latest}，满足选股所需 {expected}")
            return True

        logger.warning(f"⚠️ 日K数据最新 {latest}，缺少所需交易日 ({expected})，尝试更新...")

        # 调用 nightly_update 更新数据
        try:
            result = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "nightly_update.py")],
                capture_output=True, text=True, timeout=300, cwd=os.path.join(os.path.dirname(__file__), "..")
            )
            if result.returncode != 0:
                logger.error(f"数据更新脚本返回非0: {result.stderr[-200:]}")
            # Re-check
            conn2 = store._get_conn()
            latest2 = conn2.execute("SELECT MAX(date) FROM daily_prices").fetchone()[0]
            conn2.close()
            if latest2 and latest2 >= expected:
                logger.info(f"✅ 更新成功，数据已覆盖至 {latest2}")
                return True
            logger.error(f"❌ 更新后仍缺少所需交易日 {expected} (最新 {latest2})")
        except Exception as e:
            logger.error(f"数据更新失败: {e}")

        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _target_run_date(now: datetime, target: str) -> str:
    if target == "next-trading-day":
        return _next_trading_day(now)
    if target == "latest-open-day":
        if market_day(now.date()).is_open:
            return now.date().isoformat()
        return _next_trading_day(now)
    return now.date().isoformat()


def _merge_extra(raw: str, updates: dict) -> str:
    payload = dict(raw) if isinstance(raw, dict) else {}
    if raw and not isinstance(raw, dict):
        try:
            parsed = json.loads(str(raw))
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {"raw": str(raw)}
    payload.update(updates)
    return json.dumps(payload, ensure_ascii=False)


def _parse_extra(raw: str) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _build_screening_report(records: list[dict], run_date: str, run_label: str, target: str) -> dict:
    rows = []
    tech_count = 0
    buy_count = 0
    watch_count = 0
    mainline_counts: dict[str, int] = {}
    strong_logic = 0
    logic_covered = 0
    fundamental_covered = 0
    ai_selected = 0

    for raw in records[:10]:
        code = str(raw.get("code", "")).zfill(6)
        extra = _parse_extra(raw.get("extra", ""))
        concepts = extra.get("concepts") or FIFTEEN_FIVE_STOCKS.get(code, {}).get("concepts", [])
        if not isinstance(concepts, list):
            concepts = []
        logic = extra.get("logic_change") if isinstance(extra.get("logic_change"), dict) else {}
        ai_selection = extra.get("ai_selection") if isinstance(extra.get("ai_selection"), dict) else {}
        if ai_selection:
            ai_selected += 1
        logic_level = str(logic.get("level") or "")
        if logic_level == "strong":
            strong_logic += 1
        if raw.get("logic_available"):
            logic_covered += 1
        if raw.get("fundamental_available"):
            fundamental_covered += 1
        if code in FIFTEEN_FIVE_STOCKS:
            tech_count += 1
        signal_type = str(raw.get("signal_type") or "")
        if signal_type == "buy":
            buy_count += 1
        elif signal_type == "watch":
            watch_count += 1
        # The selector emits ``signal_tags``.  ``strategies`` is retained as a
        # compatibility fallback for reports rebuilt from persisted rows.
        tags = str(raw.get("signal_tags") or raw.get("strategies") or "")
        tag_items = [x for x in tags.split("|") if x]
        for label in tag_items:
            if "主线" in label:
                mainline_counts[label] = mainline_counts.get(label, 0) + 1
        rows.append(
            {
                "code": code,
                "name": raw.get("name", ""),
                "score": round(float(raw.get("final_score", raw.get("score")) or 0), 1),
                "base_score": round(float(raw.get("base_score", raw.get("score")) or 0), 1),
                "enrichment_score": round(float(raw.get("enrichment_score") or 0), 1),
                "signal_type": signal_type,
                "trend": raw.get("trend", ""),
                "pct_change": round(float(raw.get("pct_change") or 0), 2),
                "vol_ratio": round(float(raw.get("vol_ratio") or 0), 2),
                "tags": tag_items[:5],
                "concepts": concepts[:3],
                "logic_level": logic_level,
                "logic_reasons": (logic.get("reasons") or [])[:2] if isinstance(logic.get("reasons"), list) else [],
                "ai_rank": ai_selection.get("rank"),
                "ai_confidence": ai_selection.get("confidence", ""),
                "ai_reason": ai_selection.get("reason", ""),
                "ai_risk": ai_selection.get("risk", ""),
            }
        )

    top = rows[0] if rows else {}
    mainlines = sorted(mainline_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    mainline_text = "、".join(f"{name}({count})" for name, count in mainlines) or "无明显集中主线"
    method = "AI 综合证据最终选择" if ai_selected else "量化规则选择"
    summary = (
        f"{run_label}由{method}生成 {len(rows)} 只候选，buy {buy_count} / watch {watch_count}；"
        f"科技主线 {tech_count} 只，强逻辑变化 {strong_logic} 只；"
        f"逻辑证据 {logic_covered} 只，基本面数据 {fundamental_covered} 只。"
    )
    if top:
        summary += f" TOP1 {top.get('code')} {top.get('name')}，评分 {top.get('score')}。"

    return {
        "profile": "screening_report",
        "title": f"{run_label} {run_date}",
        "tone": "success" if rows else "warning",
        "summary": summary,
        "run": {
            "label": run_label,
            "target": target,
            "run_date": run_date,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(rows),
            "buy_count": buy_count,
            "watch_count": watch_count,
            "tech_count": tech_count,
            "strong_logic_count": strong_logic,
            "logic_covered": logic_covered,
            "fundamental_covered": fundamental_covered,
            "mainlines": [{"name": name, "count": count} for name, count in mainlines],
            "mainline_text": mainline_text,
            "selection_method": "ai" if ai_selected else "quantitative",
        },
        "candidates": rows,
        "warnings": [
            "脚本先生成量化候选池，AI读取完整数据库证据后完成最终预选；预选结果不等于交易指令"
        ],
        "source": "TechnicalScoringSelector + stock-selection MCP + agent final selection",
    }


def _save_screen_results(
    store: StockStore,
    result,
    *,
    run_date: str,
    run_time: str,
    run_label: str,
    target: str,
    generated_at: str,
    ai_selections: dict[str, dict] | None = None,
    expected_as_of: str | None = None,
) -> int:
    """Atomically replace current candidates and append this run to history."""
    columns = """
        (run_date, run_time, code, name, price, score, signal_type,
         strategies, concepts, trend, pct_change, vol_ratio, extra)
    """
    placeholders = "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    current_sql = f"INSERT OR REPLACE INTO screen_records {columns} {placeholders}"
    history_sql = f"INSERT OR REPLACE INTO screen_record_history {columns} {placeholders}"

    conn = store._get_conn()
    saved = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        if expected_as_of:
            state = conn.execute(
                "SELECT as_of,status FROM screen_candidate_state WHERE slot=1"
            ).fetchone()
            if not state or str(state["as_of"]) != expected_as_of:
                raise ValueError("AI selection candidate snapshot changed before final write")
            if str(state["status"]) != "ready":
                raise ValueError(f"AI selection snapshot is not ready: {state['status']}")
        conn.execute("DELETE FROM screen_records WHERE run_date=?", (run_date,))
        iterator = result.iterrows() if hasattr(result, "iterrows") else enumerate(result)
        for _, row in iterator:
            if row.get("signal_type") not in ("buy", "watch"):
                continue
            code = str(row["code"]).zfill(6)
            row_extra = _parse_extra(row.get("extra", ""))
            sector_context = row.get("sector_context")
            if not isinstance(sector_context, dict):
                sector_context = {}
            concepts = sector_context.get("concepts") or row_extra.get("concepts") or []
            if not isinstance(concepts, list):
                concepts = []
            extra_updates = {
                "run_label": run_label,
                "target": target,
                "generated_at": generated_at,
                "sector_context": sector_context,
                "selector": {
                    "base_score": float(row.get("base_score", row.get("score", 0))),
                    "enrichment_score": float(row.get("enrichment_score", 0)),
                    "theme_bonus": float(row.get("theme_bonus", 0)),
                    "logic_score": float(row.get("logic_score", 0)),
                    "fundamental_score": float(row.get("fundamental_score", 0)),
                    "fund_flow_score": float(row.get("fund_flow_score", 0)),
                    "sector_rotation_score": float(row.get("sector_rotation_score", 0)),
                    "sector_rotation_tags": row.get("sector_rotation_tags", ""),
                    "corporate_action_penalty": float(row.get("corporate_action_penalty", 0)),
                    "theme_concentration_penalty": float(row.get("theme_concentration_penalty", 0)),
                    "logic_available": bool(row.get("logic_available")),
                    "fundamental_available": bool(row.get("fundamental_available")),
                    "zone": row.get("zone"),
                    "entry_route": row.get("entry_route"),
                    "setup_stage": row.get("setup_stage"),
                    "setup_score": row.get("setup_score"),
                    "setup_triggers": row.get("setup_triggers", ""),
                    "setup_risks": row.get("setup_risks", ""),
                    "entry_metrics": row.get("entry_metrics") or {},
                    "buy_eligible": bool(row.get("buy_eligible")),
                    "risk_tags": row.get("risk_tags", ""),
                    "position_pct": row.get("position_pct"),
                    "theme_group": row.get("theme_group", ""),
                },
            }
            if ai_selections and code in ai_selections:
                extra_updates["ai_selection"] = ai_selections[code]
            extra = _merge_extra(row.get("extra", ""), extra_updates)
            values = (
                run_date,
                run_time,
                code,
                row.get("name", ""),
                float(row["price"]),
                float(row["final_score"]),
                row["signal_type"],
                row.get("signal_tags", ""),
                json.dumps(concepts, ensure_ascii=False),
                row.get("trend", ""),
                float(row.get("pct_change", 0)),
                float(row.get("vol_ratio", 0)),
                extra,
            )
            conn.execute(current_sql, values)
            conn.execute(history_sql, values)
            saved += 1
        if expected_as_of:
            update_selection_status(
                expected_as_of,
                status="selected",
                selected_count=saved,
                store=store,
                conn=conn,
            )
        conn.commit()
        return saved
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _selection_market_context() -> dict:
    """Capture the already-refreshed sector snapshot into the DB selection version."""
    try:
        from data.services.sector_rotation_service import SectorRotationService

        snapshot = SectorRotationService().get_snapshot(refresh=False)
        signals = snapshot.get("signals") if isinstance(snapshot.get("signals"), list) else []
        return {
            "sector_rotation_as_of": snapshot.get("created_at"),
            "sector_rotation_source": snapshot.get("source"),
            "leading_sectors": signals[:12],
        }
    except Exception as exc:
        logger.warning("板块背景快照不可用: %s", exc)
        return {"leading_sectors": [], "warning": "sector rotation unavailable"}


def run_screen(target: str = "today", run_label: str = "盘前选股") -> tuple[list, dict]:
    """Generate and stage the quantitative pool for the independent AI selector."""
    # ---- 数据新鲜度检查 ----
    if not check_data_freshness():
        logger.error("❌ 数据未更新，选股结果可能不准确")
        # 继续使用旧数据选股（盘前数据），但标记

    screener = _build_engine()
    store = StockStore()
    loader = DataLoader()

    now = datetime.now()
    run_date = _target_run_date(now, target)
    run_time = now.strftime("%H:%M:%S")

    logger.info(f"📊 {run_label}开始 — target={run_date} run_at={now.strftime('%Y-%m-%d')} {run_time}")

    # ---- 收集代码 ----
    # 只扫描最近完整交易日仍有日K的活跃股票，避免退市/吸收合并等旧行情残留
    # 继续参与当日 screen_records。
    expected_daily = _expected_daily_date(now)
    conn = store._get_conn()
    try:
        total_codes = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM daily_prices"
        ).fetchone()[0]
        fresh_rows = conn.execute(
            """SELECT dp.code, MAX(dp.date) AS latest_date
               FROM daily_prices dp
               JOIN stocks s ON s.code=dp.code AND s.is_active=1
               GROUP BY dp.code
               HAVING latest_date >= ?""",
            (expected_daily,),
        ).fetchall()
        all_codes = [r[0] for r in fresh_rows]
    finally:
        conn.close()
    all_codes = filter_tradeable(all_codes)
    logger.info(
        f"扫描全市场 ({len(all_codes)} 只可交易股票，日K>={expected_daily}；"
        f"旧行情/非活跃过滤 {max(int(total_codes) - len(fresh_rows), 0)} 只)..."
    )

    # ---- 两阶段量化筛选：生成供 AI 综合判断的完整候选池 ----
    result = screener.screen(
        codes=all_codes,
        date=expected_daily,
        top_n=DEFAULT_ENRICHMENT_POOL_SIZE,
        enrichment_pool_size=DEFAULT_ENRICHMENT_POOL_SIZE,
        refresh_intelligence=True,
    )
    if result.empty:
        logger.warning("选股引擎无结果")
        raise RuntimeError("量化候选池为空，无法进入 AI 最终选股")

    # ---- 名称兜底：部分策略结果可能未带 name，用 stocks 表补齐 ----
    conn = store._get_conn()
    try:
        name_map = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT code, name FROM stocks WHERE name IS NOT NULL AND name != ''"
            ).fetchall()
        }
    finally:
        conn.close()
    if "name" not in result.columns:
        result["name"] = ""
    result["name"] = result.apply(
        lambda r: r.get("name") or name_map.get(r["code"], ""), axis=1
    )

    # ---- 原子写入 AI 只读候选快照；不在这里改 screen_records ----
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S.%f")
    records = result.to_dict("records")
    stage = stage_candidate_pool(
        records,
        run_date=run_date,
        run_time=run_time,
        run_label=run_label,
        target=target,
        expected_daily_date=expected_daily,
        generated_at=generated_at,
        market_context=_selection_market_context(),
        store=store,
    )

    # ---- 统计 ----
    buys = result[result["signal_type"] == "buy"]
    watches = result[result["signal_type"] == "watch"]
    tech_buys = buys[buys["code"].isin(FIFTEEN_FIVE_STOCKS)]

    logger.info(
        f"✅ {run_label}量化候选池完成，目标交易日 {run_date}，"
        f"暂存 {len(result)} 条，等待 AI 最终选择"
    )
    logger.info(
        f"   🏆 强烈关注(buy): {len(buys)} 只 (含科技 {len(tech_buys)})"
    )
    logger.info(f"   👀 观察(watch):  {len(watches)} 只")

    # ---- 格式化输出 ----
    _print_results(result, FIFTEEN_FIVE_STOCKS, title=f"{run_label} 量化候选预览 ({run_date})")

    return records, stage


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _print_results(result, tech_pool, title: str = ""):
    n = min(10, len(result))
    print()
    print("=" * 100)
    print(f"🏆 {title or f'盘前选股 TOP {n}'}")
    print("=" * 100)
    print(
        f"{'评分':>5} {'终分':>5} {'代码':<8} {'名称':<10} "
        f"{'现价':<8} {'涨跌':<7} {'趋势':<6} {'量比':<6} {'信号'}"
    )
    print("-" * 100)

    for _, r in result.head(n).iterrows():
        icon = {
            "多头": "🟢", "偏多": "🟡", "偏空": "🟠", "空头": "🔴",
        }.get(r.get("trend", ""), "")
        flag = "✨" if r["code"] in tech_pool else "  "
        tags = r.get("signal_tags", "")
        print(
            f"{flag}{float(r['score']):>5.1f} {float(r.get('final_score', r['score'])):>5.1f} "
            f"{r['code']:<8} {r.get('name', ''):<10} "
            f"{float(r['price']):<8.2f} {float(r.get('pct_change', 0)):<+7.2f} "
            f"{icon + r.get('trend', ''):<6} "
            f"{float(r.get('vol_ratio', 0)):<6} {tags}"
        )

    # ---- 十五五科技方向 ----
    tech_all = result[
        result["code"].isin(tech_pool) & (result["signal_type"] != "pass")
    ]
    if not tech_all.empty:
        print()
        print(f"🌟 十五五科技方向入选 ({len(tech_all)} 只):")
        for _, r in tech_all.head(15).iterrows():
            info = tech_pool.get(r["code"], {})
            cons = ", ".join(info.get("concepts", [])[:3])
            print(
                f"  ✨ {r['code']} {r.get('name', ''):<10s} "
                f"评分{float(r['score']):.1f}  "
                f"{r.get('trend', '')}  {r.get('signal_tags', '')}"
            )
            print(f"     概念: {cons}")

    print()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每日/夜间量化候选池准备")
    parser.add_argument(
        "--target",
        choices=("today", "next-trading-day", "latest-open-day"),
        default="today",
        help="AI 最终预选股对应的目标交易日",
    )
    parser.add_argument("--label", default="盘前选股", help="本轮选股运行标签")
    parser.add_argument("--json-output", help="写入 AI 候选快照元数据 JSON")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if not ensure_market_open(task=args.label):
        raise SystemExit(0)
    _, report = run_screen(target=args.target, run_label=args.label)
    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write("\n")
