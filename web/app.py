"""
web/app.py - A股量化交易系统 Web 面板 v2
FastAPI + Vue.js + Chart.js
"""
import asyncio
import os, sys, json
import subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.templating import _TemplateResponse
import jinja2

from account.trader import SimTrader
from engine.screener import StockScreener
from data.loader import DataLoader
from data.store.sqlite_store import StockStore
from data.selection_report import latest_selection_report
from data.candidate_lifecycle import (
    load_lifecycle_snapshot,
    overlay_latest_candidate_quotes,
)
from data.fifteen_five_pool import FIFTEEN_FIVE_STOCKS
from account.reconcile import reconcile
from data.watchlist_config import load_config as load_watchlist_config, upsert_item as upsert_watch_item, delete_item as delete_watch_item
from data.live_manual_account import (
    account_snapshot as live_account_snapshot,
    expire_stale_proposed_intents,
)
from data.services.financial_analysis_service import FinancialAnalysisService, normalize_codes
from config.runtime_paths import configurable_path
from data.wecom_client import WeComClient, WeComCrypto, load_wecom_settings
from data.wecom_inbound import handle_wecom_message, parse_wecom_xml

logger = logging.getLogger(__name__)

SCREEN_TASK = {
    "process": None,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "log_path": None,
}

os.makedirs(os.path.join(os.path.dirname(__file__), "templates"), exist_ok=True)

# Jinja2 – single template for the SPA
_template_dir = os.path.join(os.path.dirname(__file__), "templates")
_jinja_env = jinja2.Environment(loader=jinja2.FileSystemLoader(_template_dir), autoescape=True, cache_size=0)

def render(name: str, context: dict) -> _TemplateResponse:
    tpl = _jinja_env.get_template(name)
    return _TemplateResponse(tpl, context)

app = FastAPI(title="A股交易系统 v2", version="2.0")

data_loader = DataLoader()

def _trader():
    return SimTrader()

def _screener():
    return StockScreener()

def _store():
    return StockStore()


def _fetch_realtime_quotes(codes: list[str]) -> dict:
    """批量拉取腾讯实时行情；失败时返回已成功的部分。"""
    if not codes:
        return {}
    import urllib.request

    quotes = {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for i in range(0, len(codes), 80):
        batch = codes[i:i + 80]
        symbols = []
        for code in batch:
            c = str(code).zfill(6)
            prefix = "sh" if c.startswith("6") else "sz"
            symbols.append(f"{prefix}{c}")
        url = f"http://qt.gtimg.cn/q={','.join(symbols)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=5)
            data = resp.read().decode("gbk", errors="ignore")
        except Exception as e:
            logger.warning("实时行情拉取失败: %s", e)
            continue

        for line in data.strip().split("\n"):
            if '="' not in line or "~" not in line:
                continue
            fields = line.split('="', 1)[1].rstrip('";').split("~")
            if len(fields) < 40:
                continue
            code = fields[2].strip().zfill(6)
            try:
                price = float(fields[3]) if fields[3] else 0.0
            except Exception:
                price = 0.0
            if price <= 0:
                continue
            try:
                prev_close = float(fields[4]) if fields[4] else 0.0
            except Exception:
                prev_close = 0.0
            day_change = round(price - prev_close, 2) if prev_close > 0 else 0.0
            day_change_pct = round(day_change / prev_close * 100, 2) if prev_close > 0 else 0.0
            quotes[code] = {
                "code": code,
                "name": fields[1].strip(),
                "price": price,
                "prev_close": prev_close,
                "day_change": day_change,
                "day_change_pct": day_change_pct,
                "datetime": now,
                "source": "tencent",
            }
    return quotes


def _refresh_trader_realtime(trader: SimTrader) -> dict:
    """用实时行情更新本次响应里的持仓市价，不在读接口里写库。"""
    codes = [p.code for p in trader.portfolio.positions]
    quotes = _fetch_realtime_quotes(codes)
    updated = []
    for p in trader.portfolio.positions:
        q = quotes.get(p.code)
        if not q:
            continue
        if q["name"] and not p.name:
            p.name = q["name"]
        p.update_market(q["price"])
        p.quote_source = q.get("source", "")
        p.quote_time = q.get("datetime", "")
        p.day_change = q.get("day_change", 0.0)
        p.day_change_pct = q.get("day_change_pct", 0.0)
        updated.append(p.code)
    if updated:
        trader.portfolio._recalc()
    return {
        "mode": "realtime" if updated else "snapshot",
        "updated": len(updated),
        "failed": [c for c in codes if c not in updated],
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# ============================================================
# 页面
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    """直接返回静态 HTML，不经 Jinja2 渲染（避免与 Vue 分隔符冲突）"""
    html_path = os.path.join(os.path.dirname(__file__), "templates", "app.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/reports/night-selection/latest", response_class=HTMLResponse)
async def latest_night_selection_report(request: Request):
    """Mobile-friendly rich report linked from WeCom cards."""
    report = latest_selection_report(_store())
    return render("selection_report.html", {"request": request, "report": report})


def _wecom_runtime():
    path = configurable_path("STOCK_RUNTIME_CONFIG", "config/runtime.local.json")
    settings = load_wecom_settings(path)
    return settings, WeComCrypto(
        settings.token, settings.encoding_aes_key, settings.corp_id,
    )


@app.get("/api/wecom/callback", response_class=PlainTextResponse)
async def verify_wecom_callback(
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
):
    """Complete WeCom's one-time callback URL verification."""
    settings, crypto = _wecom_runtime()
    if not settings.callback_enabled:
        raise HTTPException(status_code=410, detail="legacy WeCom callback is disabled")
    try:
        crypto.verify(msg_signature, timestamp, nonce, echostr)
        return PlainTextResponse(crypto.decrypt(echostr))
    except Exception as exc:
        logger.warning("WeCom callback verification failed: %s", exc)
        raise HTTPException(status_code=403, detail="invalid callback") from exc


@app.post("/api/wecom/callback", response_class=PlainTextResponse)
async def receive_wecom_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    msg_signature: str,
    timestamp: str,
    nonce: str,
):
    """Authenticate an encrypted callback and process it after acknowledging."""
    body = await request.body()
    if not body or len(body) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="invalid callback body")
    settings, crypto = _wecom_runtime()
    if not settings.callback_enabled:
        raise HTTPException(status_code=410, detail="legacy WeCom callback is disabled")
    try:
        envelope = parse_wecom_xml(body.decode("utf-8"))
        encrypted = str(envelope.get("Encrypt") or "")
        crypto.verify(msg_signature, timestamp, nonce, encrypted)
        event = parse_wecom_xml(crypto.decrypt(encrypted))
    except Exception as exc:
        logger.warning("WeCom callback rejected: %s", exc)
        raise HTTPException(status_code=403, detail="invalid callback") from exc
    background_tasks.add_task(
        handle_wecom_message,
        event,
        settings=settings,
        client=WeComClient(settings),
    )
    return PlainTextResponse("success")

# ============================================================
# API: 账户 & 持仓
# ============================================================

@app.get("/api/account")
async def api_account():
    """账户概览 + 持仓详情 + 最近交易；持仓市价按请求实时刷新。"""
    trader = _trader()
    quote_meta = _refresh_trader_realtime(trader)
    summary = trader.portfolio.summary()
    positions = []
    total_mv = sum(p.market_value for p in trader.portfolio.positions)
    for p in trader.portfolio.positions:
        d = p.to_dict()
        d["quote_source"] = getattr(p, "quote_source", "")
        d["quote_time"] = getattr(p, "quote_time", p.updated_at)
        d["day_change"] = getattr(p, "day_change", 0.0)
        d["day_change_pct"] = getattr(p, "day_change_pct", 0.0)
        d["fifteen_five"] = p.code in FIFTEEN_FIVE_STOCKS
        d["concepts"] = FIFTEEN_FIVE_STOCKS.get(p.code, {}).get("concepts", [])[:3] if p.code in FIFTEEN_FIVE_STOCKS else []
        d["allocation"] = round(p.market_value / total_mv * 100, 1) if total_mv > 0 else 0
        positions.append(d)

    # 最新优先（portfolio.orders已经是倒序加载）
    raw_orders = [o.to_dict() for o in trader.portfolio.orders]
    orders = raw_orders[:50]

    return JSONResponse({
        "summary": summary,
        "positions": positions,
        "orders": orders,
        "quote": quote_meta,
    })

# ============================================================
# API: 交易记录
# ============================================================

@app.get("/api/orders")
async def api_orders(date: str = "", limit: int = 100, all: bool = False):
    """按日期筛选交易记录"""
    store = _store()
    conn = store._get_conn()
    try:
        dates_rows = conn.execute(
            """SELECT DISTINCT date(created_at) AS d
               FROM orders
               WHERE created_at IS NOT NULL AND created_at != ''
               ORDER BY d DESC LIMIT 60"""
        ).fetchall()
        dates = [r[0] for r in dates_rows if r[0]]

        if all:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        elif date:
            rows = conn.execute(
                "SELECT * FROM orders WHERE date(created_at)=? ORDER BY id DESC LIMIT ?",
                (date, limit)
            ).fetchall()
        else:
            # 默认展示最近有交易的日期，而不是空筛选/自然日今天。
            latest_date = dates[0] if dates else ""
            rows = conn.execute(
                "SELECT * FROM orders WHERE date(created_at)=? ORDER BY id DESC LIMIT ?",
                (latest_date, limit)
            ).fetchall() if latest_date else []
        # Normalize: use 'action' not 'direction' for frontend consistency
        result = []
        for r in rows:
            d = dict(r)
            if 'direction' in d:
                d['action'] = d.pop('direction')
            result.append(d)
        return JSONResponse({
            "orders": result,
            "total": len(rows),
            "dates": dates,
            "date": "" if all else (date or (dates[0] if dates else "")),
        })
    finally:
        conn.close()

@app.get("/api/orders/summary")
async def api_orders_summary():
    """按日汇总交易统计"""
    store = _store()
    conn = store._get_conn()
    try:
        rows = conn.execute(
            """SELECT date(created_at) as d,
                      SUM(CASE WHEN direction='buy' THEN amount ELSE 0 END) as buy_total,
                      SUM(CASE WHEN direction='sell' THEN amount ELSE 0 END) as sell_total,
                      COUNT(*) as cnt
               FROM orders WHERE direction != ''
               GROUP BY d ORDER BY d DESC LIMIT 30"""
        ).fetchall()
        return JSONResponse({"daily": [dict(r) for r in rows[::-1]]})
    finally:
        conn.close()

@app.get("/api/account/reconcile")
async def api_account_reconcile():
    """从订单账本重算账户，并返回一致性/风控异常。"""
    result = reconcile(_store(), apply=False)
    return JSONResponse({
        "order_count": result.order_count,
        "buy_total": result.buy_total,
        "sell_total": result.sell_total,
        "cash": result.cash,
        "market_value": result.market_value,
        "total_equity": result.total_equity,
        "total_profit": result.total_profit,
        "position_count": len(result.positions),
        "issues": [i.__dict__ for i in result.issues],
    })

# ============================================================
# API: 选股
# ============================================================

@app.get("/api/screen")
async def api_screen(date: str = ""):
    """获取选股记录"""
    store = _store()
    conn = store._get_conn()
    try:
        dates_rows = conn.execute(
            "SELECT DISTINCT run_date FROM screen_records ORDER BY run_date DESC LIMIT 20"
        ).fetchall()
        dates = [r[0] for r in dates_rows]

        if date:
            rows = conn.execute(
                "SELECT * FROM screen_records WHERE run_date=? ORDER BY score DESC",
                (date,)
            ).fetchall()
            return JSONResponse({"records": [dict(r) for r in rows], "dates": dates, "date": date, "total": len(rows)})

        # “最新”不是自然日今天，而是数据库里最近一次选股日期。
        # 否则 0 点后刷新会去查新日期，导致昨晚刚跑出的结果看起来消失。
        latest_date = dates[0] if dates else datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM screen_records WHERE run_date=? ORDER BY score DESC LIMIT 10",
            (latest_date,)
        ).fetchall()
        return JSONResponse({
            "records": [dict(r) for r in rows],
            "dates": dates,
            "date": latest_date,
            "total": len(rows),
        })
    finally:
        conn.close()


@app.get("/api/candidate-lifecycle")
async def api_candidate_lifecycle(limit: int = 100):
    """正式候选池与固定战略观察池。"""
    payload = load_lifecycle_snapshot(_store(), limit=max(1, min(limit, 200)))
    visible_codes = list(dict.fromkeys(
        str(row.get("code") or "").zfill(6)
        for row in [
            *(payload.get("candidates") or []),
            *(payload.get("observations") or []),
        ]
    ))
    overlay_latest_candidate_quotes(payload, _fetch_realtime_quotes(visible_codes))
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})

@app.post("/api/screen/run")
async def api_run_screen():
    """后台运行完整的脚本候选池 + Codex Agent 最终选股流水线。

    Web 手动“重新选股”和 cron 保持同一条流水线：stock_agent_selection_cycle.sh。
    脚本负责数据初筛，AI 通过只读数据库工具最终选择，执行层校验后写库。
    注意：全市场扫描耗时较长，这里只启动后台任务，前端通过 /api/screen/status 轮询。
    """
    try:
        proc = SCREEN_TASK.get("process")
        if proc is not None and proc.poll() is None:
            return JSONResponse({
                "ok": True,
                "running": True,
                "message": "选股任务已在运行中",
                "started_at": SCREEN_TASK.get("started_at"),
            })

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        script = os.path.join(root, "scripts", "stock_agent_selection_cycle.sh")
        os.makedirs(os.path.join(root, "logs"), exist_ok=True)
        log_path = os.path.join(root, "logs", "daily_screen_web_latest.log")
        log_f = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [script],
            cwd=root,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log_f.close()
        SCREEN_TASK.update({
            "process": proc,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "returncode": None,
            "log_path": log_path,
        })
        return JSONResponse({
            "ok": True,
            "running": True,
            "message": "选股任务已开始，完成后会自动刷新结果",
            "started_at": SCREEN_TASK["started_at"],
        })
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"选股启动失败: {e}", "records": []}, status_code=500)


@app.get("/api/screen/status")
async def api_screen_status():
    """查询 Web 触发的选股任务状态。"""
    proc = SCREEN_TASK.get("process")
    if proc is None:
        return JSONResponse({"running": False, "message": "暂无选股任务"})

    returncode = proc.poll()
    running = returncode is None
    if not running and SCREEN_TASK.get("finished_at") is None:
        SCREEN_TASK["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        SCREEN_TASK["returncode"] = returncode

    output = ""
    log_path = SCREEN_TASK.get("log_path")
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                output = f.read()[-4000:]
        except Exception:
            output = ""

    records = []
    if not running and returncode == 0:
        store = _store()
        conn = store._get_conn()
        try:
            latest_row = conn.execute(
                "SELECT run_date FROM screen_records ORDER BY run_date DESC LIMIT 1"
            ).fetchone()
            latest_date = latest_row[0] if latest_row else datetime.now().strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT * FROM screen_records WHERE run_date=? ORDER BY score DESC LIMIT 10",
                (latest_date,),
            ).fetchall()
            records = [dict(r) for r in rows]
        finally:
            conn.close()

    ok = running or returncode == 0
    message = "选股运行中" if running else (f"选股完成，共 {len(records)} 只" if returncode == 0 else f"选股失败，退出码 {returncode}")
    return JSONResponse({
        "ok": ok,
        "running": running,
        "returncode": returncode,
        "started_at": SCREEN_TASK.get("started_at"),
        "finished_at": SCREEN_TASK.get("finished_at"),
        "records": records,
        "total": len(records),
        "message": message,
        "output": output,
    }, status_code=200 if ok else 500)

# ============================================================
# API: 数据新鲜度
# ============================================================

@app.get("/api/data-freshness")
async def api_data_freshness():
    """检查数据的最后更新时间"""
    store = _store()
    conn = store._get_conn()
    try:
        latest_price = conn.execute(
            "SELECT MAX(date) FROM daily_prices"
        ).fetchone()[0]
        stock_count = conn.execute("SELECT COUNT(*) FROM stocks WHERE is_active=1").fetchone()[0]
        screen_today = conn.execute(
            "SELECT COUNT(*) FROM screen_records WHERE run_date=date('now','localtime')"
        ).fetchone()[0]
        return JSONResponse({
            "latest_kline_date": latest_price,
            "active_stocks": stock_count,
            "screen_today": screen_today,
            "fresh": latest_price == datetime.now().strftime("%Y-%m-%d"),
        })
    finally:
        conn.close()

# ============================================================
# API: K线数据（用于图表）
# ============================================================

@app.get("/api/kline/{code}")
async def api_kline(code: str, days: int = 60):
    """获取股票K线数据"""
    df = data_loader.get_daily(code)
    if df is None or df.empty:
        raise HTTPException(404, f"无数据: {code}")

    df = df.tail(days)
    candles = []
    for _, row in df.iterrows():
        candles.append({
            "date": str(row.name)[:10] if hasattr(row, 'name') else row.get('date', ''),
            "open": float(row.get('open', 0)),
            "close": float(row.get('close', 0)),
            "high": float(row.get('high', 0)),
            "low": float(row.get('low', 0)),
            "volume": int(row.get('volume', 0)),
        })
    return JSONResponse({"code": code, "candles": candles})

# ============================================================
# API: 自选监控（隆基等）
# ============================================================

@app.get("/api/watchlist")
async def api_watchlist():
    """获取自选监控列表"""
    import urllib.request

    watch_cfg = load_watchlist_config()
    watch_items = [x for x in watch_cfg.get("items", []) if x.get("enabled", True)]
    watch_codes = [str(x.get("code", "")).zfill(6) for x in watch_items if x.get("code")]
    item_map = {str(x.get("code", "")).zfill(6): x for x in watch_items}
    # Add FIFTEEN_FIVE top picks that aren't in current portfolio
    trader = _trader()
    held_codes = {p.code for p in trader.portfolio.positions}

    # Get real-time quotes
    symbols = []
    for c in watch_codes:
        prefix = 'sh' if c.startswith('6') else 'sz'
        symbols.append(f'{prefix}{c}')

    results = []
    if symbols:
        url = f'http://qt.gtimg.cn/q={",".join(symbols)}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, timeout=10)
            data = resp.read().decode('gbk', errors='ignore')
            for line in data.strip().split('\n'):
                if '="' not in line:
                    continue
                content = line.split('="', 1)[1].rstrip('";')
                fields = content.split('~')
                if len(fields) < 40:
                    continue
                code = fields[2].strip()
                price = float(fields[3]) if fields[3] else 0
                prev = float(fields[4]) if fields[4] else 0
                chg = (price - prev) / prev * 100 if prev else 0

                # Load monitoring data if available
                import json as _json
                mon_path = f"monitoring/{code}_washout.json"
                mon_data = {}
                if os.path.exists(mon_path):
                    try:
                        mon_data = _json.load(open(mon_path))
                    except: pass

                results.append({
                    "code": code,
                    "name": fields[1],
                    "price": price,
                    "prev_close": prev,
                    "chg_pct": round(chg, 2),
                    "volume": int(fields[6]) if fields[6] else 0,
                    "held": code in held_codes,
                    "monitoring": mon_data,
                    "watch_config": item_map.get(code, {}),
                })
        except Exception as e:
            logger.warning(f"Watchlist quote error: {e}")

    return JSONResponse({"stocks": results, "config": watch_cfg})


@app.get("/api/watchlist/config")
async def api_watchlist_config():
    """获取自选监控配置。"""
    return JSONResponse(load_watchlist_config())


@app.post("/api/watchlist")
async def api_watchlist_upsert(request: Request):
    """新增/更新自选监控配置。"""
    item = await request.json()
    try:
        saved = upsert_watch_item(item)
        return JSONResponse({"ok": True, "item": saved, "config": load_watchlist_config()})
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=400)


@app.delete("/api/watchlist/{code}")
async def api_watchlist_delete(code: str):
    """删除自选监控。"""
    changed = delete_watch_item(code)
    return JSONResponse({"ok": changed, "config": load_watchlist_config()})

# ============================================================
# API: 实盘操盘建议单
# ============================================================

def _live_intent_row_to_dict(row) -> dict:
    d = dict(row)
    for k in ["suggested_price", "suggested_amount", "limit_price", "filled_price", "filled_amount"]:
        d[k] = round(float(d.get(k) or 0), 2)
    for k in ["suggested_volume", "filled_volume"]:
        d[k] = int(d.get(k) or 0)
    return d


@app.get("/api/live-intents")
async def api_live_intents(status: str = "all", limit: int = 100):
    """实盘操盘建议单列表。"""
    store = _store()
    conn = store._get_conn()
    try:
        expire_stale_proposed_intents(conn)
        if status and status != "all":
            rows = conn.execute(
                "SELECT * FROM live_trade_intents WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM live_trade_intents ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        items = [_live_intent_row_to_dict(r) for r in rows]
        counts = {r["status"]: r["cnt"] for r in conn.execute(
            "SELECT status, COUNT(*) cnt FROM live_trade_intents GROUP BY status"
        ).fetchall()}
        return JSONResponse({"items": items, "counts": counts})
    finally:
        conn.close()


@app.get("/api/live-account")
async def api_live_account():
    """实盘影子账户：按资金流水和已回填成交重建，持仓数量不限。"""
    store = _store()
    conn = store._get_conn()
    try:
        return JSONResponse(
            live_account_snapshot(conn),
            headers={"Cache-Control": "no-store"},
        )
    finally:
        conn.close()


@app.post("/api/live-intents/{intent_id}/fill")
async def api_live_intent_fill(intent_id: str, request: Request):
    """Disabled on Web: live fills are accepted only from an authorized WeCom admin."""
    raise HTTPException(
        status_code=403,
        detail="实盘成交写入已关闭 Web 入口，请由管理员 WangZhengKui 通过企业微信应用提交。",
    )


@app.post("/api/live-intents/{intent_id}/status")
async def api_live_intent_status(intent_id: str, request: Request):
    """Disabled on Web: live status writes require an authorized WeCom admin."""
    raise HTTPException(
        status_code=403,
        detail="实盘建议单状态修改已关闭 Web 入口，请由管理员 WangZhengKui 通过企业微信应用操作。",
    )


# ============================================================
# API: 新闻监控
# ============================================================

@app.get("/api/news-events")
async def api_news_events(code: str = "", limit: int = 100):
    """获取持仓新闻/热词事件。"""
    store = _store()
    conn = store._get_conn()
    try:
        if code:
            rows = conn.execute(
                "SELECT * FROM news_events WHERE code=? ORDER BY publish_at DESC, id DESC LIMIT ?",
                (code, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM news_events ORDER BY publish_at DESC, id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        events = []
        for r in rows:
            d = dict(r)
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except Exception:
                d["tags"] = []
            events.append(d)
        return JSONResponse({"events": events, "total": len(events)})
    finally:
        conn.close()

@app.post("/api/news-scan")
async def api_news_scan():
    """立即扫描当前持仓相关新闻。"""
    try:
        from scripts.monitor_news import scan_news
        result = scan_news()
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        logger.exception("news scan failed")
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)

@app.get("/api/finance-factors")
async def api_finance_factors(code: str = ""):
    """读取财务因子快照；默认当前持仓。"""
    store = _store()
    conn = store._get_conn()
    try:
        if code:
            codes = [code.zfill(6)]
        else:
            codes = [r["code"] for r in conn.execute("SELECT code FROM portfolio WHERE volume>0")]
        rows = []
        for c in codes:
            row = conn.execute(
                """SELECT f.*, s.name, s.industry
                   FROM financial_factors f
                   LEFT JOIN stocks s ON s.code=f.code
                   WHERE f.code=? ORDER BY f.period DESC LIMIT 1""",
                (c,)
            ).fetchone()
            if row:
                rows.append(dict(row))
            else:
                s = conn.execute("SELECT code,name,industry FROM stocks WHERE code=?", (c,)).fetchone()
                rows.append({"code": c, "name": s["name"] if s else "", "industry": s["industry"] if s else "", "period": "", "source": "", "missing": True})
        return JSONResponse({"factors": rows})
    finally:
        conn.close()

@app.get("/api/value-snapshots")
async def api_value_snapshots(code: str = "", limit: int = 50):
    """读取价值投资观察快照；只读展示，不参与交易。"""
    store = _store()
    conn = store._get_conn()
    try:
        if code:
            rows = conn.execute(
                """SELECT * FROM value_snapshots
                   WHERE code=?
                   ORDER BY as_of DESC, created_at DESC LIMIT ?""",
                (code.zfill(6), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT vs.*
                   FROM value_snapshots vs
                   JOIN (
                     SELECT code, MAX(as_of || ' ' || created_at) AS latest_key
                     FROM value_snapshots
                     GROUP BY code
                   ) latest ON latest.code=vs.code
                            AND latest.latest_key=vs.as_of || ' ' || vs.created_at
                   ORDER BY vs.composite_score DESC, vs.created_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["facts"] = json.loads(item.get("facts") or "{}")
            except Exception:
                item["facts"] = {"raw": item.get("facts", "")}
            item["watch_pool"] = bool(item.get("watch_pool"))
            items.append(item)
        return JSONResponse({"items": items, "total": len(items)})
    finally:
        conn.close()

@app.get("/api/value-universe")
async def api_value_universe(tier: str = "", limit: int = 200):
    """读取价值投资分层股票池及数据新鲜度摘要；只读展示。"""
    store = _store()
    conn = store._get_conn()
    try:
        params = []
        where = "WHERE vu.status='active'"
        if tier:
            where += " AND vu.tier=?"
            params.append(tier)
        params.append(limit)
        rows = conn.execute(
            f"""SELECT vu.*,
                       MAX(CASE WHEN vf.data_type='value_snapshot' THEN vf.last_success_at ELSE '' END) AS value_snapshot_at,
                       MAX(CASE WHEN vf.data_type='valuation' THEN vf.status ELSE '' END) AS valuation_status,
                       MAX(CASE WHEN vf.data_type='financial' THEN vf.status ELSE '' END) AS financial_status
                FROM value_universe vu
                LEFT JOIN value_data_freshness vf ON vf.code=vu.code
                {where}
                GROUP BY vu.code
                ORDER BY vu.priority DESC, vu.last_refreshed_at ASC, vu.code
                LIMIT ?""",
            params,
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            for key in ("reasons", "sources"):
                try:
                    item[key] = json.loads(item.get(key) or "[]")
                except Exception:
                    item[key] = []
            items.append(item)
        return JSONResponse({"items": items, "total": len(items)})
    finally:
        conn.close()

@app.get("/api/value-freshness")
async def api_value_freshness(code: str = "", limit: int = 300):
    """读取价值数据新鲜度明细；只读展示。"""
    store = _store()
    conn = store._get_conn()
    try:
        if code:
            rows = conn.execute(
                """SELECT * FROM value_data_freshness
                   WHERE code=? ORDER BY data_type""",
                (code.zfill(6),),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM value_data_freshness
                   ORDER BY updated_at DESC, code, data_type LIMIT ?""",
                (limit,),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.get("metadata") or "{}")
            except Exception:
                item["metadata"] = {"raw": item.get("metadata", "")}
            items.append(item)
        return JSONResponse({"items": items, "total": len(items)})
    finally:
        conn.close()


@app.post("/api/financial-analysis")
async def api_financial_analysis(request: Request):
    """按需刷新指定股票的财务/估值数据，并可生成无工具 AI 解读。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求 JSON 格式错误"}, status_code=400)

    raw_codes = payload.get("codes", "")
    try:
        codes = normalize_codes(raw_codes, limit=10)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    refresh = bool(payload.get("refresh", True))
    include_ai = bool(payload.get("include_ai", True))
    try:
        result = await asyncio.to_thread(
            FinancialAnalysisService().analyze,
            codes,
            refresh=refresh,
            include_ai=include_ai,
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)
    except Exception as exc:
        logger.exception("financial analysis failed")
        return JSONResponse({"ok": False, "message": str(exc), "items": []}, status_code=500)

# ============================================================
# API: 每日盈亏走势
# ============================================================

# ============================================================
# API: 每日盈亏走势
# ============================================================

@app.get("/api/today-summary")
async def api_today_summary():
    """今日操作汇总：只展示当天买入/卖出流水，不混入收益口径。"""
    store = _store()
    conn = store._get_conn()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute(
            """
            SELECT id, order_id, code, name, direction, price, volume, amount,
                   reason, strategy, created_at
            FROM orders
            WHERE direction IN ('buy', 'sell')
              AND COALESCE(status, 'filled') IN ('filled', 'done')
              AND date(created_at)=?
            ORDER BY datetime(created_at) DESC, id DESC
            """,
            (today,),
        ).fetchall()

        operations = []
        for r in rows:
            volume = int(r["volume"] or 0)
            amount = round(float(r["amount"] or 0), 2)
            created_at = str(r["created_at"] or "")
            operations.append({
                "id": r["id"],
                "order_id": r["order_id"] or "",
                "code": str(r["code"]).zfill(6),
                "name": r["name"] or str(r["code"]).zfill(6),
                "direction": r["direction"],
                "volume": volume,
                "amount": amount,
                "price": round(float(r["price"] or 0), 2),
                "reason": r["reason"] or "",
                "strategy": r["strategy"] or "",
                "created_at": created_at,
                "time_label": created_at[11:16],
            })

        buys = [item for item in operations if item["direction"] == "buy"]
        sells = [item for item in operations if item["direction"] == "sell"]

        return JSONResponse({
            "date": today,
            "operations": operations,
            "buys": buys,
            "sells": sells,
            "buy_count": len(buys),
            "sell_count": len(sells),
            "total_buy_amount": round(sum(item["amount"] for item in buys), 2),
            "total_sell_amount": round(sum(item["amount"] for item in sells), 2),
            "summary_method": "today_order_flow_only",
        })
    finally:
        conn.close()


@app.get("/api/closed-positions")
async def api_closed_positions(limit: int = 8):
    """已清仓持仓周期：按买卖流水重建，减到0股时记为一次清仓。"""
    store = _store()
    conn = store._get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, code, name, direction, price, volume, amount,
                   commission, tax, created_at
            FROM orders
            WHERE direction IN ('buy', 'sell')
              AND volume > 0
              AND price > 0
            ORDER BY datetime(created_at), id
            """
        ).fetchall()

        open_cycles = {}
        closed = []

        def empty_cycle(code: str, name: str) -> dict:
            return {
                "code": code,
                "name": name or code,
                "buy_volume": 0,
                "sell_volume": 0,
                "remaining_volume": 0,
                "buy_amount": 0.0,
                "sell_amount": 0.0,
                "buy_fee": 0.0,
                "sell_fee": 0.0,
                "tax": 0.0,
                "buy_count": 0,
                "sell_count": 0,
                "opened_at": "",
                "closed_at": "",
            }

        for row in rows:
            code = str(row["code"]).zfill(6)
            name = row["name"] or code
            direction = row["direction"]
            volume = int(row["volume"] or 0)
            amount = round(float(row["amount"] or 0), 2)
            commission = round(float(row["commission"] or 0), 2)
            tax = round(float(row["tax"] or 0), 2)
            created_at = str(row["created_at"] or "")
            if volume <= 0:
                continue

            cycle = open_cycles.get(code)
            if not cycle:
                cycle = empty_cycle(code, name)
                open_cycles[code] = cycle

            if direction == "buy":
                if cycle["remaining_volume"] <= 0 and cycle["buy_volume"] == cycle["sell_volume"] and cycle["buy_volume"] > 0:
                    cycle = empty_cycle(code, name)
                    open_cycles[code] = cycle
                cycle["name"] = name
                cycle["buy_volume"] += volume
                cycle["remaining_volume"] += volume
                cycle["buy_amount"] = round(cycle["buy_amount"] + amount, 2)
                cycle["buy_fee"] = round(cycle["buy_fee"] + commission, 2)
                cycle["buy_count"] += 1
                if not cycle["opened_at"]:
                    cycle["opened_at"] = created_at
                continue

            if direction == "sell" and cycle["buy_volume"] > 0:
                sell_volume = min(volume, cycle["remaining_volume"])
                if sell_volume <= 0:
                    continue
                ratio = sell_volume / volume
                sell_amount = round(amount * ratio, 2)
                sell_fee = round(commission * ratio, 2)
                sell_tax = round(tax * ratio, 2)

                cycle["sell_volume"] += sell_volume
                cycle["remaining_volume"] -= sell_volume
                cycle["sell_amount"] = round(cycle["sell_amount"] + sell_amount, 2)
                cycle["sell_fee"] = round(cycle["sell_fee"] + sell_fee, 2)
                cycle["tax"] = round(cycle["tax"] + sell_tax, 2)
                cycle["sell_count"] += 1
                cycle["closed_at"] = created_at

                if cycle["remaining_volume"] <= 0:
                    cost = round(cycle["buy_amount"] + cycle["buy_fee"], 2)
                    proceeds = round(cycle["sell_amount"] - cycle["sell_fee"] - cycle["tax"], 2)
                    profit = round(proceeds - cost, 2)
                    avg_buy = round(cycle["buy_amount"] / cycle["buy_volume"], 2) if cycle["buy_volume"] else 0.0
                    avg_sell = round(cycle["sell_amount"] / cycle["sell_volume"], 2) if cycle["sell_volume"] else 0.0
                    opened = cycle["opened_at"][:10]
                    closed_at = cycle["closed_at"][:10]
                    hold_days = 0
                    try:
                        hold_days = (datetime.strptime(closed_at, "%Y-%m-%d") - datetime.strptime(opened, "%Y-%m-%d")).days + 1
                    except Exception:
                        pass
                    closed.append({
                        "code": cycle["code"],
                        "name": cycle["name"],
                        "volume": cycle["sell_volume"],
                        "avg_buy": avg_buy,
                        "avg_sell": avg_sell,
                        "buy_amount": round(cycle["buy_amount"], 2),
                        "sell_amount": round(cycle["sell_amount"], 2),
                        "fee_tax": round(cycle["buy_fee"] + cycle["sell_fee"] + cycle["tax"], 2),
                        "profit": profit,
                        "profit_pct": round(profit / cost * 100, 2) if cost else 0.0,
                        "opened_at": cycle["opened_at"],
                        "closed_at": cycle["closed_at"],
                        "hold_days": hold_days,
                        "order_count": cycle["buy_count"] + cycle["sell_count"],
                    })
                    open_cycles[code] = empty_cycle(code, name)

        closed.sort(key=lambda x: x["closed_at"], reverse=True)
        total_profit = round(sum(item["profit"] for item in closed), 2)
        wins = sum(1 for item in closed if item["profit"] > 0)
        total = len(closed)
        return JSONResponse({
            "items": closed[:max(1, min(limit, 50))],
            "total": total,
            "summary": {
                "total_profit": total_profit,
                "win_count": wins,
                "loss_count": total - wins,
                "win_rate": round(wins / total * 100, 1) if total else 0.0,
            },
            "method": "order_flow_closed_cycles",
        })
    finally:
        conn.close()


# ============================================================
# API: 盈亏走势
# ============================================================

@app.get("/api/pnl-history")
async def api_pnl_history(days: int = 30):
    """获取每日权益快照（用于日盈亏走势图）"""
    store = _store()
    conn = store._get_conn()
    try:
        rows = conn.execute(
            "SELECT date, total_equity, total_profit FROM daily_equity ORDER BY date DESC LIMIT ?",
            (days,)
        ).fetchall()
        data = [dict(r) for r in rows[::-1]]  # oldest first for chart

        # Include/replace today's live data with realtime mark-to-market.
        today = datetime.now().strftime("%Y-%m-%d")
        trader = _trader()
        _refresh_trader_realtime(trader)
        summary = trader.portfolio.summary()
        live_today = {
            "date": today,
            "total_equity": summary["total_equity"],
            "total_profit": summary["total_profit"],
        }
        if data and data[-1]["date"] == today:
            data[-1] = live_today
        else:
            data.append(live_today)

        # total_profit 是累计盈亏；图表标题是“日盈亏”，这里计算相邻快照差值。
        prev_profit = None
        for item in data:
            total_profit = round(float(item.get("total_profit") or 0), 2)
            item["total_profit"] = total_profit
            item["daily_profit"] = round(total_profit if prev_profit is None else total_profit - prev_profit, 2)
            prev_profit = total_profit

        return JSONResponse({"equity": data})
    finally:
        conn.close()

# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8899)
