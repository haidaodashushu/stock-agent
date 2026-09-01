import unittest

from data.logic_change import _score_events


class LogicChangeTests(unittest.TestCase):
    def test_static_theme_is_not_logic_change_without_events(self):
        ev = _score_events("000768", [])
        self.assertEqual(ev.level, "none")
        self.assertEqual(ev.boost, 0.0)

    def test_hot_keywords_can_form_medium_logic_change(self):
        events = [
            {"title": "热词: 商业航天 热度867", "category": "hot_keyword", "sentiment": "positive", "score": 1.0, "risk_level": "low", "tags": '["热词"]'},
            {"title": "热词: 大飞机 热度890", "category": "hot_keyword", "sentiment": "positive", "score": 1.0, "risk_level": "low", "tags": '["热词"]'},
            {"title": "热词: 军工 热度926", "category": "hot_keyword", "sentiment": "positive", "score": 1.0, "risk_level": "low", "tags": '["热词"]'},
        ]
        ev = _score_events("000768", events)
        self.assertEqual(ev.level, "medium")
        self.assertEqual(ev.boost, 1.5)
        self.assertIn("中逻辑变化", ev.tags)

    def test_order_or_policy_news_is_strong_logic_change(self):
        events = [
            {"title": "公司获得重大订单", "category": "news", "sentiment": "positive", "score": 2.0, "risk_level": "medium", "tags": '["订单"]'},
        ]
        ev = _score_events("000768", events)
        self.assertEqual(ev.level, "strong")
        self.assertEqual(ev.boost, 2.5)
        self.assertIn("强逻辑变化", ev.tags)

    def test_repeated_hot_keyword_is_one_piece_of_evidence(self):
        events = [
            {"title": "热词: 人工智能 热度180", "category": "hot_keyword", "sentiment": "positive", "score": 1.0, "risk_level": "low", "tags": '["热词"]', "publish_at": f"2026-07-10 {hour:02d}:00:00"}
            for hour in range(10)
        ]
        ev = _score_events("000977", events)
        self.assertEqual(ev.level, "weak")
        self.assertEqual(ev.event_count, 1)

    def test_low_heat_keyword_is_background_not_logic_change(self):
        events = [
            {"title": "热词: 国产芯片 热度4", "category": "hot_keyword", "sentiment": "positive", "score": 1.0, "risk_level": "low", "tags": '["热词"]'},
        ]
        ev = _score_events("000977", events)
        self.assertEqual(ev.level, "none")

    def test_financial_snapshot_alone_is_medium_until_corroborated(self):
        events = [
            {"title": "问财财务: 20260331 营收同比40.0% 净利同比80.0%", "category": "iwencai_finance", "sentiment": "positive", "score": 3.5, "risk_level": "high", "tags": '["业绩高增"]'},
        ]
        ev = _score_events("000977", events)
        self.assertEqual(ev.level, "medium")
        self.assertTrue(ev.tradeable_positive)

    def test_ordinary_financial_growth_is_context_not_trade_trigger(self):
        events = [
            {"title": "问财财务: 20260331 营收同比15.0% 净利同比25.0%", "category": "iwencai_finance", "sentiment": "positive", "score": 1.5, "risk_level": "medium", "tags": '["业绩增长"]'},
        ]
        ev = _score_events("000977", events)
        self.assertEqual(ev.level, "medium")
        self.assertFalse(ev.tradeable_positive)

    def test_negative_events_create_risk_penalty(self):
        events = [
            {"title": "公司订单大幅下降", "category": "news", "sentiment": "negative", "score": -3.0, "risk_level": "high", "tags": '["订单"]'},
        ]
        ev = _score_events("000977", events)
        self.assertEqual(ev.penalty, -2.0)
        self.assertIn("重大负面逻辑", ev.risk_tags)

    def test_body_only_related_news_cannot_become_tradeable_catalyst(self):
        events = [
            {
                "title": "机器人行业进入量产阶段",
                "category": "iwencai_related_news",
                "sentiment": "positive",
                "score": 3.0,
                "risk_level": "high",
                "tags": '["机器人", "量产"]',
            }
        ]
        ev = _score_events("000977", events)
        self.assertEqual(ev.level, "weak")
        self.assertFalse(ev.tradeable_positive)

    def test_company_news_body_can_create_logic_change(self):
        events = [{
            "title": "测试公司发布公告",
            "content": "公司近日中标重大合同，项目将于年内开始实施。",
            "category": "iwencai_company_news",
            "sentiment": "neutral",
            "score": 0,
            "risk_level": "low",
            "tags": "[]",
        }]

        ev = _score_events("000977", events)

        self.assertEqual(ev.level, "strong")
        self.assertGreaterEqual(ev.max_score, 3)
        self.assertIn("重大合同", ev.reasons[0])


if __name__ == "__main__":
    unittest.main()
