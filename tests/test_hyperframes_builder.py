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

    def test_safe_area_reads_the_per_canvas_overrides(self) -> None:
        builder = HyperFramesBuilder({"video": {"canvas": "vertical", "safe_area": {
            "vertical": {"top": 300, "bottom": 500, "side": 120},
            "horizontal": {"top": 10, "bottom": 20}}}})
        self.assertEqual((builder.safe_top, builder.safe_bottom, builder.safe_side), (300, 500, 120))

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
            self.assertIn("--safe-side: 56px", html)
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


if __name__ == "__main__":
    unittest.main()
