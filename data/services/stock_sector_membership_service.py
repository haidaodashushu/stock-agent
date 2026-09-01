"""Current stock-to-sector facts shared by selection and intraday trading."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from data.ai_compute_pool import AI_COMPUTE_STOCKS
from data.fifteen_five_pool import FIFTEEN_FIVE_STOCKS
from data.store.sqlite_store import StockStore


PROFILE_SOURCE = "iwencai_profile"
PROFILE_MAX_AGE_HOURS = 24
PROFILE_ERROR_RETRY_HOURS = 2


def _codes(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(
        str(value).split(".")[0].zfill(6)
        for value in values
        if str(value or "").strip()
    ))


def _names(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(
        str(value).strip()
        for value in values
        if str(value or "").strip()
    ))


def replace_stock_memberships(
    conn,
    code: str,
    *,
    industries: Iterable[str],
    concepts: Iterable[str],
    source: str = PROFILE_SOURCE,
    observed_at: str | None = None,
) -> int:
    """Atomically replace one provider's complete membership snapshot."""
    code = str(code).split(".")[0].zfill(6)
    observed_at = observed_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    industry_names = _names(industries)
    concept_names = _names(concepts)
    rows = [
        (code, name, "industry", source, observed_at)
        for name in industry_names
    ] + [
        (code, name, "concept", source, observed_at)
        for name in concept_names
        if name not in industry_names
    ]
    conn.execute(
        "DELETE FROM stock_sector_membership WHERE code=? AND source=?",
        (code, source),
    )
    if rows:
        conn.executemany(
            """INSERT INTO stock_sector_membership
               (code,sector_name,sector_type,source,observed_at)
               VALUES (?,?,?,?,?)""",
            rows,
        )
    conn.execute(
        """INSERT INTO stock_sector_profile_state
           (code,source,refreshed_at,status,error)
           VALUES (?,?,?,'ok','')
           ON CONFLICT(code,source) DO UPDATE SET
             refreshed_at=excluded.refreshed_at,
             status='ok',
             error=''""",
        (code, source, observed_at),
    )
    return len(rows)


def load_stock_memberships(
    store: StockStore, codes: Iterable[str],
) -> dict[str, list[dict[str, Any]]]:
    """Load normalized facts with legacy/static sources as explicit fallbacks."""
    normalized = _codes(codes)
    result: dict[str, list[dict[str, Any]]] = {code: [] for code in normalized}
    if not normalized:
        return result
    placeholders = ",".join("?" for _ in normalized)
    conn = store._get_conn()
    try:
        normalized_codes: set[str] = set()
        for row in conn.execute(
            f"""SELECT code,sector_name,sector_type,source,observed_at
                  FROM stock_sector_membership
                 WHERE code IN ({placeholders})
                 ORDER BY code,sector_type DESC,sector_name""",
            normalized,
        ):
            code = str(row["code"]).zfill(6)
            normalized_codes.add(code)
            result[code].append(dict(row))

        for row in conn.execute(
            f"SELECT code,industry,updated_at FROM stocks WHERE code IN ({placeholders})",
            normalized,
        ):
            industry = str(row["industry"] or "").strip()
            if industry:
                result[str(row["code"]).zfill(6)].append({
                    "code": str(row["code"]).zfill(6),
                    "sector_name": industry,
                    "sector_type": "industry",
                    "source": "stock_metadata",
                    "observed_at": row["updated_at"],
                })

        requested = set(normalized)
        for row in conn.execute("SELECT name,stocks,updated_at FROM concepts WHERE stocks!=''"):
            members = {
                str(value).strip().zfill(6)
                for value in str(row["stocks"] or "").split(",")
                if str(value).strip()
            }
            for code in requested.intersection(members):
                if code in normalized_codes:
                    continue
                result[code].append({
                    "code": code,
                    "sector_name": str(row["name"] or ""),
                    "sector_type": "concept",
                    "source": "legacy_concepts",
                    "observed_at": row["updated_at"],
                })
    finally:
        conn.close()

    for code in normalized:
        static = FIFTEEN_FIVE_STOCKS.get(code) or {}
        for name in _names(static.get("concepts", [])):
            result[code].append({
                "code": code, "sector_name": name, "sector_type": "concept",
                "source": "static_fifteen_five", "observed_at": "",
            })
        ai = AI_COMPUTE_STOCKS.get(code) or {}
        for name in _names(ai.get("sectors", [])):
            result[code].append({
                "code": code, "sector_name": name, "sector_type": "concept",
                "source": "static_ai_compute", "observed_at": "",
            })

        industry_names = {
            str(item.get("sector_name") or "").strip()
            for item in result[code]
            if item.get("sector_type") == "industry"
        }
        deduped: dict[tuple[str, str], dict[str, Any]] = {}
        for item in result[code]:
            name = str(item.get("sector_name") or "").strip()
            kind = str(item.get("sector_type") or "concept")
            if kind == "concept" and name in industry_names:
                continue
            if name:
                deduped.setdefault((name, kind), item)
        result[code] = list(deduped.values())
    return result


class StockSectorMembershipService:
    """Refresh only stale/missing profiles for a bounded trading scope."""

    def __init__(self, *, store: StockStore | None = None, adapter=None, batch_size: int = 10):
        self.store = store or StockStore()
        if adapter is None:
            from data.adapters.iwencai_intelligence_adapter import IwenCaiIntelligenceAdapter
            adapter = IwenCaiIntelligenceAdapter()
        self.adapter = adapter
        self.batch_size = max(1, min(10, int(batch_size)))

    def ensure(self, codes: Iterable[str], *, max_age_hours: int = PROFILE_MAX_AGE_HOURS) -> dict:
        normalized = _codes(codes)
        stale = self._stale_codes(normalized, max_age_hours=max_age_hours)
        result = {
            "requested": len(normalized),
            "refreshed": 0,
            "memberships": 0,
            "missing": [],
            "errors": [],
        }
        if not stale:
            return result

        conn = self.store._get_conn()
        try:
            for offset in range(0, len(stale), self.batch_size):
                batch = stale[offset:offset + self.batch_size]
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    profiles = self.adapter.query_stock_profiles(batch)
                except Exception as exc:
                    result["errors"].append(str(exc))
                    self._mark_failed(conn, batch, now, str(exc))
                    continue
                by_code = {
                    str(row.get("code") or "").split(".")[0].zfill(6): row
                    for row in profiles
                    if isinstance(row, dict) and row.get("code")
                }
                for code in batch:
                    profile = by_code.get(code)
                    if not profile:
                        # Batch profile queries occasionally omit one member
                        # without returning an error.  Retry only that code
                        # once; this is bounded and runs at most daily.
                        try:
                            retry_rows = self.adapter.query_stock_profiles([code])
                        except Exception as exc:
                            retry_rows = []
                            result["errors"].append(f"{code}: {exc}")
                        profile = next((
                            row for row in retry_rows
                            if isinstance(row, dict)
                            and str(row.get("code") or "").split(".")[0].zfill(6) == code
                        ), None)
                        if not profile:
                            result["missing"].append(code)
                            self._mark_failed(conn, [code], now, "profile missing", status="missing")
                            continue
                    industries = _names(profile.get("industries", []))
                    concepts = _names(profile.get("concepts", []))
                    result["memberships"] += replace_stock_memberships(
                        conn,
                        code,
                        industries=industries,
                        concepts=concepts,
                        observed_at=now,
                    )
                    name = str(profile.get("name") or "").strip()
                    primary_industry = industries[-1] if industries else ""
                    conn.execute(
                        """UPDATE stocks
                              SET name=CASE WHEN ?!='' THEN ? ELSE name END,
                                  industry=CASE WHEN ?!='' THEN ? ELSE industry END,
                                  updated_at=datetime('now','localtime')
                            WHERE code=?""",
                        (name, name, primary_industry, primary_industry, code),
                    )
                    result["refreshed"] += 1
                conn.commit()
        finally:
            conn.close()
        return result

    def _stale_codes(self, codes: list[str], *, max_age_hours: int) -> list[str]:
        if not codes:
            return []
        placeholders = ",".join("?" for _ in codes)
        conn = self.store._get_conn()
        try:
            rows = conn.execute(
                f"""SELECT code,refreshed_at,status
                      FROM stock_sector_profile_state
                     WHERE source=? AND code IN ({placeholders})""",
                [PROFILE_SOURCE, *codes],
            ).fetchall()
        finally:
            conn.close()
        state = {str(row["code"]).zfill(6): row for row in rows}
        now = datetime.now()
        stale: list[str] = []
        for code in codes:
            row = state.get(code)
            if not row:
                stale.append(code)
                continue
            try:
                refreshed = datetime.fromisoformat(str(row["refreshed_at"]))
            except (TypeError, ValueError):
                stale.append(code)
                continue
            hours = (
                PROFILE_ERROR_RETRY_HOURS
                if str(row["status"]) != "ok"
                else max(1, int(max_age_hours))
            )
            if refreshed < now - timedelta(hours=hours):
                stale.append(code)
        return stale

    @staticmethod
    def _mark_failed(conn, codes: list[str], refreshed_at: str, error: str, *, status: str = "error") -> None:
        conn.executemany(
            """INSERT INTO stock_sector_profile_state
               (code,source,refreshed_at,status,error)
               VALUES (?,?,?,?,?)
               ON CONFLICT(code,source) DO UPDATE SET
                 refreshed_at=excluded.refreshed_at,
                 status=excluded.status,
                 error=excluded.error""",
            [
                (str(code).zfill(6), PROFILE_SOURCE, refreshed_at, status, str(error)[:300])
                for code in codes
            ],
        )
        conn.commit()
