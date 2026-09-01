import tempfile
import unittest
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from data.candidate_board import refresh_candidate_board
from data.candidate_lifecycle import load_lifecycle_snapshot
from data.stock_selection_repository import stage_candidate_pool
from data.store.sqlite_store import StockStore
from data.trading_state import _validate_candidate_board_scope
from strategy.selector.technical_scoring import TechnicalScoringSelector


def discovery(
    code: str,
    *,
    route: str = "early_start",
    stage: str = "actionable",
    score: float = 7.0,
    setup_score: float = 5.0,
) -> dict:
    return {
        "code": code,
        "name": f"测试{code}",
        "price": 10.0,
        "score": score,
        "final_score": score,
        "signal_type": "buy" if stage == "actionable" else "watch",
        "trend": "偏多",
        "pct_change": 1.0,
        "vol_ratio": 1.2,
        "position_pct": 0.3,
        "zone": "低位",
        "route": "low_logic",
        "entry_route": route,
        "setup_stage": stage,
        "setup_score": setup_score,
        "setup_triggers": "波动收敛|低点抬高|MA5上行",
        "setup_risks": "",
        "buy_eligible": stage == "actionable",
        "risk_tags": "",
    }


def stage(
    store: StockStore,
    rows: list[dict],
    *,
    as_of: str,
    evidence_date: str,
    target: str,
) -> None:
    stage_candidate_pool(
        rows,
        run_date=target,
        run_time="08:30:00",
        run_label="测试选股",
        target="today",
        expected_daily_date=evidence_date,
        generated_at=as_of,
        store=store,
    )


class CandidateLifecycleTests(unittest.TestCase):
    def test_early_start_requires_two_complete_bar_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            stage(
                store, [discovery("000001")],
                as_of="2026-08-20 22:45:00.000001",
                evidence_date="2026-08-20", target="2026-08-21",
            )
            conn = store._get_conn()
            first = conn.execute(
                "SELECT * FROM candidate_lifecycle WHERE code='000001'"
            ).fetchone()
            conn.close()
            self.assertEqual(first["state"], "warming")
            self.assertEqual(first["observation_sessions"], 1)
            self.assertFalse(first["buy_eligible"])

            stage(
                store, [discovery(
                    "000001", stage="warming", score=7.2, setup_score=4.8,
                )],
                as_of="2026-08-21 22:45:00.000001",
                evidence_date="2026-08-21", target="2026-08-24",
            )
            conn = store._get_conn()
            second = conn.execute(
                "SELECT * FROM candidate_lifecycle WHERE code='000001'"
            ).fetchone()
            conn.close()
            self.assertEqual(second["state"], "actionable")
            self.assertEqual(second["observation_sessions"], 2)
            self.assertEqual(second["improving_streak"], 0)
            self.assertFalse(second["buy_eligible"])

    def test_same_complete_bar_does_not_age_or_promote_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            stage(
                store, [discovery("000001")],
                as_of="2026-08-20 22:45:00.000001",
                evidence_date="2026-08-20", target="2026-08-21",
            )
            stage(
                store, [discovery("000001", score=7.2)],
                as_of="2026-08-21 08:30:00.000001",
                evidence_date="2026-08-20", target="2026-08-21",
            )
            conn = store._get_conn()
            row = conn.execute(
                "SELECT observation_sessions,state FROM candidate_lifecycle WHERE code='000001'"
            ).fetchone()
            conn.close()
            self.assertEqual(row["observation_sessions"], 1)
            self.assertEqual(row["state"], "warming")

    def test_missing_candidate_cools_then_expires_instead_of_vanishing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            stage(
                store, [discovery("000001")],
                as_of="2026-08-17 08:30:00.000001",
                evidence_date="2026-08-14", target="2026-08-17",
            )
            for index, evidence_date in enumerate(("2026-08-17", "2026-08-18", "2026-08-19"), 1):
                stage(
                    store, [discovery("000002", route="strong_continuation")],
                    as_of=f"{evidence_date} 22:45:00.000001",
                    evidence_date=evidence_date, target=evidence_date,
                )
                conn = store._get_conn()
                row = conn.execute(
                    "SELECT state,stale_sessions FROM candidate_lifecycle WHERE code='000001'"
                ).fetchone()
                conn.close()
                self.assertEqual(row["stale_sessions"], index)
            self.assertEqual(row["state"], "expired")

    def test_lifecycle_observation_cannot_enter_formal_pool_without_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            stage(
                store, [discovery("000001", route="strong_continuation")],
                as_of="2026-08-24 22:45:00.000001",
                evidence_date="2026-08-24", target="2026-08-25",
            )
            now = datetime(2026, 8, 25, 8, 1)
            result = refresh_candidate_board(store, now=now)
            conn = store._get_conn()
            row = conn.execute(
                """SELECT state,buy_eligible,payload FROM candidate_board_members
                     WHERE as_of=? AND code='000001'""",
                (result["as_of"],),
            ).fetchone()
            conn.close()
            self.assertIsNone(row)
            self.assertEqual(result["active"], 0)

    def test_daily_final_selection_directly_qualifies_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            stage(
                store, [discovery("000001")],
                as_of="2026-08-24 22:45:00.000001",
                evidence_date="2026-08-24", target="2026-08-25",
            )
            conn = store._get_conn()
            conn.execute(
                """INSERT INTO screen_records
                   (run_date,run_time,code,name,price,score,signal_type,strategies,extra)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    "2026-08-25", "08:30:00", "000001", "测试一", 10, 8,
                    "buy", "低位趋势升温",
                    json.dumps({
                        "selector": {
                            "entry_route": "early_start", "setup_stage": "actionable",
                            "setup_score": 5.0, "buy_eligible": True,
                        },
                        "ai_selection": {"rank": 1},
                    }, ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()
            result = refresh_candidate_board(store, now=datetime(2026, 8, 25, 8, 31))
            conn = store._get_conn()
            row = conn.execute(
                """SELECT buy_eligible,payload FROM candidate_board_members
                     WHERE as_of=? AND code='000001'""",
                (result["as_of"],),
            ).fetchone()
            conn.close()
            self.assertTrue(row["buy_eligible"])
            payload = json.loads(row["payload"])
            self.assertTrue(payload["extra"]["selector"]["buy_eligible"])
            self.assertEqual(payload["extra"]["selector"]["setup_stage"], "actionable")

    def test_lifecycle_does_not_override_daily_final_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            rows = [discovery("000001", score=20)] + [
                discovery(
                    f"600{index:03d}", route="strong_continuation",
                    score=30 - index,
                )
                for index in range(1, 8)
            ]
            stage(
                store, rows,
                as_of="2026-08-24 22:45:00.000001",
                evidence_date="2026-08-24", target="2026-08-25",
            )
            conn = store._get_conn()
            conn.execute(
                """INSERT INTO screen_records
                   (run_date,run_time,code,name,price,score,signal_type,strategies,extra)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    "2026-08-25", "08:30:00", "000001", "测试一", 10, 20,
                    "buy", "低位趋势升温",
                    json.dumps({
                        "selector": {
                            "entry_route": "early_start", "setup_stage": "actionable",
                            "setup_score": 20.0, "buy_eligible": True,
                        },
                        "ai_selection": {"rank": 1},
                    }, ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()
            result = refresh_candidate_board(
                store, now=datetime(2026, 8, 25, 8, 31),
            )
            conn = store._get_conn()
            row = conn.execute(
                """SELECT buy_eligible,payload FROM candidate_board_members
                     WHERE as_of=? AND code='000001'""",
                (result["as_of"],),
            ).fetchone()
            conn.close()
            payload = json.loads(row["payload"])
            self.assertTrue(row["buy_eligible"])
            self.assertEqual(
                payload["extra"]["selector"]["setup_stage"], "actionable",
            )
            self.assertTrue(payload["extra"]["selector"]["buy_eligible"])
            self.assertIn("candidate_lifecycle", payload["extra"])
            self.assertEqual(payload["extra"]["candidate_lifecycle"]["state"], "warming")

    def test_lifecycle_snapshot_matches_the_board_consumed_by_trading(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            stage(
                store, [discovery("000001", route="strong_continuation")],
                as_of="2026-08-24 22:45:00.000001",
                evidence_date="2026-08-24", target="2026-08-25",
            )
            conn = store._get_conn()
            conn.execute(
                """INSERT INTO screen_records
                   (run_date,run_time,code,name,price,score,signal_type,strategies,extra)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    "2026-08-25", "08:30:00", "000001", "测试一", 10, 8,
                    "buy", "强势延续", json.dumps({
                        "selector": {"entry_route": "strong_continuation"},
                        "ai_selection": {"rank": 1},
                    }, ensure_ascii=False),
                ),
            )
            conn.commit()
            conn.close()
            refresh_candidate_board(store, now=datetime(2026, 8, 25, 8, 1))
            snapshot = load_lifecycle_snapshot(store)
            self.assertEqual(snapshot["strategy"], "candidate_observation.v2")
            self.assertEqual(snapshot["board"]["active_count"], 1)
            self.assertEqual(snapshot["candidates"][0]["code"], "000001")
            self.assertEqual(snapshot["candidates"][0]["board_state"], "active")
            self.assertEqual(snapshot["candidates"][0]["lifecycle_state"], "actionable")
            self.assertTrue(snapshot["candidates"][0]["buy_eligible"])

    def test_snapshot_keeps_raw_radar_stock_in_observation_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "stock.db"))
            conn = store._get_conn()
            conn.execute(
                """INSERT INTO candidate_board_runs
                   (as_of,trade_date,status,active_count,source_fingerprint)
                   VALUES ('2026-08-26 11:21:05','2026-08-26','ready',1,'board')"""
            )
            conn.execute(
                """INSERT INTO candidate_board_members
                   (as_of,state,rank,code,name,primary_source,source_types,
                    buy_eligible,expires_at,payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "2026-08-26 11:21:05", "active", 1, "000001", "测试一",
                    "daily_final_selection", '["daily_final_selection"]', 1, "",
                    json.dumps(discovery("000001"), ensure_ascii=False),
                ),
            )
            conn.execute(
                """INSERT INTO intraday_radar_candidates
                   (as_of,rank,code,name,theme_group,score,price,change_pct,triggers,risk_tags,evidence,expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "2026-08-26 10:50:00", 2, "300033", "同花顺",
                    "AI算力与数字科技", 8.0, 228.7, 6.14,
                    '["盘中价格加速","实时主力资金净流入"]', '[]', '{}',
                    "2026-08-26 11:30:00",
                ),
            )
            conn.commit()
            conn.close()

            pool = {
                "version": 1,
                "stocks": {
                    "300033": {
                        "code": "300033", "name": "测试观察股", "group": "测试主题",
                    },
                },
            }
            with patch("data.candidate_lifecycle.load_strategic_pool", return_value=pool):
                snapshot = load_lifecycle_snapshot(store)
            self.assertEqual(snapshot["candidates"][0]["code"], "000001")
            radar = next(row for row in snapshot["observations"] if row["code"] == "300033")
            self.assertEqual(radar["code"], "300033")
            self.assertEqual(radar["observation_source"], "intraday_radar")
            self.assertEqual(radar["signal_at"], "2026-08-26 10:50:00")
            self.assertIn("盘中价格加速", radar["triggers"])
            self.assertFalse(radar["buy_eligible"])

    def test_ready_nonempty_board_cannot_silently_become_empty_in_session(self):
        status = {"status": "ready", "active_count": 8}
        with self.assertRaisesRegex(RuntimeError, "active scope is empty"):
            _validate_candidate_board_scope(status, [], datetime(2026, 8, 25, 10, 0))
        # A maintenance smoke refresh after candidate expiry is valid and must
        # not be mistaken for a market-session scope fault.
        _validate_candidate_board_scope(status, [], datetime(2026, 8, 25, 21, 0))


class EntryRouteScoringTests(unittest.TestCase):
    @staticmethod
    def _frame(closes: list[float], volumes: list[float]) -> pd.DataFrame:
        return pd.DataFrame({
            "open": [value - 0.04 for value in closes],
            "high": [value + 0.02 for value in closes],
            "low": [value - 0.10 for value in closes],
            "close": closes,
            "volume": volumes,
        })

    def test_strong_continuation_is_not_rejected_for_high_position(self):
        closes = [10 + index * 0.10 for index in range(60)]
        result = TechnicalScoringSelector()._score_one(
            "000001", self._frame(closes, [1_000_000] * 60),
            include_enrichment=False,
        )
        self.assertEqual(result["entry_route"], "strong_continuation")
        self.assertEqual(result["setup_stage"], "actionable")
        self.assertTrue(result["buy_eligible"])

    def test_low_position_improving_structure_is_identified_before_large_rise(self):
        closes = [15 - index * 0.08 for index in range(45)]
        closes.extend([11.45, 11.40, 11.44, 11.42, 11.47, 11.45, 11.51, 11.49,
                       11.56, 11.54, 11.62, 11.60, 11.69, 11.67, 11.76])
        volumes = [1_000_000] * 45
        for index in range(15):
            volumes.append(1_300_000 if index % 2 == 0 else 700_000)
        result = TechnicalScoringSelector()._score_one(
            "000001", self._frame(closes, volumes), include_enrichment=False,
        )
        self.assertEqual(result["entry_route"], "early_start")
        self.assertIn(result["setup_stage"], {"warming", "actionable"})
        self.assertLessEqual(abs(result["pct_change"]), 2.0)
        self.assertIn("近期低点不再下移", result["setup_triggers"])

    def test_discovery_pool_reserves_capacity_for_both_entry_routes(self):
        selector = TechnicalScoringSelector()
        selector.set_param("top_n", 100)
        daily_data = {
            f"{index:06d}": pd.DataFrame({"close": [10.0] * 30})
            for index in range(160)
        }

        def fake_score(code, *_args, **_kwargs):
            index = int(code)
            early = index >= 80
            return {
                "code": code,
                "score": 3.5 if early else 10.0,
                "discovery_score": 6.0 if early else 10.0,
                "entry_route": "early_start" if early else "strong_continuation",
                "setup_stage": "warming" if early else "actionable",
                "setup_score": 5.0,
            }

        selector._score_one = fake_score
        result = selector.evaluate({
            "daily_data": daily_data,
            "include_enrichment": False,
            "score_floor": 4.0,
        })
        counts = result["entry_route"].value_counts().to_dict()
        self.assertEqual(len(result), 100)
        self.assertEqual(counts["early_start"], 60)
        self.assertEqual(counts["strong_continuation"], 40)


if __name__ == "__main__":
    unittest.main()
