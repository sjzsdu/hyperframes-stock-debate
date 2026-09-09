import unittest

from stocktalk.modules.stock_data import StockDataClient, StockDataConfig, TongstockCommandError, TongstockUnavailableError, normalize_code


class StockDataClientTests(unittest.TestCase):
    def test_normalizes_exchange_prefixes(self):
        self.assertEqual(normalize_code("SH600519"), "600519")
        self.assertEqual(normalize_code("000001.SZ"), "000001")
        with self.assertRaises(ValueError):
            normalize_code("AAPL")

    def test_parses_quote_text_and_json_with_log_prefix(self):
        text_client = StockDataClient(runner=lambda argv, timeout: "600519 贵州茅台\n  最新价: 1292.960\n  开盘: 1305.010 最高: 1309.300 最低: 1292.010\n  成交量: 13855.00 手\n  成交额: 179762.41 万\n")
        quote = text_client.get_quote("600519")
        self.assertEqual(quote["price"], 1292.96)
        self.assertEqual(quote["high"], 1309.3)

        json_client = StockDataClient(runner=lambda argv, timeout: "sync log\n{\"code\":\"000001\",\"name\":\"平安银行\",\"current\": 10.5}")
        self.assertEqual(json_client.get_quote("000001")["price"], 10.5)

    def test_unsupported_finance_is_reported_without_losing_other_data(self):
        def runner(argv, timeout):
            if argv[1] in {"finance", "kline", "company"}:
                raise TongstockUnavailableError("tongstock does not support command")
            if argv[1] == "quote":
                return "{\"code\": \"600519\", \"price\": 100}"
            return "{\"summary\": {\"trend\": \"bullish\"}, \"history\": []}"

        data = StockDataClient(runner=runner).get_stock_data("600519")
        self.assertEqual(data["quote"]["price"], 100.0)
        self.assertEqual(data["technical"]["summary"]["trend"], "bullish")
        self.assertIn("financials", data["unavailable"])
        self.assertIn("f10", data["unavailable"])

    def test_f10_keeps_directory_and_each_block_separately(self):
        def runner(argv, timeout):
            if argv[1] == "company":
                return 'log\n{"files": ["gsgk.txt"]}'
            if argv[1] == "company-content" and "公司概况" in argv:
                return "贵州茅台公司概况"
            if argv[1] == "company-content":
                raise TongstockCommandError("section unavailable")
            raise AssertionError(argv)

        client = StockDataClient(StockDataConfig(retries=0), runner=runner)
        f10 = client.get_f10("600519")
        self.assertEqual(f10["directory"], {"files": ["gsgk.txt"]})
        self.assertEqual(f10["sections"]["公司概况"], "贵州茅台公司概况")
        self.assertIn("财务分析", f10["unavailable_sections"])

    def test_kline_normalizes_history(self):
        client = StockDataClient(runner=lambda argv, timeout: '{"data": [{"date":"2026-01-01","open":1,"close":2,"ignored":3}]}')
        self.assertEqual(client.get_kline("000001"), [{"date": "2026-01-01", "open": 1, "close": 2}])


if __name__ == "__main__":
    unittest.main()
