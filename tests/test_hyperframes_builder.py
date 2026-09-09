import subprocess
import tempfile
import unittest
from pathlib import Path

from stocktalk.modules.hyperframes_builder import HyperFramesBuildError, HyperFramesBuilder


class HyperFramesBuilderTests(unittest.TestCase):
    def _config(self, directory):
        return {"video": {"output_dir": str(directory)}, "characters": {"bull": {"name": "多头"}, "bear": {"name": "空头"}}}

    def test_builds_timed_multitrack_composition(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = HyperFramesBuilder(self._config(temp))
            project = builder.build_project(
                {"code": "600519", "quote": {"name": "贵州茅台", "price": 1500, "change_pct": 1.2}, "financials": {"revenue": "100亿"}, "kline": [{"open": 10, "close": 12, "high": 13, "low": 9}]},
                {},
                {"segments": [{"character": "bull", "line": "营收保持韧性", "start_time": 1, "duration": 3, "audio_path": "/tmp/audio.wav"}], "total_duration": 4},
            )
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertEqual(project.duration, 4.0)
            self.assertIn('data-composition-id="stock-debate"', content)
            self.assertIn('data-track-index="4"', content)
            self.assertIn('id="audio-1"', content)
            self.assertIn('window.__timelines["stock-debate"]', content)
            self.assertIn("<svg", content)
            self.assertIn('data-topic="financial"', content)
            self.assertIn('class="ma draw-line"', content)
            self.assertTrue((project.directory / "stock-debate.css").is_file())
            self.assertTrue((project.directory / "stock-debate.js").is_file())
            self.assertTrue((project.directory / "index.motion.json").is_file())

    def test_uses_visual_prompt_to_select_risk_visual(self):
        with tempfile.TemporaryDirectory() as temp:
            project = HyperFramesBuilder(self._config(temp)).build_project(
                {},
                {"turns": [{"speaker": "bull", "line": "需要审慎", "visual_prompt": "展示下行风险"}]},
                {},
            )
            content = project.composition_path.read_text(encoding="utf-8")
            self.assertIn('data-topic="risk"', content)
            self.assertIn("展示下行风险", content)

    def test_render_requires_nonempty_mp4(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            builder = HyperFramesBuilder(self._config(root), runner=lambda command, cwd: subprocess.CompletedProcess(command, 0, "", ""))
            project = builder.build_project({}, {}, {})
            with self.assertRaises(HyperFramesBuildError):
                builder.render_mp4(project, root / "missing.mp4")


if __name__ == "__main__":
    unittest.main()
