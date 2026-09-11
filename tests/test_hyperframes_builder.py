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
            self.assertEqual(project.duration, 4.0)
            self.assertIn('data-composition-id="stock-debate"', content)
            self.assertIn('data-track-index="4"', content)
            self.assertIn('id="audio-1"', content)
            self.assertIn("window.__timelines['stock-debate']", (project.directory / "stock-debate.js").read_text(encoding="utf-8"))
            self.assertIn("<svg", content)
            self.assertIn('data-topic="financial"', content)
            self.assertIn('class="ma draw-line"', content)
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
            for banned in ("看多方", "看空方", "看多", "看空", "免责", "不构成", "投资建议", "虚拟人物", "谈什么", "字幕突出", "visual_prompt"):
                self.assertNotIn(banned, content)
            self.assertIn("正在讨论", content)
            self.assertIn('data-topic="risk"', content)

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
            self.assertIn("暂无公司业务资料", content)

    def test_render_requires_nonempty_mp4(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            builder = HyperFramesBuilder(self._config(root), runner=lambda command, cwd: subprocess.CompletedProcess(command, 0, "", ""))
            project = builder.build_project({}, {}, {})
            with self.assertRaises(HyperFramesBuildError):
                builder.render_mp4(project, root / "missing.mp4")


if __name__ == "__main__":
    unittest.main()
