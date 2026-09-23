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
        """默认配置按 2-5 分钟算预算，轮次多、单轮短（2026-09-22 用户反馈）。

        轮数用典型单轮（60 字）估算而不是极值：极值会配出宽区间，
        模型往中间凑，长片的时长反而失控。
        """
        message = DialogueGenerator()._user_prompt({"code": "1"})
        self.assertIn("120-300 秒", message)
        self.assertIn("约 2-5 分钟", message)
        # 120s × 4.8 = 576 字，300s × 4.8 = 1440 字
        self.assertIn("576-1440 字", message)
        # 单轮 40-80 字，轮数按典型 60 字估 → 9-24 轮
        self.assertIn("40-80 字", message)
        self.assertIn("9-24 轮", message)
        # 长片要的是深度，prompt 必须点明这一点
        self.assertIn("这是一条长视频", message)
        # 节奏导向：多轮短交锋，但每轮仍要有新信息
        self.assertIn("多轮短交锋", message)
        self.assertIn("严禁把同一个意思换句话再说一遍", message)

    def test_fixed_duration_reads_as_a_single_minute_span(self):
        """--duration-minutes 4 会把上下限压成同一个值，prompt 别写成「约 4-4 分钟」。"""
        generator = DialogueGenerator({"dialogue": {"min_duration_seconds": 240, "max_duration_seconds": 240}})
        message = generator._user_prompt({"code": "1"})
        self.assertIn("（约 4 分钟）", message)
        self.assertNotIn("4-4 分钟", message)

    def test_prompt_puts_sentence_integrity_above_the_char_budget(self):
        """2026-09-22 000066 实锤：模型把字数上限当硬约束，把句子压成半截
        （「…产能释。」「…反而。」），配音照念，听感就是语音被掐掉。
        prompt 必须明确：字数是节奏参考，句子完整性优先，装不下拆轮。"""
        message = DialogueGenerator()._user_prompt({"code": "1"})
        self.assertIn("先把句子说完整", message)
        self.assertIn("宁可多拆一轮", message)
        self.assertIn("完整自然的话", message)

    def test_overlong_line_is_split_into_complete_continuation_turns(self):
        """模型没管住字数时，超长台词拆成同说话人的续轮——一个字不丢，
        也不再把半句硬截成「…产能释。」。"""
        long_line = ("政企采购的订单毛利其实不高，计算产业毛利率只有百分之十七，"
                     "但是它的粘性在于后续的运维和集成服务，客户一旦用了就很难换供应商，"
                     "这才是这门生意真正的护城河所在，也是利润率能慢慢爬起来的原因。")
        self.assertGreater(len(long_line), 80)
        response = json.dumps({"title": "t", "turns": [
            {"speaker": "bull", "beat": "生意本质", "line": long_line}]}, ensure_ascii=False)
        generator = DialogueGenerator({"dialogue": {"max_line_chars": 80},
                                       "characters": {"bull": {"name": "新手"}, "bear": {"name": "老手"}}},
                                      runner=lambda argv, timeout: response)
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = generator.generate({"code": "000001", "quote": {"name": "平安银行"}})
        turns = script["turns"]
        self.assertGreater(len(turns), 1, "超长台词必须拆成多轮")
        self.assertTrue(all(turn["speaker"] == "bull" for turn in turns))
        self.assertTrue(all(len(turn["line"]) <= 80 for turn in turns))
        self.assertTrue(all(turn["beat"] == turns[0]["beat"] for turn in turns),
                        "续轮必须落在同一个节拍上")
        # 内容无损：拆分只会在断点处把句读点换成句号，文字一个不丢
        import re as _re
        strip = lambda s: _re.sub(r"[。！？…，、；：,]", "", s)
        self.assertEqual(strip("".join(turn["line"] for turn in turns)), strip(long_line))
        # 续轮是正常发言，不能被当成短回应跳过图板
        self.assertFalse(any(turn["short"] for turn in turns))

    def test_split_prefers_sentence_boundaries_over_hard_cuts(self):
        """拆分点优先落在完整句边界，切出来的每轮都以完整句收尾。"""
        generator = DialogueGenerator({"dialogue": {"max_line_chars": 40}})
        text = ("第一句话讲的是生意模式到底是什么。第二句话讲的是这门生意最要命的成本在哪里。"
                "第三句话讲的是现金流什么时候能转正。")
        pieces = generator._split_line(text)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(piece[-1] in "。！？…" for piece in pieces))
        self.assertTrue(all(len(piece) <= 40 for piece in pieces))
        self.assertEqual("".join(pieces), text)

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


    # ---- 剧本骨架：结构不再由 prompt 字符串里的固定主线决定 ---------------

    def _arc_response(self):
        """带 beat / 互动字段的完整响应。"""
        return json.dumps({
            "title": "华瓷股份（001216）：九成收入靠色釉陶瓷",
            "turns": [
                {"speaker": "bull", "beat": "revenue", "line": "上半年 5.76 亿营收里色釉陶瓷占了 90.56%，毛利率 33.52%。"},
                {"speaker": "bear", "beat": "margin", "line": "九成收入压在一类产品上，抗风险能力得再看两眼。"},
                {"speaker": "bull", "beat": "cash", "line": "嗯。"},
                {"speaker": "bear", "beat": "occupied", "line": "存货 2.76 亿、应收 1.67 亿都压在账上。"},
                {"speaker": "bull", "beat": "shadow", "line": "所以真正要盯的是产能爬坡的实际产出。"},
            ],
            "sides": {"bull": "单一品类做到九成，说明产品力扎实", "bear": "客户集中就是命门"},
            "hook": "下一份财报的外销收入占比",
            "question": "你更信渠道还是产能？",
        }, ensure_ascii=False)

    def _annual_generator(self, response=None):
        return DialogueGenerator({"dialogue": {"arc": "annual", "max_line_chars": 80},
                                  "characters": {"bull": {"name": "新手"}, "bear": {"name": "老手"}}},
                                 runner=lambda argv, timeout: response or self._arc_response())

    def _full_data(self):
        """年报逐条所需的真实数据：主营构成（收入/毛利）+ 现金流与存货/应收。

        骨架只认"压平后的 payload"路径（f10.主营构成.明细 / financials.*），所以这里给
        stock_data 的原始形状，由 _compact_data 压平。缺数据的节拍会被正常剪枝——那是
        设计如此；这几个测试要验的恰恰是"数据齐了，骨架就该全须全尾地进 prompt"。
        """
        return {
            "code": "001216",
            "quote": {"name": "华瓷股份"},
            "f10": {"profile": {
                "主营业务": "色釉陶瓷的研发、生产与销售",
                "所属行业": "轻工制造-陶瓷",
                "主营构成": {"报告期": "2026-06-30", "明细": [
                    {"项目": "色釉陶瓷(产品)", "收入": "5.22亿", "收入占比(%)": "90.56", "毛利率(%)": "33.52"},
                    {"项目": "其他(产品)", "收入": "0.54亿", "收入占比(%)": "9.44", "毛利率(%)": "18.10"}]}}},
            "financials": {"报告期": "2026-06-30", "营业收入(亿元)": 5.76, "净利润(亿元)": 0.71,
                           "经营现金流(亿元)": 0.83, "存货(亿元)": 2.76, "应收账款(亿元)": 1.67},
        }

    def test_system_prompt_renders_the_chosen_arc_instead_of_a_fixed_mainline(self):
        generator = self._annual_generator()
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            generator.generate(self._full_data())
        system = generator._system_prompt()
        # 骨架的节拍与分工进了 prompt
        self.assertIn("本期骨架：年报逐条", system)
        for key in ("revenue", "margin", "cash", "occupied", "shadow"):
            self.assertIn(key, system)
        # 旧的固定主线（①…⑥）必须消失，否则模型还是会照着它写
        self.assertNotIn("①这门生意是什么", system)
        # 反套话与短回应许可
        self.assertIn("反套话", system)
        self.assertIn("咱们先看", system)   # 作为被禁止的套式被列出来
        self.assertIn("该长则长、该短则短", system)

    def test_user_prompt_carries_the_arc_order_and_interaction_fields(self):
        generator = self._annual_generator()
        message = generator._user_prompt(generator._compact_data(self._full_data()))
        self.assertIn("revenue→margin→cash→occupied→shadow", message)
        self.assertNotIn("整体按这条主线层层递进", message)
        for field in ("hook", "question", "sides"):
            self.assertIn(field, message)
        # 互动文案的红线要写在 prompt 里，不能等合规层事后擦
        self.assertIn("禁止询问买卖", message)
        self.assertIn("可验证的具体变量", message)

    def test_script_records_the_arc_beats_and_interaction_copy(self):
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = self._annual_generator().generate(self._full_data())
        self.assertEqual(script["arc"]["id"], "annual")
        self.assertEqual(script["arc"]["beats"], ["revenue", "margin", "cash", "occupied", "shadow"])
        self.assertEqual(script["hook"], "下一份财报的外销收入占比")
        self.assertEqual(script["question"], "你更信渠道还是产能？")
        self.assertEqual(script["sides"]["bull"], "单一品类做到九成，说明产品力扎实")
        self.assertEqual(script["sides"]["bear"], "客户集中就是命门")
        self.assertEqual(script["arc_notes"], [])

    def test_missing_interaction_fields_degrade_without_breaking(self):
        """模型没给 hook/question/sides 时不能抛错——只是片尾退化成原样。"""
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = self._annual_generator(response=self._response()).generate(self._full_data())
        self.assertEqual(script["hook"], "")
        self.assertEqual(script["question"], "")
        self.assertEqual(script["sides"], {"bull": "", "bear": ""})

    def test_beat_outside_the_arc_is_carried_forward_and_reported(self):
        """模型报了个骨架外的 beat：结构不能被它带乱，但偏差要留痕。"""
        response = json.dumps({"turns": [
            {"speaker": "bull", "beat": "revenue", "line": "收入结构先看主营构成。"},
            {"speaker": "bear", "beat": "生意本质", "line": "这个 beat 不在年报逐条里。"},
        ]}, ensure_ascii=False)
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = self._annual_generator(response=response).generate(self._full_data())
        self.assertEqual([turn["beat"] for turn in script["turns"]], ["revenue", "revenue"])
        self.assertTrue(any("不在本期骨架内" in note for note in script["arc_notes"]))

    def test_beat_cannot_rewind_the_arc_order(self):
        response = json.dumps({"turns": [
            {"speaker": "bull", "beat": "cash", "line": "先跳到现金流。"},
            {"speaker": "bear", "beat": "revenue", "line": "又想回到收入结构。"},
        ]}, ensure_ascii=False)
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = self._annual_generator(response=response).generate(self._full_data())
        self.assertEqual([turn["beat"] for turn in script["turns"]], ["cash", "cash"])
        self.assertTrue(any("回退了骨架顺序" in note for note in script["arc_notes"]))

    def test_short_reply_is_flagged_and_tolerated(self):
        """一句"嗯"也是合法的一轮：标成 short，交给渲染层跳过换图板。"""
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = self._annual_generator().generate(self._full_data())
        short = next(turn for turn in script["turns"] if turn["line"] == "嗯。")
        self.assertTrue(short["short"])
        self.assertFalse(script["turns"][0]["short"])   # 长台词不算短回应

    def test_forced_arc_that_lacks_data_still_falls_back_to_a_usable_arc(self):
        """配置强制一条骨架、这只票却没那份数据时，不能产出一个缺开场的空壳。"""
        generator = DialogueGenerator({"dialogue": {"arc": "peers"}, "characters": {}},
                                      runner=lambda argv, timeout: self._response())
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            generated = generator.generate({"code": "1", "quote": {"name": "平安银行"}})
        self.assertEqual(generated["arc"]["id"], "peers")   # 显式配置要被尊重
        self.assertTrue(generated["arc"]["forced"])
        self.assertTrue(generated["arc"]["pruned"], "缺数据的节拍要记录成被剪掉")


if __name__ == "__main__":
    unittest.main()
