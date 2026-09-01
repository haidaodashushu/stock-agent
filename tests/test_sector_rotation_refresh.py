import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from data.services import sector_rotation_service as sector_module
from data.services.sector_rotation_service import SectorRotationService, SectorRotationSignal
from data.store.sqlite_store import StockStore


class SectorRotationRefreshTests(unittest.TestCase):
    def test_cache_age_uses_observation_time_instead_of_file_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "sector.json"
            cache.write_text(
                json.dumps({
                    "created_at": (datetime.now() - timedelta(hours=2)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "source": "iwencai",
                    "signals": [{"name": "旧板块", "score": 9}],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch.object(sector_module, "CACHE_PATH", cache):
                self.assertIsNone(SectorRotationService(cache_minutes=25)._read_cache())

    def test_failed_refresh_is_explicit_and_does_not_replace_last_good_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "sector.json"
            previous = {
                "created_at": "2026-07-28 08:40:00",
                "source": "iwencai",
                "status": "available",
                "signals": [{"name": "旧板块", "score": 9}],
            }
            cache.write_text(json.dumps(previous, ensure_ascii=False), encoding="utf-8")
            service = SectorRotationService(cache_minutes=25)
            failed = {
                key: {"datas": [], "error": f"{key} unavailable"}
                for key in ("six_month", "three_month", "one_month", "five_day", "fund", "low_recent")
            }
            with (
                patch.object(sector_module, "CACHE_PATH", cache),
                patch.object(service, "_fetch_raw", return_value=failed),
            ):
                snapshot = service.get_snapshot(refresh=True)

            self.assertEqual(snapshot["status"], "unavailable")
            self.assertEqual(snapshot["signals"], [])
            self.assertIn("unavailable", snapshot["error"])
            self.assertEqual(json.loads(cache.read_text(encoding="utf-8")), previous)

    def test_candidate_concept_memberships_map_rotation_to_multiple_stocks(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            store = StockStore(handle.name)
            conn = store._get_conn()
            conn.executemany(
                "INSERT INTO stocks(code,name,industry,is_active) VALUES (?,?,?,1)",
                [
                    ("000001", "测试一", "软件开发"),
                    ("000002", "测试二", "电力设备"),
                ],
            )
            conn.executemany(
                "INSERT INTO concepts(name,stocks) VALUES (?,?)",
                [("人工智能", "000001"), ("储能", "000002")],
            )
            conn.commit()
            conn.close()

            service = SectorRotationService(store=store)
            signals = [
                SectorRotationSignal(name="人工智能", score=3.0, stage="leader"),
                SectorRotationSignal(name="储能", score=2.4, stage="accelerating"),
            ]
            snapshot = {
                "created_at": "2026-07-29 10:00:00",
                "source": "test",
                "status": "available",
                "signals": [signal.to_dict() for signal in signals],
            }
            with patch.object(service, "get_snapshot", return_value=snapshot):
                boosts = service.get_stock_boosts(["000001", "000002"])

            self.assertEqual(set(boosts), {"000001", "000002"})
            self.assertGreater(boosts["000001"][0], 0)
            self.assertGreater(boosts["000002"][0], 0)

    def test_membership_match_is_exact_or_explicit_alias_not_substring(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            store = StockStore(handle.name)
            conn = store._get_conn()
            conn.executemany(
                "INSERT INTO stocks(code,name,industry,is_active) VALUES (?,?,?,1)",
                [
                    ("000001", "汽车公司", "汽车"),
                    ("000002", "光模块公司", "通信设备"),
                ],
            )
            conn.executemany(
                "INSERT INTO concepts(name,stocks) VALUES (?,?)",
                [("CPO光模块", "000002")],
            )
            conn.commit()
            conn.close()
            snapshot = {
                "created_at": "2026-07-29 10:00:00",
                "source": "test",
                "status": "available",
                "signals": [
                    SectorRotationSignal(name="汽车电子", score=3, stage="leader").to_dict(),
                    SectorRotationSignal(name="CPO", score=2, stage="accelerating").to_dict(),
                ],
            }
            contexts = SectorRotationService(store=store).get_stock_contexts(
                ["000001", "000002"], snapshot=snapshot,
            )

            self.assertEqual(contexts["000001"]["matches"], [])
            self.assertEqual(contexts["000001"]["alignment"], "neutral")
            self.assertEqual(contexts["000002"]["matches"][0]["name"], "CPO")
            self.assertEqual(contexts["000002"]["matches"][0]["match_type"], "alias")


if __name__ == "__main__":
    unittest.main()
