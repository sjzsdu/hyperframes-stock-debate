"""Minimal tests for the StockTalk pipeline wiring.

These tests validate that the pipeline instantiates and calls each stage in the
correct order, using lightweight stubs that avoid real CLI calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import MagicMock, call, patch

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
    "rounds": [
        {
            "bull": {"line": "平安银行净利润增长稳健", "visual_prompt": "财报数据卡片", "character_name": "股市新手"},
            "bear": {"line": "但不良贷款率仍需关注", "visual_prompt": "风险提示卡片", "character_name": "股市老登"},
        }
    ],
    "disclaimer": "内容为虚拟人物观点碰撞，不构成投资建议。",
}

FAKE_AUDIO: dict[str, Any] = {
    "segments": [
        {"character": "bull", "line": "平安银行净利润增长稳健", "start": 0.0, "end": 3.0},
        {"character": "bear", "line": "但不良贷款率仍需关注", "start": 3.0, "end": 6.0},
    ],
    "total_duration": 6.0,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_load_config_missing_file() -> None:
    assert load_config("/nonexistent.yaml") == {}


def test_load_config_real_file(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("dialogue:\n  rounds: 5\n", encoding="utf-8")
    result = load_config(cfg)
    assert result["dialogue"]["rounds"] == 5


def test_pipeline_runs_e2e(tmp_path: Path) -> None:
    """Full pipeline with stubs — ensures all stages are wired."""
    config = {"output": {"dir": str(tmp_path / "out")}}
    pipeline = Pipeline(config)

    # Stub out each stage
    pipeline.stock_client.get_stock_data = MagicMock(return_value=FAKE_STOCK_DATA)
    pipeline.dialogue_gen.generate = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.compliance.review = MagicMock(return_value=FAKE_SCRIPT)
    pipeline.tts.synthesize = MagicMock(return_value=FAKE_AUDIO)
    pipeline.tts.write_srt = MagicMock(return_value=tmp_path / "out" / "test.srt")
    pipeline.builder.build = MagicMock(return_value={"index_html": "ok"})

    result = pipeline.run("000001", stock_name="平安银行")

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
    pipeline.tts.write_srt = MagicMock(return_value=tmp_path / "out2" / "x.srt")
    pipeline.builder.build = MagicMock(return_value={})

    pipeline.run("000001")

    results = list((tmp_path / "out2").glob("*.json"))
    assert len(results) == 1
    data = json.loads(results[0].read_text(encoding="utf-8"))
    assert data["stock_code"] == "000001"
