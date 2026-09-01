import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hermes_trading_cycle.sh"


class LiveCycleDelayedStartContractTest(unittest.TestCase):
    def test_live_cycle_accepts_serialized_scheduler_delay(self):
        text = SCRIPT.read_text(encoding="utf-8")
        live_tokens = set(re.findall(r"live:(\d{4})", text))

        scheduled_stages = ("0932", "1002", "1032", "1102", "1302", "1332", "1402", "1432")
        expected = {
            f"{int(stage) + delay:04d}"
            for stage in scheduled_stages
            for delay in range(8)
        }
        self.assertTrue(
            expected.issubset(live_tokens),
            f"实盘轮次必须容忍串行调度延迟；缺少启动分钟: {sorted(expected - live_tokens)}",
        )

    def test_model_timeout_retries_and_persists_failed_cycle_artifacts(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("AI decision attempt 1 failed", text)
        self.assertIn("RETRY_RESPONSE_FILE", text)
        self.assertIn("persisting failed cycle", text)
        retry_branch = text.split("persisting failed cycle", 1)[1]
        self.assertIn("scripts/execute_trading_cycle.py", retry_branch)
        self.assertIn('--decision-out "$DECISION_FILE"', retry_branch)
        self.assertIn('--result-out "$RESULT_FILE"', retry_branch)


if __name__ == "__main__":
    unittest.main()
