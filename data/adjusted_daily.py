"""Fetch and restate a complete, explicitly adjusted technical window."""
from __future__ import annotations

import json
import math
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from data.fetcher.tencent_quote import _tencent_symbol
from data.market_calendar import market_day


def expected_session(now=None):
    now=now or datetime.now()
    day=now.date()
    if now.hour<15:
        day-=timedelta(days=1)
    while not market_day(day).is_open:
        day-=timedelta(days=1)
    return str(day)


def fetch_window(code, count=320, through=None):
    through=through or expected_session()
    symbol=_tencent_symbol(code)
    url=f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,{through},{max(80,min(count,640))},qfq"
    last_error=""
    for attempt in range(2):
        try:
            request=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Referer":"https://gu.qq.com/"})
            with urllib.request.urlopen(request,timeout=12) as response:
                payload=json.load(response)
            series=payload.get("data",{}).get(symbol,{}).get("qfqday")
            if not series:
                raise ValueError("explicit qfqday missing; refusing raw-day fallback")
            rows=[]
            for item in series:
                if len(item)<6:
                    raise ValueError("incomplete daily bar")
                day=str(item[0])
                if len(day)==8:
                    day=f"{day[:4]}-{day[4:6]}-{day[6:]}"
                datetime.strptime(day,"%Y-%m-%d")
                if day>through:
                    continue
                values=[float(v) for v in item[1:6]]
                op,close,high,low,volume=values
                if not all(math.isfinite(v) for v in values) or min(op,close,high,low)<=0 or volume<0 or not low<=min(op,close)<=max(op,close)<=high:
                    raise ValueError("invalid daily OHLCV")
                # Provider volume is lots. Amount is absent from qfqday and
                # must not be synthesized from an adjusted close.
                rows.append((code,day,op,close,high,low,int(volume*100),None))
            dates=[r[1] for r in rows]
            if not rows or dates!=sorted(set(dates)):
                raise ValueError("daily dates missing, unordered or duplicate")
            return rows
        except Exception as exc:
            last_error=str(exc)
            if attempt==0:
                time.sleep(.25)
    raise RuntimeError(f"{code}: adjusted daily fetch failed: {last_error}")


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS adjusted_daily_windows (
       code TEXT PRIMARY KEY, start_date TEXT NOT NULL,end_date TEXT NOT NULL,
       source TEXT NOT NULL, fetched_at TEXT NOT NULL)""")


def save_window(conn, code, rows):
    if not rows:
        return
    ensure_table(conn)
    conn.executemany("""INSERT OR REPLACE INTO daily_prices
       (code,date,open,close,high,low,volume,amount,adjust_flag)
       VALUES(?,?,?,?,?,?,?,?,'qfq')""",rows)
    conn.execute("INSERT OR REPLACE INTO adjusted_daily_windows VALUES(?,?,?,?,?)",
                 (code,rows[0][1],rows[-1][1],"tencent_fqkline.qfqday",datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def ensure_windows(store,codes,now=None):
    through=expected_session(now)
    with store._get_conn() as conn:
        ensure_table(conn)
        fresh={r["code"] for r in conn.execute("SELECT code FROM adjusted_daily_windows WHERE end_date=?",(through,))}
    missing=[c for c in codes if c not in fresh]
    def fetch(code):
        try:
            return code,fetch_window(code,through=through),None
        except Exception as exc:
            return code,[],str(exc)
    errors={}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for code,rows,error in pool.map(fetch,missing):
            if rows:
                with store._get_conn() as conn:
                    save_window(conn,code,rows)
                if rows[-1][1]!=through:
                    errors[code]="daily source lacks expected session (suspension or stale source)"
            else:
                errors[code]=error
    return {"expected_date":through,"requested":len(missing),"errors":errors}
