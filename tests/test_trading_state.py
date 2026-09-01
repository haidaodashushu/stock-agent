import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from pathlib import Path

from data.live_manual_account import account_snapshot, validate_intent
from data.candidate_board import refresh_candidate_board
from data.store.sqlite_store import StockStore
from data.trading_state import (
    _screen_candidates,
    classify_market_regime,
    load_trading_state,
    persist_trading_state,
)
from data import trading_decision_repository as decision_repo


def _item(
    code: str, *, candidate: bool = False, holding: bool = False, live_holding: bool = False,
) -> dict:
    return {
        "code": code,
        "name": f"股票{code}",
        "is_candidate": candidate,
        "is_sim_holding": holding,
        "is_live_holding": live_holding,
        "screen": {
            "run_date": "2026-07-14" if candidate else "",
            "score": 8 if candidate else 0,
            "signal_type": "buy" if candidate else "",
            "extra": {"selector": {
                "route": "low_logic",
                "entry_route": "early_start",
            }} if candidate else {},
        },
        "quote": {"price": 10, "source": "test"},
        "sector": {
            "membership_status": "available",
            "membership_as_of": "2026-07-14 09:59:00",
            "primary_industry": "软件开发",
            "industries": ["软件开发"],
            "concepts": ["人工智能"],
            "rotation_status": "available",
            "rotation_as_of": "2026-07-14 10:29:00",
            "rotation_source": "test",
            "rotation_score": 0.8,
            "alignment": "positive",
            "matches": [{
                "name": "人工智能",
                "stage": "leader",
                "score": 4.0,
                "stock_boost": 0.8,
                "matched_membership": "人工智能",
                "membership_type": "concept",
                "membership_source": "test",
                "match_type": "exact",
            }],
        },
    }


class TradingStateTests(unittest.TestCase):
    def test_market_regime_is_deterministic_from_broad_indices_and_breadth(self):
        strong = classify_market_regime({
            "sh000001": {"name": "上证", "change_pct": 0.8},
            "sz399001": {"name": "深证", "change_pct": 1.2},
            "sh000300": {"name": "沪深300", "change_pct": 0.7},
            "sz399006": {"name": "创业板", "change_pct": 1.5},
            "sh000688": {"name": "科创50", "change_pct": 2.1},
        })
        weak = classify_market_regime({
            "sh000001": {"name": "上证", "change_pct": -0.8},
            "sz399001": {"name": "深证", "change_pct": -1.2},
            "sh000300": {"name": "沪深300", "change_pct": -0.7},
            "sz399006": {"name": "创业板", "change_pct": -1.5},
            "sh000688": {"name": "科创50", "change_pct": 0.1},
        })
        self.assertEqual(strong["regime"], "strong")
        self.assertEqual(weak["regime"], "weak")
        self.assertEqual(strong["source"], "deterministic_indices.v1")

    def test_reads_every_candidate_for_the_trading_day(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            now = datetime.combine(date.today(), time(10, 0))
            day = now.date().isoformat()
            conn = store._get_conn()
            try:
                conn.executemany(
                    """INSERT INTO screen_records
                       (run_date, run_time, code, name, score, signal_type,extra)
                       VALUES (?, '08:30:00', ?, ?, ?, 'watch',?)""",
                    [
                        (day, f"{index:06d}", f"候选{index}", float(index), json.dumps({
                            "selector": {"entry_route": "early_start"},
                            "ai_selection": {"rank": 13-index},
                        }))
                        for index in range(1, 13)
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(_screen_candidates(store, day), [])
            refresh_candidate_board(store, now=now)
            with patch("data.candidate_board.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = now
                candidates = _screen_candidates(store, day)
            self.assertEqual(len(candidates), 12)
            self.assertEqual(candidates[0]["code"], "000012")

    def test_current_state_overwrites_scope_without_cycle_history(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            first = {
                "stage": "1000",
                "as_of": "2026-07-14 10:00:00",
                "market": {"indices": {}},
                "account": {"total_equity": 1_000_000},
                "account_policy": {},
                "positions": [_item("000001", holding=True)],
                "candidates": [_item("000002", candidate=True)],
                "tracked": [],
            }
            persist_trading_state(first, "simulated", store)
            loaded = load_trading_state("simulated", store)
            self.assertEqual([row["code"] for row in loaded["positions"]], ["000001"])
            self.assertEqual([row["code"] for row in loaded["candidates"]], ["000002"])

            second = {
                **first,
                "stage": "1030",
                "as_of": "2026-07-14 10:30:00",
                "positions": [],
                "candidates": [_item("000003", candidate=True)],
            }
            persist_trading_state(second, "simulated", store)
            loaded = load_trading_state("simulated", store)
            self.assertEqual(loaded["stage"], "1030")
            self.assertEqual(loaded["positions"], [])
            self.assertEqual([row["code"] for row in loaded["candidates"]], ["000003"])
            self.assertEqual(loaded["as_of"], "2026-07-14 10:30:00")
            conn = store._get_conn()
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM trading_market_state").fetchone()[0], 1)
                columns = [row[1] for row in conn.execute("PRAGMA table_info(trading_stock_state)")]
                self.assertIn("mode", columns)
                self.assertNotIn("cycle_id", columns)
            finally:
                conn.close()

    def test_live_account_uses_supplied_database_quotes_without_network(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            conn = store._get_conn()
            conn.row_factory = sqlite3.Row
            try:
                with patch("data.live_manual_account._fetch_quotes", side_effect=AssertionError("network called")):
                    snapshot = account_snapshot(conn, quotes={})
            finally:
                conn.close()
            self.assertEqual(snapshot["positions"], [])

    def test_live_account_exposes_t1_sellable_volume(self):
        config = {
            "initial_cash": 20_000,
            "capital_flows": [],
            "max_positions": None,
            "min_lot": 100,
            "blocked_boards": ["688", "8", "4"],
        }
        today = date.today()
        yesterday = today - timedelta(days=1)
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            conn = store._get_conn()
            conn.executemany(
                """INSERT INTO live_trade_intents
                   (intent_id,code,name,action,suggested_price,suggested_volume,
                    suggested_amount,status,filled_price,filled_volume,filled_amount,filled_at)
                   VALUES (?,?,?,'buy',10,?,?, 'filled',10,?,?,?)""",
                [
                    ("old-buy", "600001", "测试股票", 500, 5000, 500, 5000,
                     f"{yesterday.isoformat()} 10:00:00"),
                    ("today-buy", "600001", "测试股票", 300, 3000, 300, 3000,
                     f"{today.isoformat()} 10:00:00"),
                ],
            )
            conn.commit()
            with (
                patch("data.live_manual_account.load_config", return_value=config),
                patch("data.live_manual_account._fetch_quotes", return_value={"600001": {"price": 10}}),
            ):
                snapshot = account_snapshot(conn, quotes={"600001": {"price": 10}})
                allowed = validate_intent(conn, "sell", "600001", 10, 500)
                blocked = validate_intent(conn, "sell", "600001", 10, 600)
            conn.close()

        position = snapshot["positions"][0]
        self.assertEqual(position["volume"], 800)
        self.assertEqual(position["today_buy_volume"], 300)
        self.assertEqual(position["available_to_sell"], 500)
        self.assertEqual(allowed, [])
        self.assertIn("A股T+1可卖数量不足", blocked[0])

    def test_live_deposit_increases_cash_and_capital_without_becoming_profit(self):
        config = {
            "initial_cash": 20_000,
            "capital_flows": [{
                "flow_id": "deposit-test",
                "effective_at": "2026-01-05 00:00:00",
                "type": "deposit",
                "amount": 10_000,
            }],
            "max_positions": None,
            "min_lot": 100,
            "blocked_boards": ["688", "8", "4"],
        }
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            conn = store._get_conn()
            conn.row_factory = sqlite3.Row
            try:
                with patch("data.live_manual_account.load_config", return_value=config):
                    snapshot = account_snapshot(conn, quotes={})
            finally:
                conn.close()

        summary = snapshot["summary"]
        self.assertEqual(summary["initial_cash"], 20_000)
        self.assertEqual(summary["net_external_cash_flow"], 10_000)
        self.assertEqual(summary["net_contributed_capital"], 30_000)
        self.assertEqual(summary["available_cash"], 30_000)
        self.assertEqual(summary["total_equity"], 30_000)
        self.assertEqual(summary["total_profit"], 0)

    def test_agent_reads_versioned_domain_views_instead_of_sql(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            snapshot = {
                "stage": "1030",
                "as_of": "2026-07-14 10:30:00",
                "market": {
                    "indices": {},
                    "sector_rotation": {
                        "status": "partial",
                        "created_at": "2026-07-14 10:29:00",
                        "source": "iwencai",
                        "error": "one query failed",
                        "signals": [{"name": "人工智能", "score": 3.2, "stage": "leader"}],
                    },
                },
                "account": {"total_equity": 1_000_000},
                "account_policy": {"max_positions": None},
                "positions": [_item("000001", holding=True)],
                "candidates": [_item("000002", candidate=True)],
                "tracked": [],
            }
            persist_trading_state(snapshot, "simulated", store)
            with patch.object(decision_repo, "DB_PATH", Path(db.name)):
                overview = decision_repo.get_trading_overview("simulated")
                self.assertEqual(overview["required_evidence_codes"], ["000001", "000002"])
                self.assertEqual(overview["market"]["sector_context"]["status"], "partial")
                self.assertEqual(overview["market"]["sector_context"]["as_of"], "2026-07-14 10:29:00")
                self.assertEqual(overview["market"]["leading_sectors"][0]["name"], "人工智能")
                self.assertEqual(overview["universe"][1]["entry_route"], "early_start")
                self.assertNotIn("route", overview["universe"][1])
                evidence = decision_repo.get_stock_evidence(
                    overview["required_evidence_codes"], overview["as_of"], "simulated"
                )
                self.assertEqual(evidence["count"], 2)
                self.assertIn("position", evidence["stocks"][0])
                self.assertNotIn("live_position", evidence["stocks"][0])
                self.assertEqual(evidence["stocks"][0]["sector"]["alignment"], "positive")
                self.assertEqual(evidence["stocks"][0]["sector"]["matches"][0]["name"], "人工智能")
                with self.assertRaisesRegex(ValueError, "state changed"):
                    decision_repo.get_stock_evidence(
                        ["000001"], "stale-version", "simulated"
                    )

    def test_account_views_do_not_expose_the_other_account(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            simulated_snapshot = {
                "stage": "1030",
                "as_of": "2026-07-14 10:30:00",
                "market": {"indices": {}},
                "account": {"total_equity": 1_000_000},
                "account_policy": {"max_positions": None},
                "positions": [_item("000001", holding=True)],
                "candidates": [_item("000002", candidate=True)],
                "tracked": [],
            }
            live_snapshot = {
                "stage": "1032",
                "as_of": "2026-07-14 10:32:00",
                "market": {"indices": {}},
                "account": {"total_equity": 20_000},
                "account_policy": {"manual_only": True},
                "positions": [_item("000003", live_holding=True)],
                "candidates": [_item("000002", candidate=True)],
                "tracked": [],
            }
            persist_trading_state(simulated_snapshot, "simulated", store)
            persist_trading_state(live_snapshot, "live", store)
            with patch.object(decision_repo, "DB_PATH", Path(db.name)):
                simulated = decision_repo.get_trading_overview("simulated")
                live = decision_repo.get_trading_overview("live")

            self.assertEqual(simulated["required_evidence_codes"], ["000001", "000002"])
            self.assertEqual(live["required_evidence_codes"], ["000003", "000002"])
            self.assertEqual(simulated["as_of"], "2026-07-14 10:30:00")
            self.assertEqual(live["as_of"], "2026-07-14 10:32:00")
            self.assertNotIn("accounts", simulated)
            self.assertNotIn("simulated", live["account"])

    def test_live_recent_activity_includes_expired_and_completed_intents(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            snapshot = {
                "stage": "1332",
                "as_of": "2026-07-14 13:32:00",
                "market": {"indices": {}},
                "account": {"total_equity": 20_000},
                "account_policy": {"manual_only": True},
                "positions": [],
                "candidates": [_item("600001", candidate=True)],
                "tracked": [],
            }
            persist_trading_state(snapshot, "live", store)
            conn = store._get_conn()
            try:
                conn.executemany(
                    """INSERT INTO live_trade_intents
                       (intent_id, code, name, action, status, created_at, expires_at)
                       VALUES (?, '600001', '测试股票', 'buy', ?, ?, ?)""",
                    [
                        ("expired-intent", "expired", "2026-07-14 13:02:00", "2026-07-14 13:17:00"),
                        ("filled-intent", "filled", "2026-07-14 13:20:00", "2026-07-14 13:35:00"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(decision_repo, "DB_PATH", Path(db.name)):
                result = decision_repo.get_recent_trading_activity(
                    snapshot["as_of"], "live", limit=10,
                )

            self.assertEqual(
                [row["status"] for row in result["activity"]],
                ["filled", "expired"],
            )

    def test_order_count_is_independent_of_recent_history_limit(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            conn = store._get_conn()
            try:
                conn.executemany(
                    """INSERT INTO orders (order_id, code, direction)
                       VALUES (?, '600001', 'buy')""",
                    [(f"order-{index}",) for index in range(60)],
                )
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(len(store.get_orders(limit=50)), 50)
            self.assertEqual(store.get_order_count(), 60)


if __name__ == "__main__":
    unittest.main()
