import json
import unittest
from unittest.mock import patch

from stocktalk.modules.dialogue_generator import DialogueGenerationError, DialogueGenerator


class DialogueGeneratorTests(unittest.TestCase):
    def _response(self):
        return json.dumps({"title": "平安银行（000001）：靠息差吃饭的生意", "turns": [
            {"speaker": "bull", "beat": "生意本质", "line": "银行的生意说简单也简单，把存款变成贷款，赚中间的息差，平安银行零售做得多，客户基础是关键。"},
            {"speaker": "bear", "beat": "行业追问", "line": "息差这门生意要看行业周期，利率下行的时候大家都紧，不良贷款率才是真功夫。"},
            {"speaker": "bull", "beat": "观点修正", "line": "这点我认同。增长不是直线，真正值得讨论的是管理层能否在周期波动里把客户关系变成更深的护城河。"},
        ]}, ensure_ascii=False)

    def test_generates_natural_ordered_turns(self):
        calls = []
        generator = DialogueGenerator({"dialogue": {"max_line_chars": 80}, "characters": {"bull": {"name": "新手"}}},
                                      runner=lambda argv, timeout: calls.append((argv, timeout)) or self._response())
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = generator.generate({"code": "000001", "quote": {"name": "平安银行", "price": 10.5}})
        self.assertNotIn("rounds", script)
        self.assertEqual([turn["speaker"] for turn in script["turns"]], ["bull", "bear", "bull"])
        self.assertEqual(script["turns"][0]["character_name"], "新手")
        self.assertTrue(all(len(turn["line"]) <= 80 for turn in script["turns"]))
        self.assertIn("不要编号、不要 Round", calls[0][0][calls[0][0].index("--message") + 1])
        self.assertIn("行业", calls[0][0][calls[0][0].index("--system") + 1])

    def test_prompt_focuses_on_business_not_technicals(self):
        generator = DialogueGenerator(runner=lambda argv, timeout: self._response())
        system = generator._system_prompt()
        message = generator._user_prompt({"code": "1"})
        self.assertIn("靠什么赚钱", system)
        self.assertIn("行业", system)
        self.assertIn("生意", system)
        # 公司档案（F10 解析出的主营/主营构成/管理层）是讲生意的第一手依据
        self.assertIn("公司档案", system)
        self.assertIn("主营构成", system)
        self.assertIn("管理层", system)
        self.assertIn("board", system)
        # 数据里没有的产品/客户/管理层一律不得脑补
        self.assertIn("不得编造", system)
        self.assertIn("至少一半的篇幅围绕公司业务和行业本身", message)
        self.assertIn("技术信号最多作为一句带过的佐证", message)

    def test_prompt_budgets_characters_from_the_duration_target(self):
        """A 110s target must become a hard character budget, not 'about 2 minutes'."""
        generator = DialogueGenerator({"dialogue": {"min_duration_seconds": 70, "max_duration_seconds": 110,
                                                    "max_line_chars": 90}})
        message = generator._user_prompt({"code": "1"})
        self.assertIn("70-110 秒", message)
        # 110s * 4.8 chars/s = 528, 70s * 4.8 = 336
        self.assertIn("336-528 字", message)
        self.assertIn("45-90 字", message)
        self.assertNotIn("分钟之间", message)

    def test_default_budget_targets_a_two_to_five_minute_cut(self):
        """默认配置按 2-5 分钟算预算，并把轮数收敛成「少轮、讲透」的区间。

        轮数用典型单轮（97 字）估算而不是极值：极值会配出 4-22 轮这种宽区间，
        模型往中间凑，长片的时长反而失控。
        """
        message = DialogueGenerator()._user_prompt({"code": "1"})
        self.assertIn("120-300 秒", message)
        self.assertIn("约 2-5 分钟", message)
        # 120s × 4.8 = 576 字，300s × 4.8 = 1440 字
        self.assertIn("576-1440 字", message)
        # 单轮 65-130 字，轮数按典型 97 字估 → 6-14 轮
        self.assertIn("65-130 字", message)
        self.assertIn("6-14 轮", message)
        # 长片要的是深度，prompt 必须点明这一点
        self.assertIn("这是一条长视频", message)
        self.assertIn("把一轮讲透", message)

    def test_fixed_duration_reads_as_a_single_minute_span(self):
        """--duration-minutes 4 会把上下限压成同一个值，prompt 别写成「约 4-4 分钟」。"""
        generator = DialogueGenerator({"dialogue": {"min_duration_seconds": 240, "max_duration_seconds": 240}})
        message = generator._user_prompt({"code": "1"})
        self.assertIn("（约 4 分钟）", message)
        self.assertNotIn("4-4 分钟", message)

    def test_script_reports_estimated_speech_duration(self):
        """结果里带上字数与换算时长，好让流水线在渲染前就能提醒超长。"""
        generator = DialogueGenerator(runner=lambda argv, timeout: self._response())
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = generator.generate({"code": "000001", "quote": {"name": "平安银行"}})

        chars = sum(len(turn["line"]) for turn in script["turns"])
        self.assertEqual(script["char_count"], chars)
        self.assertAlmostEqual(script["estimated_seconds"], round(chars / 4.8, 1), places=1)

    def test_script_has_no_disclaimer_or_visual_prompt_fields(self):
        generator = DialogueGenerator(runner=lambda argv, timeout: self._response())
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = generator.generate({"code": "000001", "quote": {"name": "平安银行"}})
        self.assertNotIn("disclaimer", script)
        for turn in script["turns"]:
            self.assertNotIn("visual_prompt", turn)

    def test_accepts_bailian_json_envelope(self):
        response = json.dumps({"content": json.dumps({"turns": [{"speaker": "bear", "line": "数据的边界比结论更重要。"}]}, ensure_ascii=False)})
        generator = DialogueGenerator(runner=lambda argv, timeout: response)
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = generator.generate({"code": "1"})
        self.assertEqual(script["turns"][0]["speaker"], "bear")

    def test_rejects_missing_or_invalid_speaker(self):
        generator = DialogueGenerator(runner=lambda argv, timeout: '{"turns":[{"speaker":"host","line":"x"}]}')
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            with self.assertRaises(DialogueGenerationError): generator.generate({"code": "1"})

    def test_prompt_includes_trimmed_recent_market_digest(self):
        generator = DialogueGenerator(runner=lambda argv, timeout: self._response())
        message = generator._user_prompt(generator._compact_data({"technical": {"summary": {"rsi": 60}, "count": 2,
                                                                               "history": [{"timestamp": "2026-09-10", "price": {"current": 12.3, "change_pct": 1.2},
                                                                                           "macd": {"signal": "golden_cross"}}]}}))
        self.assertIn("rsi", message)
        self.assertIn("2026-09-10", message)
        self.assertIn("golden_cross", message)
        self.assertNotIn("\"history\"", message)

    def test_company_profile_replaces_raw_f10_tables_in_the_prompt(self):
        """F10 原文是几万字的制表框，进去只会挤掉别的内容；进 prompt 的是解析后的档案。"""
        generator = DialogueGenerator(runner=lambda argv, timeout: self._response())
        payload = generator._compact_data({"code": "600519", "f10": {
            "sections": {"公司概况": "│" * 40 + " 几万字的表格 "},
            "profile": {"主营业务": "茅台酒及系列酒的生产与销售", "所属行业": "食品饮料-酿酒",
                        "主营构成": {"报告期": "2026-06-30", "明细": [
                            {"项目": "茅台酒(产品)", "收入": "777.24亿", "收入占比(%)": "84.23", "毛利率(%)": "92.28"}]},
                        "管理层": [{"姓名": "陈华", "职务": "董事长"}]}}})
        self.assertEqual(payload["f10"]["主营业务"], "茅台酒及系列酒的生产与销售")
        self.assertEqual(payload["f10"]["主营构成"]["明细"][0]["毛利率(%)"], "92.28")
        self.assertNotIn("sections", payload["f10"])
        self.assertNotIn("几万字的表格", json.dumps(payload, ensure_ascii=False))

    def test_empty_company_profile_is_dropped_from_the_prompt(self):
        generator = DialogueGenerator(runner=lambda argv, timeout: self._response())
        self.assertNotIn("f10", generator._compact_data({"code": "600519", "f10": {"sections": {}, "profile": {}}}))

    def test_market_metrics_go_into_prompt_with_explicit_units(self):
        generator = DialogueGenerator(runner=lambda argv, timeout: self._response())
        payload = generator._compact_data({"code": "600519", "stockinfo": {
            "code": "600519", "name": "贵州茅台", "metrics": {"市值(亿元)": 16168.55, "换手率(%)": 0.09}}})
        self.assertEqual(payload["stockinfo"], {"市值(亿元)": 16168.55, "换手率(%)": 0.09})

    def test_news_digest_goes_into_prompt_and_empties_are_dropped(self):
        generator = DialogueGenerator(runner=lambda argv, timeout: self._response())
        payload = generator._compact_data({"code": "6001-9X".replace("-", ""), "news": {"items": [
            {"title": "壹评级：线下自营店调价", "type": "其他", "source": "东方财富", "publish_time": "2026-09-09",
             "summary": "飞天由1753元上调至1766元", "url": "http://e.com/1"},
            {"title": ""},
        ]}})
        message = generator._user_prompt(payload)
        self.assertIn("线下自营店调价", message)
        self.assertIn("2026-09-09", message)
        self.assertIn("飞天由1753元上调至1766元", message)
        self.assertNotIn("url", message)
        self.assertIn("news", payload)

        empty = generator._compact_data({"code": "600519", "news": {"items": []}})
        self.assertNotIn("news", empty)


if __name__ == "__main__":
    unittest.main()
