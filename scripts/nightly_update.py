#!/usr/bin/env python3
"""Update A-share daily bars from Tencent for routine or bootstrap use."""
import argparse
import sys, os, time, json, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from data.store.sqlite_store import StockStore
from engine.screener import filter_tradeable
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

DAYS = 5       # 只更新最近5天
WORKERS = 16   # 并发线程


def market_prefix(code):
    """腾讯行情市场前缀。"""
    code = str(code).zfill(6)
    if code.startswith("6"):
        return "sh"
    if code.startswith(("8", "4", "9")):
        return "bj"
    return "sz"


def get_all_codes():
    """获取活跃股票代码。

    不再直接扫 daily_prices 的历史全集，避免退市/合并老代码反复进入夜间更新。
    """
    store = StockStore()
    conn = store._get_conn()
    rows = conn.execute(
        """SELECT DISTINCT code FROM stocks
           WHERE is_active=1
             AND length(code)=6
             AND code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'
           ORDER BY code"""
    ).fetchall()
    codes = [r[0] for r in rows]
    if not codes:
        rows = conn.execute('SELECT DISTINCT code FROM daily_prices ORDER BY code').fetchall()
        codes = [r[0] for r in rows]
    conn.close()
    log.info(f"数据库活跃股票 {len(codes)} 只")
    return codes


def fetch_recent(code, days=DAYS):
    """获取单只股票最近N天日K"""
    prefix = market_prefix(code)
    url = f"https://ifzq.gtimg.cn/appstock/app/kline/kline?param={prefix}{code},day,,,{days+10}"
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0', 'Accept': '*/*',
            'Referer': 'https://gu.qq.com/',
        })
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode('utf-8', errors='ignore'))
        klines = data.get('data', {}).get(f'{prefix}{code}', {}).get('day',
                  data.get('data', {}).get(f'{prefix}{code}', {}).get('qfqday', []))
        if not klines:
            return code, []

        # 只取最近 days 天的
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        rows = []
        for item in klines:
            if len(item) < 5:
                continue
            ds = str(item[0])
            # 腾讯返回两种日期格式：20260616 或 2026-06-16
            if len(ds) == 8:
                date_str = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
            elif len(ds) == 10:
                date_str = ds
            else:
                continue
            if date_str >= cutoff:
                rows.append((code, date_str,
                             float(item[1]), float(item[2]),
                             float(item[3]), float(item[4]),
                             int(float(item[5])),
                             float(item[6]) if len(item) > 6 else 0))
        return code, rows
    except Exception as e:
        return code, []


def save_updates(rows_list, conn):
    """批量写入日K更新"""
    ins = "INSERT OR REPLACE INTO daily_prices (code,date,open,close,high,low,volume,amount,adjust_flag) VALUES (?,?,?,?,?,?,?,?,'qfq')"
    for code, rows in rows_list:
        if not rows:
            continue
        for r in rows:
            conn.execute(ins, r)


def run(days: int = DAYS, workers: int = WORKERS):
    codes = get_all_codes()
    if not codes:
        log.error("数据库无股票数据")
        return

    log.info(f"🌙 日K更新启动: {len(codes)} 只股票, 每只最近{days}天")

    store = StockStore()
    conn = store._get_conn()

    success = 0
    fail = 0
    failed_codes = []
    batch = []
    start = time.time()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        fs = {pool.submit(fetch_recent, c, days): c for c in codes}
        done = 0
        for f in as_completed(fs):
            code, rows = f.result()
            done += 1
            batch.append((code, rows))
            if rows:
                success += 1
            else:
                fail += 1
                failed_codes.append(code)

            # 每200只或全部完成时写入
            if len(batch) >= 200 or done == len(codes):
                save_updates(batch, conn)
                conn.commit()
                batch = []

            if done % 1000 == 0:
                el = time.time() - start
                rate = done / el if el > 0 else 0
                rem = len(codes) - done
                log.info(f"  [{done}/{len(codes)}] 成功:{success} 失败:{fail} | {rate:.1f}只/秒 | 预计{rem/rate/60:.0f}分")

    conn.close()
    el = time.time() - start
    log.info(f"✅ 夜间更新完成! {el/60:.1f}分 成功:{success} 失败:{fail}")
    if failed_codes:
        out = os.path.join(os.path.dirname(__file__), "..", "logs", "nightly_update_failures_latest.json")
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump({
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "total": len(failed_codes),
                    "codes": failed_codes,
                }, f, ensure_ascii=False, indent=2)
            log.info(f"失败代码列表: {out}")
        except Exception as e:
            log.warning(f"写入失败代码列表失败: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="更新股票日K；默认用于夜间增量更新")
    parser.add_argument(
        "--days", type=int, default=DAYS,
        help="拉取最近多少个自然日；空库初始化可使用 500（默认 5）",
    )
    parser.add_argument("--workers", type=int, default=WORKERS, help="并发数（默认 16）")
    args = parser.parse_args()
    if args.days <= 0 or args.workers <= 0:
        parser.error("--days 和 --workers 必须为正整数")
    run(days=args.days, workers=args.workers)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
