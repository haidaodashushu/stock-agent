import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from data.candidate_board import refresh_candidate_board
from data.intraday_radar import (
    daily_setup,
    expected_volume_fraction,
    latest_radar_candidates,
    radar_expiry,
    save_radar_result,
    score_launch_candidate,
)
from data.opening_auction import save_auction_watch_candidates
from data.store.sqlite_store import StockStore
from data.strategic_theme_pool import load_strategic_pool, strategic_pool_codes
from data.trading_state import _screen_candidates
from scripts.scan_intraday_radar import scan
from scripts.run_scheduled_intraday_radar import TARGET_SLOTS


class StrategicPoolTest(unittest.TestCase):
    def test_default_or_example_pool_has_unique_tradeable_members(self):
        pool = load_strategic_pool()
        codes = strategic_pool_codes()
        self.assertEqual(pool["target_size"], len(codes))
        self.assertGreater(len(codes), 0)
        self.assertEqual(len(set(codes)), len(codes))
        self.assertFalse(any(code.startswith(("688", "8", "4")) for code in codes))
        self.assertEqual(sum(pool["group_counts"].values()), len(codes))

    def test_radar_has_lunch_close_and_afternoon_open_observations(self):
        self.assertIn((11, 30), TARGET_SLOTS)
        self.assertIn((13, 0), TARGET_SLOTS)
        self.assertFalse(any(hour == 12 for hour, _ in TARGET_SLOTS))


class RadarScoringTest(unittest.TestCase):
    def setUp(self):
        self.setup = {
            "available": True,
            "close": 10.0,
            "ret_5d_pct": 3.0,
            "ret_20d_pct": -8.0,
            "position_60d_pct": 25.0,
            "previous_volume_ratio_20d": 1.35,
            "range_compression_5v20": 0.8,
            "prior20_high": 10.45,
            "avg20_volume": 1_000_000,
        }

    def test_live_ignition_needs_multiple_evidence_families(self):
        result = score_launch_candidate(
            "600001",
            {
                "price": 10.5, "prev_close": 10.0, "open": 10.1, "high": 10.55,
                "change_pct": 5.0, "volume": 500_000,
            },
            self.setup,
            market_change_pct=0.5,
            volume_fraction=0.25,
            intraday={
                "above_vwap": True, "last_15m_pct": 0.8,
                "half_hour": {"available": True, "price_pct": 1.2, "volume_ratio": 1.3},
            },
            fund_flow={"status": "available", "detail": {"main_net_inflow": 20_000_000}},
            sector={"alignment": "positive"},
        )
        self.assertGreaterEqual(result["score"], 5.5)
        self.assertTrue(result["actionable"])
        self.assertTrue({"price", "volume", "intraday"}.issubset(result["confirmation_families"]))

    def test_near_limit_or_large_pullback_is_not_actionable(self):
        near_limit = score_launch_candidate(
            "600001",
            {"price": 10.96, "prev_close": 10.0, "open": 10.2, "high": 10.96,
             "change_pct": 9.6, "volume": 500_000},
            self.setup, volume_fraction=0.25,
        )
        pullback = score_launch_candidate(
            "600001",
            {"price": 10.4, "prev_close": 10.0, "open": 10.2, "high": 10.9,
             "change_pct": 4.0, "volume": 500_000},
            self.setup, volume_fraction=0.25,
        )
        self.assertFalse(near_limit["actionable"])
        self.assertFalse(pullback["actionable"])

    def test_volume_curve_is_monotonic_across_trading_session(self):
        points = [
            datetime(2026, 8, 13, 9, 30), datetime(2026, 8, 13, 10, 0),
            datetime(2026, 8, 13, 11, 30), datetime(2026, 8, 13, 13, 30),
            datetime(2026, 8, 13, 15, 0),
        ]
        fractions = [expected_volume_fraction(point) for point in points]
        self.assertEqual(fractions, sorted(fractions))
        self.assertEqual(fractions[-1], 1.0)

    def test_daily_setup_uses_only_supplied_complete_bars(self):
        rows = []
        for index in range(30):
            close = 10 + index * 0.02
            rows.append({
                "date": f"2026-07-{index + 1:02d}", "open": close - 0.02,
                "high": close + 0.1, "low": close - 0.1, "close": close,
                "volume": 1_000_000 + index * 10_000,
            })
        setup = daily_setup(pd.DataFrame(rows))
        self.assertTrue(setup["available"])
        self.assertEqual(setup["as_of"], "2026-07-30")
        self.assertGreater(setup["prior20_high"], setup["close"])


class RadarPersistenceTest(unittest.TestCase):
    def test_late_morning_radar_expiry_skips_lunch_break(self):
        self.assertEqual(
            radar_expiry("2026-08-26 11:20:00", 40),
            "2026-08-26 13:30:00",
        )
        self.assertEqual(
            radar_expiry("2026-08-26 10:20:00", 40),
            "2026-08-26 11:00:00",
        )

    def test_only_unexpired_latest_run_is_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "radar.db"))
            now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
            old = (now - timedelta(minutes=50)).strftime("%Y-%m-%d %H:%M:%S")
            fresh = now.strftime("%Y-%m-%d %H:%M:%S")
            candidate = {
                "code": "002371", "name": "北方华创", "theme_group": "半导体与国产替代",
                "score": 7.0, "price": 100.0, "change_pct": 4.0,
                "triggers": ["成交速度放大"], "risk_tags": [],
                "evidence": {"radar_actionable": True},
            }
            save_radar_result(
                store, as_of=old, pool_size=100, quote_count=100,
                prefiltered_count=10, candidates=[candidate], market_context={},
            )
            self.assertEqual(latest_radar_candidates(store, now=now), [])
            save_radar_result(
                store, as_of=fresh, pool_size=100, quote_count=100,
                prefiltered_count=10, candidates=[candidate], market_context={},
            )
            rows = latest_radar_candidates(store, now=now)
            self.assertEqual([row["code"] for row in rows], ["002371"])
            self.assertEqual(rows[0]["triggers"], ["成交速度放大"])

    def test_raw_radar_observation_does_not_enter_formal_candidate_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "radar.db"))
            now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
            conn = store._get_conn()
            try:
                conn.execute(
                    """INSERT INTO screen_records
                       (run_date,run_time,code,name,price,score,signal_type,strategies,extra)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (now.date().isoformat(), "08:30:00", "000001", "平安银行", 10, 8, "buy", "盘前", json.dumps({
                        "selector": {"entry_route": "early_start"},
                        "ai_selection": {"rank": 1},
                    })),
                )
                conn.commit()
            finally:
                conn.close()
            candidate = {
                "code": "002371", "name": "北方华创", "theme_group": "半导体与国产替代",
                "score": 7.0, "price": 100.0, "change_pct": 4.0,
                "triggers": ["成交速度放大", "站上VWAP"], "risk_tags": [],
                "evidence": {
                    "radar_actionable": True, "metrics": {"volume_pace_20d": 1.8},
                    "setup": {"position_60d_pct": 50},
                },
            }
            save_radar_result(
                store, as_of=now.strftime("%Y-%m-%d %H:%M:%S"), pool_size=100,
                quote_count=100, prefiltered_count=10, candidates=[candidate], market_context={},
            )
            created = refresh_candidate_board(store, now=now)
            unchanged = refresh_candidate_board(store, now=now + timedelta(minutes=1))
            self.assertEqual(created["status"], "ready")
            self.assertEqual(unchanged["status"], "unchanged")
            with patch("data.candidate_board.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = now
                rows = _screen_candidates(store, now.date().isoformat())
            self.assertEqual({row["code"] for row in rows}, {"000001"})
            self.assertTrue(rows[0]["extra"]["selector"]["buy_eligible"])

    def test_raw_auction_observation_cannot_replace_daily_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "radar.db"))
            now = datetime(2026, 8, 20, 9, 35)
            conn = store._get_conn()
            try:
                conn.executemany(
                    """INSERT INTO screen_records
                       (run_date,run_time,code,name,price,score,signal_type,strategies,extra)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    [
                        (now.date().isoformat(), "08:30:00", f"{index:06d}",
                         f"候选{index}", 10, index, "watch", "盘前", json.dumps({
                             "selector": {"entry_route": "early_start"},
                             "ai_selection": {"rank": 13-index},
                         }))
                        for index in range(1, 13)
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            save_auction_watch_candidates(
                store, trade_date=now.date().isoformat(), generated_at="2026-08-20 09:28:00",
                candidates=[{
                    "code": "000021", "name": "深科技", "theme_group": "半导体与国产替代",
                    "score": 7.0, "price": 10.2, "change_pct": 2.0,
                    "triggers": ["竞价匹配价增强"],
                    "risk_tags": ["仅进入观察候选，尚未获得买入资格"],
                    "evidence": {
                        "source": "strategic_pool_opening_auction_watch",
                        "candidate_only": True, "radar_actionable": False,
                    },
                }],
            )
            created = refresh_candidate_board(store, now=now)
            unchanged = refresh_candidate_board(store, now=now + timedelta(minutes=1))
            self.assertEqual(created["status"], "ready")
            self.assertEqual(unchanged["status"], "unchanged")
            with patch("data.candidate_board.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = now
                rows = _screen_candidates(store, now.date().isoformat())
            codes = {row["code"] for row in rows}
            self.assertEqual(len(rows), 12)
            self.assertNotIn("000021", codes)
            self.assertIn("000001", codes)
            conn = store._get_conn()
            try:
                incoming = conn.execute(
                    """SELECT replaced_code,replacement_reason FROM candidate_board_members
                        WHERE as_of=? AND code='000021'""",
                    (created["as_of"],),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNone(incoming)

    def test_scanner_promotes_only_confirmed_short_lived_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "radar.db"))
            pool = load_strategic_pool()
            codes = list(strategic_pool_codes())
            setups = {
                code: {
                    "available": True, "close": 10.0, "ret_5d_pct": 1.0,
                    "ret_20d_pct": -5.0, "position_60d_pct": 25.0,
                    "previous_volume_ratio_20d": 1.3, "range_compression_5v20": 0.8,
                    "prior20_high": 10.45, "avg20_volume": 1_000_000,
                }
                for code in codes
            }
            quotes = {
                code: {
                    "code": code, "name": pool["stocks"][code]["name"], "price": 10.0,
                    "prev_close": 10.0, "open": 10.0, "high": 10.0, "low": 9.9,
                    "change_pct": 0.0, "volume": 100_000, "amount": 1_000_000,
                }
                for code in codes
            }
            target = codes[0]
            quotes[target].update({
                "price": 10.5, "open": 10.1, "high": 10.55, "change_pct": 5.0,
                "volume": 500_000,
            })
            with (
                patch("scripts.scan_intraday_radar.StockStore", return_value=store),
                patch("scripts.scan_intraday_radar.fetch_quotes", return_value=quotes),
                patch("scripts.scan_intraday_radar.load_daily_setups", return_value=setups),
                patch("scripts.scan_intraday_radar.fetch_market_indices", return_value={
                    "sh000001": {"change_pct": 0.5},
                }),
                patch("scripts.scan_intraday_radar._minute_states", return_value={
                    target: {"above_vwap": True, "last_15m_pct": 0.5,
                             "half_hour": {"available": True, "price_pct": 1.0, "volume_ratio": 1.2}},
                }),
                patch("scripts.scan_intraday_radar._fund_flows", return_value={
                    target: {"status": "available", "detail": {"main_net_inflow": 10_000_000}},
                }),
                patch("scripts.scan_intraday_radar._sector_state", return_value={
                    "status": "available", "created_at": "2026-08-13 09:59:00", "signals": [],
                }),
                patch("scripts.scan_intraday_radar._ensure_sector_memberships", return_value={}),
                patch("scripts.scan_intraday_radar.SectorRotationService.get_stock_contexts",
                      return_value={target: {"alignment": "positive"}}),
                patch("scripts.scan_intraday_radar._news", return_value=({target: []}, [])),
            ):
                report = scan(datetime(2026, 8, 13, 10, 0))
            self.assertEqual(report["selected_count"], 1)
            self.assertEqual(report["candidates"][0]["code"], target)
            self.assertEqual(len(latest_radar_candidates(
                store, now=datetime(2026, 8, 13, 10, 0),
            )), 1)


if __name__ == "__main__":
    unittest.main()
