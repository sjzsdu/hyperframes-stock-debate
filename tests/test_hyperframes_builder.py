import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from stocktalk.modules.hyperframes_builder import HyperFramesBuildError, HyperFramesBuilder


class HyperFramesBuilderTests(unittest.TestCase):
    def _config(self, directory):
        return {"video": {"output_dir": str(directory)}}

    # ---- Safe area: platform players overlay their own UI on the frame -------

    def test_default_safe_area_is_reserved_per_canvas(self) -> None:
        vertical = HyperFramesBuilder({"video": {"canvas": "vertical"}})
        horizontal = HyperFramesBuilder({"video": {"canvas": "horizontal"}})
        self.assertEqual((vertical.safe_top, vertical.safe_bottom), (240, 460))
        # The bottom band is the dangerous one: caption + action bar + hashtags.
        self.assertGreaterEqual(vertical.safe_bottom, 400)
        self.assertLess(horizontal.safe_bottom, vertical.safe_bottom)
        # Vertical sides are symmetric: 180×2 left the stage too narrow,
        # 60/140 asymmetric read lopsided and 100×2 still read as huge empty
        # bands — 25×2 (a quarter of 100) is the 2026-09-19 verdict, trading
        # the right-edge overlay margin for a 1030px column.
        self.assertEqual((vertical.safe_side_left, vertical.safe_side_right), (25, 25))
        self.assertEqual(vertical.safe_side_left, vertical.safe_side_right)
        self.assertEqual((horizontal.safe_top, horizontal.safe_side_left, horizontal.safe_side_right), (120, 70, 70))

    def test_safe_area_reads_the_per_canvas_overrides(self) -> None:
        builder = HyperFramesBuilder({"video": {"canvas": "vertical", "safe_area": {
            "vertical": {"top": 300, "bottom": 500, "side": 120},
            "horizontal": {"top": 10, "bottom": 20}}}})
        self.assertEqual((builder.safe_top, builder.safe_bottom), (300, 500))
        self.assertEqual((builder.safe_side_left, builder.safe_side_right), (120, 120))  # 对称 side 兼容
        split = HyperFramesBuilder({"video": {"canvas": "vertical", "safe_area": {
            "vertical": {"side_left": 40, "side_right": 160}}}})
        self.assertEqual((split.safe_side_left, split.safe_side_right), (40, 160))

    def test_safe_area_is_clamped_to_the_canvas(self) -> None:
        builder = HyperFramesBuilder({"video": {"canvas": "vertical", "safe_area": {"vertical": {"top": -40, "bottom": 99999}}}})
        self.assertEqual(builder.safe_top, 0)
        self.assertLessEqual(builder.safe_bottom, 1920 // 2)

    def test_stage_bands_are_positioned_from_the_safe_area(self) -> None:
        """版面必须跟随安全区变量——写死像素会在改安全区时悄悄越界。"""
        with tempfile.TemporaryDirectory() as temp:
            builder = HyperFramesBuilder({"video": {"output_dir": temp, "canvas": "vertical"}})
            project = builder.build_project(
                {"code": "600519", "quote": {"name": "贵州茅台", "price": 1500, "change_pct": 1.2}}, {},
                {"segments": [], "total_duration": 30},
            )
            html = project.composition_path.read_text(encoding="utf-8")
            css = (project.directory / "stock-debate.css").read_text(encoding="utf-8")
            self.assertIn("--safe-top: 240px; --safe-bottom: 460px;", html)
            self.assertIn("--safe-side-left: 25px; --safe-side-right: 25px;", html)
            for selector in ("#headline", ".slot-kicker", "#visual-frame", "#caption-zone"):
                block = css.split(f"#root.layout-vertical {selector} {{")[1].split("}")[0]
                self.assertIn("var(--safe-", block, selector)

    def test_builds_timed_multitrack_composition(self):
        with tempfile.TemporaryDirectory() as temp:
            audio_source = Path(temp) / "audio.wav"
            audio_source.write_bytes(b"fake-audio")
            builder = HyperFramesBuilder(self._config(temp))
            bars = [{"date": f"2026-09-{d:02d}", "open": 10 + i, "close": 11 + i, "high": 12 + i, "low": 9 + i, "volume": 100} for i, d in enumerate((1, 2, 3, 4))]
            project = builder.build_project(
                {"code": "600519", "quote": {"name": "贵州茅台", "price": 1500, "change_pct": 1.2}, "financials": {"revenue": "100亿"}, "kline": bars},
                {},
                {"segments": [{"character": "bull", "line": "营收保持韧性", "start_time": 1, "duration": 3, "audio_path": str(audio_source)}], "total_duration": 4},
            )
            content = project.composition_path.read_text(encoding="utf-8")
            # Dialogue 4.0s + 0.45s intro fade-in + 0.45s outro recap pad.
            self.assertEqual(project.duration, 4.9)
            self.assertIn('data-composition-id="stock-debate"', content)
            self.assertIn('data-track-index="4"', content)
            self.assertIn('id="audio-1"', content)
            self.assertIn("window.__timelines['stock-debate']", (project.directory / "stock-debate.js").read_text(encoding="utf-8"))
            self.assertIn("<svg", content)
            self.assertIn('data-topic="financial"', content)
            self.assertIn('class="ma draw-line"', content)
            self.assertIn('id="visual-frame"', content)
            self.assertIn('data-slot="visual"', content)
            self.assertIn('data-audio-group="dialogue"', content)
            self.assertIn('data-fx-chain="', content)
            self.assertIn('data-automation="', content)
            self.assertIn('src="gsap.min.js"', content)
            self.assertTrue((project.directory / "stock-debate.css").is_file())
            self.assertTrue((project.directory / "stock-debate.js").is_file())
            self.assertTrue((project.directory / "gsap.min.js").is_file())
            self.assertTrue((project.directory / "index.motion.json").is_file())

    def test_no_role_labels_or_disclaimers_in_output(self):
        with tempfile.TemporaryDirectory() as temp:
            project = HyperFramesBuilder(self._config(temp)).build_project(
                {"code": "600519", "quote": {"name": "贵州茅台", "price": 1500}},
                {"turns": [{"speaker": "bull", "line": "这家公司主要靠高端白酒赚钱，品牌就是护城河。"},
                            {"speaker": "bear", "line": "但行业竞争在加剧，估值也不便宜，风险要留意。"}]},
                {},
            )
            content = project.composition_path.read_text(encoding="utf-8")
            for banned in ("看多方", "看空方", "看多", "看空", "免责", "不构成", "投资建议", "虚拟人物",
                           "谈什么", "字幕突出", "visual_prompt", "股市新手", "股市老登", "THE ROOKIE",
                           "THE VETERAN", "TOPIC VISUAL", "正在讨论", "正在发言", "AI", "生成"):
                self.assertNotIn(banned, content)
            self.assertIn("slot-kicker bear", content)
            self.assertIn('data-topic="risk"', content)

    def test_news_topic_slide_shows_real_headlines(self):
        with tempfile.TemporaryDirectory() as temp:
            project = HyperFramesBuilder(self._config(temp)).build_project(
                {"code": "600519", "quote": {"name": "贵州茅台"},
                 "news": {"items": [{"title": "线下自营店调价，短期催化值得关注", "type": "其他", "publish_time": "2026-09-09"},
                                    {"title": "研报：Q3主动降速", "type": "研报", "publish_time": "2025-11-02"}]}},
                {"turns": [{"speaker": "bear", "line": "这些消息会不会已经被价格消化掉了？"},
                            {"speaker": "bull", "line": "最近研报和消息面都提到，公司对线下自营店的产品进行了调价。"}]},
                {},
            )
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn('data-topic="news"', content)
            self.assertIn("近期资讯", content)
            self.assertIn("线下自营店调价", content)
            self.assertIn("研报", content)

    def test_news_card_falls_back_to_real_quote_snapshot(self):
        """No news items → real quote snapshot card, never an empty placeholder."""
        with tempfile.TemporaryDirectory() as temp:
            project = HyperFramesBuilder(self._config(temp)).build_project(
                {"code": "600519", "quote": {"name": "贵州茅台", "price": 1500.0, "change_pct": 1.2}},
                {"turns": [{"speaker": "bull", "line": "消息面上暂时没有新的公告或研报，我们看回生意本身。"}]},
                {},
            )
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn('data-topic="news"', content)
            self.assertIn("最新价", content)
            self.assertIn("1,500.00", content)
            self.assertNotIn("暂无资讯数据", content)

    def test_unchanged_slot_content_is_merged_into_one_element(self):
        """连续几轮讲同一话题：标签槽合并成单个元素，时间轴上不产生任何新动画。

        「内容没变就不要动」就是靠这次合并实现的——窗口延长而不是重建。
        """
        with tempfile.TemporaryDirectory() as temp:
            project = HyperFramesBuilder(self._config(temp)).build_project(
                {},
                {"turns": [
                    {"speaker": "bull", "line": "行业格局稳定，渠道和品牌都很扎实。"},
                    {"speaker": "bull", "line": "同行业的对手份额变化也值得看。"},
                    {"speaker": "bear", "line": "这个业务的护城河主要来自渠道规模。"}]},
                {},
            )
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertEqual(content.count('data-slot="kicker"'), 1)
            # 话题没变，但说话人换了：氛围层按说话人合并，仍是两层。
            self.assertEqual(content.count('data-slot="tint"'), 2)

    def test_captions_are_single_rows_on_an_absolute_clock(self):
        """一行字幕按绝对时间排布：逐条递增、首条从 0 开始、都不超过一行上限。"""
        long_line = "这家公司的主营业务是锂电池和电子器件，产能扩张之后收入弹性很大，但资本开支也水涨船高。"
        with tempfile.TemporaryDirectory() as temp:
            builder = HyperFramesBuilder(self._config(temp))
            project = builder.build_project({}, {"turns": [{"speaker": "bull", "line": long_line}]}, {})
            content = project.composition_path.read_text(encoding="utf-8")
            starts = [float(x) for x in re.findall(
                r'class="caption-line[^"]*" data-start="([0-9.]+)"', content)]
            self.assertEqual(starts, sorted(starts))
            self.assertEqual(starts[0], 0.0)
            rows = re.findall(r'data-slot="caption" data-layout-ignore>([^<]+)</p>', content)
            self.assertGreater(len(rows), 1, "长句必须切成多条一行字幕，而不是换行撑高版面")
            for row in rows:
                self.assertLessEqual(len(row), builder.caption_max_chars + 1)

    def test_every_topic_card_has_data_or_real_fallback(self):
        """All 9 topic visuals must exist; none may be an empty placeholder SVG."""
        with tempfile.TemporaryDirectory() as temp:
            builder = HyperFramesBuilder(self._config(temp))
            visuals = builder._visuals({}, {}, [], {}, {})
            self.assertEqual(set(visuals), {"financial", "valuation", "industry", "money", "technical", "risk", "sentiment", "outlook", "news", "intro", "outro"})
            for key, svg in visuals.items():
                self.assertNotIn("暂无", svg, f"topic {key} still renders an empty placeholder")

    def test_classifies_industry_topic_from_business_line(self):
        with tempfile.TemporaryDirectory() as temp:
            project = HyperFramesBuilder(self._config(temp)).build_project(
                {},
                {"turns": [{"speaker": "bull", "line": "行业格局稳定，公司靠渠道和品牌站稳，这门生意的护城河很深。"}]},
                {},
            )
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn('data-topic="industry"', content)
            self.assertIn("行业与业务", content)

    def test_topic_visual_falls_back_to_company_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            project = HyperFramesBuilder(self._config(temp)).build_project(
                {},
                {"turns": [{"speaker": "bull", "line": "嗯，这个话题值得展开聊聊。"},
                            {"speaker": "bear", "line": "那我们拆开看看这门生意到底靠什么赚钱。"}]},
                {},
            )
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn("公司与行业", content)
            # Every turn after the opening market panel gets its own real-data
            # board; with no quote/kline it degrades to a clean "暂无K线数据"
            # panel instead of fabricating data.
            self.assertIn("公司与行业·真实数据", content)
            self.assertIn("暂无K线数据", content)
            self.assertNotIn('aria-label="公司概览"', content)

    def test_close_line_used_when_no_real_ohlc(self):
        """Without genuine OHLC bars the chart must be a close line, never fake candles."""
        with tempfile.TemporaryDirectory() as temp:
            technical = {"summary": {}, "count": 2, "history": [
                {"timestamp": "2026-09-10", "price": {"current": 12.5, "change": 0.1}},
                {"timestamp": "2026-09-11", "price": {"current": 12.9, "change": 0.4}},
            ]}
            project = HyperFramesBuilder(self._config(temp)).build_project({"code": "600519", "quote": {"name": "贵州茅台"}, "technical": technical}, {}, {})
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn("close-line", content)
            self.assertNotIn("class=\"candle", content)

    def test_candles_kept_when_real_ohlc_present(self):
        with tempfile.TemporaryDirectory() as temp:
            bars = [{"date": "2026-09-10", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
                    {"date": "2026-09-11", "open": 11, "high": 13, "low": 10, "close": 12, "volume": 110}]
            project = HyperFramesBuilder(self._config(temp)).build_project({"code": "600519", "quote": {"name": "贵州茅台"}, "kline": bars}, {}, {})
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn("class=\"candle", content)

    def test_slide_keywords_from_script_visual_field(self):
        with tempfile.TemporaryDirectory() as temp:
            project = HyperFramesBuilder(self._config(temp)).build_project(
                {},
                {"turns": [{"speaker": "bull", "line": "这家公司靠高端白酒赚钱，品牌就是护城河。",
                            "visual": ["高端白酒", "护城河"]}]},
                {},
            )
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn("高端白酒", content)
            self.assertIn("护城河", content)

    def test_render_requires_nonempty_mp4(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            builder = HyperFramesBuilder(self._config(root), runner=lambda command, cwd, env=None: subprocess.CompletedProcess(command, 0, "", ""))
            project = builder.build_project({}, {}, {})
            with self.assertRaises(HyperFramesBuildError):
                builder.render_mp4(project, root / "missing.mp4")

    def test_render_enables_streaming_encode_for_long_compositions(self):
        """Long renders must stream frames instead of buffering ~60GB on disk."""
        captured: dict[str, Any] = {}

        def fake_runner(command, cwd, env=None):
            captured["command"] = command
            captured["env"] = env
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "out.mp4"
            destination.write_bytes(b"x")
            builder = HyperFramesBuilder(self._config(root), runner=fake_runner)
            project = builder.build_project({}, {}, {})
            builder.render_mp4(project, destination)
        self.assertGreaterEqual(int(captured["env"]["PRODUCER_STREAMING_ENCODE_MAX_DURATION_SECONDS"]), 7200)
        self.assertEqual(captured["env"]["HF_CAPTURE_PARALLEL_STREAM"], "true")
        self.assertEqual(captured["env"]["HF_DE_PARALLEL_STREAM"], "true")
        self.assertIn("render", captured["command"])

    # ---- Per-stock identity: board, temperament tags, ambience --------------

    def test_board_comes_from_the_symbol_prefix(self) -> None:
        """板块只能由代码前缀推——F10 接口并非每个 tongstock 版本都有。"""
        builder = HyperFramesBuilder({"video": {"output_dir": "/tmp"}})
        self.assertEqual(builder._board("600519"), ("沪市主板", "沪主板"))
        self.assertEqual(builder._board("300750"), ("创业板", "创业"))
        self.assertEqual(builder._board("688111"), ("科创板", "科创"))
        self.assertEqual(builder._board("000001"), ("深市主板", "深主板"))
        self.assertEqual(builder._board("920002"), ("北交所", "北交所"))
        self.assertEqual(builder._board(""), ("A股", "A股"))

    def test_temperament_tags_are_measured_from_the_real_kline(self) -> None:
        """标签必须是真算出来的数字，不是形容词。"""
        builder = HyperFramesBuilder({"video": {"output_dir": "/tmp"}})
        # 10 days climbing from 10 to ~15.9 (+58.9% over 5 days).
        bars = [{"close": 10 * (1.1 ** i), "volume": 1000} for i in range(10)]
        identity = builder._identity("600519", bars, {"change_pct": 1.2})
        labels = [tag["label"] for tag in identity.tags]
        self.assertIn("近5日", labels)
        change = next(tag["value"] for tag in identity.tags if tag["label"] == "近5日")
        self.assertTrue(change.startswith("+"))
        # 连涨 9 天 → 连阳，方向为涨（红）
        streak = next((t for t in identity.tags if t["label"] == "连阳"), None)
        self.assertIsNotNone(streak)
        self.assertEqual(streak["tone"], "up")

    def test_identity_degrades_without_a_kline(self) -> None:
        """K线缺失时只留板块，不能编数字。"""
        builder = HyperFramesBuilder({"video": {"output_dir": "/tmp"}})
        identity = builder._identity("300750", [], {})
        self.assertEqual(identity.board, "创业板")
        self.assertEqual(identity.tags, ())
        self.assertIn("创业板", identity.hook)

    def test_hue_is_stable_per_symbol_but_differs_between_symbols(self) -> None:
        builder = HyperFramesBuilder({"video": {"output_dir": "/tmp"}})
        self.assertEqual(builder._hue("600519"), builder._hue("600519"))
        self.assertNotEqual(builder._hue("600519"), builder._hue("300750"))
        # 避开红绿语义带：涨=红(≈355)、跌=绿(≈150)，氛围色必须落在青→蓝→紫。
        for code in ("600519", "300750", "688111", "000001", "920002"):
            hue = builder._hue(code)
            self.assertTrue(190 <= hue <= 319, f"{code} hue {hue} 侵入了涨跌语义色区")

    def test_identity_reaches_the_composition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            builder = HyperFramesBuilder({"video": {"output_dir": temp, "canvas": "horizontal"}})
            bars = [{"close": 10 * (1.1 ** i), "volume": 1000} for i in range(10)]
            project = builder.build_project(
                {"code": "600519", "quote": {"name": "贵州茅台", "price": 1500, "change_pct": 1.2},
                 "kline": bars}, {}, {"segments": [], "total_duration": 30})
            html = project.composition_path.read_text(encoding="utf-8")
            self.assertIn("--stock-accent:", html)
            self.assertIn('id="identity"', html)
            self.assertIn("沪主板", html)
            # 开场钩子不再是千篇一律的「聊公司」
            self.assertNotIn(">聊公司<", html)


class ChartLibraryTests(unittest.TestCase):
    """图表库：每轮右半屏按话题轮换，同一支视频尽量不重样，数据不足就换下一个。"""

    def _builder(self) -> HyperFramesBuilder:
        return HyperFramesBuilder({"video": {"canvas": "horizontal"}})

    def _f10(self) -> dict:
        return {"profile": {
            "主营业务": "印制电路板的研发、生产和销售",
            "主营构成": {"报告期": "2026-06-30", "明细": [
                {"项目": "印刷线路板(产品)", "收入": "6.54亿", "收入占比(%)": "84.62", "毛利率(%)": "11.98"},
                {"项目": "其他业务(产品)", "收入": "1.19亿", "收入占比(%)": "15.38", "毛利率(%)": "94.58"},
                {"项目": "内销(地区)", "收入": "8.64亿", "收入占比(%)": "64.10", "毛利率(%)": "11.77"},
            ]},
            "题材标签": {"概念": ["PCB概念", "汽车电子", "CPO概念"]}}}

    def _financials(self) -> dict:
        return {"报告期": "2026-08-28", "营业收入(亿元)": 7.73, "营业利润(亿元)": 0.73, "净利润(亿元)": 0.66}

    def _ranking_f10(self) -> dict:
        return {"profile": {"行业地位": {"研究行业": "元器件", "同行家数": 66,
                                        "市场表现": {"排名": 1}, "公司规模": {"排名": 52},
                                        "估值水平": {"排名": 10}}}}

    def _full_ctx(self, f10: dict, financials: dict) -> dict:
        return {"bars": [{"close": 10.0 + i} for i in range(40)], "quote": {"name": "澳弘电子"},
                "keywords": [], "f10": f10, "financials": financials, "news": {"items": []}}

    def test_financial_topic_rotates_charts_within_one_video(self) -> None:
        builder = self._builder()
        used: set = set()
        ctx = {"bars": [{"close": 10.0 + i} for i in range(40)], "quote": {"name": "澳弘电子"},
               "keywords": [], "f10": self._f10(), "financials": self._financials(), "news": {"items": []}}
        first = builder._turn_chart("financial", used, ctx)
        second = builder._turn_chart("financial", used, ctx)
        self.assertIn("业务拆解", first)          # mindmap first
        self.assertIn("收入构成", second)         # then composition bars
        self.assertEqual(used, {"mindmap", "composition"})

    def test_chart_falls_back_when_data_is_missing(self) -> None:
        builder = self._builder()
        used: set = set()
        ctx = {"bars": [{"close": 10.0 + i} for i in range(40)], "quote": {"name": "某股"},
               "keywords": [], "f10": {}, "financials": {}, "news": {"items": []}}
        body = builder._turn_chart("financial", used, ctx)
        # mindmap/composition/waterfall 全没数据 → 只剩真实 K 线兜底
        self.assertIn("真实日K", body)

    def test_mindmap_shows_real_composition_and_margins(self) -> None:
        body = self._builder()._business_mindmap_body({"name": "澳弘电子"}, self._f10())
        self.assertIn("印刷线路板", body)
        self.assertIn("毛利率 11.98%", body)
        self.assertIn("主营构成 · 2026-06-30", body)
        self.assertNotIn("汽车电子", body)  # 有主营构成时就不用概念标签当分支

    def test_mindmap_uses_concept_tags_when_composition_missing(self) -> None:
        f10 = {"profile": {"题材标签": {"概念": ["PCB概念", "汽车电子", "CPO概念"]}}}
        body = self._builder()._business_mindmap_body({"name": "澳弘电子"}, f10)
        self.assertIn("PCB概念", body)
        self.assertIn("题材标签", body)

    def test_composition_bars_carry_real_shares(self) -> None:
        body = self._builder()._composition_bars_body(self._f10())
        self.assertIn("84.6%", body)
        self.assertIn("6.54亿", body)
        self.assertIn("毛利 11.98%", body)

    def test_waterfall_shows_real_money_and_margin_line(self) -> None:
        body = self._builder()._profit_waterfall_body(self._financials())
        self.assertIn("7.73亿", body)
        self.assertIn("0.66亿", body)
        self.assertIn("每 100 元收入落下 8.5 元净利", body)
        self.assertIsNone(self._builder()._profit_waterfall_body({}))

    def test_waterfall_grows_a_level_when_profit_total_exists(self) -> None:
        financials = dict(self._financials(), **{"利润总额(亿元)": 0.70})
        body = self._builder()._profit_waterfall_body(financials)
        self.assertIn("利润总额", body)
        self.assertIn("0.70亿", body)
        # 主营利润(ZhuYingLiRun) 口径与 F10 差一个量级，永远不许上画面
        self.assertNotIn("主营利润", body)

    def test_working_capital_bars_use_real_inventory_and_receivables(self) -> None:
        financials = {"报告期": "2026-08-28", "营业收入(亿元)": 7.73,
                      "存货(亿元)": 4.24, "应收账款(亿元)": 3.94}
        body = self._builder()._working_capital_body(financials)
        self.assertIn("4.24亿", body)
        self.assertIn("占收入 55%", body)
        self.assertIn("占收入 51%", body)
        self.assertIn("每 100 元年收入", body)
        # 没有营收就没法算占比，宁缺毋滥
        self.assertIsNone(self._builder()._working_capital_body({"存货(亿元)": 4.24}))

    def test_industry_rank_ruler_marks_real_positions(self) -> None:
        body = self._builder()._industry_rank_body(self._ranking_f10())
        self.assertIn("元器件 同行 66 家", body)
        self.assertIn("第 1 名", body)
        self.assertIn("第 52 名", body)
        self.assertIn("第 10 名", body)
        # 没有同行家数或没有任何真实排名 → 不画，绝不编位置
        self.assertIsNone(self._builder()._industry_rank_body({}))
        self.assertIsNone(self._builder()._industry_rank_body(
            {"profile": {"行业地位": {"研究行业": "元器件", "同行家数": 66}}}))

    def test_chart_borrows_unused_charts_before_repeating_kline(self) -> None:
        builder = self._builder()
        used: set = {"mindmap", "composition", "waterfall", "working_capital", "kline"}
        ctx = self._full_ctx(self._ranking_f10(), self._financials())
        body = builder._turn_chart("money", used, ctx)
        # money 的候选只有 K线且已用过 → 从全片未用过的池里借排名标尺
        self.assertIn("行业里站哪", body)
        self.assertIn("ranking", used)

    def test_chart_prefers_kline_for_money_before_borrowing(self) -> None:
        builder = self._builder()
        used: set = {"mindmap", "composition", "waterfall"}
        ctx = self._full_ctx(self._ranking_f10(), self._financials())
        builder._turn_chart("money", used, ctx)
        self.assertIn("kline", used)   # 本话题候选还没用过，先轮自己
        self.assertNotIn("ranking", used)

    def test_exhausted_library_rotates_least_recently_used(self) -> None:
        """六种图全登场后，按最久未用轮转，而不是 K线连播。"""
        builder = self._builder()
        used: set = set()
        history: list = []
        profile = {**self._f10()["profile"],
                   "行业地位": {"研究行业": "元器件", "同行家数": 66,
                               "市场表现": {"排名": 1}, "公司规模": {"排名": 52}}}
        ctx = self._full_ctx({"profile": profile}, self._financials())
        for topic in ("financial", "money", "sentiment", "risk", "industry"):
            builder._turn_chart(topic, used, ctx, history)
        sixth = builder._turn_chart("money", used, ctx, history)
        self.assertIn("业务拆解", sixth)          # 最久没用的是脑图，轮它
        seventh = builder._turn_chart("risk", used, ctx, history)
        self.assertIn("真实日K", seventh)         # 再轮到 K线

    def test_timeline_shows_headlines_and_handles_empty_news(self) -> None:
        builder = self._builder()
        body = builder._news_timeline_body({"items": [
            {"title": "泰国基地仍在认证", "type": "公告", "publish_time": "2026-09-17"}]})
        self.assertIn("泰国基地仍在认证", body)
        self.assertIn("09-17", body)
        self.assertIn("保持跟踪", builder._news_timeline_body({"items": []}))

    # ---- 横版铺满：board 加宽、图表按 zone 右边界展开，消灭两侧死白 ----

    def test_horizontal_board_stretches_but_vertical_stacks(self) -> None:
        segment = {"line": "聊聊走势", "topic": "technical", "topic_title": "技术走势",
                   "keywords": ["MA5"]}
        bars = [{"close": 10 + i} for i in range(40)]
        horizontal = self._builder()._turn_board_svg(segment, {"name": "澳弘电子"}, bars, {}, {})
        self.assertIn('viewBox="0 0 1956 500"', horizontal)      # 横版铺满宽面板
        self.assertIn('x1="392"', horizontal)                    # 横版：左 facts / 右图
        vertical = HyperFramesBuilder({"video": {"canvas": "vertical"}})._turn_board_svg(
            segment, {"name": "澳弘电子"}, bars, {}, {})
        self.assertIn('viewBox="0 0 1160 800"', vertical)        # 竖版：上下堆叠
        self.assertNotIn('x1="392"', vertical)                   # 不再左右分栏
        self.assertIn('<svg x="0" y="246"', vertical)            # 图表整体移到 facts 条之下（嵌套 svg，不受 CSS transform 影响）
        self.assertIn('y="786"', vertical)                       # 讨论点/来源落到底部

    def test_vertical_board_puts_facts_above_the_chart(self) -> None:
        """竖版：facts 横排在顶部，图表占下方整行（2026-09-19 用户反馈）。"""
        segment = {"line": "最新价 2.69 聊聊走势", "topic": "technical", "topic_title": "技术走势",
                   "keywords": ["MA5"]}
        bars = [{"close": 10 + i} for i in range(40)]
        vertical = HyperFramesBuilder({"video": {"canvas": "vertical"}})._turn_board_svg(
            segment, {"name": "澳弘电子", "price": 2.69, "change_pct": 5.91}, bars, {}, {})
        facts_pos = vertical.index("最新价")
        chart_pos = vertical.index("真实日K")
        strip_pos = vertical.index('y1="226"')
        self.assertLess(facts_pos, strip_pos)                    # facts 条在前
        self.assertLess(strip_pos, chart_pos)                    # 分隔线之后才是图表

    def test_charts_spread_to_the_horizontal_zone_edge(self) -> None:
        builder = self._builder()
        body = builder._composition_bars_body(self._f10())
        self.assertIn('x="1922"', body)                          # 毛利列顶到右边界
        rank = builder._industry_rank_body(self._ranking_f10())
        self.assertIn('x="1922"', rank)
        waterfall = builder._profit_waterfall_body(self._financials())
        net_x = float(re.search(r'x="([\d.]+)"[^>]*>净利润', waterfall).group(1))
        self.assertGreater(net_x, 1500)                          # 瀑布柱列铺开到右半区
        kline = builder._mini_chart_body([{"close": 10 + i} for i in range(40)], "technical", {})
        self.assertIn('x2="1926"', kline)                        # K线网格铺到右缘

    def test_composition_revenue_never_collides_with_margin_column(self) -> None:
        """首行占比 100% 时金额改画进条内，绝不压到毛利列（用户截图 bug）。"""
        f10 = {"profile": {"主营构成": {"报告期": "2026-06-30", "明细": [
            {"项目": "消费电子(行业)", "收入": "84.68亿", "收入占比(%)": "100", "毛利率(%)": "45.02"},
            {"项目": "其他业务(产品)", "收入": "0.38亿", "收入占比(%)": "0.5", "毛利率(%)": "72.41"},
        ]}}}
        vertical = HyperFramesBuilder({"video": {"canvas": "vertical"}})
        body = vertical._composition_bars_body(f10)
        self.assertIn('fill="#0b1220"', body)                    # 金额画进条内（深底深字→反白）
        # 毛利列仍在 zone 右缘 1126，金额右端不得越过 996
        self.assertIn('x="1126"', body)
        margin_x = body.index("毛利 45.02%")
        self.assertLess(body.rfind('text-anchor="end" fill="#0b1220"', 0, margin_x), margin_x)

    def test_headline_stats_only_use_verified_stockinfo(self) -> None:
        stats = HyperFramesBuilder._headline_stats(
            {"metrics": {"总市值(亿元)": 588.42, "换手率(%)": 1.124}})
        self.assertEqual(stats, [("总市值", "588.42亿"), ("换手率", "1.12%")])
        self.assertEqual(HyperFramesBuilder._headline_stats({}), [])
        self.assertEqual(HyperFramesBuilder._headline_stats("junk"), [])

    # ---- Poster frame: 视频号分享卡直接取视频首帧，第 0 帧必须完整 ----------

    def test_intro_scene_is_fully_composed_at_frame_zero(self) -> None:
        """第 0 帧即完整排版（海报帧）——入场动画全部让位于可见性。

        2026-09-20 实测：视频号 4:3 封面设置失败后回退到首帧当分享卡，
        而首帧是 GSAP 入场动画未完成的半空画面。自此开场场景一律用
        tl.set(..., 0) 置为完成态，入场感只靠开场卡溶解。
        """
        js = (HyperFramesBuilder({"video": {}})._asset_dir / "stock-debate.js").read_text(encoding="utf-8")
        # 舞台三件套不再做入场淡入，第 0 帧就是完成态
        self.assertNotIn("fromTo('#headline'", js)
        self.assertIn("tl.set('#headline', { opacity: 1, y: 0 }, 0)", js)
        # 开场卡第 0 帧完整上屏（只保留此后的溶解）
        self.assertIn("tl.set(intro, { opacity: 1 }, 0)", js)
        # 首个槽位版本与首个图板的细节（蜡烛/柱/画线）都 born-complete
        self.assertIn("const born = start === 0", js)
        self.assertIn("if (born) tl.set(candles, { opacity: 1, scaleY: 1 }, 0);", js)
        # 首条字幕（at === 0）直接可见
        self.assertIn("tl.set(el, { visibility: 'visible', opacity: 1, y: 0 }, 0);", js)


if __name__ == "__main__":
    unittest.main()
