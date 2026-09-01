import unittest

from data.fetcher.tencent_quote import _stock_type, _tencent_symbol
from data.trading_state import MARKET_INDEX_SYMBOLS, _symbols


class TencentQuoteSymbolTest(unittest.TestCase):
    def test_maps_shanghai_etfs_to_shanghai(self):
        self.assertEqual(_tencent_symbol("510300"), "sh510300")
        self.assertEqual(_tencent_symbol("588000"), "sh588000")
        self.assertEqual(_stock_type("510300"), (1, "510300"))
        self.assertEqual(_symbols(["510300", "588000"]), ["sh510300", "sh588000"])

    def test_keeps_stock_exchange_mapping(self):
        self.assertEqual(_tencent_symbol("600519"), "sh600519")
        self.assertEqual(_tencent_symbol("300308"), "sz300308")

    def test_keeps_kstar_index_separate_from_shenzhen_stock(self):
        self.assertEqual(_symbols(["000688"]), ["sz000688"])
        self.assertEqual(MARKET_INDEX_SYMBOLS["sh000688"], "科创50")
        self.assertNotIn("sz000688", MARKET_INDEX_SYMBOLS)


if __name__ == "__main__":
    unittest.main()
