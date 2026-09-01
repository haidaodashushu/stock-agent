"""Opening call-auction facts and candidate-only discovery signals.

Tencent is used for time-point raw order-book snapshots; iWenCai is used only
for the final 09:25 auction result whose field units are explicit in the
provider response.  Derived watch candidates expand the decision scope for a
short time, but never grant buy eligibility by themselves.
"""
from __future__ import annotations

import json
import math
import urllib.request
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from data.adapters.iwencai_intelligence_adapter import IwenCaiIntelligenceAdapter
from data.candidate_observations import latest_auction_watch_candidates
from data.store.sqlite_store import StockStore
from data.strategic_theme_pool import load_strategic_pool


AUCTION_PHASES = ("cancelable_end", "locked_end", "final")
AUCTION_WATCH_RULE_VERSION = 1
AUCTION_WATCH_LIMIT = 3
AUCTION_WATCH_EXPIRES_AT = time(10, 0)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _symbol(code: str) -> str:
    code = str(code).split(".")[0].zfill(6)
    return ("sh" if code.startswith(("5", "6")) else "sz") + code


def parse_tencent_auction_line(
    line: str, *, phase: str, observed_at: str,
) -> dict[str, Any] | None:
    """Parse a Tencent quote while retaining the original fields.

    Level volumes and aggregate volume/amount are intentionally named ``raw``:
    their auction-stage semantics will be validated from the collected sample
    before any factor is built from them.
    """
    if phase not in AUCTION_PHASES or '="' not in line:
        return None
    fields = line.split('="', 1)[1].rstrip('";\r\n').split("~")
    if len(fields) < 38:
        return None
    code = str(fields[2] or "").zfill(6)
    if len(code) != 6 or not code.isdigit():
        return None

    def levels(start: int) -> list[dict[str, float]]:
        return [
            {
                "level": index + 1,
                "price": _number(fields[start + index * 2]),
                "volume_raw": _number(fields[start + index * 2 + 1]),
            }
            for index in range(5)
        ]

    provider_time = str(fields[30] or "") if len(fields) > 30 else ""
    provider_current = provider_time.startswith(observed_at[:10].replace("-", ""))
    return {
        "trade_date": observed_at[:10],
        "phase": phase,
        "code": code,
        "name": str(fields[1] or ""),
        "observed_at": observed_at,
        "provider_time": provider_time,
        "provider_current": provider_current,
        "previous_close": _number(fields[4]),
        "last_price": _number(fields[3]),
        "open_price": _number(fields[5]),
        "reported_volume_raw": _number(fields[6]),
        "reported_amount_raw": _number(fields[37]),
        "bid_levels": levels(9),
        "ask_levels": levels(19),
        "raw_fields": fields,
        "source": "tencent",
    }


def fetch_tencent_auction_snapshots(
    codes: Iterable[str], *, phase: str, now: datetime | None = None, timeout: int = 10,
) -> tuple[list[dict[str, Any]], list[str]]:
    if phase not in AUCTION_PHASES:
        raise ValueError(f"unsupported auction phase: {phase}")
    now = now or datetime.now()
    observed_at = now.strftime("%Y-%m-%d %H:%M:%S")
    normalized = list(dict.fromkeys(str(code).split(".")[0].zfill(6) for code in codes))
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for offset in range(0, len(normalized), 80):
        batch = normalized[offset : offset + 80]
        url = "http://qt.gtimg.cn/q=" + ",".join(_symbol(code) for code in batch)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            body = urllib.request.urlopen(request, timeout=timeout).read().decode("gbk", errors="ignore")
        except Exception as exc:
            errors.append(f"tencent batch {offset // 80 + 1}: {exc}")
            continue
        for line in body.splitlines():
            parsed = parse_tencent_auction_line(line, phase=phase, observed_at=observed_at)
            if parsed:
                rows.append(parsed)
    returned = {row["code"] for row in rows}
    missing = sorted(set(normalized) - returned)
    if missing:
        errors.append(f"tencent missing {len(missing)}: {','.join(missing[:10])}")
    return rows, errors


def _first(item: dict[str, Any], *prefixes: str, default: Any = "") -> Any:
    for prefix in prefixes:
        if prefix in item and item[prefix] not in (None, ""):
            return item[prefix]
    for key, value in item.items():
        if value in (None, ""):
            continue
        if any(str(key).startswith(prefix) for prefix in prefixes):
            return value
    return default


def parse_iwencai_auction_final(
    item: dict[str, Any], *, trade_date: str, observed_at: str,
) -> dict[str, Any] | None:
    code = str(_first(item, "股票代码", "代码")).split(".")[0].zfill(6)
    if len(code) != 6 or not code.isdigit():
        return None
    return {
        "trade_date": trade_date,
        "code": code,
        "name": str(_first(item, "股票简称", "名称")),
        "auction_price": _number(_first(item, "竞价匹配价")),
        "auction_change_pct": _number(_first(item, "竞价涨幅")),
        "matched_volume_shares": _number(_first(item, "竞价匹配量", "竞价量")),
        "matched_amount_yuan": _number(_first(item, "竞价匹配金额", "竞价金额")),
        "unmatched_volume_signed": _number(_first(item, "竞价未匹配量")),
        "unmatched_amount_signed": _number(_first(item, "竞价未匹配金额")),
        "anomaly_type": str(_first(item, "竞价异动类型")),
        "anomaly_note": str(_first(item, "竞价异动说明")),
        "rating": str(_first(item, "竞价评级", "集合竞价评级")),
        "observed_at": observed_at,
        "raw_payload": item,
        "source": "iwencai",
    }


def fetch_iwencai_auction_final(
    codes: Iterable[str], *, trade_date: str | date, adapter: Any | None = None,
    batch_size: int = 5, now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch final auction facts in batches of five (provider's stable limit)."""
    normalized = list(dict.fromkeys(str(code).split(".")[0].zfill(6) for code in codes))
    day = str(trade_date)[:10]
    day_text = datetime.strptime(day, "%Y-%m-%d").strftime("%Y年%-m月%-d日")
    observed_at = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    adapter = adapter or IwenCaiIntelligenceAdapter()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for offset in range(0, len(normalized), max(1, min(5, batch_size))):
        batch = normalized[offset : offset + max(1, min(5, batch_size))]
        query = (
            f"{','.join(batch)} {day_text}竞价涨幅 竞价匹配价 竞价匹配金额 "
            "竞价匹配量 竞价未匹配金额 竞价未匹配量 竞价异动类型 竞价异动说明 竞价评级"
        )
        try:
            raw = adapter.query_raw(query, skill_id="hithink-stock-selector", limit=len(batch))
        except Exception as exc:
            errors.append(f"iwencai batch {offset // 5 + 1}: {exc}")
            continue
        parsed_batch = [
            parsed for item in raw.get("datas", [])
            if (parsed := parse_iwencai_auction_final(item, trade_date=day, observed_at=observed_at))
        ]
        rows.extend(parsed_batch)
        returned = {row["code"] for row in parsed_batch}
        missing = sorted(set(batch) - returned)
        if missing:
            errors.append(f"iwencai missing {len(missing)}: {','.join(missing)}")
    return rows, errors


def save_tencent_snapshots(store: StockStore, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    conn = store._get_conn()
    try:
        conn.executemany(
            """INSERT OR REPLACE INTO opening_auction_snapshots
               (trade_date,phase,code,name,observed_at,provider_time,provider_current,previous_close,
                last_price,open_price,reported_volume_raw,reported_amount_raw,
                bid_levels,ask_levels,raw_fields,source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(
                row["trade_date"], row["phase"], row["code"], row["name"],
                row["observed_at"], row["provider_time"], int(row["provider_current"]), row["previous_close"],
                row["last_price"], row["open_price"], row["reported_volume_raw"],
                row["reported_amount_raw"], json.dumps(row["bid_levels"], ensure_ascii=False),
                json.dumps(row["ask_levels"], ensure_ascii=False),
                json.dumps(row["raw_fields"], ensure_ascii=False), row["source"],
            ) for row in rows],
        )
        conn.commit()
    finally:
        conn.close()


def save_iwencai_final(store: StockStore, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    conn = store._get_conn()
    try:
        conn.executemany(
            """INSERT OR REPLACE INTO opening_auction_final
               (trade_date,code,name,auction_price,auction_change_pct,
                matched_volume_shares,matched_amount_yuan,unmatched_volume_signed,
                unmatched_amount_signed,anomaly_type,anomaly_note,rating,observed_at,
                raw_payload,source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(
                row["trade_date"], row["code"], row["name"], row["auction_price"],
                row["auction_change_pct"], row["matched_volume_shares"],
                row["matched_amount_yuan"], row["unmatched_volume_signed"],
                row["unmatched_amount_signed"], row["anomaly_type"], row["anomaly_note"],
                row["rating"], row["observed_at"],
                json.dumps(row["raw_payload"], ensure_ascii=False), row["source"],
            ) for row in rows],
        )
        conn.commit()
    finally:
        conn.close()


def build_auction_watch_candidates(
    store: StockStore, *, trade_date: str, limit: int = AUCTION_WATCH_LIMIT,
) -> list[dict[str, Any]]:
    """Build conservative candidate-only signals from the final auction.

    The rule is intentionally narrow and auditable.  It was chosen from the
    initial observation sample as a discovery filter, not as a return model:
    bullish provider rating, a moderate positive gap, and a strengthening
    indicative price between 09:19 and the final match.
    """
    pool = load_strategic_pool()
    conn = store._get_conn()
    try:
        rows = conn.execute(
            """SELECT f.*, s.last_price AS cancelable_price,
                      s.observed_at AS cancelable_observed_at
                 FROM opening_auction_final f
                 JOIN opening_auction_snapshots s
                   ON s.trade_date=f.trade_date AND s.code=f.code
                  AND s.phase='cancelable_end' AND s.provider_current=1
                WHERE f.trade_date=?
                ORDER BY f.matched_amount_yuan DESC, f.code""",
            (str(trade_date)[:10],),
        ).fetchall()
    finally:
        conn.close()

    selected: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        code = str(row.get("code") or "").zfill(6)
        meta = pool["stocks"].get(code)
        name = str(row.get("name") or (meta or {}).get("name") or code)
        auction_price = _number(row.get("auction_price"))
        cancelable_price = _number(row.get("cancelable_price"))
        gap = _number(row.get("auction_change_pct"))
        matched_amount = _number(row.get("matched_amount_yuan"))
        if (
            not meta or "ST" in name.upper() or str(row.get("rating") or "").strip() != "看多"
            or auction_price <= 0 or cancelable_price <= 0 or matched_amount <= 0
            or not 0.5 <= gap <= 4.0
        ):
            continue
        strengthening = (auction_price / cancelable_price - 1) * 100
        if strengthening < 0.5:
            continue

        # This score is only an within-source attention order.  It is never
        # compared directly with morning-selection or intraday-radar scores.
        amount_component = min(2.0, max(0.0, math.log10(max(1.0, matched_amount / 1_000_000))))
        score = min(9.8, 5.0 + min(2.0, strengthening) + min(4.0, gap) * 0.25 + amount_component)
        selected.append({
            "code": code,
            "name": name,
            "theme_group": meta["group"],
            "score": round(score, 2),
            "price": auction_price,
            "change_pct": round(gap, 2),
            "matched_amount_yuan": matched_amount,
            "triggers": [
                f"竞价高开{gap:.2f}%",
                f"09:19至最终匹配价增强{strengthening:.2f}%",
                f"竞价匹配金额{matched_amount / 10_000:.0f}万元",
            ],
            "risk_tags": ["仅进入观察候选，尚未获得买入资格"],
            "evidence": {
                "source": "strategic_pool_opening_auction_watch",
                "rule_version": AUCTION_WATCH_RULE_VERSION,
                "candidate_only": True,
                "radar_actionable": False,
                "pool_version": pool["version"],
                "auction": {
                    "trade_date": str(trade_date)[:10],
                    "rating": str(row.get("rating") or ""),
                    "auction_price": auction_price,
                    "auction_change_pct": round(gap, 4),
                    "cancelable_price": cancelable_price,
                    "strengthening_pct": round(strengthening, 4),
                    "matched_volume_shares": _number(row.get("matched_volume_shares")),
                    "matched_amount_yuan": matched_amount,
                    "unmatched_volume_signed": _number(row.get("unmatched_volume_signed")),
                    "unmatched_amount_signed": _number(row.get("unmatched_amount_signed")),
                    "anomaly_type": str(row.get("anomaly_type") or ""),
                    "anomaly_note": str(row.get("anomaly_note") or ""),
                    "cancelable_observed_at": str(row.get("cancelable_observed_at") or ""),
                    "final_observed_at": str(row.get("observed_at") or ""),
                },
            },
        })
    selected.sort(key=lambda item: (-_number(item.get("matched_amount_yuan")), item["code"]))
    return selected[: max(1, int(limit))]


def save_auction_watch_candidates(
    store: StockStore, *, trade_date: str, generated_at: str,
    candidates: list[dict[str, Any]],
) -> None:
    generated = datetime.fromisoformat(generated_at)
    expires = datetime.combine(generated.date(), AUCTION_WATCH_EXPIRES_AT)
    if expires <= generated:
        expires = generated + timedelta(minutes=1)
    conn = store._get_conn()
    try:
        conn.execute(
            "DELETE FROM opening_auction_watch_candidates WHERE trade_date=?",
            (str(trade_date)[:10],),
        )
        conn.executemany(
            """INSERT INTO opening_auction_watch_candidates
               (trade_date,rank,code,name,theme_group,score,auction_price,change_pct,
                triggers,risk_tags,evidence,generated_at,expires_at,rule_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(
                str(trade_date)[:10], rank, row["code"], row.get("name", ""),
                row.get("theme_group", ""), row.get("score", 0), row.get("price", 0),
                row.get("change_pct", 0), json.dumps(row.get("triggers", []), ensure_ascii=False),
                json.dumps(row.get("risk_tags", []), ensure_ascii=False),
                json.dumps(row.get("evidence", {}), ensure_ascii=False), generated_at,
                expires.strftime("%Y-%m-%d %H:%M:%S"), AUCTION_WATCH_RULE_VERSION,
            ) for rank, row in enumerate(candidates, 1)],
        )
        conn.commit()
    finally:
        conn.close()


def save_auction_run(
    store: StockStore, *, trade_date: str, phase: str, status: str,
    scope_count: int, tencent_count: int, iwencai_count: int = 0,
    started_at: str, completed_at: str, errors: Iterable[str] = (),
) -> None:
    conn = store._get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO opening_auction_runs
               (trade_date,phase,status,scope_count,tencent_count,iwencai_count,
                started_at,completed_at,error) VALUES (?,?,?,?,?,?,?,?,?)""",
            (trade_date, phase, status, scope_count, tencent_count, iwencai_count,
             started_at, completed_at, " | ".join(errors)[:2000]),
        )
        conn.commit()
    finally:
        conn.close()


def observation_coverage(store: StockStore, limit_days: int = 5) -> list[dict[str, Any]]:
    conn = store._get_conn()
    try:
        rows = conn.execute(
            """SELECT trade_date,
                      SUM(CASE WHEN phase='cancelable_end' THEN tencent_count ELSE 0 END) cancelable_count,
                      SUM(CASE WHEN phase='locked_end' THEN tencent_count ELSE 0 END) locked_count,
                      SUM(CASE WHEN phase='final' THEN tencent_count ELSE 0 END) final_tencent_count,
                      SUM(CASE WHEN phase='final' THEN iwencai_count ELSE 0 END) final_iwencai_count,
                      GROUP_CONCAT(status) statuses
                 FROM opening_auction_runs GROUP BY trade_date
                ORDER BY trade_date DESC LIMIT ?""",
            (max(1, int(limit_days)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
