"""Minimal tests for the StockTalk pipeline wiring.

These tests validate that the pipeline instantiates and calls each stage in the
correct order, using lightweight stubs that avoid real CLI calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import MagicMock

import pytest

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
    "title": "平安银行（000001）观点碰撞",
    "turns": [
        {"speaker": "bull", "line": "平安银行净利润增长稳健", "beat": "数据表象", "visual_prompt": "财报数据卡片", "character_name": "股市新手"},
        {"speaker": "bear", "line": "但不良贷款率仍需关注", "beat": "风险追问", "visual_prompt": "风险提示卡片", "character_name": "股市老登"},
    ],
    "disclaimer": "内容为虚拟人物观点碰撞，不构成投资建议。",
}

FAKE_AUDIO: dict[str, Any] = {
    "segments": [
        {"character": "bull", "line": "平安银行净利润增长稳健", "start": 0.0, "end": 3.0},
        {"character": "bear", "line": "但不良贷款率仍需关注", "start": 3.0, "end": 6.0},
    ],
    "srt_path": "/tmp/fake_subtitles.srt",
    "total_duration": 6.0,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_load_config_missing_file() -> None:
    config = load_config("/nonexistent.yaml")
    assert config["characters"]["bull"]["voice_id"] == "longfeifei_v3"


def test_load_config_real_file(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("dialogue:\n  rounds: 5\n", encoding="utf-8")
    result = load_config(cfg)
    assert result["dialogue"]["rounds"] == 5
    assert result["characters"]["bear"]["voice_id"] == "longtian_v3"


def test_load_config_uses_bundled_defaults() -> None:
    config = load_config()
    assert config["characters"]["bull"]["voice_id"] == "longfeifei_v3"
    assert config["characters"]["bear"]["voice_id"] == "longtian_v3"
    assert "forbidden_words" in config["compliance"]
    assert config["tts"]["model"] == "cosyvoice-v3-flash"


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
