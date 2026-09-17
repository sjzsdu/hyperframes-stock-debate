"""Guardrails on the shipped defaults.

These are monetisation decisions, not cosmetic ones: the platforms only pay
playback revenue for landscape clips over a minute, so the defaults are pinned
here so a future edit cannot quietly switch them back.
"""

from __future__ import annotations

from typing import Any

from stocktalk.pipeline import load_config


def _config() -> dict[str, Any]:
    return load_config(None)


def test_master_canvas_is_landscape() -> None:
    """抖音/视频号/快手的播放分成都要求原创横屏 ≥1 分钟。"""
    assert _config()["video"]["canvas"] == "horizontal"


def test_every_platform_names_its_preferred_canvas() -> None:
    """桌面版（横版）拿播放分成，手机版（竖版）只给小红书引流。

    platform_canvas 同时决定「一次运行渲几版」：这里列了两种画幅，
    所以会渲出横竖两个 MP4，各平台取自己那一版。
    """
    canvases = _config()["publish"]["platform_canvas"]
    assert set(canvases) == {"douyin", "bilibili", "kuaishou", "xiaohongshu", "tencent"}
    assert set(canvases.values()) == {"horizontal", "vertical"}
    # 抖音/视频号/快手的分成只认原创横屏，B站本身也是横屏生态。
    for platform in ("douyin", "bilibili", "kuaishou", "tencent"):
        assert canvases[platform] == "horizontal"
    # 小红书没有播放分成，竖版完播与互动更好。
    assert canvases["xiaohongshu"] == "vertical"


def test_captions_are_one_line_on_both_canvases() -> None:
    """一行字幕：整句换行会把版面顶上去，主画面就稳不住。"""
    assert _config()["video"]["subtitles"] == "line"


def test_target_duration_stays_under_two_minutes() -> None:
    """完播率决定有效播放，超过两分钟的长片分成反而更低。"""
    dialogue = _config()["dialogue"]
    assert dialogue["min_duration_seconds"] >= 60
    assert dialogue["max_duration_seconds"] <= 120


def test_ai_content_label_is_configured_for_both_cli_platforms() -> None:
    label = _config()["publish"]["ai_content_label"]
    assert label["kuaishou"]
    assert label["xiaohongshu"]
