import unittest

from stocktalk.modules.stock_data import (StockDataClient, StockDataConfig, StockDataError, TongstockCommandError,
                                          TongstockUnavailableError, normalize_code)


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
        self.assertEqual(quote["name"], "贵州茅台")

        json_client = StockDataClient(runner=lambda argv, timeout: "sync log\n{\"code\":\"000001\",\"name\":\"平安银行\",\"current\": 10.5}")
        self.assertEqual(json_client.get_quote("000001")["price"], 10.5)

    def test_text_quote_without_name_does_not_capture_field_labels(self):
        client = StockDataClient(runner=lambda argv, timeout: "600519 \n  最新价: 1281.000\n  开盘: 1281.000 最高: 1281.000 最低: 1281.000\n  成交量: 158.00 手\n  成交额: 2023.98 万\n")
        quote = client.get_quote("600519")
        self.assertEqual(quote["name"], "")
        self.assertEqual(quote["price"], 1281.0)

    def test_missing_quote_name_resolved_from_codes_list(self):
        def runner(argv, timeout):
            if argv[1] == "quote":
                return "600519 \n  最新价: 100.0\n"
            if argv[1] == "codes":
                return "600519 贵州茅台 [沪市A股] 上交所\n000001 平安银行 [深市A股] 深交所\n"
            return "{\"summary\": {}, \"history\": []}"

        data = StockDataClient(runner=runner).get_stock_data("600519")
        self.assertEqual(data["quote"]["name"], "贵州茅台")

    def test_exchange_flag_matches_code_prefix(self):
        self.assertEqual(StockDataClient._exchange_of("600519"), "sh")
        self.assertEqual(StockDataClient._exchange_of("000001"), "sz")
        self.assertEqual(StockDataClient._exchange_of("300750"), "sz")
        self.assertEqual(StockDataClient._exchange_of("832000"), "bj")

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

    def test_history_bars_stay_close_only_without_fake_ohlc(self):
        """Derived bars must never synthesize open/high/low — charts use real candles only."""
        technical = {"history": [{"timestamp": "2026-09-10", "price": {"current": 12.5, "change": 0.3}, "volume": 100}]
                              }
        bars = StockDataClient._history_to_bars(technical)
        self.assertEqual(bars, [{"date": "2026-09-10", "close": 12.5, "change": 0.3, "volume": 100.0}])
        self.assertNotIn("open", bars[0])

    def test_news_query_normalized_and_optional_days(self):
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            if "--days" in argv:
                raise AssertionError("--days should not be passed when news_days is 0")
            return ('sync log\n{"code": "600519", "items": ['
                    '{"title": "自营店调价", "summary": "飞天上调13元", "source": "东方财富", "newsType": "其他", '
                    '"publishTime": "2026-09-09T09:59:52+08:00", "tags": ["第一财经"], "url": "http://e.com/1", "extra": 1}, '
                    '{"title": ""}, "not-a-dict"]}')

        news = StockDataClient(runner=runner).get_news("600519")
        self.assertEqual(news["code"], "600519")
        self.assertEqual(news["items"], [{"title": "自营店调价", "summary": "飞天上调13元", "source": "东方财富",
                                          "type": "其他", "publish_time": "2026-09-09", "url": "http://e.com/1", "tags": ["第一财经"]}])
        self.assertIn("--limit", calls[0])

    def test_news_respects_days_config_and_fails_closed(self):
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return '{"items": []}'

        client = StockDataClient(StockDataConfig(retries=0, news_days=30), runner=runner)
        self.assertEqual(client.get_news("600519")["items"], [])
        self.assertIn("--days", calls[0])
        with self.assertRaises(StockDataError):
            StockDataClient(runner=lambda argv, timeout: "not json").get_news("600519")

    def test_get_stock_data_collects_news_and_reports_unavailability(self):
        def runner(argv, timeout):
            if argv[1] == "news":
                return '{"items": [{"title": "研报点评", "newsType": "研报", "publishTime": "2026-09-10T00:00:00+08:00"}]}'
            if argv[1] in {"finance", "kline", "company"}:
                raise TongstockUnavailableError("tongstock does not support command")
            if argv[1] == "quote":
                return "{\"code\": \"600519\", \"price\": 100}"
            return "{\"summary\": {\"trend\": \"bullish\"}, \"history\": []}"

        data = StockDataClient(runner=runner).get_stock_data("600519")
        self.assertEqual(data["news"]["items"][0]["title"], "研报点评")

        def failing(argv, timeout):
            if argv[1] == "news":
                raise TongstockCommandError("news source down")
            if argv[1] == "quote":
                return "{\"code\": \"600519\", \"price\": 100}"
            if argv[1] == "indicator":
                return "{\"summary\": {}, \"history\": []}"
            raise TongstockUnavailableError("tongstock does not support command")

        data = StockDataClient(runner=failing).get_stock_data("600519")
        self.assertIn("news", data["unavailable"])
        self.assertIsNone(data["news"])


if __name__ == "__main__":
    unittest.main()
