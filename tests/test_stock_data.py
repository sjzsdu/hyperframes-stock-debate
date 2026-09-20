import json
import unittest

from stocktalk.modules.stock_data import (StockDataClient, StockDataConfig, StockDataError, StockDataIncompleteError,
                                          TongstockCommandError, TongstockUnavailableError, normalize_code)


# tongstock returns F10 blocks as {"block","code","content"} where content is
# TDX box-drawing table text.  Tests must feed that real shape, otherwise the
# profile parser is not being exercised at all.
F10_OVERVIEW = (
    "公司概况☆ ◇600519 贵州茅台 更新日期：2026-09-18◇ 通达信沪深京F10\n"
    "★本栏包括【1.基本资料】【2.发行和交易】\n"
    "【1.基本资料】\n"
    "┌───────┬───────────────────────────────┐\n"
    "│公司名称      │贵州茅台酒股份有限公司                                        │\n"
    "├───────┼───────────────────────────────┤\n"
    "│主营业务      │茅台酒及系列酒的生产与销售                                    │\n"
    "├───────┼───────────────────────────────┤\n"
    "│通达信研究行业│食品饮料-酿酒              │证监会行业    │酒、饮料和精制茶制造业│\n"
    "├───────┼───────────────────────────────┤\n"
    "│董事长        │陈华                      │总经理        │王莉                  │\n"
    "├───────┼───────────────────────────────┤\n"
    "│经营范围      │茅台酒系列产品的生产与销售；饮料、食品、包装材料的生产、销售；│\n"
    "│              │防伪技术开发、信息产业相关产品的研制、开发。                  │\n"
    "└───────┴───────────────────────────────┘\n"
)


def f10_response(argv):
    """Serve `company` / `company-content` / `finance` from the samples.

    ``finance`` is included because 公司基本面 datasets are mandatory: a runner
    that answers them with an empty object aborts the whole call, which would
    turn every unrelated test into a "missing fundamentals" test.
    """
    if argv[1] == "company":
        return 'log\n[{"Name": "公司概况", "Length": 1}]'
    if argv[1] == "company-content":
        block = argv[argv.index("--block") + 1]
        content = F10_OVERVIEW if block == "公司概况" else ""
        return json.dumps({"block": block, "code": "600519", "content": content})
    if argv[1] == "finance":
        # 600519 2026H1 in TDX units: 千元 for amounts, 万股 for share counts.
        return json.dumps({"ZhuYingShouRu": 90703264, "JingLiRun": 44516880, "YingYeLiRun": 61411288,
                           "ZongGuBen": 125008.15625, "LiuTongGuBen": 125008.15625,
                           "MeiGuJingZiChan": 200.99, "UpdatedDate": 20260815})
    return None


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
            return f10_response(argv) or '{"summary": {}, "history": []}'

        data = StockDataClient(runner=runner).get_stock_data("600519")
        self.assertEqual(data["quote"]["name"], "贵州茅台")

    def test_spaced_cjk_name_in_codes_list_is_collapsed(self):
        # Real `tongstock codes list` output spaces every CJK character:
        # "002224 三 力 士 [深市主板] 深交所".
        def runner(argv, timeout):
            if argv[1] == "quote":
                return "002224 \n  最新价: 3.660\n"
            if argv[1] == "codes":
                return "002224 三 力 士 [深市主板] 深交所\n600519 贵 州 茅 台 [沪市A股] 上交所\n"
            return f10_response(argv) or '{"summary": {}, "history": []}'

        data = StockDataClient(runner=runner).get_stock_data("002224")
        self.assertEqual(data["quote"]["name"], "三力士")

    def test_exchange_flag_matches_code_prefix(self):
        self.assertEqual(StockDataClient._exchange_of("600519"), "sh")
        self.assertEqual(StockDataClient._exchange_of("000001"), "sz")
        self.assertEqual(StockDataClient._exchange_of("300750"), "sz")
        self.assertEqual(StockDataClient._exchange_of("832000"), "bj")

    def test_unsupported_finance_aborts_instead_of_degrading_the_script(self):
        """公司基本面拿不到就中断——缺口该由 tongstock 补，不在本项目里静默跳过。"""
        def runner(argv, timeout):
            if argv[1] in {"finance", "kline", "company"}:
                raise TongstockUnavailableError("tongstock does not support command")
            if argv[1] == "quote":
                return "{\"code\": \"600519\", \"price\": 100}"
            return "{\"summary\": {\"trend\": \"bullish\"}, \"history\": []}"

        with self.assertRaises(StockDataIncompleteError) as ctx:
            StockDataClient(runner=runner).get_stock_data("600519")
        self.assertIn("tongstock", str(ctx.exception))
        # 只有明确关掉校验才放行（应急用，不推荐）
        data = StockDataClient(StockDataConfig(require_company_data=False), runner=runner).get_stock_data("600519")
        self.assertEqual(data["quote"]["price"], 100.0)
        self.assertEqual(data["technical"]["summary"]["trend"], "bullish")
        self.assertIn("financials", data["unavailable"])

    def test_f10_keeps_directory_and_parses_each_block_into_a_profile(self):
        def runner(argv, timeout):
            if argv[1] == "company":
                return 'log\n[{"Name": "公司概况", "Length": 1}]'
            if argv[1] == "company-content" and "公司概况" in argv:
                return json.dumps({"block": "公司概况", "code": "600519", "content": F10_OVERVIEW})
            if argv[1] == "company-content":
                raise TongstockCommandError("section unavailable")
            raise AssertionError(argv)

        client = StockDataClient(StockDataConfig(retries=0), runner=runner)
        f10 = client.get_f10("600519")
        self.assertEqual(f10["directory"], [{"Name": "公司概况", "Length": 1}])
        self.assertIn("贵州茅台酒股份有限公司", f10["sections"]["公司概况"])
        # 表格文本必须被解析成可讲的生意事实，而不是原样丢给模型
        self.assertEqual(f10["profile"]["主营业务"], "茅台酒及系列酒的生产与销售")
        self.assertEqual(f10["profile"]["所属行业"], "食品饮料-酿酒")
        self.assertEqual(f10["profile"]["董事长"], "陈华")
        # 经营范围跨行，续行要接回上一个字段，不能丢
        self.assertIn("防伪技术开发", f10["profile"]["经营范围"])
        self.assertIn("经营分析", f10["unavailable_sections"])

    def test_f10_aborts_when_no_business_fact_can_be_parsed(self):
        """拿得到分块却解析不出主营/行业 = 解析链路坏了，必须中断而不是空着跑。"""
        client = StockDataClient(runner=lambda argv, timeout: json.dumps({"block": "x", "content": ""}))
        with self.assertRaises(StockDataError):
            client.get_f10("600519")

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

        # 基本面缺失 → 中断；关掉校验才继续（此处只验证新闻本身被正确收集）
        data = StockDataClient(StockDataConfig(require_company_data=False), runner=runner).get_stock_data("600519")
        self.assertEqual(data["news"]["items"][0]["title"], "研报点评")

        def failing(argv, timeout):
            if argv[1] == "news":
                raise TongstockCommandError("news source down")
            if argv[1] == "quote":
                return "{\"code\": \"600519\", \"price\": 100}"
            if argv[1] == "indicator":
                return f10_response(argv) or '{"summary": {}, "history": []}'
            return f10_response(argv) or "{}"

        # 新闻不是必需数据集：拿不到只记录，不中断（脚本会少引用几条资讯）
        data = StockDataClient(runner=failing).get_stock_data("600519")
        self.assertIn("news", data["unavailable"])
        self.assertIsNone(data["news"])

    # ---- Board membership: the industry/concept facts behind the script ----

    def test_board_parses_industry_and_concepts_from_block_output(self):
        """行业/概念是 tongstock 真实提供的分类，是讲清生意的素材来源。"""
        def runner(argv, timeout):
            if "block_fg.dat" in argv:
                return ("2026/09/18 连接到 124.71.187.122:7709 成功\n"
                        "股票: 600519 贵州茅台 所属板块:\n"
                        "--------------------------------------------------\n"
                        "  行业龙头 (type:2, 127只成分股)\n"
                        "  非周期股 (type:2, 399只成分股)\n")
            return ("股票: 600519 贵州茅台 所属板块:\n"
                    "--------------------------------------------------\n"
                    "  白酒概念 (type:2, 49只成分股)\n"
                    "  乡村振兴 (type:2, 399只成分股)\n")

        board = StockDataClient(runner=runner).get_board("600519")
        self.assertEqual(board["name"], "贵州茅台")
        self.assertEqual(board["industry"], ["行业龙头", "非周期股"])
        self.assertIn("白酒概念", board["concepts"])

    def test_board_is_required_and_aborts_when_missing(self):
        def runner(argv, timeout):
            if argv[1] == "block":
                raise TongstockUnavailableError("tongstock does not support command")
            if argv[1] == "quote":
                return "{\"code\": \"600519\", \"price\": 100}"
            return f10_response(argv) or '{"summary": {}, "history": []}'

        with self.assertRaises(StockDataIncompleteError):
            StockDataClient(runner=runner).get_stock_data("600519")

    def test_quote_name_falls_back_to_block_header(self):
        """quote 常返回空名，block 的输出头带着交易所的正式名称。"""
        def runner(argv, timeout):
            if argv[1] == "quote":
                return "600519 \n  最新价: 100.0\n"
            if argv[1] == "block":
                return "股票: 600519 贵州茅台 所属板块:\n  白酒概念 (type:2, 49只成分股)\n"
            if argv[1] == "codes":
                return "600519 贵州茅台 [沪市A股] 上交所\n"
            return f10_response(argv) or '{"summary": {}, "history": []}'

        data = StockDataClient(runner=runner).get_stock_data("600519")
        self.assertEqual(data["quote"]["name"], "贵州茅台")


class F10ParsingTests(unittest.TestCase):
    """F10 是制表框文本，解析规则全是实测踩出来的坑，逐个钉住。"""

    def test_main_composition_keeps_only_the_newest_period(self):
        text = (
            "【2.主营构成分析】\n"
            "截止日期:2026-06-30\n"
            "项目名                             营业收入(元)  收入比例(%) 营业利润(元)  利润比例(%)  毛利率(%)\n"
            "─────────────────────────────────────────────────\n"
            "茅台酒(产品)                           777.24亿        84.23     717.24亿        86.62      92.28\n"
            "直销(销售模式)                         520.07亿        56.36     483.29亿        58.37      92.93\n"
            "\n"
            "截止日期:2025-12-31\n"
            "项目名                             营业收入(元)  收入比例(%) 营业利润(元)  利润比例(%)  毛利率(%)\n"
            "─────────────────────────────────────────────────\n"
            "酒类(行业)                            1687.75亿        98.09    1539.69亿        97.97      91.23\n"
        )
        result = StockDataClient._parse_main_composition(text)
        self.assertEqual(result["报告期"], "2026-06-30")
        self.assertEqual([row["项目"] for row in result["明细"]], ["茅台酒(产品)", "直销(销售模式)"])
        self.assertEqual(result["明细"][0]["毛利率(%)"], "92.28")

    def test_executives_exclude_part_time_roles_at_other_companies(self):
        """高管兼职里同一个人挂着别家公司的头衔，算进来就是张冠李戴。"""
        text = (
            "★本栏包括【1.高管持股变动】【2.高管列表】【3.高管兼职】【4.高管简介】\n"
            "【2.高管列表】\n"
            "姓名     性别   公司职务           学历                 薪酬(万元)   持股数(万股)\n"
            "─────────────────────────────────────────\n"
            "陈华     男     董事长             硕士研究生                  ---            ---\n"
            "郭田勇   男     独立董事           博士研究生                20.00            ---\n"
            "王莉     女     总经理(代)         硕士研究生                  ---            ---\n"
            "【3.高管兼职】\n"
            "郭田勇   平安健康医疗科技有限公司         独立非执行董事 201805                  -\n"
            "盛雷鸣   青岛啤酒股份有限公司             独立董事       202006                  -\n"
        )
        people = StockDataClient._parse_executives(text)
        self.assertEqual([p["姓名"] for p in people], ["陈华", "王莉"])
        self.assertEqual(people[1]["职务"], "总经理(代)")

    def test_themes_rejoin_tags_broken_across_lines(self):
        """续行会把「北上重仓」从中间劈开，直接切就变成两个废标签。"""
        text = (
            "★本栏包括【1.所属板块】【2.主题投资】\n"
            "【1.所属板块】\n"
            "概念:通达信88、白酒概念、乡村振兴\n"
            "风格:融资融券、大盘股、绩优股、北上 \n"
            "     重仓、券商金股、非周期股\n"
            "指数:上证50、沪深300\n"
            "【2.主题投资】\n"
        )
        themes = StockDataClient._parse_themes(text)
        self.assertEqual(themes["概念"], ["通达信88", "白酒概念", "乡村振兴"])
        self.assertIn("北上重仓", themes["风格"])
        self.assertNotIn("北上", themes["风格"])
        # 指数成员身份跟生意无关，且最长，不进 prompt
        self.assertNotIn("指数", themes)

    def test_financials_convert_tdx_units_to_yi(self):
        """千元→亿元、万股→亿股，用茅台 2026H1 的真实数值锚定。"""
        metrics = StockDataClient._normalize_financials({
            "ZhuYingShouRu": 90703264, "JingLiRun": 44516880, "ZongGuBen": 125008.15625,
            "MeiGuJingZiChan": 200.99, "UpdatedDate": 20260815})
        self.assertEqual(metrics["营业收入(亿元)"], 907.03)
        self.assertEqual(metrics["净利润(亿元)"], 445.17)
        self.assertEqual(metrics["总股本(亿股)"], 12.5)
        self.assertEqual(metrics["每股净资产(元)"], 200.99)
        self.assertEqual(metrics["报告期"], "2026-08-15")
        self.assertAlmostEqual(metrics["净利率(%)"], 49.08, places=2)

    def test_financials_without_metrics_fails_loudly(self):
        with self.assertRaises(StockDataError):
            StockDataClient._normalize_financials({"unrelated": 1})

    def test_industry_ranking_pulls_the_stock_rows_from_top30_tables(self):
        """排名表是空白对齐文本；只取本股所在行，不在表里就不给这个维度。"""
        text = (
            "行业分析☆ ◇605058 澳弘电子 更新日期：2026-09-19◇ 通达信沪深京F10\n"
            "★本栏包括【1.所属行业】【2.市场表现排名】【3.公司规模排名】\n"
            "          【4.估值水平排名】【5.财务状况排名】\n"
            "\n"
            "【1.所属行业】\n"
            " 所属研究行业:元器件(共66家)\n"
            "\n"
            "【2.市场表现排名】(前30) 截止日期:2026-09-18\n"
            "排名     股票名称      一周涨跌幅%    一月涨跌幅%    半年涨跌幅%\n"
            "───────────────────────────────────────────────\n"
            "1        澳弘电子            42.30          69.71          35.47\n"
            "2        威尔高              30.64          58.60          64.70\n"
            "\n"
            "【3.公司规模排名】(前30) 截止日期:2026-09-18\n"
            "排名     股票名称    A股总市值(亿)  股价(元)\n"
            "───────────────────────────────────────────────\n"
            "1        东山精密          3592.70       196.15\n"
        )
        # 公司概况给的是全称，排名表用简称——双向子串匹配必须兜住这个差
        ranking = StockDataClient._parse_industry_ranking(text, "苏州澳弘电子股份有限公司")
        self.assertEqual(ranking["研究行业"], "元器件")
        self.assertEqual(ranking["同行家数"], 66)
        self.assertEqual(ranking["市场表现"],
                         {"排名": 1, "一周涨跌幅%": 42.3, "一月涨跌幅%": 69.71, "半年涨跌幅%": 35.47})
        self.assertNotIn("公司规模", ranking)   # 本股不在该表 → 宁缺毋滥
        self.assertEqual(StockDataClient._parse_industry_ranking("【1.所属行业】\n x", "某股"), {})

    def test_industry_ranking_works_without_a_stock_name(self):
        text = "【1.所属行业】\n 所属研究行业:元器件(共66家)\n"
        ranking = StockDataClient._parse_industry_ranking(text)
        self.assertEqual(ranking, {"研究行业": "元器件", "同行家数": 66})


if __name__ == "__main__":
    unittest.main()
