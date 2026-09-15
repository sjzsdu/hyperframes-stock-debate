import subprocess
import tempfile
import unittest
from pathlib import Path

from stocktalk.modules.hyperframes_builder import HyperFramesBuildError, HyperFramesBuilder


class HyperFramesBuilderTests(unittest.TestCase):
    def _config(self, directory):
        return {"video": {"output_dir": str(directory)}}

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
            self.assertIn('id="slide-1"', content)
            self.assertIn('data-cover-slide', content)
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
            self.assertIn("class=\"slide bear risk", content)
            self.assertIn('data-topic="risk"', content)

    def test_news_topic_slide_shows_real_headlines(self):
        with tempfile.TemporaryDirectory() as temp:
            project = HyperFramesBuilder(self._config(temp)).build_project(
                {"code": "600519", "quote": {"name": "贵州茅台"},
                 "news": {"items": [{"title": "线下自营店调价，短期催化值得关注", "type": "其他", "publish_time": "2026-09-09"},
                                    {"title": "研报：Q3主动降速", "type": "研报", "publish_time": "2025-11-02"}]}},
                {"turns": [{"speaker": "bull", "line": "最近研报和消息面都提到，公司对线下自营店的产品进行了调价。"}]},
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
                {"turns": [{"speaker": "bull", "line": "嗯，这个话题值得展开聊聊。"}]},
                {},
            )
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn("公司与行业", content)
            # Without any F10 directory the card falls back to a clean name/price card.
            self.assertIn('aria-label="公司概览"', content)

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
            builder = HyperFramesBuilder(self._config(root), runner=lambda command, cwd: subprocess.CompletedProcess(command, 0, "", ""))
            project = builder.build_project({}, {}, {})
            with self.assertRaises(HyperFramesBuildError):
                builder.render_mp4(project, root / "missing.mp4")


if __name__ == "__main__":
    unittest.main()
