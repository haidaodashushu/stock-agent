import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.export_migration_snapshot import _copy_database, _scope_payload


class MigrationSnapshotTests(unittest.TestCase):
    def test_database_copy_keeps_market_and_longer_scope_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            target = Path(directory) / "target.db"
            conn = sqlite3.connect(source)
            conn.executescript(
                """
                CREATE TABLE daily_prices (
                    code TEXT NOT NULL,
                    date TEXT NOT NULL,
                    close REAL,
                    PRIMARY KEY (code, date)
                );
                CREATE INDEX idx_daily_code_date ON daily_prices(code, date);
                CREATE TABLE account_state (id INTEGER PRIMARY KEY, cash REAL);
                INSERT INTO account_state VALUES (1, 12345);
                """
            )
            for day in range(1, 7):
                for code in ("000001", "000002", "000003"):
                    conn.execute(
                        "INSERT INTO daily_prices VALUES (?,?,?)",
                        (code, f"2026-01-{day:02d}", 10 + day),
                    )
            conn.commit()
            conn.close()

            result = _copy_database(
                source,
                target,
                scope_codes={"000001"},
                market_sessions=2,
                scope_sessions=4,
            )

            conn = sqlite3.connect(target)
            counts = dict(conn.execute(
                "SELECT code,COUNT(*) FROM daily_prices GROUP BY code ORDER BY code"
            ))
            cash = conn.execute("SELECT cash FROM account_state WHERE id=1").fetchone()[0]
            indexes = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
                )
            }
            conn.close()
            self.assertEqual(counts, {"000001": 4, "000002": 2, "000003": 2})
            self.assertEqual(cash, 12345)
            self.assertIn("idx_daily_code_date", indexes)
            self.assertEqual(result["market_cutoff"], "2026-01-05")
            self.assertEqual(result["scope_cutoff"], "2026-01-03")
            self.assertEqual(result["integrity_check"], "ok")

    def test_scope_payload_deduplicates_union_and_preserves_categories(self):
        payload = _scope_payload(
            {
                "holdings": {"000001", "000002"},
                "candidates": {"000002", "000003"},
            },
            {"000001": "甲", "000002": "乙", "000003": "丙"},
        )
        self.assertEqual(payload["union_count"], 3)
        self.assertEqual(payload["categories"]["holdings"]["count"], 2)
        self.assertEqual(
            [row["code"] for row in payload["union"]],
            ["000001", "000002", "000003"],
        )


if __name__ == "__main__":
    unittest.main()
