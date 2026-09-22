"""Shared Fuyao sector facts. No fund-flow inference from price/turnover.

Catalogs/memberships are slow data; history refresh is bounded. Cached failures
back off, and a cold/partial cache remains explicitly partial, never zero data.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import fcntl
import hashlib
import json
import math
import os
import re
import time

from data.adapters.fuyao_adapter import FuyaoAdapter
from data.store.sqlite_store import StockStore

TZ = ZoneInfo("Asia/Shanghai")
SOURCE = "fuyao_constituents"


def number(value):
    try:
        result = float(value) if value is not None and not isinstance(value, bool) else None
        return result if result is not None and math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


class FuyaoSectorService:
    def __init__(self, store=None, adapter=None, cache_dir=None):
        self.store = store or StockStore()
        self.adapter = adapter or FuyaoAdapter()
        self.cache_dir = Path(cache_dir or (str(self.store.db_path) + ".fuyao-cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def cached(self, key, ttl, fetch, budget=None):
        path = self.cache_dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        with path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                entry = json.loads(path.read_text())
            except (OSError, ValueError):
                entry = {}
            now = time.time()
            if entry.get("data") is not None and now - entry.get("fetched_at", 0) < ttl:
                return entry["data"]
            if entry.get("retry_after", 0) > now:
                raise RuntimeError("Fuyao sector refresh deferred after failure")
            if budget is not None:
                if budget[0] <= 0:
                    raise RuntimeError("Fuyao sector refresh deferred by batch budget")
                budget[0] -= 1
            try:
                data = fetch()
                entry = {"data": data, "fetched_at": now}
            except Exception:
                entry["retry_after"] = now + 900
                path.write_text(json.dumps(entry, ensure_ascii=False))
                raise
            path.write_text(json.dumps(entry, ensure_ascii=False))
            return data

    def catalog(self):
        result = []
        for tag, kind in (("cn_concept", "concept"), ("industry", "industry")):
            def fetch(tag=tag):
                data = self.adapter.get("/api/a-share-index/catalog/ths-index-list", tag=tag)
                rows = data.get("item")
                if not isinstance(rows, list) or not rows:
                    raise ValueError("Fuyao sector catalog empty")
                if any(not re.fullmatch(r"\d{6}\.TI", str(row.get("thscode", ""))) or not row.get("name") for row in rows):
                    raise ValueError("Fuyao sector catalog invalid")
                return rows
            rows = self.cached("catalog:" + tag, 7*86400, fetch)
            result.extend({**row, "kind": kind} for row in rows)
        return result

    def quotes(self, sectors):
        result = {}
        symbols = sorted({row["thscode"] for row in sectors})
        for offset in range(0, len(symbols), 100):
            batch = symbols[offset:offset+100]
            def fetch():
                data = self.adapter.get("/api/a-share-index/prices/snapshot", thscodes=",".join(batch))
                observed = number(data.get("timestamp"))
                if observed is None or observed/1000 > time.time()+60 or time.time()-observed/1000 > 18*3600:
                    raise ValueError("Fuyao sector quote timestamp stale or invalid")
                rows = data.get("item")
                if not isinstance(rows, list) or not rows:
                    raise ValueError("Fuyao sector quotes empty")
                if any(row.get("thscode") not in batch for row in rows):
                    raise ValueError("Fuyao sector quote identity mismatch")
                return data
            data = self.cached("quotes:" + ",".join(batch), 900, fetch)
            for row in data["item"]:
                if (number(row.get("last_price")) or 0) > 0 and number(row.get("price_change_ratio_pct")) is not None:
                    result[row["thscode"]] = {**row, "source_time": datetime.fromtimestamp(data["timestamp"]/1000, TZ).strftime("%Y-%m-%d %H:%M:%S")}
        return result

    def history(self, symbol, budget):
        # Only completed sessions are used as bases; today's quote supplies the numerator.
        def fetch():
            now = datetime.now(TZ)
            data = self.adapter.get("/api/a-share-index/prices/historical", thscode=symbol,
                interval="1d", start=int((now-timedelta(days=240)).timestamp()*1000), end=int(now.timestamp()*1000))
            if (data.get("thscode") is not None and data["thscode"] != symbol) or not isinstance(data.get("item"), list) or not data["item"]:
                raise ValueError("Fuyao sector history missing or identity mismatch")
            return data
        return self.cached("history:" + symbol, 18*3600, fetch, budget)

    def members(self, sector, budget):
        def fetch():
            data = self.adapter.get("/api/a-share-index/constituents/ths-stock-list", thscode=sector["thscode"])
            rows = data.get("item")
            if not isinstance(rows, list) or not rows:
                raise ValueError("Fuyao sector constituents empty; keep previous membership")
            if any(not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(row.get("thscode", ""))) for row in rows):
                raise ValueError("Fuyao sector constituent identity invalid")
            observed = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Replace one sector atomically, never erase a stock's other sectors.
            with self.store._get_conn() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS fuyao_sector_state (sector_name TEXT PRIMARY KEY, thscode TEXT NOT NULL, observed_at TEXT NOT NULL)")
                conn.execute("DELETE FROM stock_sector_membership WHERE source=? AND sector_name=?", (SOURCE, sector["name"]))
                conn.executemany("INSERT OR REPLACE INTO stock_sector_membership(code,sector_name,sector_type,source,observed_at) VALUES(?,?,?,?,?)",
                    [(row["thscode"].split(".")[0], sector["name"], sector["kind"], SOURCE, observed) for row in rows])
                conn.execute("INSERT OR REPLACE INTO fuyao_sector_state VALUES(?,?,?)", (sector["name"], sector["thscode"], observed))
            return {"observed_at": observed, "item": rows}
        return self.cached("members:" + sector["thscode"], 7*86400, fetch, budget)

    def ensure_memberships(self, codes, budget=3):
        from data.services.stock_sector_membership_service import load_stock_memberships
        codes = list(dict.fromkeys(codes))
        result = {"requested": len(codes), "refreshed": 0, "memberships": 0, "missing": [], "errors": [], "source": SOURCE, "coverage": "partial"}
        try:
            catalog = self.catalog()
            facts = load_stock_memberships(self.store, codes)
            names = {row["sector_name"] for rows in facts.values() for row in rows}
            sectors = sorted(catalog, key=lambda row: row["name"] not in names)
            remaining = [budget]
            for sector in sectors:
                try:
                    self.members(sector, remaining)
                except Exception as exc:
                    if remaining[0] <= 0:
                        break
                    result["errors"].append(str(exc))
            current = load_stock_memberships(self.store, codes)
            for code, rows in current.items():
                verified = [row for row in rows if row["source"] == SOURCE]
                result["memberships"] += len(verified)
                result["refreshed"] += bool(verified)
                if not verified:
                    result["missing"].append(code)
        except Exception as exc:
            result["errors"].append(str(exc)); result["missing"] = codes
        return result

    def snapshot_rows(self, limit=30, budget=6):
        catalog = self.catalog()
        quotes = self.quotes(catalog)
        # Warm price leaders first, then score all cached history. Partial history
        # coverage is explicit and never presented as global fund-flow ranking.
        ranked = sorted((row for row in catalog if row["thscode"] in quotes),
                        key=lambda row: quotes[row["thscode"]]["price_change_ratio_pct"], reverse=True)
        selected = ranked
        histories, memberships = [budget], [budget]
        rows, errors = [], []
        for sector in selected:
            quote = quotes[sector["thscode"]]
            item = {"name": sector["name"], "code": sector["thscode"],
                    "pct_1d": quote["price_change_ratio_pct"], "turnover": quote.get("turnover"),
                    "source_time": quote["source_time"], "fund_inflow": None,
                    "pct_5d": None, "pct_1m": None, "pct_3m": None, "pct_6m": None}
            try:
                history = self.history(sector["thscode"], histories)
                cutoff = quote["source_time"][:10]
                bars = sorted((bar for bar in history["item"] if number(bar.get("date_ms")) is not None
                    and datetime.fromtimestamp(bar["date_ms"]/1000, TZ).strftime("%Y-%m-%d") < cutoff
                    and (number(bar.get("close_price")) or 0) > 0), key=lambda bar: bar["date_ms"])
                # Holiday/weekend gaps allowed; an old trailing series is not current evidence.
                if not bars or (datetime.fromisoformat(cutoff).date() - datetime.fromtimestamp(bars[-1]["date_ms"]/1000, TZ).date()).days > 10:
                    raise ValueError("Fuyao sector historical base stale")
                bars = list({bar["date_ms"]: bar for bar in bars}.values())
                for field, sessions in (("pct_5d",5), ("pct_1m",21), ("pct_3m",63), ("pct_6m",126)):
                    if len(bars) >= sessions:
                        item[field] = round((float(quote["last_price"])/float(bars[-sessions]["close_price"])-1)*100,4)
            except Exception as exc:
                errors.append(f"{sector['thscode']} history: {exc}")
            if sector in ranked[:limit]:
                try:
                    self.members(sector, memberships)
                except Exception as exc:
                    errors.append(f"{sector['thscode']} members: {exc}")
            rows.append(item)
        return {"rows": rows, "errors": errors, "catalog_count": len(catalog), "quote_count": len(quotes),
                "scope": "catalog_with_available_history", "return_windows": "5/21/63/126 trading sessions", "fund_flow_status": "unavailable"}
