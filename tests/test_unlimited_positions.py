import tempfile
import unittest
from unittest.mock import patch

from account.models import Portfolio, Position
from account.portfolio_policy import SIM_HARD_MAX_POSITIONS
from account.trader import SimTrader
from data.live_manual_account import validate_intent
from data.store.sqlite_store import StockStore
from scripts.execute_trade_signal import execute as execute_simulated_signals


class UnlimitedPositionCountTests(unittest.TestCase):
    @staticmethod
    def _memory_trader(position_count: int) -> SimTrader:
        trader = SimTrader.__new__(SimTrader)
        trader.portfolio = Portfolio(
            available_cash=1_000_000,
            positions=[
                Position(code=f"600{i:03d}", name=f"持仓{i}", volume=1000, cost_price=10.0)
                for i in range(position_count)
            ],
        )
        trader._today_buy_volume = lambda code: 0
        trader._save_order = lambda order: None
        trader._save_portfolio = lambda: None
        return trader

    def test_sim_trader_can_open_more_than_eight_positions(self):
        positions = [
            Position(code=f"600{i:03d}", name=f"持仓{i}", volume=100, cost_price=1.0)
            for i in range(8)
        ]
        trader = SimTrader.__new__(SimTrader)
        trader.portfolio = Portfolio(available_cash=1_000_000, positions=positions)
        trader._save_order = lambda order: None
        trader._save_portfolio = lambda: None

        order = trader.buy("601999", "第九只", 10.0, 100)

        self.assertIsNotNone(order)
        self.assertEqual(len(trader.portfolio.positions), 9)

    def test_sim_trader_rejects_new_position_at_hard_cap_but_allows_add(self):
        positions = [
            Position(code=f"600{i:03d}", name=f"持仓{i}", volume=100, cost_price=10.0)
            for i in range(SIM_HARD_MAX_POSITIONS)
        ]
        trader = SimTrader.__new__(SimTrader)
        trader.portfolio = Portfolio(available_cash=1_000_000, positions=positions)
        trader._save_order = lambda order: None
        trader._save_portfolio = lambda: None

        self.assertIsNone(trader.buy("601999", "第十六只", 10.0, 100))
        self.assertIsNotNone(trader.buy("600000", "已有持仓", 10.0, 100))
        self.assertEqual(len(trader.portfolio.positions), SIM_HARD_MAX_POSITIONS)

    def test_replacement_exit_executes_before_new_entry(self):
        trader = self._memory_trader(12)
        payload = {"signals": [
            {
                "code": "000002", "name": "更强候选", "action": "buy",
                "target_amount": 10_000, "replacement_code": "600000",
            },
            {
                "code": "600000", "name": "弱持仓", "action": "reduce",
                "sell_pct": 0.5,
            },
        ]}
        with (
            patch("scripts.execute_trade_signal.SimTrader", return_value=trader),
            patch(
                "scripts.execute_trade_signal.fetch_live_prices",
                return_value={"000002": 10.0, "600000": 10.0},
            ),
        ):
            result = execute_simulated_signals(payload)

        self.assertEqual([row["action"] for row in result["results"]], ["reduce", "buy"])
        self.assertTrue(all(row["executed"] for row in result["results"]))
        self.assertEqual(trader.portfolio.position_count(), 13)

    def test_reduction_cannot_push_portfolio_past_hard_cap(self):
        trader = self._memory_trader(SIM_HARD_MAX_POSITIONS)
        payload = {"signals": [
            {"code": "600000", "action": "reduce", "sell_pct": 0.5},
            {
                "code": "000002", "action": "buy", "target_amount": 10_000,
                "replacement_code": "600000",
            },
        ]}
        with (
            patch("scripts.execute_trade_signal.SimTrader", return_value=trader),
            patch(
                "scripts.execute_trade_signal.fetch_live_prices",
                return_value={"000002": 10.0, "600000": 10.0},
            ),
        ):
            result = execute_simulated_signals(payload)

        buy = next(row for row in result["results"] if row["action"] == "buy")
        self.assertFalse(buy["executed"])
        self.assertIn("硬上限15只", buy["errors"][0])

    def test_live_intent_has_no_position_count_rejection_when_limit_is_null(self):
        config = {
            "initial_cash": 20_000,
            "max_positions": None,
            "max_single_buy_amount": None,
            "min_lot": 100,
            "blocked_boards": ["300", "301", "688", "8", "4"],
        }
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            store = StockStore(db_path=db.name)
            conn = store._get_conn()
            try:
                for index, code in enumerate(("600001", "600002", "600003"), start=1):
                    conn.execute(
                        """INSERT INTO live_trade_intents
                           (intent_id, code, name, action, suggested_price,
                            suggested_volume, suggested_amount, status)
                           VALUES (?, ?, ?, 'buy', 1, 100, 100, 'proposed')""",
                        (f"test-{index}", code, f"股票{index}"),
                    )
                conn.commit()
                with patch("data.live_manual_account.load_config", return_value=config):
                    issues = validate_intent(conn, "buy", "600004", 1.0, 100)
            finally:
                conn.close()

        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
