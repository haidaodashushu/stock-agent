from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
import json
import re
import tempfile
from unittest.mock import patch

from data.market_calendar import is_actionable_trading_time
from scripts import execute_trading_cycle


ROOT = Path(__file__).resolve().parents[1]


class TradingSessionGuardTests(unittest.TestCase):
    def test_session_boundaries_are_strict(self) -> None:
        cases = {
            "2026-09-01 09:29:59": False,
            "2026-09-01 09:30:00": True,
            "2026-09-01 11:29:59": True,
            "2026-09-01 11:30:00": False,
            "2026-09-01 12:59:59": False,
            "2026-09-01 13:00:00": True,
            "2026-09-01 14:59:59": True,
            "2026-09-01 15:00:00": False,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    is_actionable_trading_time(datetime.fromisoformat(raw)), expected,
                )

    def test_weekend_is_never_actionable(self) -> None:
        self.assertFalse(
            is_actionable_trading_time(datetime.fromisoformat("2026-09-05 10:00:00"))
        )

    def test_codex_cycle_excludes_lunch_and_preserves_intended_stages(self) -> None:
        script = (ROOT / "scripts" / "stock_agent_trading_cycle.sh").read_text(
            encoding="utf-8"
        )
        for stage in ("0930", "1000", "1030", "1100", "1300", "1330", "1400", "1430"):
            self.assertIn(f"simulated:{stage}", script)
        for stage in ("0932", "1002", "1032", "1102", "1302", "1332", "1402", "1432"):
            self.assertIn(f"live:{stage}", script)
        self.assertNotIn("simulated:1130", script)
        self.assertNotIn("live:1132", script)
        live_tokens = set(re.findall(r"live:(\d{4})", script))
        expected_delayed_live_stages = {
            f"{int(stage) + delay:04d}"
            for stage in ("0932", "1002", "1032", "1102", "1302", "1332", "1402", "1432")
            for delay in range(8)
        }
        self.assertTrue(expected_delayed_live_stages.issubset(live_tokens))

    def test_late_model_completion_cannot_reach_simulated_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            response = root / "response.json"
            decision_out = root / "decision.json"
            result_out = root / "result.json"
            response.write_text("{}", encoding="utf-8")
            argv = [
                "execute_trading_cycle.py", "--mode", "simulated",
                "--stage", "1100", "--expected-as-of", "v1",
                "--response", str(response), "--decision-out", str(decision_out),
                "--result-out", str(result_out),
            ]
            context = {"stage": "1100", "as_of": "v1", "account": {"available_cash": 1}}
            decision = {"status": "ok", "signals": [], "report": {}}
            with (
                patch.object(execute_trading_cycle, "build_execution_context", return_value=context),
                patch.object(execute_trading_cycle, "_load_decision", return_value=decision),
                patch.object(execute_trading_cycle, "ensure_actionable_trading_time", return_value=False),
                patch.object(execute_trading_cycle, "execute_simulated") as executor,
                patch("sys.argv", argv),
            ):
                self.assertEqual(execute_trading_cycle.main(), 0)

            executor.assert_not_called()
            result = json.loads(result_out.read_text(encoding="utf-8"))
            self.assertTrue(result["execution"]["skipped"])


if __name__ == "__main__":
    unittest.main()
