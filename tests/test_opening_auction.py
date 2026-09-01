import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from data.opening_auction import (
    build_auction_watch_candidates,
    fetch_iwencai_auction_final,
    latest_auction_watch_candidates,
    observation_coverage,
    parse_iwencai_auction_final,
    parse_tencent_auction_line,
    save_auction_run,
    save_auction_watch_candidates,
    save_iwencai_final,
    save_tencent_snapshots,
)
from data.store.sqlite_store import StockStore


class OpeningAuctionParsingTest(unittest.TestCase):
    def test_tencent_snapshot_keeps_raw_book_without_claiming_auction_semantics(self):
        fields = [""] * 88
        fields[1:7] = ["测试股份", "000001", "10.20", "10.00", "10.15", "1234"]
        fields[9:29] = ["10.20", "100", "10.19", "80", "10.18", "60", "10.17", "40", "10.16", "20",
                           "10.21", "90", "10.22", "70", "10.23", "50", "10.24", "30", "10.25", "10"]
        fields[30] = "20260814091950"
        fields[37] = "345.67"
        row = parse_tencent_auction_line(
            'v_sz000001="' + "~".join(fields) + '";',
            phase="cancelable_end", observed_at="2026-08-14 09:19:50",
        )
        self.assertEqual(row["code"], "000001")
        self.assertEqual(row["bid_levels"][0]["volume_raw"], 100)
        self.assertEqual(row["ask_levels"][0]["price"], 10.21)
        self.assertEqual(row["raw_fields"][30], "20260814091950")
        self.assertTrue(row["provider_current"])

    def test_iwencai_final_parses_dated_keys_and_signed_unmatched_value(self):
        row = parse_iwencai_auction_final({
            "股票代码": "300996.SZ", "股票简称": "普联软件",
            "竞价涨幅[20260813]": -1.668352, "竞价匹配价[20260813]": 19.45,
            "竞价匹配量[20260813]": 1790517, "竞价匹配金额[20260813]": 34825555.65,
            "竞价未匹配量[20260813]": -11683, "竞价未匹配金额[20260813]": -227234.35,
            "竞价异动类型[20260813]": "竞价抢筹", "竞价评级[20260813]": "看多",
        }, trade_date="2026-08-13", observed_at="2026-08-13 09:27:00")
        self.assertEqual(row["code"], "300996")
        self.assertEqual(row["matched_amount_yuan"], 34825555.65)
        self.assertEqual(row["unmatched_volume_signed"], -11683)


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def query_raw(self, query, *, skill_id, limit):
        self.calls.append((query, skill_id, limit))
        codes = query.split(" ", 1)[0].split(",")
        return {"datas": [{
            "股票代码": f"{code}.SZ", "股票简称": code,
            "竞价匹配价[20260814]": 10, "竞价匹配金额[20260814]": 1000,
        } for code in codes]}


class OpeningAuctionFetchAndPersistenceTest(unittest.TestCase):
    def test_iwencai_is_batched_at_five(self):
        adapter = FakeAdapter()
        rows, errors = fetch_iwencai_auction_final(
            [f"000{i:03d}" for i in range(12)], trade_date="2026-08-14", adapter=adapter,
        )
        self.assertEqual([call[2] for call in adapter.calls], [5, 5, 2])
        self.assertEqual(len(rows), 12)
        self.assertEqual(errors, [])

    def test_observation_tables_are_separate_and_report_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "auction.db"))
            snapshot = {
                "trade_date": "2026-08-14", "phase": "final", "code": "000001", "name": "测试",
                "observed_at": "2026-08-14 09:25:05", "provider_time": "20260814092505",
                "provider_current": True,
                "previous_close": 10, "last_price": 10.2, "open_price": 10.2,
                "reported_volume_raw": 100, "reported_amount_raw": 20,
                "bid_levels": [], "ask_levels": [], "raw_fields": [], "source": "tencent",
            }
            final = {
                "trade_date": "2026-08-14", "code": "000001", "name": "测试",
                "auction_price": 10.2, "auction_change_pct": 2, "matched_volume_shares": 10000,
                "matched_amount_yuan": 102000, "unmatched_volume_signed": 100,
                "unmatched_amount_signed": 1020, "anomaly_type": "", "anomaly_note": "",
                "rating": "", "observed_at": "2026-08-14 09:27:00",
                "raw_payload": {"x": 1}, "source": "iwencai",
            }
            save_tencent_snapshots(store, [snapshot])
            save_iwencai_final(store, [final])
            save_auction_run(
                store, trade_date="2026-08-14", phase="final", status="ready", scope_count=1,
                tencent_count=1, iwencai_count=1, started_at="2026-08-14 09:25:05",
                completed_at="2026-08-14 09:27:01",
            )
            coverage = observation_coverage(store)
            self.assertEqual(coverage[0]["final_iwencai_count"], 1)
            conn = store._get_conn()
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM opening_auction_final").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM intraday_radar_candidates").fetchone()[0], 0)
            finally:
                conn.close()

    def test_conservative_watch_candidate_is_short_lived_and_not_buy_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StockStore(str(Path(directory) / "auction.db"))
            snapshot = {
                "trade_date": "2026-08-20", "phase": "cancelable_end", "code": "000021",
                "name": "深科技", "observed_at": "2026-08-20 09:19:50",
                "provider_time": "20260820091950", "provider_current": True,
                "previous_close": 10, "last_price": 10.08, "open_price": 0,
                "reported_volume_raw": 100, "reported_amount_raw": 20,
                "bid_levels": [], "ask_levels": [], "raw_fields": [], "source": "tencent",
            }
            final = {
                "trade_date": "2026-08-20", "code": "000021", "name": "深科技",
                "auction_price": 10.2, "auction_change_pct": 2.0,
                "matched_volume_shares": 500000, "matched_amount_yuan": 5_100_000,
                "unmatched_volume_signed": 100, "unmatched_amount_signed": 1020,
                "anomaly_type": "竞价抢筹", "anomaly_note": "", "rating": "看多",
                "observed_at": "2026-08-20 09:27:00", "raw_payload": {}, "source": "iwencai",
            }
            save_tencent_snapshots(store, [snapshot])
            save_iwencai_final(store, [final])
            pool = {
                "version": 1,
                "stocks": {
                    "000021": {
                        "code": "000021", "name": "测试观察股", "group": "测试主题",
                    },
                },
            }
            with patch("data.opening_auction.load_strategic_pool", return_value=pool):
                candidates = build_auction_watch_candidates(store, trade_date="2026-08-20")
            self.assertEqual([row["code"] for row in candidates], ["000021"])
            self.assertFalse(candidates[0]["evidence"]["radar_actionable"])
            self.assertTrue(candidates[0]["evidence"]["candidate_only"])

            save_auction_watch_candidates(
                store, trade_date="2026-08-20", generated_at="2026-08-20 09:28:00",
                candidates=candidates,
            )
            fresh = latest_auction_watch_candidates(
                store, now=datetime(2026, 8, 20, 9, 35),
            )
            expired = latest_auction_watch_candidates(
                store, now=datetime(2026, 8, 20, 10, 1),
            )
            self.assertEqual([row["code"] for row in fresh], ["000021"])
            self.assertEqual(expired, [])
