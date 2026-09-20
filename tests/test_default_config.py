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
    """抖音/视频号的播放分成硬性要求原创横屏 ≥1 分钟。"""
    assert _config()["video"]["canvas"] == "horizontal"


def test_every_platform_names_its_preferred_canvas() -> None:
    """横版拿播放分成，竖版给竖屏社区。

    platform_canvas 同时决定「一次运行渲几版」：这里列了两种画幅，
    所以会渲出横竖两个 MP4，各平台取自己那一版。
    小红书 2026-09-18 停发（封面生成不稳定），不在默认列表里。
    """
    canvases = _config()["publish"]["platform_canvas"]
    assert set(canvases) == {"douyin", "bilibili", "kuaishou", "tencent", "baijiahao"}
    assert set(canvases.values()) == {"horizontal", "vertical"}
    # 抖音/视频号的分成硬性要求原创横屏 ≥1 分钟；B站横屏生态；百家号横版才有
    # 播放分成且上传器强制横版封面。
    for platform in ("douyin", "bilibili", "tencent", "baijiahao"):
        assert canvases[platform] == "horizontal"
    # 快手激励不强制横屏且流量收益集中在竖屏。
    assert canvases["kuaishou"] == "vertical"
    # 小红书已从发布渠道移除。
    assert "xiaohongshu" not in _config()["publish"]["platforms"]


def test_safe_areas_cover_platform_ui() -> None:
    """安全区按用户反馈定稿。

    竖版 25×2 对称：100×2 的 1/4（2026-09-19 用户反馈两侧留白太大），
    内容列 1030px；代价是抖音/快手右缘互动竖排可能压图表右端文字，
    真机若被遮用 side_right 单独抬高。180×2 只剩 720px、60/140 非对称
    都被否掉。横版顶部 120：B站播放器左上角 logo/标题会压住 60 的旧值。
    """
    safe = _config()["video"]["safe_area"]
    assert safe["vertical"] == {"top": 240, "bottom": 460, "side": 25}
    assert safe["horizontal"] == {"top": 120, "bottom": 100, "side": 70}


def test_vertical_captions_fit_the_safe_strip() -> None:
    """竖版字幕一行必须装进安全条（当前 25×2 边距 → 1030px）。"""
    from stocktalk.modules.hyperframes_builder import CAPTION_MAX_CHARS

    assert CAPTION_MAX_CHARS * 44 + 72 <= 1080 - 2 * 25  # 44px 字号 + 36px 内边距×2


def test_captions_are_one_line_on_both_canvases() -> None:
    """一行字幕：整句换行会把版面顶上去，主画面就稳不住。"""
    assert _config()["video"]["subtitles"] == "line"


def test_target_duration_stays_under_two_minutes() -> None:
    """完播率决定有效播放，超过两分钟的长片分成反而更低。"""
    dialogue = _config()["dialogue"]
    assert dialogue["min_duration_seconds"] >= 60
    assert dialogue["max_duration_seconds"] <= 120


def test_ai_content_label_is_configured_for_the_cli_platform() -> None:
    """快手是唯一走 --ai-content-label 通道的默认平台（小红书已停发）。"""
    label = _config()["publish"]["ai_content_label"]
    assert label["kuaishou"]


def test_baijiahao_spec_needs_a_wide_cover() -> None:
    """百家号上传器强制横版封面：spec 必须带 wide，缺失会导致上传直接失败。"""
    from stocktalk.modules.publisher import PLATFORM_SPECS, cover_sizes_for

    spec = PLATFORM_SPECS["baijiahao"]
    assert dict(spec["covers"])["--thumbnail"] == "wide"
    assert "wide" in cover_sizes_for(["baijiahao"])
