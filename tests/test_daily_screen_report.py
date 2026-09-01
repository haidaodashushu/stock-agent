import tempfile
import unittest

import pandas as pd

from data.store.sqlite_store import StockStore
from scripts.daily_screen import _build_screening_report, _save_screen_results
from scripts.render_cron_report import build_screening_report_blocks, render_markdown, render_presentation


class DailyScreenReportTests(unittest.TestCase):
    def test_current_candidates_are_replaced_while_each_run_is_retained(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            store = StockStore(handle.name)
            first = pd.DataFrame([{
                "code": "000001",
                "name": "第一批",
                "price": 10,
                "score": 7,
                "base_score": 7,
                "final_score": 8,
                "signal_type": "buy",
            }])
            second = pd.DataFrame([{
                "code": "000002",
                "name": "第二批",
                "price": 20,
                "score": 6,
                "base_score": 6,
                "final_score": 7,
                "signal_type": "watch",
            }])

            _save_screen_results(
                store,
                first,
                run_date="2026-07-17",
                run_time="22:45:00",
                run_label="夜间预选股",
                target="next-trading-day",
                generated_at="2026-07-16 22:45:00",
            )
            _save_screen_results(
                store,
                second,
                run_date="2026-07-17",
                run_time="08:30:00",
                run_label="每日盘前选股",
                target="today",
                generated_at="2026-07-17 08:30:00",
            )

            conn = store._get_conn()
            current = conn.execute(
                "SELECT code FROM screen_records WHERE run_date='2026-07-17'"
            ).fetchall()
            history = conn.execute(
                """SELECT run_time,code FROM screen_record_history
                   WHERE run_date='2026-07-17' ORDER BY run_time"""
            ).fetchall()
            conn.close()

            self.assertEqual([row["code"] for row in current], ["000002"])
            self.assertEqual(
                [(row["run_time"], row["code"]) for row in history],
                [("08:30:00", "000002"), ("22:45:00", "000001")],
            )

    def test_selector_signal_tags_are_preserved_in_report(self):
        report = _build_screening_report(
            [
                {
                    "code": "000001",
                    "name": "测试股票",
                    "score": 7.2,
                    "signal_type": "buy",
                    "signal_tags": "多头排列|未来产业主线|自定义观察",
                    "trend": "多头",
                    "pct_change": 1.2,
                    "vol_ratio": 1.1,
                }
            ],
            "2026-07-14",
            "夜间预选股",
            "next-trading-day",
        )

        self.assertEqual(
            report["candidates"][0]["tags"],
            ["多头排列", "未来产业主线", "自定义观察"],
        )
        self.assertEqual(report["run"]["mainline_text"], "未来产业主线(1)")

        blocks = build_screening_report_blocks(report)
        candidate_block = next(
            block["text"]
            for block in blocks
            if block.get("text", "").startswith("**TOP 候选**")
        )
        self.assertIn("未来产业主线", candidate_block)

    def test_persisted_strategies_remain_supported(self):
        report = _build_screening_report(
            [
                {
                    "code": "000001",
                    "name": "测试股票",
                    "score": 7.0,
                    "signal_type": "watch",
                    "strategies": "低位主线确认|高位量价确认",
                }
            ],
            "2026-07-14",
            "夜间预选股",
            "next-trading-day",
        )

        self.assertEqual(
            report["candidates"][0]["tags"],
            ["低位主线确认", "高位量价确认"],
        )

    def test_report_displays_final_score_and_preserves_base_score(self):
        report = _build_screening_report(
            [{
                "code": "000001",
                "name": "测试股票",
                "score": 7.2,
                "base_score": 7.2,
                "final_score": 9.7,
                "signal_type": "buy",
            }],
            "2026-07-14",
            "夜间预选股",
            "next-trading-day",
        )

        self.assertEqual(report["candidates"][0]["score"], 9.7)
        self.assertEqual(report["candidates"][0]["base_score"], 7.2)
        self.assertIn("评分 9.7", report["summary"])

    def test_ai_selection_reason_is_visible_in_card(self):
        report = _build_screening_report(
            [{
                "code": "000001",
                "name": "测试股票",
                "score": 7.2,
                "final_score": 8.0,
                "signal_type": "buy",
                "extra": {
                    "ai_selection": {
                        "rank": 1,
                        "confidence": "strong",
                        "reason": "趋势、板块和增量逻辑同向",
                        "risk": "放量持续性待确认",
                    }
                },
            }],
            "2026-07-23",
            "盘前选股",
            "today",
        )
        blocks = build_screening_report_blocks(report)
        ai_block = next(
            block["text"] for block in blocks
            if block.get("text", "").startswith("**AI 入选依据**")
        )
        self.assertIn("趋势、板块和增量逻辑同向", ai_block)
        self.assertIn("放量持续性待确认", ai_block)
        self.assertEqual(report["run"]["selection_method"], "ai")

        markdown = render_markdown(report)
        self.assertIn("**1. 000001 测试股票**｜评分 8.0｜strong", markdown)
        self.assertIn("> 入选：趋势、板块和增量逻辑同向", markdown)
        self.assertIn("> 风险：放量持续性待确认", markdown)
        self.assertNotIn("| 代码 | 名称 |", markdown)

    def test_screening_presentation_uses_card_markdown(self):
        report = _build_screening_report(
            [{
                "code": "000001",
                "name": "测试股票",
                "score": 7.2,
                "signal_type": "buy",
                "signal_tags": "未来产业主线",
                "trend": "多头",
                "pct_change": 1.2,
                "vol_ratio": 1.1,
            }],
            "2026-07-14",
            "夜间预选股",
            "next-trading-day",
        )

        card = render_presentation(report)
        self.assertEqual(card["schema"], "2.0")
        content = "\n".join(
            element.get("content", "")
            for element in card["body"]["elements"]
            if element.get("tag") == "markdown"
        )
        self.assertIn("| 代码 | 名称 | 评分 |", content)
        self.assertIn("未来产业主线", content)

    def test_close_review_presentation_is_native_feishu_card(self):
        report = {
            "profile": "close_review",
            "title": "每日收盘复盘 2026-07-15",
            "tone": "success",
            "summary": "收盘账户保持稳定。",
            "account": {
                "total_equity": 1010000,
                "available_cash": 900000,
                "position_market_value": 110000,
                "position_count": 1,
                "total_profit": 10000,
                "total_profit_pct": 1,
            },
            "positions": [{
                "code": "600001",
                "name": "测试股票",
                "volume": 100,
                "market_value": 1000,
                "today_chg_pct": 1.2,
                "pnl_pct": 2.3,
            }],
            "orders": [{
                "time": "10:00",
                "action": "buy",
                "code": "600001",
                "name": "测试股票",
                "volume": 100,
                "price": 10,
                "reason": "趋势增强",
            }],
            "fund_flows": [{
                "code": "600001",
                "name": "测试股票",
                "summary": "主力净入100万",
            }],
            "source": "测试数据源",
        }

        card = render_presentation(report)
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["config"]["width_mode"], "fill")
        self.assertEqual(card["header"]["template"], "green")
        elements = card["body"]["elements"]
        self.assertEqual(elements[0]["tag"], "column_set")
        self.assertFalse(any(element.get("tag") == "table" for element in elements))
        markdown = "\n".join(
            element.get("content", "")
            for element in elements
            if element.get("tag") == "markdown"
        )
        self.assertIn("**持仓表现**", markdown)
        self.assertIn("| 代码 | 名称 | 股数 |", markdown)
        self.assertIn("| 10:00 | 买 | 600001 |", markdown)
        self.assertIn("测试数据源", markdown)
        account_block = next(
            element["content"]
            for element in elements
            if element.get("tag") == "markdown" and "**账户总览**" in element.get("content", "")
        )
        self.assertNotIn("今日收益", account_block)

    def test_close_review_keeps_all_positions_for_feishu_pagination(self):
        positions = [
            {
                "code": f"6000{index:02d}",
                "name": f"测试股票{index}",
                "volume": 100,
                "market_value": 1000,
                "today_chg_pct": 1.2,
                "pnl_pct": 2.3,
            }
            for index in range(12)
        ]
        report = {
            "profile": "close_review",
            "title": "每日收盘复盘 2026-07-31",
            "summary": "测试完整持仓列表。",
            "positions": positions,
        }

        card = render_presentation(report)
        markdown = "\n".join(
            element.get("content", "")
            for element in card["body"]["elements"]
            if element.get("tag") == "markdown"
        )

        self.assertIn("| 600011 | 测试股票11 |", markdown)
        self.assertNotIn("另有 2 条", markdown)


if __name__ == "__main__":
    unittest.main()
