#!/usr/bin/env python3
"""Build a compact, consistent migration package for another stock server.

The package keeps all account, ledger, candidate and runtime-state tables.  It
only trims the large ``daily_prices`` table: the whole market keeps a recent
technical-analysis window, while currently relevant stocks keep a longer
window.  Credentials are deliberately excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from account.reconcile import DEFAULT_RESOLVED_ISSUES_PATH  # noqa: E402
from config.runtime_paths import configurable_path  # noqa: E402
from data.live_manual_account import CONFIG_PATH as LIVE_ACCOUNT_CONFIG_PATH  # noqa: E402
from data.strategic_theme_pool import POOL_PATH as STRATEGIC_POOL_PATH  # noqa: E402
from data.watchlist_config import CONFIG_PATH as WATCHLIST_CONFIG_PATH  # noqa: E402


DEFAULT_DB = configurable_path("STOCK_DB_PATH", "data/stock_data.db")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trade_date_cutoff(conn: sqlite3.Connection, sessions: int) -> str:
    row = conn.execute(
        """SELECT date FROM (
               SELECT DISTINCT date FROM daily_prices ORDER BY date DESC
           ) LIMIT 1 OFFSET ?""",
        (max(1, sessions) - 1,),
    ).fetchone()
    if row:
        return str(row[0])
    fallback = conn.execute("SELECT MIN(date) FROM daily_prices").fetchone()[0]
    return str(fallback or "0000-00-00")


def _query_codes(conn: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> set[str]:
    return {
        str(row[0]).strip().zfill(6)
        for row in conn.execute(sql, tuple(params))
        if str(row[0] or "").strip()
    }


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def collect_scope(source_db: Path) -> tuple[dict[str, set[str]], dict[str, str]]:
    conn = sqlite3.connect(source_db)
    conn.row_factory = sqlite3.Row
    try:
        latest_board = conn.execute(
            """SELECT as_of FROM candidate_board_runs
               WHERE status='ready' ORDER BY as_of DESC LIMIT 1"""
        ).fetchone()
        categories: dict[str, set[str]] = {
            "simulated_holdings": _query_codes(
                conn, "SELECT code FROM portfolio WHERE COALESCE(volume, 0) > 0",
            ),
            "live_holdings": _query_codes(
                conn,
                """SELECT code FROM live_trade_intents WHERE status='filled'
                   GROUP BY code HAVING SUM(
                       CASE WHEN action='buy' THEN COALESCE(NULLIF(filled_volume, 0), suggested_volume, 0)
                            WHEN action='sell' THEN -COALESCE(NULLIF(filled_volume, 0), suggested_volume, 0)
                            ELSE 0 END
                   ) > 0""",
            ),
            "active_candidates": (
                _query_codes(
                    conn,
                    """SELECT code FROM candidate_board_members
                       WHERE as_of=? AND state='active'""",
                    (latest_board["as_of"],),
                )
                if latest_board else set()
            ),
            "trading_state": _query_codes(
                conn, "SELECT DISTINCT code FROM trading_stock_state",
            ),
            "pending_live_intents": _query_codes(
                conn,
                """SELECT DISTINCT code FROM live_trade_intents
                   WHERE status IN ('proposed', 'notified')""",
            ),
        }
        strategic_path = (
            STRATEGIC_POOL_PATH if STRATEGIC_POOL_PATH.exists()
            else ROOT / "config/strategic_theme_pool.example.json"
        )
        strategic = _load_json(strategic_path)
        categories["strategic_observations"] = {
            str(row[0]).strip().zfill(6)
            for group in strategic.get("groups", []) if isinstance(group, dict)
            for row in group.get("stocks", []) if isinstance(row, list) and row
            if str(row[0] or "").strip()
        }
        watchlist = _load_json(WATCHLIST_CONFIG_PATH)
        categories["watchlist"] = {
            str(item.get("code")).strip().zfill(6)
            for item in watchlist.get("items", []) if isinstance(item, dict) and item.get("code")
        }
        all_codes = set().union(*categories.values())
        names: dict[str, str] = {}
        if all_codes:
            placeholders = ",".join("?" for _ in all_codes)
            names.update({
                str(row["code"]).zfill(6): str(row["name"] or "")
                for row in conn.execute(
                    f"SELECT code,name FROM stocks WHERE code IN ({placeholders})",
                    sorted(all_codes),
                )
            })
        return categories, names
    finally:
        conn.close()


def _copy_database(
    source_db: Path,
    target_db: Path,
    *,
    scope_codes: set[str],
    market_sessions: int,
    scope_sessions: int,
) -> dict[str, object]:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    if target_db.exists():
        raise FileExistsError(target_db)
    conn = sqlite3.connect(target_db)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-262144")
        conn.execute("ATTACH DATABASE ? AS source", (str(source_db),))
        conn.execute("BEGIN")
        source_user_version = conn.execute("PRAGMA source.user_version").fetchone()[0]
        conn.execute(f"PRAGMA user_version={int(source_user_version)}")

        objects = conn.execute(
            """SELECT type,name,tbl_name,sql FROM source.sqlite_master
               WHERE sql IS NOT NULL
               ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1
                                  WHEN 'trigger' THEN 2 WHEN 'view' THEN 3 ELSE 4 END,
                        name"""
        ).fetchall()
        table_objects = [row for row in objects if row[0] == "table" and row[1] != "sqlite_sequence"]
        deferred_objects = [row for row in objects if row[0] in {"index", "trigger", "view"}]
        for _kind, _name, _table, ddl in table_objects:
            conn.execute(ddl)

        source = sqlite3.connect(source_db)
        try:
            market_cutoff = _trade_date_cutoff(source, market_sessions)
            scope_cutoff = _trade_date_cutoff(source, scope_sessions)
        finally:
            source.close()

        copied_rows: dict[str, int] = {}
        for _kind, table_name, _table, _ddl in table_objects:
            quoted = _quote_identifier(table_name)
            if table_name == "daily_prices":
                params: list[object] = [market_cutoff]
                where = "date >= ?"
                if scope_codes:
                    placeholders = ",".join("?" for _ in scope_codes)
                    where += f" OR (date >= ? AND code IN ({placeholders}))"
                    params.extend([scope_cutoff, *sorted(scope_codes)])
                conn.execute(
                    f"INSERT INTO main.{quoted} SELECT * FROM source.{quoted} WHERE {where}",
                    params,
                )
            else:
                conn.execute(f"INSERT INTO main.{quoted} SELECT * FROM source.{quoted}")
            copied_rows[table_name] = int(
                conn.execute(f"SELECT COUNT(*) FROM main.{quoted}").fetchone()[0]
            )

        for _kind, _name, _table, ddl in deferred_objects:
            conn.execute(ddl)
        conn.commit()
        conn.execute("DETACH DATABASE source")
        conn.execute("ANALYZE")
        conn.execute("PRAGMA optimize")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"exported database integrity check failed: {integrity}")
        min_date, max_date = conn.execute(
            "SELECT MIN(date), MAX(date) FROM daily_prices"
        ).fetchone()
        return {
            "market_sessions": market_sessions,
            "market_cutoff": market_cutoff,
            "scope_sessions": scope_sessions,
            "scope_cutoff": scope_cutoff,
            "daily_min_date": min_date,
            "daily_max_date": max_date,
            "copied_rows": copied_rows,
            "integrity_check": integrity,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _scope_payload(categories: dict[str, set[str]], names: dict[str, str]) -> dict:
    def rows(codes: set[str]) -> list[dict[str, str]]:
        return [{"code": code, "name": names.get(code, "")} for code in sorted(codes)]

    union = set().union(*categories.values())
    return {
        "union_count": len(union),
        "union": rows(union),
        "categories": {
            name: {"count": len(codes), "stocks": rows(codes)}
            for name, codes in sorted(categories.items())
        },
    }


def _private_configs(*, include_runtime: bool) -> list[tuple[str, Path]]:
    values = [
        ("live_manual_account", LIVE_ACCOUNT_CONFIG_PATH),
        ("watchlist", WATCHLIST_CONFIG_PATH),
        ("strategic_theme_pool", STRATEGIC_POOL_PATH),
        ("reconcile_resolved_issues", DEFAULT_RESOLVED_ISSUES_PATH),
    ]
    if include_runtime:
        values.append((
            "runtime",
            configurable_path("STOCK_RUNTIME_CONFIG", "config/runtime.local.json"),
        ))
    return values


def _write_restore_readme(path: Path, package_name: str, included_configs: list[str]) -> None:
    configs = "\n".join(f"- `{name}`" for name in included_configs) or "- none"
    path.write_text(
        f"""# Stock migration snapshot

This private package contains a compact SQLite snapshot and local operational
configuration. It intentionally contains no Git history, Codex login, lark-cli
login, API token or user keyring.

Included local configs:

{configs}

## Restore

1. Check out the same or a newer `stock-agent` code version on the target host.
2. Stop the Web, listener and scheduled jobs so no process writes the database.
3. Back up any existing `data/stock_data.db` and `config/*.local.json` files.
4. Copy this directory's `data/stock_data.db` and `config/` files into the
   project root, preserving mode 0600.
5. Recreate `.venv`, install `requirements.txt`, and run:

   ```bash
   .venv/bin/python -c "from data.store.sqlite_store import StockStore; StockStore()"
   .venv/bin/python scripts/sync_baostock_basic.py --verify-only
   ```

6. Re-authenticate Codex and lark-cli on the new host. Restore the IwenCai key
   through `IWENCAI_API_KEY` or `~/.config/stock/iwencai_api_keys`; it is not in
   this archive.
7. Start the Web first and compare positions/candidates with `scope.json` before
   enabling cron or the Feishu listener.

Package directory: `{package_name}`
""",
        encoding="utf-8",
    )


def export_snapshot(
    *,
    source_db: Path,
    output_dir: Path,
    market_sessions: int,
    scope_sessions: int,
    include_runtime_config: bool,
) -> dict[str, object]:
    source_db = source_db.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not source_db.is_file() or source_db.stat().st_size == 0:
        raise FileNotFoundError(f"source database is missing or empty: {source_db}")
    if market_sessions < 60:
        raise ValueError("market_sessions must be at least 60 for current technical indicators")
    if scope_sessions < market_sessions:
        raise ValueError("scope_sessions must be greater than or equal to market_sessions")

    categories, names = collect_scope(source_db)
    scope = _scope_payload(categories, names)
    scope_codes = {row["code"] for row in scope["union"]}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    package_name = f"stock-migration-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{package_name}.tar.zst"
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    if archive_path.exists() or checksum_path.exists():
        raise FileExistsError(archive_path)

    with tempfile.TemporaryDirectory(prefix="stock-migration-export-") as temporary:
        package_root = Path(temporary) / package_name
        database_path = package_root / "data/stock_data.db"
        database_stats = _copy_database(
            source_db,
            database_path,
            scope_codes=scope_codes,
            market_sessions=market_sessions,
            scope_sessions=scope_sessions,
        )
        database_path.chmod(0o600)

        config_dir = package_root / "config"
        included_configs: list[str] = []
        for config_name, source_path in _private_configs(include_runtime=include_runtime_config):
            if not source_path.is_file():
                continue
            config_dir.mkdir(parents=True, exist_ok=True)
            target_path = config_dir / source_path.name
            shutil.copy2(source_path, target_path)
            target_path.chmod(0o600)
            included_configs.append(source_path.name)

        (package_root / "scope.json").write_text(
            json.dumps(scope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        manifest = {
            "format": "stock-migration.v1",
            "generated_at": datetime.now().astimezone().isoformat(),
            "source_db_bytes": source_db.stat().st_size,
            "exported_db_bytes": database_path.stat().st_size,
            "exported_db_sha256": _sha256(database_path),
            "scope_union_count": scope["union_count"],
            "scope_category_counts": {
                name: details["count"] for name, details in scope["categories"].items()
            },
            "database": database_stats,
            "included_configs": included_configs,
            "excluded_credentials": [
                "Codex login", "lark-cli login", "IWENCAI_API_KEY", "user keyrings",
            ],
        }
        (package_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        _write_restore_readme(package_root / "MIGRATION_README.md", package_name, included_configs)

        if shutil.which("zstd"):
            subprocess.run(
                ["tar", "--zstd", "-cf", str(archive_path), "-C", temporary, package_name],
                check=True,
            )
        else:
            archive_path = output_dir / f"{package_name}.tar.gz"
            checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(package_root, arcname=package_name)

    archive_path.chmod(0o600)
    digest = _sha256(archive_path)
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    checksum_path.chmod(0o600)
    return {
        "archive": str(archive_path),
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": digest,
        "checksum": str(checksum_path),
        "scope_union_count": scope["union_count"],
        "scope_category_counts": {
            name: details["count"] for name, details in scope["categories"].items()
        },
        "database": database_stats,
        "included_configs": included_configs,
        "runtime_config_included": include_runtime_config,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a compact stock-server migration package")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument(
        "--market-sessions", type=int, default=120,
        help="recent sessions retained for the whole market (minimum 60)",
    )
    parser.add_argument(
        "--scope-sessions", type=int, default=500,
        help="recent sessions retained for holdings/candidates/observations",
    )
    parser.add_argument(
        "--include-runtime-config", action="store_true",
        help="include runtime.local.json with Feishu IDs (credentials remain excluded)",
    )
    args = parser.parse_args()
    try:
        result = export_snapshot(
            source_db=args.db,
            output_dir=args.output_dir,
            market_sessions=args.market_sessions,
            scope_sessions=args.scope_sessions,
            include_runtime_config=args.include_runtime_config,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
