import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pandas as pd

from data.adapters.fuyao_adapter import FuyaoAdapter
from data import opportunity_trial as trial
from data.store.sqlite_store import StockStore
from data.trading_data_quality import summarize_minutes, reusable_minutes
from data.trading_decision_repository import _compact_stock
from tests.test_opportunity_trial import NOW, candidate, plan, quote


class MinuteCutoffTests(unittest.TestCase):
    def frame(self):
        start = NOW.replace(hour=9, minute=30)
        frame = pd.DataFrame([{"time": (start+timedelta(minutes=i)).strftime("%H%M"),
                              "price": 10., "volume": (i+1)*100., "amount": (i+1)*100000.}
                             for i in range(63)])
        frame.attrs["trading_date"] = "20260918"
        return frame

    def test_forming_tail_cannot_change_any_price_or_volume_statistic(self):
        frame = self.frame()
        now = NOW.replace(hour=10, minute=31, second=6)
        expected = summarize_minutes(frame.iloc[:61], now)
        # Both current (10:31) and next (10:32) labels must be excluded before
        # numeric validation or any high/low/VWAP/volume computation.
        frame.loc[61:, ["price", "volume", "amount"]] = [999., 1., 999999999.]
        result = summarize_minutes(frame, now)
        self.assertEqual(result["source_time"], "2026-09-18 10:30:00")
        self.assertEqual(result["excluded_incomplete_points"], 2)
        self.assertEqual(result["points"], 61)
        for key in ("half_hour", "vwap", "pullback_from_high_pct", "last_15m_pct", "above_vwap"):
            self.assertEqual(result[key], expected[key])
        exported = _compact_stock({"intraday": result}, now.isoformat(sep=" "))["intraday"]
        self.assertEqual(exported["time_policy"], result["time_policy"])
        self.assertEqual(exported["excluded_incomplete_points"], 2)

    def test_open_empty_future_and_stale_series_are_not_usable(self):
        frame = self.frame().iloc[:1]
        self.assertIn("no completed minute", summarize_minutes(frame, NOW.replace(hour=9, minute=30))["error"])
        self.assertIn("future", summarize_minutes(self.frame(), NOW.replace(hour=10, minute=20))["error"])
        self.assertIn("stale", summarize_minutes(frame, NOW)["error"])

    def test_cache_requires_new_policy_and_actual_source_age(self):
        now = NOW.replace(hour=10, minute=31, second=6)
        result = summarize_minutes(self.frame(), now)
        self.assertTrue(reusable_minutes(result, now))
        self.assertFalse(reusable_minutes(result, now+timedelta(minutes=5)))
        self.assertFalse(reusable_minutes(result, now-timedelta(days=1)))
        result.pop("time_policy")
        self.assertFalse(reusable_minutes(result, now))

    def test_lunch_keeps_trading_minute_window_without_artificial_gap(self):
        times = list(pd.date_range("2026-09-18 09:30", "2026-09-18 11:30", freq="min"))
        times += list(pd.date_range("2026-09-18 13:01", "2026-09-18 13:32", freq="min"))
        frame = pd.DataFrame([{"time": t.strftime("%H%M"), "price": 10.,
                              "volume": (i+1)*100., "amount": (i+1)*100000.} for i,t in enumerate(times)])
        frame.attrs["trading_date"] = "20260918"
        result = summarize_minutes(frame, NOW.replace(hour=13, minute=31, second=6))
        self.assertEqual(result["source_time"], "2026-09-18 13:30:00")
        self.assertFalse(result["has_gaps"])
        self.assertEqual(result["half_hour"]["volume_last30_vs_prev30"], 1)


class DueReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StockStore(str(Path(self.tmp.name)/"test.db"))
        trial.ingest(self.store, [candidate()], NOW)

    def record(self, at=NOW, mode="simulated"):
        row = {"code": "002185", "action": "watch", "watch_plan": plan(
            review_after_minutes=15, review_above=None, invalidation_below=None)}
        trial.record_decision(self.store, mode, {"signals" if mode=="simulated" else "decisions": [row]},
                              {"as_of": trial.stamp(at), "positions": []}, {}, at)

    def observe(self, at, holdings=None):
        trial.observe(self.store, "simulated", {"002185": quote(16, at)}, holdings or {}, at)

    def test_no_price_crossing_still_queues_once_at_due_and_renews_after_review(self):
        self.record()
        self.observe(NOW+timedelta(minutes=14))
        self.assertEqual(trial.claim_events(self.store, "simulated", NOW+timedelta(minutes=14)), [])
        due = NOW+timedelta(minutes=15)
        for _ in range(3): self.observe(due)
        rows = trial.claim_events(self.store, "simulated", due)
        self.assertEqual([r["kind"] for r in rows], ["review_due"])
        self.assertEqual(trial.claim_events(self.store, "live", due), [])
        trial.finish_events(self.store, rows, True)
        self.record(due)
        later = due+timedelta(minutes=15)
        self.observe(later)
        self.assertEqual([r["kind"] for r in trial.claim_events(self.store, "simulated", later)], ["review_due"])

    def test_trading_clock_crosses_lunch_weekend_and_protected_session_end(self):
        self.assertEqual(trial.next_review_at("2026-09-21 11:20:00", 15), "2026-09-21 13:05:00")
        self.assertEqual(trial.next_review_at("2026-09-21 11:10:00", 15), "2026-09-21 13:00:00")
        self.assertEqual(trial.next_review_at("2026-09-18 14:50:00", 15), "2026-09-21 09:35:00")
        self.assertEqual(trial.next_review_at("2026-09-21 14:40:00", 15), "2026-09-22 09:30:00")
        with patch.object(trial, "market_day", side_effect=lambda d: type("Day", (), {"is_open": d.date().isoformat()=="2026-09-23"})()):
            self.assertEqual(trial.next_review_at("2026-09-21 15:00:00", 15), "2026-09-23 09:45:00")

    def test_due_review_has_no_daily_quota_even_after_many_prior_batches(self):
        self.record()
        due = NOW+timedelta(minutes=15)
        self.observe(due)
        with self.store._get_conn() as conn:
            for i in range(17):
                at = trial.stamp(NOW-timedelta(minutes=40)+timedelta(seconds=i))
                conn.execute("""INSERT INTO opportunity_events
                    (mode,code,setup_id,kind,dedup,payload,created_at,status,batch_id)
                    VALUES('simulated','002189','history','review_due',?,'{}',?,'done',?)""",
                    (f"history-{i}",at,f"simulated:{at}"))
        rows = trial.claim_events(self.store, "simulated", due)
        self.assertEqual([r["kind"] for r in rows], ["review_due"])
        trial.finish_events(self.store, rows, True)
        self.record(due)
        self.observe(due+timedelta(minutes=15))
        self.assertEqual([r["kind"] for r in trial.claim_events(self.store, "simulated", due+timedelta(minutes=15))], ["review_due"])

    def test_due_review_still_respects_account_cooldown(self):
        self.record()
        due = NOW+timedelta(minutes=15)
        self.observe(due)
        at = trial.stamp(due-timedelta(minutes=1))
        with self.store._get_conn() as conn:
            conn.execute("""INSERT INTO opportunity_events
                (mode,code,setup_id,kind,dedup,payload,created_at,status,batch_id)
                VALUES('simulated','002189','history','review_due','recent','{}',?,'done',?)""",
                (at,f"simulated:{at}"))
        self.assertEqual(trial.claim_events(self.store, "simulated", due), [])
        self.assertEqual([r["kind"] for r in trial.claim_events(self.store, "simulated", due+timedelta(minutes=14))], ["review_due"])

    def test_holdings_are_prioritized_and_superseded_timers_do_not_replay(self):
        self.record()
        due = NOW+timedelta(minutes=15)
        self.observe(due, {"002185":100})
        rows = trial.claim_events(self.store, "simulated", due)
        self.assertEqual(rows[0]["kind"], "holding_review_due")
        trial.finish_events(self.store, rows, True)
        self.record(due)
        self.observe(due+timedelta(minutes=15), {"002185":100})
        # Represents a timer queued while the previous model was running.
        self.record(due+timedelta(minutes=10))
        self.assertEqual(trial.claim_events(self.store, "simulated", due+timedelta(minutes=16)), [])
        with self.store._get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunity_events WHERE error='review superseded'").fetchone()[0], 1)

    def test_holding_timers_merge_without_consuming_candidate_slots(self):
        codes = [f"00218{i}" for i in range(5)]
        trial.ingest(self.store, [candidate(c) for c in codes], NOW)
        for code in codes:
            row = {"code":code, "action":"watch", "watch_plan":plan(
                review_after_minutes=15, review_above=None, invalidation_below=None)}
            trial.record_decision(self.store, "simulated", {"signals":[row]},
                                  {"as_of":trial.stamp(NOW), "positions":[]}, {}, NOW)
        due = NOW+timedelta(minutes=15)
        trial.observe(self.store, "simulated", {c:quote(16,due) for c in codes},
                      {c:100 for c in codes[:4]}, due)
        cfg = trial.settings() | {"event_batch_size":1}
        with patch.object(trial, "settings", return_value=cfg):
            rows = trial.claim_events(self.store, "simulated", due)
        self.assertEqual(sum(r["kind"]=="holding_review_due" for r in rows), 4)
        self.assertEqual([r["code"] for r in rows if r["kind"]=="review_due"], [codes[-1]])


class FuyaoRotationTests(unittest.TestCase):
    def response(self):
        return io.BytesIO(json.dumps({"code":0, "data":{"thscode":"002185.SZ", "report":"2026-2", "abilities":[]}}).encode())

    def test_429_switches_key_and_persists_preference_without_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"state.json"
            adapter = FuyaoAdapter(api_keys=["first-secret", "second-secret"], state_path=path)
            with patch("data.adapters.fuyao_adapter.urllib.request.urlopen", side_effect=[HTTPError("url",429,"limited",{"Retry-After":"120"},None), self.response()]) as fetch:
                self.assertIsNotNone(adapter.financials("002185","2026-2"))
                self.assertEqual([x.args[0].get_header("X-api-key") for x in fetch.call_args_list], ["first-secret","second-secret"])
            with patch("data.adapters.fuyao_adapter.urllib.request.urlopen", return_value=self.response()) as fetch:
                FuyaoAdapter(api_keys=["first-secret","second-secret"],state_path=path).financials("002185","2026-2")
                self.assertEqual(fetch.call_args.args[0].get_header("X-api-key"), "second-secret")
            self.assertNotIn("secret", path.read_text())

    def test_both_limited_stop_and_cooldown_prevents_cross_instance_hammering(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"state.json"
            with patch("data.adapters.fuyao_adapter.time.time", return_value=1000), \
                 patch("data.adapters.fuyao_adapter.urllib.request.urlopen", side_effect=HTTPError("url",429,"limited",{},None)) as fetch:
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, "cooling down"):
                        FuyaoAdapter(api_keys=["a","b"],state_path=path).financials("002185","2026-2")
                self.assertEqual(fetch.call_count, 2)
            # Once cooled, either credential can be used again; no permanent ban.
            with patch("data.adapters.fuyao_adapter.time.time", return_value=1061), \
                 patch("data.adapters.fuyao_adapter.urllib.request.urlopen", return_value=self.response()) as fetch:
                FuyaoAdapter(api_keys=["a","b"],state_path=path).financials("002185","2026-2")
                fetch.assert_called_once()

    def test_rotation_can_return_to_first_key_after_its_cooldown(self):
        adapter = FuyaoAdapter(api_keys=["a", "b"])
        with patch("data.adapters.fuyao_adapter.time.time", return_value=1000), \
             patch("data.adapters.fuyao_adapter.urllib.request.urlopen", side_effect=[HTTPError("url",429,"limited",{},None), self.response()]):
            adapter.financials("002185","2026-2")
        with patch("data.adapters.fuyao_adapter.time.time", return_value=1061), \
             patch("data.adapters.fuyao_adapter.urllib.request.urlopen", side_effect=[HTTPError("url",429,"limited",{},None), self.response()]) as fetch:
            adapter.financials("002185","2026-2")
            self.assertEqual([x.args[0].get_header("X-api-key") for x in fetch.call_args_list], ["b", "a"])

    def test_non_rate_errors_do_not_rotate_and_business_messages_cannot_leak(self):
        for response in [HTTPError("url",504,"timeout",{},None),
                         io.BytesIO(b'{"code":"credential-must-not-appear"}')]:
            with self.subTest(response=type(response).__name__), \
                 patch("data.adapters.fuyao_adapter.urllib.request.urlopen", side_effect=[response]) as fetch:
                with self.assertRaises(RuntimeError) as error:
                    FuyaoAdapter(api_keys=["a","b"]).financials("002185","2026-2")
                self.assertNotIn("credential-must-not-appear", str(error.exception))
                fetch.assert_called_once()
