import json
import tempfile
import unittest
from pathlib import Path

from scripts.render_live_trade_fortune import (
    DISCLAIMER,
    DETAIL_BEGIN,
    DETAIL_END,
    FORTUNE_BEGIN,
    FORTUNE_END,
    PLAIN_BEGIN,
    PLAIN_END,
    FortuneContext,
    FortuneReading,
    build_prompt,
    clean_reading,
    load_context,
    render_block,
)


def _write_result(path, *, created=True, dry_run=False):
    path.write_text(
        json.dumps(
            {
                "mode": "live",
                "dry_run": dry_run,
                "execution": {
                    "as_of": "2026-01-15 14:04:51",
                    "results": [
                        {
                            "code": "600000",
                            "name": "示例股票",
                            "action": "buy" if created else "hold",
                            "created_intent": created,
                            "volume": 100 if created else 0,
                            "price": 10.00,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class LiveTradeFortuneTests(unittest.TestCase):
    def test_load_context_uses_only_created_live_buy_or_sell_intents(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "execution.json"
            _write_result(result)

            context = load_context(result)

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.as_of, "2026-01-15 14:04")
        self.assertEqual(len(context.actions), 1)
        self.assertEqual(context.actions[0]["code"], "600000")

    def test_load_context_skips_noop_and_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            noop = Path(tmp) / "noop.json"
            dry = Path(tmp) / "dry.json"
            _write_result(noop, created=False)
            _write_result(dry, dry_run=True)

            self.assertIsNone(load_context(noop))
            self.assertIsNone(load_context(dry))

    def test_prompt_keeps_divination_downstream_and_non_operational(self):
        context = FortuneContext(
            as_of="2026-01-15 14:04",
            actions=(
                {
                    "code": "600000",
                    "name": "示例股票",
                    "action": "buy",
                    "volume": 100,
                    "price": 10.00,
                },
            ),
        )

        prompt = build_prompt(context, {"课体": "知一课", "三传": [{"支": "子"}]})

        self.assertIn("已经生成完毕", prompt)
        self.assertIn("不可被本次解读修改", prompt)
        self.assertIn("不查询也不使用行情、资金、新闻、账户", prompt)
        self.assertIn("不输出买入、卖出、加减仓", prompt)
        self.assertIn("买入 600000 示例股票", prompt)
        self.assertIn(FORTUNE_BEGIN, prompt)
        self.assertIn(FORTUNE_END, prompt)
        self.assertIn(PLAIN_BEGIN, prompt)
        self.assertIn(DETAIL_BEGIN, prompt)

    def test_clean_reading_discards_reasoning_and_duplicate_disclaimer(self):
        raw = (
            "┌─ Reasoning ─┐\nplanning\n"
            f"{FORTUNE_BEGIN}\n"
            f"{PLAIN_BEGIN}\n适配度：偏不适合\n"
            f"解释：开头可能有支撑，但后劲不足。\n{PLAIN_END}\n"
            f"{DETAIL_BEGIN}\n先有承托，后劲转弱。"
            f"术数无科学依据，仅供传统文化参考。\n{DETAIL_END}\n"
            f"{FORTUNE_END}\n"
        )

        reading = clean_reading(raw)

        self.assertEqual(reading.suitability, "偏不适合")
        self.assertEqual(reading.plain, "开头可能有支撑，但后劲不足。")
        self.assertEqual(reading.detail, "先有承托，后劲转弱。")

    def test_clean_reading_rejects_an_operational_instruction(self):
        raw = (
            f"{FORTUNE_BEGIN}\n{PLAIN_BEGIN}\n适配度：偏适合\n"
            f"解释：宜加仓等待上涨。\n{PLAIN_END}\n"
            f"{DETAIL_BEGIN}\n财象较旺。\n{DETAIL_END}\n{FORTUNE_END}"
        )

        with self.assertRaisesRegex(ValueError, "non-operative boundary"):
            clean_reading(raw)

    def test_render_block_has_fixed_boundary_disclaimer(self):
        context = FortuneContext(
            as_of="2026-01-15 14:04",
            actions=({"action": "buy"},),
        )

        block = render_block(
            context,
            FortuneReading(
                suitability="中性",
                plain="有一些助力，但结果并不明确。",
                detail="传统象意偏平，过程有反复。",
            ),
        )

        self.assertIn("不影响实盘建议", block)
        self.assertIn("本次买入的适配度为 **中性**", block)
        self.assertIn("有一些助力，但结果并不明确", block)
        self.assertIn("> **课象原文**", block)
        self.assertIn("传统象意偏平", block)
        self.assertIn(DISCLAIMER, block)


if __name__ == "__main__":
    unittest.main()
