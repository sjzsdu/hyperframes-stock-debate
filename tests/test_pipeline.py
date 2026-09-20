"""谈股论金流水线连接的最小测试。

这些测试验证流水线实例化并按正确顺序调用每个阶段，
使用轻量级存根避免真正的 CLI 调用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import MagicMock

import pytest

import stocktalk.pipeline as pipeline_module
from stocktalk.pipeline import Pipeline, load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_STOCK_DATA: dict[str, Any] = {
    "code": "000001",
    "quote": {"code": "000001", "name": "平安银行", "price": 12.5, "change_pct": 1.2},
    "technical": {"summary": {}, "history": [], "count": 0},
    "financials": {},
    "f10": {},
    "kline": [],
    "unavailable": {},
}

FAKE_SCRIPT: dict[str, Any] = {
    "stock_code": "000001",
    "stock_name": "平安银行",
    "title": "平安银行（000001）：靠息差吃饭的生意",
    "turns": [
        {"speaker": "bull", "line": "平安银行净利润增长稳健", "beat": "生意本质", "character_name": "股市新手"},
        {"speaker": "bear", "line": "但不良贷款率仍需关注", "beat": "风险追问", "character_name": "股市老登"},
    ],
}

FAKE_AUDIO: dict[str, Any] = {
    "segments": [
        {"character": "bull", "line": "平安银行净利润增长稳健", "start_time": 0.0, "end_time": 3.0, "duration": 3.0},
        {"character": "bear", "line": "但不良贷款率仍需关注", "start_time": 3.0, "end_time": 6.0, "duration": 3.0},
    ],
    "srt_path": "/tmp/fake_subtitles.srt",
    "total_duration": 6.0,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_load_config_missing_file() -> None:
    config = load_config("/nonexistent.yaml")
    assert "voice_id" in config["characters"]["bull"]


def test_load_config_real_file(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("dialogue:\n  rounds: 5\n", encoding="utf-8")
    result = load_config(cfg)
    assert result["dialogue"]["rounds"] == 5
    assert "voice_id" in result["characters"]["bear"]


def test_load_config_uses_bundled_defaults() -> None:
    config = load_config()
    assert config["characters"]["bull"]["voice_id"]
    assert config["characters"]["bear"]["voice_id"]
    assert "forbidden_words" in config["compliance"]
    assert config["tts"]["model"]
    assert config["tts"]["speed"] == 1.0
    assert config["tts"]["pitch"] == 1.0
    assert config["tts"]["volume"] == 1.0


def test_pipeline_runs_e2e(tmp_path: Path) -> None:
    """Full pipeline with stubs — ensures all stages are wired."""
    config = {"output": {"dir": str(tmp_path / "out")}}
    pipeline = Pipeline(config)

    # Stub out each stage
    pipeline.stock_client.get_stock_data = MagicMock(return_value=FAKE_STOCK_DATA)
    pipeline.dialogue_gen.generate = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.compliance.review = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.tts.synthesize = MagicMock(return_value=FAKE_AUDIO)
    pipeline.builder.build = MagicMock(return_value={"index_html": "ok"})

    result = pipeline.run("000001", stock_name="平安银行", render=False)

    pipeline.stock_client.get_stock_data.assert_called_once_with("000001")
    pipeline.dialogue_gen.generate.assert_called_once()
    pipeline.compliance.review.assert_called_once()
    pipeline.tts.synthesize.assert_called_once()
    pipeline.builder.build.assert_called_once()
    assert result["stock_code"] == "000001"
    assert result["stock_name"] == "平安银行"


def test_pipeline_stores_result_json(tmp_path: Path) -> None:
    config = {"output": {"dir": str(tmp_path / "out2")}}
    pipeline = Pipeline(config)
    pipeline.stock_client.get_stock_data = MagicMock(return_value=FAKE_STOCK_DATA)
    pipeline.dialogue_gen.generate = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.compliance.review = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.tts.synthesize = MagicMock(return_value=FAKE_AUDIO)
    pipeline.builder.build = MagicMock(return_value={})

    pipeline.run("000001", render=False)

    results = list((tmp_path / "out2").glob("*.json"))
    assert len(results) == 1
    data = json.loads(results[0].read_text(encoding="utf-8"))
    assert data["stock_code"] == "000001"
    assert data["srt"] == FAKE_AUDIO["srt_path"]


def test_pipeline_tts_failure_falls_back_to_silent_subtitles(tmp_path: Path) -> None:
    pipeline = Pipeline({"output": {"dir": str(tmp_path / "out")}}, sleep=lambda _: None)
    pipeline.stock_client.get_stock_data = MagicMock(return_value=FAKE_STOCK_DATA)
    pipeline.dialogue_gen.generate = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.compliance.review = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.tts.synthesize = MagicMock(side_effect=RuntimeError("service unavailable"))
    pipeline.builder.build = MagicMock(return_value={})

    result = pipeline.run("000001", render=False)

    assert pipeline.tts.synthesize.call_count == 3
    assert result["audio"]["fallback"] == "silent"
    assert Path(result["srt"]).is_file()
    pipeline.builder.build.assert_called_once()


def test_pipeline_renders_unless_disabled(tmp_path: Path) -> None:
    pipeline = Pipeline({"output": {"dir": str(tmp_path / "out")}})
    pipeline.stock_client.get_stock_data = MagicMock(return_value=FAKE_STOCK_DATA)
    pipeline.dialogue_gen.generate = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.compliance.review = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.tts.synthesize = MagicMock(return_value=FAKE_AUDIO)
    pipeline.builder.build = MagicMock(return_value={})
    video = tmp_path / "out" / "video.mp4"
    pipeline.builder.render_mp4 = MagicMock(return_value=video)

    result = pipeline.run("000001")

    pipeline.builder.render_mp4.assert_called_once()
    assert result["rendered"] is True


def test_preview_skips_rendering(tmp_path: Path) -> None:
    pipeline = Pipeline({"output": {"dir": str(tmp_path / "out")}})
    pipeline.stock_client.get_stock_data = MagicMock(return_value=FAKE_STOCK_DATA)
    pipeline.dialogue_gen.generate = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.compliance.review = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.tts.synthesize = MagicMock(return_value=FAKE_AUDIO)
    pipeline.builder.build = MagicMock(return_value={})
    pipeline.builder.render_mp4 = MagicMock()

    result = pipeline.run("000001", preview=True)

    pipeline.builder.render_mp4.assert_not_called()
    assert result["preview"] is True


def test_platform_cuts_follow_each_platforms_preferred_canvas(tmp_path: Path, monkeypatch: Any) -> None:
    """桌面版+手机版各渲一次，五平台各取自己那一版。

    platform_canvas 列了两种画幅 → 除主片外只补渲一版竖版，
    且只有小红书拿竖版，其余四家拿主片（横版）。
    """
    from rich.progress import Progress

    pipeline = Pipeline({
        "output": {"dir": str(tmp_path / "out")},
        "video": {"canvas": "horizontal"},
        "publish": {
            "platforms": ["douyin", "bilibili", "kuaishou", "xiaohongshu", "tencent"],
            "platform_canvas": {"douyin": "horizontal", "bilibili": "horizontal",
                                "kuaishou": "horizontal", "xiaohongshu": "vertical",
                                "tencent": "horizontal"},
        },
    })

    class FakeBuilder:
        def __init__(self, config: Any) -> None:
            self.config = config

        def build(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

        def render_mp4(self, variant: Any, output_path: Any) -> Path:
            Path(output_path).write_bytes(b"mp4")
            return Path(output_path)

    monkeypatch.setattr(pipeline_module, "HyperFramesBuilder", FakeBuilder)
    with Progress() as progress:
        task = progress.add_task("render", total=1)
        cuts = pipeline._render_platform_cuts(FAKE_STOCK_DATA, FAKE_SCRIPT, FAKE_AUDIO,
                                              tmp_path, "tag", progress, task)
    # 只补渲了与主画幅不同的小红书竖版。
    assert set(cuts) == {"xiaohongshu"}
    assert cuts["xiaohongshu"].endswith(".vertical.mp4")


def test_covers_and_review_page_ship_with_the_video(tmp_path: Path) -> None:
    """封面与审查页跟着成片一起出，不必等到发布（2026-09-20 用户要求）。

    不发布的人同样要能审查信息流里的样子，所以封面不能再挂在 ``publish``
    分支上；审查页失败也只降级，不影响成片。
    """
    pipeline = Pipeline({
        "output": {"dir": str(tmp_path / "out")},
        "video": {"canvas": "horizontal"},
        "publish": {"platforms": ["douyin"], "platform_canvas": {"douyin": "horizontal"}},
    })
    pipeline.stock_client.get_stock_data = MagicMock(return_value=FAKE_STOCK_DATA)
    pipeline.dialogue_gen.generate = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.compliance.review = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.tts.synthesize = MagicMock(return_value=FAKE_AUDIO)
    pipeline.builder.build = MagicMock(return_value={})
    pipeline.builder.render_mp4 = MagicMock(return_value=tmp_path / "out" / "video.mp4")
    cover = str(tmp_path / "out" / "video.cover-portrait.png")
    pipeline._render_covers = MagicMock(return_value={"portrait": cover})
    pipeline.publisher.publish = MagicMock()

    result = pipeline.run("000001", stock_name="平安银行")

    pipeline._render_covers.assert_called_once()
    pipeline.publisher.publish.assert_not_called()
    assert result["covers"] == {"portrait": cover}
    review = Path(result["review_path"])
    assert review.is_file() and review.name.endswith(".review.html")
    assert "video.mp4" in review.read_text(encoding="utf-8")


def test_review_failure_never_breaks_the_run(tmp_path: Path) -> None:
    """审查页只是审查辅助：写崩了也只记一行，成片照旧返回。"""
    pipeline = Pipeline({"output": {"dir": str(tmp_path / "out")}})
    pipeline.stock_client.get_stock_data = MagicMock(return_value=FAKE_STOCK_DATA)
    pipeline.dialogue_gen.generate = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.compliance.review = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.tts.synthesize = MagicMock(return_value=FAKE_AUDIO)
    pipeline.builder.build = MagicMock(return_value={})
    pipeline.builder.render_mp4 = MagicMock(return_value=tmp_path / "out" / "video.mp4")
    pipeline._render_covers = MagicMock(return_value={})

    monkeypatch_target = "stocktalk.pipeline.write_review_page"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(monkeypatch_target, MagicMock(side_effect=OSError("disk full")))
        result = pipeline.run("000001")

    assert result["rendered"] is True
    assert result["review_path"] is None
