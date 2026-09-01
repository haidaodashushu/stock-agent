from pathlib import Path
import unittest

from data.candidate_lifecycle import overlay_latest_candidate_quotes


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "web" / "templates" / "app.html"
WEB_APP = ROOT / "web" / "app.py"


class LiveWebRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TEMPLATE.read_text(encoding="utf-8")
        cls.web_app = WEB_APP.read_text(encoding="utf-8")

    def test_periodic_refresh_always_loads_live_account(self):
        refresh_body = self.template.split(
            "async function refreshLiveAccount(){", 1
        )[1].split("async function loadOrders(){", 1)[0]

        self.assertIn("loadLiveAccount()", refresh_body)
        self.assertNotIn("if(tab.value==='live') await loadLiveIntents()", refresh_body)
        self.assertIn("setInterval(refreshLiveAccount, 30000)", self.template)

    def test_live_account_requests_bypass_browser_cache(self):
        self.assertIn(
            "fetch('/api/live-account', {cache:'no-store'})",
            self.template,
        )
        self.assertIn('headers={"Cache-Control": "no-store"}', self.web_app)

    def test_dashboard_candidate_pool_uses_the_active_trading_board(self):
        self.assertIn("🏆 候选池", self.template)
        self.assertIn("activeBoardCandidates.length", self.template)
        self.assertIn('v-for="c in activeBoardCandidates"', self.template)
        self.assertNotIn('v-for="r in screenRecords.slice(0,7)"', self.template)

    def test_candidate_board_refreshes_on_dashboard_and_screening_tabs(self):
        self.assertIn(
            "if(tab.value==='screening'||tab.value==='dashboard') loadCandidateLifecycle()",
            self.template,
        )

    def test_candidate_page_exposes_promotion_service_health(self):
        self.assertIn("promotion_health", self.template)
        self.assertIn("晋升异常", self.template)
        self.assertIn("candidateLifecycle.promotion_health?.message", self.template)

    def test_candidate_cards_overlay_latest_quote_without_losing_signal_price(self):
        payload = {
            "candidates": [{
                "code": "600479", "price": 11.02, "change_pct": 9.98,
                "board_state": "active",
            }],
            "observations": [],
        }
        result = overlay_latest_candidate_quotes(payload, {
            "600479": {
                "price": 12.12, "day_change_pct": 9.98,
                "datetime": "2026-08-27 15:00:00", "source": "tencent",
            },
        })
        row = result["candidates"][0]
        self.assertEqual(row["price"], 12.12)
        self.assertEqual(row["signal_price"], 11.02)
        self.assertEqual(row["quote_as_of"], "2026-08-27 15:00:00")


if __name__ == "__main__":
    unittest.main()
