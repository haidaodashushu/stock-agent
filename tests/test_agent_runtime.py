from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from data.agent_runtime import AgentRunResult, CodexCliProvider, codex_environment
from data.stock_selection_repository import get_staged_rows, stage_candidate_pool
from data.store.sqlite_store import StockStore
from scripts import run_stock_agent


class AgentRuntimeTests(unittest.TestCase):
    def test_scheduled_entrypoint_clears_proxy_environment(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "stock_scheduled_job.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY", script)
        self.assertIn("unset http_proxy https_proxy all_proxy no_proxy", script)

    def test_codex_environment_loads_only_supported_proxy_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proxy_file = Path(tmp) / "proxy.env"
            proxy_file.write_text(
                "export HTTPS_PROXY=http://127.0.0.1:2080\n"
                "UNRELATED=do-not-load\n",
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "STOCK_PROXY_ENV_FILE": str(proxy_file),
                    "HTTP_PROXY": "http://old-proxy",
                    "UNRELATED": "preserve-parent",
                },
                clear=True,
            ):
                env = codex_environment()
            self.assertNotIn("HTTP_PROXY", env)
            self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:2080")
            self.assertEqual(env["UNRELATED"], "preserve-parent")

    @patch("data.agent_runtime.shutil.which", return_value="/opt/codex/bin/codex")
    @patch("data.agent_runtime.Path.exists", return_value=True)
    def test_codex_command_is_ephemeral_read_only_and_preapproves_only_mcp(self, *_args) -> None:
        provider = CodexCliProvider(model="gpt-test")
        command = provider.build_command(
            workspace=Path("/workspace"),
            mcp_command="/workspace/.venv/bin/python",
            mcp_args=["/workspace/scripts/tool.py", "--mode", "live"],
            final_message_path=Path("/tmp/final.txt"),
        )
        joined = " ".join(command)
        self.assertIn("--ephemeral", command)
        for feature in (
            "apps", "plugins", "remote_plugin", "browser_use",
            "computer_use", "image_generation", "multi_agent",
        ):
            self.assertIn(feature, command)
        self.assertIn("read-only", command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn('default_tools_approval_mode="approve"', joined)
        self.assertIn("/workspace/.venv/bin/python", joined)
        self.assertNotIn("dangerously-bypass-approvals-and-sandbox", command)

    @patch.dict("os.environ", {"STOCK_DB_PATH": "/tmp/shadow.db"})
    @patch("data.agent_runtime.shutil.which", return_value="/opt/codex/bin/codex")
    @patch("data.agent_runtime.Path.exists", return_value=True)
    def test_shadow_database_is_forwarded_to_mcp_process(self, *_args) -> None:
        command = CodexCliProvider(model="gpt-test").build_command(
            workspace=Path("/workspace"), final_message_path=Path("/tmp/final.txt"),
            mcp_command="/workspace/.venv/bin/python", mcp_args=["tool.py"],
        )
        self.assertIn(
            'mcp_servers.stock_agent.env.STOCK_DB_PATH="/tmp/shadow.db"', command,
        )

    @patch("data.agent_runtime.shutil.which", return_value="/opt/codex/bin/codex")
    @patch("data.agent_runtime.Path.exists", return_value=True)
    def test_persistent_codex_session_can_be_resumed_by_id(self, *_args) -> None:
        provider = CodexCliProvider(model="gpt-test", sandbox="read-only", ephemeral=False)
        initial = provider.build_command(
            workspace=Path("/workspace"), final_message_path=Path("/tmp/initial.txt"),
        )
        resumed = provider.build_command(
            workspace=Path("/workspace"), final_message_path=Path("/tmp/resumed.txt"),
            resume_session_id="01a00000-0000-7000-8000-000000000001",
            image_paths=[Path("/tmp/quoted.png")],
        )

        self.assertNotIn("--ephemeral", initial)
        self.assertIn("--sandbox", initial)
        self.assertEqual(resumed[:3], ["/opt/codex/bin/codex", "exec", "resume"])
        self.assertNotIn("--sandbox", resumed)
        self.assertIn("/tmp/quoted.png", resumed)
        self.assertEqual(resumed[-2:], ["01a00000-0000-7000-8000-000000000001", "-"])

    def test_missing_submission_retries_without_invalidating_selection_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StockStore(str(Path(tmp) / "stock.db"))
            as_of = "2026-08-31 22:45:02.654711"
            stage_candidate_pool(
                [{
                    "code": "000001", "name": "测试", "price": 10,
                    "score": 8, "final_score": 8, "signal_type": "buy",
                    "route": "balanced", "entry_route": "strong_continuation",
                }],
                run_date="2026-09-01", run_time="22:45:02", run_label="夜间预选股",
                target="next-trading-day", expected_daily_date="2026-08-31",
                generated_at=as_of, store=store,
            )
            provider = MagicMock()
            provider.run.return_value = AgentRunResult(
                provider="codex-cli", model="gpt-test", command=(), returncode=1,
                final_message="", events="", stderr="connection reset by peer",
            )
            overview = {"as_of": as_of}
            argv = [
                "run_stock_agent.py", "--task", "selection", "--model", "gpt-test",
                "--max-attempts", "2", "--retry-delay", "0",
                "--run-root", str(Path(tmp) / "runs"),
            ]
            with (
                patch.object(run_stock_agent, "StockStore", return_value=store),
                patch.object(run_stock_agent, "_task_snapshot", return_value=("selection", "", overview)),
                patch.object(run_stock_agent, "CodexCliProvider", return_value=provider),
                patch("sys.argv", argv),
            ):
                rc = run_stock_agent.main()

            self.assertEqual(rc, 1)
            self.assertEqual(provider.run.call_count, 2)
            state, _ = get_staged_rows(as_of, store=store)
            self.assertEqual(state["status"], "ready")


if __name__ == "__main__":
    unittest.main()
