import unittest

from scripts.scan_candidate_intelligence import _is_tradeable, _parse_time


class CandidateIntelligenceTests(unittest.TestCase):
    def test_parse_compact_date(self):
        self.assertEqual(_parse_time("20260618"), "2026-06-18 00:00:00")

    def test_filters_forbidden_boards(self):
        self.assertFalse(_is_tradeable("688001"))
        self.assertFalse(_is_tradeable("830000"))
        self.assertFalse(_is_tradeable("430000"))
        self.assertTrue(_is_tradeable("000768"))

if __name__ == "__main__":
    unittest.main()
