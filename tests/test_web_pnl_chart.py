from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "web" / "templates" / "app.html"


class WebPnlChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TEMPLATE.read_text(encoding="utf-8")

    def test_daily_pnl_chart_uses_horizontal_scroll_stage(self):
        self.assertIn('class="pnl-chart-scroll" id="pnlChartScroll"', self.template)
        self.assertIn('class="pnl-chart-stage" id="pnlChartStage"', self.template)
        self.assertIn("overflow-x:auto", self.template)

    def test_chart_width_scales_with_history_and_keeps_latest_visible(self):
        self.assertIn("72 + d.equity.length * 48", self.template)
        self.assertIn("scroll.scrollWidth - scroll.clientWidth", self.template)
        self.assertIn("scroll.dataset.ready", self.template)

    def test_axis_uses_short_dates_and_visible_amount_range(self):
        self.assertIn("String(x.date||'').slice(5)", self.template)
        self.assertIn("autoSkip:false,maxRotation:0,minRotation:0", self.template)
        self.assertIn("const visibleProfits = left=>", self.template)
        self.assertIn("const initialScale = scaleForValues(visibleProfits(targetScrollLeft))", self.template)
        self.assertIn("y:{min:initialScale.min,max:initialScale.max", self.template)
        self.assertIn("chart.options.scales.y.min", self.template)
        self.assertIn("chart.options.scales.y.max", self.template)
        self.assertIn("scroll.addEventListener('scroll'", self.template)


if __name__ == "__main__":
    unittest.main()
