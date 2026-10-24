"""单人独白形态（dialogue.format: monologue）的端到端守卫。

对辩片的所有既有测试仍是回归基线；这里钉死独白片与对辩片的分界线：
配置开关、骨架注册表隔离、prompt 分叉、speaker 归一、画面阵容与片尾图板。
"""
import json
import tempfile
import unittest
from pathlib import Path

from stocktalk.modules.arcs import (ARCS, DEEP_DIVE, MONOLOGUE_ARCS,
                                    MONOLOGUE_FALLBACK, MONOLOGUE_SPEAKER,
                                    arcs_for_format, fallback_arc, select_arc)
from stocktalk.modules.dialogue_generator import (FORMAT_DEBATE,
                                                 FORMAT_MONOLOGUE, FORMATS,
                                                 DialogueGenerator,
                                                 MonologueGenerator,
                                                 create_dialogue_generator,
                                                 dialogue_format,
                                                 ensure_narrator_character)
from stocktalk.modules.hyperframes_builder import HyperFramesBuilder


def _monologue_response():
    return json.dumps({"title": "平安银行（000001）：靠息差吃饭的生意", "turns": [
        {"speaker": MONOLOGUE_SPEAKER, "beat": "business",
         "line": "银行的生意说简单也简单，把存款变成贷款，赚中间的息差；平安银行零售做得多，客户基础就是它的本钱。"},
        {"speaker": MONOLOGUE_SPEAKER, "beat": "industry",
         "line": "但息差这门生意吃行业周期：利率下行时全行业都紧，不良贷款率才是真功夫，零售转型的代价也在这里。"},
        {"speaker": MONOLOGUE_SPEAKER, "beat": "takeaway",
         "line": "所以值得盯的变量就一个：下一季净息差能不能稳住——渠道里还有多少低成本存款，决定了它还有多少腾挪空间。"},
    ], "hook": "下一季净息差", "question": "低成本存款还能守多久"}, ensure_ascii=False)


class FormatConfigTests(unittest.TestCase):
    def test_default_is_debate(self):
        self.assertEqual(dialogue_format({}), FORMAT_DEBATE)
        self.assertEqual(dialogue_format(None), FORMAT_DEBATE)

    def test_unknown_format_falls_back_to_debate(self):
        self.assertEqual(dialogue_format({"dialogue": {"format": "talkshow"}}), FORMAT_DEBATE)

    def test_monologue_is_selectable(self):
        self.assertEqual(dialogue_format({"dialogue": {"format": "monologue"}}), FORMAT_MONOLOGUE)
        self.assertIn(FORMAT_MONOLOGUE, FORMATS)

    def test_ensure_narrator_fills_defaults_and_is_idempotent(self):
        config: dict = {"dialogue": {"format": "monologue"}, "characters": {}}
        ensure_narrator_character(config)
        narrator = config["characters"][MONOLOGUE_SPEAKER]
        self.assertEqual(narrator["name"], "主讲人")
        self.assertTrue(str(narrator["voice_id"]).strip())
        # 幂等：再补一次不覆盖已有值
        config["characters"][MONOLOGUE_SPEAKER]["name"] = "老王"
        ensure_narrator_character(config)
        self.assertEqual(config["characters"][MONOLOGUE_SPEAKER]["name"], "老王")

    def test_ensure_narrator_is_a_noop_in_debate(self):
        config: dict = {"characters": {"bull": {"name": "新手"}}}
        ensure_narrator_character(config)
        self.assertNotIn(MONOLOGUE_SPEAKER, config["characters"])


class ArcRegistryTests(unittest.TestCase):
    def test_format_registry_is_split(self):
        self.assertIn(DEEP_DIVE.id, MONOLOGUE_ARCS)
        self.assertNotIn(DEEP_DIVE.id, ARCS)
        self.assertEqual(arcs_for_format(FORMAT_MONOLOGUE), MONOLOGUE_ARCS)
        self.assertEqual(arcs_for_format(FORMAT_DEBATE), ARCS)
        self.assertEqual(arcs_for_format(None), ARCS)
        self.assertEqual(fallback_arc(FORMAT_MONOLOGUE), MONOLOGUE_FALLBACK)
        self.assertEqual(fallback_arc(FORMAT_DEBATE).id, "mainline")

    def test_deep_dive_carries_monologue_format_and_no_dual_speaker_beats(self):
        self.assertEqual(DEEP_DIVE.format, FORMAT_MONOLOGUE)
        for beat in DEEP_DIVE.beats:
            self.assertIsNone(beat.speaker, f"{beat.key} 不该指派说话人——独白只有 narrator")
            self.assertFalse(beat.short, f"{beat.key} 不该是短回应拍——没有对方可接")

    def test_select_arc_never_crosses_formats(self):
        # 独白形态下选骨架：结果必须来自 MONOLOGUE_ARCS（目前只有 deep_dive）
        choice = select_arc({"quote": {"code": "000001"}, "financials": {"revenue": "100亿"}},
                            format=FORMAT_MONOLOGUE)
        self.assertIn(choice.arc.id, MONOLOGUE_ARCS)
        # 对辩形态下强制 deep_dive：它不在对辩注册表里，应回落 mainline
        forced = select_arc({}, configured=DEEP_DIVE.id, format=FORMAT_DEBATE)
        self.assertNotEqual(forced.arc.id, DEEP_DIVE.id)
        # 对辩骨架一条都不该带 monologue 标记
        for arc in ARCS.values():
            self.assertEqual(arc.format, FORMAT_DEBATE)


class MonologueGeneratorTests(unittest.TestCase):
    def _generator(self, config=None):
        base = {"dialogue": {"format": "monologue"}, "characters": {}}
        if config:
            base.update(config)
        return MonologueGenerator(base, runner=lambda argv, timeout: _monologue_response())

    def test_factory_picks_generator_by_format(self):
        self.assertIsInstance(
            create_dialogue_generator({"dialogue": {"format": "monologue"}}), MonologueGenerator)
        self.assertIsInstance(
            create_dialogue_generator({"dialogue": {"format": "debate"}}), DialogueGenerator)
        self.assertIsInstance(create_dialogue_generator({}), DialogueGenerator)

    def test_monologue_budget_defaults(self):
        generator = self._generator()
        self.assertEqual(generator.config.min_duration_seconds, 120)
        self.assertEqual(generator.config.max_duration_seconds, 240)
        self.assertEqual(generator.config.max_line_chars, 220)
        self.assertFalse(generator.config.risk_exchange)
        self.assertEqual(generator.speakers, (MONOLOGUE_SPEAKER,))

    def test_monologue_config_overrides_are_honoured(self):
        generator = MonologueGenerator({
            "dialogue": {"format": "monologue",
                         "monologue": {"min_duration_seconds": 150,
                                       "max_duration_seconds": 200}},
            "characters": {}}, runner=lambda argv, timeout: _monologue_response())
        self.assertEqual(generator.config.min_duration_seconds, 150)
        self.assertEqual(generator.config.max_duration_seconds, 200)

    def test_debate_generator_ignores_monologue_block(self):
        generator = DialogueGenerator({
            "dialogue": {"monologue": {"max_line_chars": 220}},
            "characters": {}}, runner=lambda argv, timeout: "{}")
        self.assertEqual(generator.config.max_line_chars, 80)
        self.assertEqual(generator.speakers, ("bull", "bear"))

    def test_system_prompt_forces_single_speaker(self):
        system = self._generator()._system_prompt()
        self.assertIn(MONOLOGUE_SPEAKER, system)
        self.assertIn("一个人把一家公司讲透", system)
        self.assertNotIn("两人必须交替发言", system)

    def test_user_prompt_budgets_576_to_1152_chars(self):
        message = self._generator()._user_prompt({"code": "000001"})
        # 120s × 4.8 = 576, 240s × 4.8 = 1152
        self.assertIn("576-1152 字", message)
        self.assertIn("2-4 分钟", message)
        self.assertNotIn("sides", message)
        self.assertNotIn("看多立场", message)

    def test_generate_normalizes_speaker_and_skips_risk_exchange(self):
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return _monologue_response()

        generator = MonologueGenerator({"dialogue": {"format": "monologue"}, "characters": {}},
                                       runner=runner)
        script = generator.generate({"code": "000001", "quote": {"name": "平安银行"}})
        self.assertEqual(script["format"], FORMAT_MONOLOGUE)
        speakers = {turn["speaker"] for turn in script["turns"]}
        self.assertEqual(speakers, {MONOLOGUE_SPEAKER})
        # risk_exchange 关闭：没有"股市有风险/投资需谨慎"接力轮
        lines = [turn["line"] for turn in script["turns"]]
        self.assertFalse(any("股市有风险" in line or "投资需谨慎" in line for line in lines))
        self.assertEqual(script.get("sides"), {})

    def test_generate_coerces_debate_speaker_back_to_narrator(self):
        def runner(argv, timeout):
            return json.dumps({"title": "t", "turns": [
                {"speaker": "bull", "line": "模型带回来的对辩习惯，speaker 写了 bull，这里必须归一。"},
                {"speaker": "分析师", "line": "中文别名同样要归一，形态是产品决策不是模型的自由。"},
            ]}, ensure_ascii=False)

        generator = MonologueGenerator({"dialogue": {"format": "monologue"}, "characters": {}},
                                       runner=runner)
        script = generator.generate({"code": "000001"})
        self.assertTrue(all(turn["speaker"] == MONOLOGUE_SPEAKER for turn in script["turns"]))

    def test_debate_prompt_is_byte_identical_without_format_switch(self):
        """不开 format 时，对辩 prompt 一个字都不能变——独白是加法不是改写。"""
        debate = DialogueGenerator({"characters": {}}, runner=lambda argv, timeout: "{}")
        system = debate._system_prompt()
        self.assertIn("两人必须交替发言", system)
        user = debate._user_prompt({"code": "1"})
        self.assertIn("sides", user)
        self.assertIn("看多一方的一句话立场", user)


class MonologueBuilderTests(unittest.TestCase):
    def _build(self, temp, script, timeline=None):
        builder = HyperFramesBuilder({
            "video": {"output_dir": str(temp)},
            "dialogue": {"format": "monologue"},
            "characters": {MONOLOGUE_SPEAKER: {"name": "主讲人"}}})
        return builder, builder.build_project(
            {"code": "000001", "quote": {"name": "平安银行", "price": 10.5}}, script, timeline or {})

    def test_builder_cast_is_narrator_only(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = HyperFramesBuilder({
                "dialogue": {"format": "monologue"}, "characters": {}})
            self.assertEqual(builder._cast(), (MONOLOGUE_SPEAKER,))
            self.assertEqual(builder.format, FORMAT_MONOLOGUE)

    def test_debate_builder_cast_is_unchanged(self):
        builder = HyperFramesBuilder({})
        self.assertEqual(builder._cast(), ("bull", "bear"))
        self.assertEqual(builder.format, FORMAT_DEBATE)

    def test_narrator_turns_render_segments(self):
        with tempfile.TemporaryDirectory() as temp:
            _, project = self._build(temp, {"turns": [
                {"speaker": MONOLOGUE_SPEAKER, "line": "银行的生意是把存款变成贷款，赚中间的息差。"},
                {"speaker": MONOLOGUE_SPEAKER, "line": "零售转型的代价要看不良率和拨备覆盖率这两组数。"},
            ]})
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn('data-slot="caption"', content)
            self.assertIn("主讲人", content)

    def test_round_badge_uses_section_label(self):
        with tempfile.TemporaryDirectory() as temp:
            _, project = self._build(temp, {"turns": [
                {"speaker": MONOLOGUE_SPEAKER, "line": "第一段：这门生意靠息差吃饭，客户基础是本钱。"},
                {"speaker": MONOLOGUE_SPEAKER, "line": "第二段：息差吃行业周期，不良率才是真功夫。"},
            ]})
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn("第 1 节 / 2", content)
            self.assertIn("第 2 节 / 2", content)
            self.assertNotIn("观点 1 /", content)

    def test_intro_card_shows_one_role_without_cross(self):
        with tempfile.TemporaryDirectory() as temp:
            _, project = self._build(temp, {"turns": [
                {"speaker": MONOLOGUE_SPEAKER, "line": "这门生意靠息差吃饭，客户基础是本钱。"}]})
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn('class="role narrator"', content)
            self.assertNotIn('<em>×</em>', content)
            self.assertNotIn('class="role bull"', content)
            self.assertNotIn('class="role bear"', content)

    def test_debate_intro_card_still_shows_two_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = HyperFramesBuilder({"video": {"output_dir": temp}})
            project = builder.build_project(
                {"code": "000001", "quote": {"name": "平安银行"}},
                {"turns": [{"speaker": "bull", "line": "这门生意靠息差吃饭。"},
                            {"speaker": "bear", "line": "但息差要看行业周期。"}]}, {})
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn('class="role bull"', content)
            self.assertIn('class="role bear"', content)
            self.assertIn("<em>×</em>", content)
            self.assertIn("观点 1 / 2", content)

    def test_monologue_skips_dual_column_cta_board(self):
        """独白没有两方立场：片尾不画双列互动图板（会是两个空列）。

        hook/question 仍要走 outro 标题与提示行——那才是观众要带走的东西。
        """
        with tempfile.TemporaryDirectory() as temp:
            _, project = self._build(temp, {"format": "monologue", "turns": [
                {"speaker": MONOLOGUE_SPEAKER, "line": "这门生意靠息差吃饭，客户基础是本钱。"},
                {"speaker": MONOLOGUE_SPEAKER, "line": "下一季净息差能不能稳住，是接下来唯一要盯的变量。"},
            ], "hook": "下一季净息差", "question": "低成本存款还能守多久"})
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertNotIn("更乐观的一边", content)
            self.assertNotIn("更谨慎的一边", content)
            self.assertNotIn("两方还没谈拢的地方", content)
            # outro 仍消费 hook/question
            self.assertIn("下一季净息差", content)

    def test_debate_cta_board_still_renders(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = HyperFramesBuilder({"video": {"output_dir": temp}})
            project = builder.build_project(
                {"code": "000001", "quote": {"name": "平安银行"}},
                {"turns": [{"speaker": "bull", "line": "这门生意靠息差吃饭，客户基础是本钱。"},
                            {"speaker": "bear", "line": "但息差要看行业周期，不良率是真功夫。"}],
                 "sides": {"bull": "零售客户基础还在加深", "bear": "息差压力没有缓解"},
                 "hook": "下一季净息差", "question": "低成本存款还能守多久"}, {})
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn("更乐观的一边", content)
            self.assertIn("更谨慎的一边", content)

    def test_script_format_overrides_config(self):
        """脚本自带 format（归一化时写入）优先于 config——二次构建不用重设配置。"""
        with tempfile.TemporaryDirectory() as temp:
            builder = HyperFramesBuilder({"video": {"output_dir": temp}})  # config 默认 debate
            project = builder.build_project(
                {"code": "000001", "quote": {"name": "平安银行"}},
                {"format": "monologue", "turns": [
                    {"speaker": MONOLOGUE_SPEAKER, "line": "这门生意靠息差吃饭，客户基础是本钱。"},
                    {"speaker": MONOLOGUE_SPEAKER, "line": "下一季净息差是唯一要盯的变量。"}]}, {})
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn("第 1 节 / 2", content)
            self.assertIn('class="role narrator"', content)
            self.assertNotIn('<em>×</em>', content)


class MonologueCssTests(unittest.TestCase):
    def test_narrator_has_explicit_tone_rules(self):
        """narrator 的画面色必须显式写在 CSS 里——静默降级（无色/继承 bull 粉）不可接受。"""
        css_path = Path(__file__).resolve().parents[1] / "stocktalk" / "templates" / "hyperframes" / "stock-debate.css"
        css = css_path.read_text(encoding="utf-8")
        self.assertIn("--narrator:", css)
        self.assertIn("--narrator-soft:", css)
        for selector in (".stage-tint.narrator", ".speaker-chip.narrator",
                         ".round-badge.narrator", ".caption-line.narrator",
                         ".intro-roles .role.narrator"):
            self.assertIn(selector, css, f"缺少 {selector} 的配色规则")


if __name__ == "__main__":
    unittest.main()
