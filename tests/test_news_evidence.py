import unittest

from data.news_evidence import build_news_evidence, match_policy_evidence


class NewsEvidenceTests(unittest.TestCase):
    def test_company_diagnostics_and_redirect_pages_are_background(self):
        from data.news_evidence import is_aggregate_news
        for url in ('https://basic.10jqka.com.cn/300502/worth.html',
                    'https://basic.10jqka.com.cn/astockpc/astockmain/index.html?code=300502'):
            with self.subTest(url=url):
                self.assertTrue(is_aggregate_news({'url':url,'title':'公司资料'}))
        self.assertFalse(is_aggregate_news({'url':'https://example.com/notice/123','title':'公司：半年报公告'}))

    def test_reindexed_directory_is_not_a_new_policy_or_risk_event(self):
        evidence = build_news_evidence({"title":"华天科技资讯", "url":"https://basic.10jqka.com.cn/002185/news.html",
            "content":"去年曾出现减持、监管函。工信部印发人工智能政策。", "publish_at":"2026-09-20 02:48:00",
            "category":"policy_hotspot", "score":5, "risk_level":"high"})
        self.assertEqual(evidence['published_at'],'')
        self.assertEqual(evidence['risk'],'unknown')
        self.assertEqual(match_policy_evidence([evidence],{'concepts':['人工智能']}),[])

    def test_body_drives_score_summary_and_trace_fields(self):
        evidence = build_news_evidence({
            "title": "有关部门发布最新通知",
            "content": "通知提出支持工业互联网平台建设。到2028年建设一批工业5G专网。",
            "source": "政府网站",
            "publish_at": "2026-07-29 09:00:00",
            "category": "policy_hotspot",
            "sentiment": "neutral",
            "score": 0,
            "risk_level": "low",
            "tags": "[]",
        })

        self.assertEqual(evidence["analysis_basis"], "title_content")
        self.assertTrue(evidence["content_available"])
        self.assertGreaterEqual(evidence["score"], 1)
        self.assertIn("工业互联网", evidence["summary"])
        self.assertIn("工业5G", evidence["mentioned_topics"])
        self.assertEqual(evidence["source"], "政府网站")
        self.assertTrue(evidence["content_digest"])
        self.assertEqual(evidence["evidence_type"], "policy_related")

    def test_policy_match_requires_explicit_stock_sector_overlap(self):
        policy = [{
            "mentioned_topics": ["人工智能", "算力"],
            "title": "政策",
            "evidence_type": "policy_document",
        }]

        matched = match_policy_evidence(policy, {"concepts": ["人工智能"]})
        unrelated = match_policy_evidence(policy, {"concepts": ["乳业"]})

        self.assertEqual(matched[0]["matched_topics"], ["人工智能"])
        self.assertEqual(unrelated, [])

    def test_market_interpretation_does_not_map_as_policy_catalyst(self):
        interpretation = build_news_evidence({
            "title": "某证券研报：算力产业景气向上",
            "content": "研报提到国务院部署人工智能发展有关工作。",
            "category": "policy_hotspot",
        })

        self.assertEqual(interpretation["evidence_type"], "policy_interpretation")
        self.assertEqual(
            match_policy_evidence([interpretation], {"concepts": ["算力"]}),
            [],
        )


if __name__ == "__main__":
    unittest.main()
