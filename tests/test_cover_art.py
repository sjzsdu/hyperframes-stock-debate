"""AI 封面底图测试：可降级、可复现、按 ticker 缓存、尺寸对齐封面预设。

2026-10-28 用户要求「每个个股通过 AI 生成个性化专业封面」。底图是一层背景，
失败必须等价于「没有这个功能」——所以这里锁的是降级与成本，不是画面好看。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from stocktalk.modules.cover import COVER_PRESETS, CoverGenerator
from stocktalk.modules.cover_art import (ENV_TOGGLE, AiCoverArt, FALLBACK_IMAGERY,
                                         art_prompt, art_seed, error_detail,
                                         image_data_uri, industry_imagery, resolve_mode)

PNG = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + (1080).to_bytes(4, "big") + (1440).to_bytes(4, "big")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32

SCRIPT: dict[str, Any] = {
    "title": "贵州茅台（600519）：一瓶酒凭什么卖出这么高的毛利",
    "turns": [
        {"speaker": "bull", "line": "直销渠道把钱留在了自己口袋", "visual": ["直销渠道", "毛利率"]},
        {"speaker": "bear", "line": "批价一松，提价空间就没了", "visual": ["批价"]},
    ],
}

STOCK: dict[str, Any] = {
    "quote": {"code": "600519", "name": "贵州茅台", "price": 1712.4, "change_pct": 2.64},
    "technical": {"history": [{"timestamp": f"2026-09-{day:02d}", "price": {"current": 1600 + day * 5}}
                              for day in range(1, 25)]},
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's own toggle out of the test's decisions."""
    monkeypatch.delenv(ENV_TOGGLE, raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)


def ai_config(**ai: Any) -> dict[str, Any]:
    return {"cover": {"ai": {"enabled": "auto", **ai}}, "output": {"dir": "output"}}


def fake_runner(calls: list[list[str]], payload: bytes = PNG):
    """Pretend to be ``bl image generate``: drop a file in --out-dir, record argv."""
    def runner(argv, timeout):
        calls.append(list(argv))
        out_dir = Path(argv[argv.index("--out-dir") + 1])
        (out_dir / "cover_0.png").write_bytes(payload)
        return "{}"
    return runner


# ---------------------------------------------------------------------------
# 可降级：没有传输就一行钱都不花
# ---------------------------------------------------------------------------

def test_no_cli_and_no_key_means_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stocktalk.modules.cover_art.shutil.which", lambda name: None)
    art = AiCoverArt(ai_config())
    assert art.enabled is False
    assert art.background_uri(stock_name="贵州茅台", stock_code="600519") is None
    assert art.last_error == ""


def test_key_env_alone_is_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stocktalk.modules.cover_art.shutil.which", lambda name: None)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    assert AiCoverArt(ai_config()).enabled is True


def test_without_ai_section_the_feature_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧 config / 直接构造 CoverGenerator 的调用方不能被「本机有 bl」拖去花钱。"""
    monkeypatch.setattr("stocktalk.modules.cover_art.shutil.which", lambda name: "bl")
    art = AiCoverArt({"cover": {"enabled": True}})
    assert art.enabled is False


def test_env_toggle_off_beats_config_that_says_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stocktalk.modules.cover_art.shutil.which", lambda name: "bl")
    monkeypatch.setenv(ENV_TOGGLE, "0")
    assert AiCoverArt(ai_config(enabled=True)).enabled is False


def test_env_toggle_on_beats_config_that_says_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stocktalk.modules.cover_art.shutil.which", lambda name: "bl")
    monkeypatch.setenv(ENV_TOGGLE, "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    assert AiCoverArt(ai_config(enabled=False)).enabled is True


def test_resolve_mode_defaults() -> None:
    assert resolve_mode(None, section_present=False) == "off"
    assert resolve_mode(None) == "auto"
    assert resolve_mode("auto") == "auto"
    assert resolve_mode("false") == "off"
    assert resolve_mode(True) == "on"


def test_failure_degrades_and_stops_retrying(tmp_path: Path) -> None:
    """失败一次就不再试：一次渲染要出 1-3 个尺寸，逐个等超时会拖死链路。"""
    calls: list[list[str]] = []

    def boom(argv, timeout):
        calls.append(list(argv))
        raise RuntimeError("bl: not logged in")

    art = AiCoverArt(ai_config(cache_dir=str(tmp_path)), runner=boom)
    assert art.background_uri(stock_name="贵州茅台", stock_code="600519", size="portrait") is None
    assert "not logged in" in art.last_error
    assert art.background_uri(stock_name="贵州茅台", stock_code="600519", size="landscape") is None
    assert len(calls) == 1


def test_error_detail_reads_the_cli_json_envelope() -> None:
    """bl 的失败是 JSON：只取最后一行会得到 ``}``，日志里等于没说。"""
    assert error_detail('{\n  "error": {"code": 2, "message": "Invalid API-key provided."}\n}') \
        == "Invalid API-key provided."
    assert error_detail("bl: command not found") == "bl: command not found"
    assert error_detail("") == ""


def test_runner_output_without_image_degrades(tmp_path: Path) -> None:
    art = AiCoverArt(ai_config(cache_dir=str(tmp_path)), runner=lambda argv, timeout: "{}")
    assert art.background_uri(stock_name="贵州茅台", stock_code="600519") is None
    assert "没有产出图片" in art.last_error


# ---------------------------------------------------------------------------
# 尺寸：与封面预设一一对应，不引入第二种画幅
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", sorted(COVER_PRESETS))
def test_request_uses_the_cover_preset_pixels(tmp_path: Path, size: str) -> None:
    calls: list[list[str]] = []
    art = AiCoverArt(ai_config(cache_dir=str(tmp_path)), runner=fake_runner(calls))
    art.background_uri(stock_name="贵州茅台", stock_code="600519", size=size,
                       preset=COVER_PRESETS[size])
    width, height = COVER_PRESETS[size]
    assert calls[0][calls[0].index("--size") + 1] == f"{width}*{height}"


def test_request_never_lets_the_service_rewrite_or_stamp(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    art = AiCoverArt(ai_config(cache_dir=str(tmp_path)), runner=fake_runner(calls))
    art.background_uri(stock_name="贵州茅台", stock_code="600519")
    argv = calls[0]
    # prompt 扩写会让同一 ticker 的图漂移；水印会压在封面上。
    assert argv[argv.index("--prompt-extend") + 1] == "false"
    assert argv[argv.index("--watermark") + 1] == "false"
    assert argv[argv.index("--model") + 1] == "wan2.6-t2i"
    assert argv[argv.index("--n") + 1] == "1"


# ---------------------------------------------------------------------------
# 可复现：ticker 决定 seed，prompt 结构固定
# ---------------------------------------------------------------------------

def test_seed_is_stable_per_ticker_and_differs_between_tickers() -> None:
    assert art_seed("600519") == art_seed(" 600519 ")
    assert art_seed("600519") != art_seed("000001")
    assert 0 <= art_seed("600519") < 2_147_483_647


def test_prompt_never_puts_names_codes_or_copy_into_the_image() -> None:
    """公司名/代码/叙事原句绝不进 prompt：实测它们会被原样画成图上的字。

    2026-10-28 对比实测：旧 prompt 把「贵州茅台（600519）」和标题原句写进去，
    出来的底图上有整版假标题、假图标条（甚至「崩盘预警」这类合规禁语）——
    而图里的字不受合规审查。这条测试是那次事故的护栏。
    """
    prompt = art_prompt(stock_name="贵州茅台", industry="饮料制造", tone="up", size="portrait")
    assert "贵州茅台" not in prompt
    assert "600519" not in prompt
    assert "饮料制造" not in prompt          # 行业只用来查意象，也不进正文
    assert "酒窖" in prompt                  # 换成可画出来的意象
    assert "暖红金" in prompt
    # 风格锚必须在最前面：整账号封面「一家人」的锚点。
    assert prompt.startswith("电影感氛围背景摄影")
    # 英文禁字约束必须存在且排在最后。
    assert prompt.endswith("no brand packaging.")


def test_prompt_only_changes_in_its_slots() -> None:
    first = art_prompt(stock_name="贵州茅台", tone="up", size="portrait")
    second = art_prompt(stock_name="贵州茅台", tone="down", size="portrait")
    assert first.replace("暖红金色调，向上的势能", "冷绿青色偏调，承压而克制的氛围") == second


def test_the_same_ticker_always_maps_to_the_same_imagery() -> None:
    """身份感来自「意象 + seed」，而不是往图上写字。"""
    assert industry_imagery("贵州茅台") == industry_imagery("茅台股份")
    assert industry_imagery("宁德时代") != industry_imagery("贵州茅台")
    assert industry_imagery("某某科技") == industry_imagery("某某科技")


def test_f10_industry_beats_guessing_from_the_name() -> None:
    assert industry_imagery("紫金矿业", "银行") == industry_imagery("", "银行")
    assert industry_imagery("", "") == FALLBACK_IMAGERY


# ---------------------------------------------------------------------------
# 不重复付费：按 ticker 缓存，命中就不再调服务
# ---------------------------------------------------------------------------

def test_second_call_reuses_the_cached_art(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    art = AiCoverArt(ai_config(cache_dir=str(tmp_path)), runner=fake_runner(calls))
    first = art.background_uri(stock_name="贵州茅台", stock_code="600519", size="portrait")
    second = art.background_uri(stock_name="贵州茅台", stock_code="600519", size="portrait")
    assert first == second and first is not None
    assert len(calls) == 1
    assert list((tmp_path / "600519").iterdir())


def test_ticker_reuse_ignores_a_new_episode_narrative(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    art = AiCoverArt(ai_config(cache_dir=str(tmp_path)), runner=fake_runner(calls))
    art.background_uri(stock_name="贵州茅台", stock_code="600519", narrative="第一期的说法")
    art.background_uri(stock_name="贵州茅台", stock_code="600519", narrative="第二期的说法")
    assert len(calls) == 1


def test_episode_reuse_regenerates_on_a_new_narrative(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    art = AiCoverArt(ai_config(cache_dir=str(tmp_path), reuse="episode"),
                     runner=fake_runner(calls))
    art.background_uri(stock_name="贵州茅台", stock_code="600519", narrative="第一期的说法")
    art.background_uri(stock_name="贵州茅台", stock_code="600519", narrative="第二期的说法")
    assert len(calls) == 2


def test_switching_the_model_invalidates_the_cache(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    runner = fake_runner(calls)
    for model in ("qwen-image-3.0", "wan2.6-t2i"):
        art = AiCoverArt(ai_config(cache_dir=str(tmp_path), model=model), runner=runner)
        assert art.background_uri(stock_name="贵州茅台", stock_code="600519") is not None
    assert len(calls) == 2


def test_cache_lives_under_output_dir_by_default() -> None:
    art = AiCoverArt({"cover": {"ai": {"enabled": True}}, "output": {"dir": "build/out"}})
    assert art.cache_dir == Path("build/out") / "cover-art"


# ---------------------------------------------------------------------------
# data URI：模板拿到的就是这张图
# ---------------------------------------------------------------------------

def test_data_uri_matches_the_actual_bytes(tmp_path: Path) -> None:
    target = tmp_path / "art"
    target.write_bytes(JPEG)
    uri = image_data_uri(target)
    assert uri.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == JPEG


def test_background_uri_is_a_png_data_uri(tmp_path: Path) -> None:
    art = AiCoverArt(ai_config(cache_dir=str(tmp_path)), runner=fake_runner([]))
    uri = art.background_uri(stock_name="贵州茅台", stock_code="600519")
    assert uri is not None and uri.startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# 模板接入：有图时是一层背景，没图时封面与加功能之前一模一样
# ---------------------------------------------------------------------------

def html_for(tmp_path: Path, art_runner=None, script: dict[str, Any] | None = None,
             ai: dict[str, Any] | None = None) -> str:
    config = {"cover": {"chrome_path": "chrome",
                        "ai": {"enabled": True, "cache_dir": str(tmp_path / "art"),
                               **(ai or {})}}}
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    config["cover"]["chrome_path"] = str(chrome)
    generator = CoverGenerator(config, art_runner=art_runner)
    return generator._html(STOCK, script or SCRIPT, "600519", "portrait", 1080, 1440)


def test_cover_html_gets_the_art_layer(tmp_path: Path) -> None:
    html = html_for(tmp_path, art_runner=fake_runner([]))
    assert 'class="up has-art"' in html
    assert '<div class="ai-art" aria-hidden="true">' in html
    assert "data:image/png;base64," in html
    # 底图是纯背景层：铺满 + 裁切 + 柔焦，不改 grid 布局。
    assert "object-fit: cover" in html
    assert "filter: blur(3px)" in html


def test_cover_html_is_unchanged_without_art(tmp_path: Path) -> None:
    """关掉 AI 或调用失败时，出来的 HTML 与加这个功能之前一模一样。"""
    html = html_for(tmp_path, ai={"enabled": False})
    assert '<div class="ai-art"' not in html
    assert 'class="up has-art"' not in html


def test_ai_failure_still_renders_the_normal_cover(tmp_path: Path) -> None:
    def boom(argv, timeout):
        raise RuntimeError("network down")

    html = html_for(tmp_path, art_runner=boom)
    assert '<div class="ai-art"' not in html
    assert "贵州茅台" in html


def test_art_prompt_never_carries_trade_advice(tmp_path: Path) -> None:
    """合规禁语也不能借 AI 图绕过审查：prompt 里根本不出现标题原句。"""
    script = dict(SCRIPT, title="贵州茅台（600519）：建议买入并加仓，目标价 2000")
    calls: list[list[str]] = []
    html = html_for(tmp_path, art_runner=fake_runner(calls), script=script)
    prompt = calls[0][calls[0].index("--prompt") + 1]
    for banned in ("买入", "加仓", "目标价", "贵州茅台", "600519"):
        assert banned not in prompt
    assert "has-art" in html


def test_cover_dimensions_are_still_the_preset(tmp_path: Path) -> None:
    """接入底图后封面 PNG 尺寸不变——渲染器布局不需要任何改动。"""
    def shooter(chrome, html_path, png, size, timeout):
        width, height = size
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
                        + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x00" * 64)
        return True

    chrome = tmp_path / "chrome"
    chrome.write_text("")
    config = {"cover": {"chrome_path": str(chrome),
                        "ai": {"enabled": True, "cache_dir": str(tmp_path / "art")}}}
    generator = CoverGenerator(config, screenshotter=shooter, art_runner=fake_runner([]))
    rendered = generator.generate(STOCK, SCRIPT, "600519", ["portrait", "wide"], tmp_path, "tag")
    assert rendered
    for size, path in rendered.items():
        header = path.read_bytes()[:24]
        assert (int.from_bytes(header[16:20], "big"),
                int.from_bytes(header[20:24], "big")) == COVER_PRESETS[size]
