"""Explicit offline acceptance fixtures, never selected as a production fallback."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

from stocktalk.modules.hyperframes_builder import HyperFramesBuilder
from stocktalk.video_types import bind_type


class DemoPipeline:
    def __init__(self, config):
        self.config = config

    def run(self, stock, *, render, publish):
        assert render and not publish
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("offline acceptance requires ffmpeg")
        t = bind_type(self.config)
        root = Path(self.config["output"]["dir"])
        root.mkdir(parents=True, exist_ok=True)
        script = {"stock_code": stock, "stock_name": "示例公司", "title": "示例公司：行情与风险",
                  "format": t.strategy, "type_id": t.id, "type_digest": t.digest,
                  "turns": [{"speaker": role["speaker_id"], "character_name": role["name"],
                             "line": "这是固定验收数据，股市有风险，投资需谨慎。"} for role in t.declaration["cast"]]}
        media = root / "fixture.mp4"
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=0x142c39:s=640x360:d=1",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(media)], check=True, timeout=20)
        cover = root / "cover.png"
        subprocess.run([ffmpeg, "-v", "error", "-i", str(media), "-frames:v", "1", str(cover)], check=True, timeout=10)
        (root / "script.json").write_text(json.dumps(script, ensure_ascii=False))
        (root / "subtitles.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n固定验收数据；投资需谨慎。\n")
        result = {"acceptance_fixture": True, "stock_name": "示例公司", "approved_script": script, "video_path": str(media),
                  "platform_videos": {}, "covers": {"landscape": str(cover)}, "canvas": "horizontal", "elapsed_seconds": 1}
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False))
        return result


class DemoPublisher:
    def __init__(self, config):
        self.config = config

    def _require_sau(self):
        return "offline-fixture"

    def check_platforms(self, platforms):
        return {p: {"ok": True} for p in platforms}

    def publish(self, video, script, name, stock, *, platforms, schedule, platform_videos, covers):
        assert Path(video).is_file()
        return {"platforms": [{"platform": p, "ok": True} for p in platforms]}
