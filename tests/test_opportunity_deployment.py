import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import configure_opportunity_trial as installer
from scripts import run_opportunity_trading as worker


class TrialDeploymentTests(unittest.TestCase):
    def test_install_and_rollback_preserve_other_cron_and_production_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            production=Path(folder)/"production"
            checkout=Path(folder)/"trial"
            for path in (production/"data",production/".venv/bin",production/"config",checkout/"config"):
                path.mkdir(parents=True)
            (production/"data/stock_data.db").touch()
            (production/".venv/bin/python").touch()
            (production/"config/opportunity_trial.local.json").write_text('{"enabled": false}')
            (production/"config/runtime.local.json").write_text('{}')
            (checkout/"config/opportunity_trial.json").write_text('{"enabled": false}')
            original=("# unrelated\n0 0 * * * /usr/bin/true\n# BEGIN STOCK_AGENT_RUNTIME\n"
                      "STOCK_AGENT_CODEX_BIN=/custom/model\n"
                      f"0,30 9-11,13-14 * * 1-5 {production}/scripts/stock_scheduled_job.sh simulated-trading\n"
                      "# END STOCK_AGENT_RUNTIME\n")
            cron=[original]
            def install(*args,**kwargs): cron[0]=kwargs["input"]
            with patch.object(installer,"ROOT",checkout),patch.object(installer.subprocess,"check_output",side_effect=lambda *a,**k:cron[0]),patch.object(installer.subprocess,"run",side_effect=install):
                installer.configure(production)
                self.assertTrue(cron[0].startswith("# unrelated\n0 0 * * * /usr/bin/true\n"))
                self.assertIn("STOCK_AGENT_CODEX_BIN=/custom/model",cron[0])
                self.assertNotIn(str(production)+"/scripts/stock_scheduled_job.sh",cron[0])
                self.assertEqual(cron[0].count(" simulated-trading"),1)
                self.assertEqual(cron[0].count(" opportunity-monitor"),1)
                self.assertIn(str(production/"data/stock_data.db"),(checkout/"config/trial_runtime.local.env").read_text())
                self.assertFalse(json.loads((production/"config/opportunity_trial.local.json").read_text())["enabled"])
                installed=cron[0]
                cron[0]+="# subsequent edit\n"
                with self.assertRaisesRegex(RuntimeError,"cron changed"):
                    installer.configure(production,rollback=True)
                cron[0]=installed
                installer.configure(production,rollback=True)
                self.assertEqual(cron[0],original)
                self.assertFalse(json.loads((checkout/"config/opportunity_trial.local.json").read_text())["enabled"])

    def test_account_lock_prevents_refresh_and_model_execution(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(worker,"ROOT",Path(folder)),patch.object(worker.trial,"enabled",return_value=True),patch.object(worker,"is_actionable_trading_time",return_value=True),patch.object(worker.fcntl,"flock",side_effect=BlockingIOError),patch.object(worker,"StockStore") as store,patch.object(worker,"refresh_trading_state") as refresh,patch.object(worker.subprocess,"run") as execute:
            self.assertEqual(worker.run("simulated"),0)
            store.assert_not_called()
            refresh.assert_not_called()
            execute.assert_not_called()

    def test_closed_market_does_not_claim_or_execute(self):
        with patch.object(worker.trial,"enabled",return_value=True),patch.object(worker,"is_actionable_trading_time",return_value=False),patch.object(worker,"StockStore") as store,patch.object(worker.subprocess,"run") as execute:
            self.assertEqual(worker.run("live",event=True),0)
            store.assert_not_called()
            execute.assert_not_called()


if __name__=="__main__":
    unittest.main()
