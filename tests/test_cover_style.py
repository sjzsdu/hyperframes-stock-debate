"""封面风格测试：独白片与辩论片的封面必须是两种视觉，且共用同一套内容纪律。

2026-10-25 用户意见：「封面能否做一个调整，以便与之前的风格做一个区别」。
这里锁住三件事：
  1) 风格选择（cover.style=auto 跟剧本形态走；写死则强制）；
  2) 独白封面真的换了版式（墨黑+紫+细走势带+章节导轨，不再有「多空对话」和图板）；
  3) 辩论封面一个字没动（旧成片重渲/补发的封面不能跟着变样）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stocktalk.modules import dialogue_generator
from stocktalk.modules.cover import (COVER_PRESETS, COVER_STYLES, COVER_TEMPLATES,
                                     SECTION_MARK_LIMIT, SPEECH_CHARS_PER_SECOND,
                                     STYLE_DEBATE, STYLE_MONOLOGUE, CoverGenerator)

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def png_bytes(width: int, height: int) -> bytes:
    return PNG_HEADER + (13).to_bytes(4, "big") + b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")


STOCK: dict[str, Any] = {
    "quote": {"code": "600519", "name": "贵州茅台", "price": 1712.4, "change_pct": 2.64},
    "technical": {"history": [{"timestamp": f"2026-09-{day:02d}", "price": {"current": 1600 + day * 5}}
                              for day in range(1, 25)],
                  "summary": {"signal": "多头排列"}},
}

MONOLOGUE_SCRIPT: dict[str, Any] = {
    "format": "monologue",
    "title": "贵州茅台（600519）：一瓶酒凭什么卖出这么高的毛利",
    "hook": "直销渠道还能撑多久",
    "question": "提价空间还剩多少",
    "spotlight": {"label": "直销占比", "value": "45.7%"},
    "estimated_seconds": 187.9,
    "turns": [
        {"speaker": "narrator", "line": "先把生意讲清楚：卖的是酒，赚的是品牌溢价。", "visual": ["品牌溢价"]},
        {"speaker": "narrator", "line": "再看账：毛利率九成以上，现金回款比利润还稳。", "visual": ["毛利率"]},
        {"speaker": "narrator", "line": "第三看渠道：直销占比在往上走。", "visual": ["直销"]},
        {"speaker": "narrator", "line": "第四看护城河：产能是硬约束。", "visual": ["产能"]},
        {"speaker": "narrator", "line": "第五看风险：批价波动最伤情绪。", "visual": ["批价"]},
        {"speaker": "narrator", "line": "最后一句：盯住批价和直销比例。", "visual": ["批价"]},
    ],
}

DEBATE_SCRIPT: dict[str, Any] = {
    "format": "debate",
    "title": "贵州茅台（600519）：多空双方各自盯什么",
    "hook": "多空的分歧点在哪",
    "turns": [
        {"speaker": "bull", "line": "品牌与产能是硬底盘。", "visual": ["品牌"]},
        {"speaker": "bear", "line": "批价一松估值就要退。", "visual": ["批价"]},
    ],
}


def generator(tmp_path: Path, **config: Any) -> CoverGenerator:
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    base: dict[str, Any] = {"cover": {"chrome_path": str(chrome)}}
    base.update(config)
    return CoverGenerator(base)


def html(tmp_path: Path, script: dict[str, Any], size: str = "portrait",
         **config: Any) -> str:
    width, height = COVER_PRESETS[size]
    return generator(tmp_path, **config)._html(STOCK, script, "600519", size, width, height)


def body(page: str) -> str:
    """只看上屏内容：模板里的注释会提到两套版式，断言文案必须排除注释。"""
    return page.split("<body>", 1)[-1]


# ---------------------------------------------------------------------------
# 字面量与模板注册
# ---------------------------------------------------------------------------

def test_style_names_match_the_script_format_literals() -> None:
    """风格名与剧本 format 是同一套字面量——两边各自写死，靠这个断言防漂移。"""
    assert STYLE_DEBATE == dialogue_generator.FORMAT_DEBATE
    assert STYLE_MONOLOGUE == dialogue_generator.FORMAT_MONOLOGUE
    assert SPEECH_CHARS_PER_SECOND == dialogue_generator.SPEECH_CHARS_PER_SECOND
    assert set(COVER_STYLES) == set(COVER_TEMPLATES)


def test_both_cover_templates_exist() -> None:
    root = Path(__file__).resolve().parents[1] / "stocktalk" / "templates" / "cover"
    for name in COVER_TEMPLATES.values():
        assert (root / name).is_file(), name


# ---------------------------------------------------------------------------
# 风格选择
# ---------------------------------------------------------------------------

def test_auto_follows_the_script_format(tmp_path: Path) -> None:
    gen = generator(tmp_path)
    assert gen.style_for(MONOLOGUE_SCRIPT) == STYLE_MONOLOGUE
    assert gen.style_for(DEBATE_SCRIPT) == STYLE_DEBATE


def test_auto_falls_back_to_the_configured_dialogue_format(tmp_path: Path) -> None:
    """剧本没写 format（手写剧本/旧 JSON）时才看 config。"""
    gen = generator(tmp_path, dialogue={"format": "monologue"})
    assert gen.style_for({"turns": []}) == STYLE_MONOLOGUE


def test_script_format_beats_config(tmp_path: Path) -> None:
    """补发旧辩论片时 config 可能已是独白：封面必须跟剧本走，不能跟 config。"""
    gen = generator(tmp_path, dialogue={"format": "monologue"})
    assert gen.style_for(DEBATE_SCRIPT) == STYLE_DEBATE


def test_explicit_cover_style_wins(tmp_path: Path) -> None:
    forced = generator(tmp_path, cover={"chrome_path": str(tmp_path / "chrome"), "style": "debate"})
    assert forced.style_for(MONOLOGUE_SCRIPT) == STYLE_DEBATE
    forced_mono = generator(tmp_path, cover={"chrome_path": str(tmp_path / "chrome"), "style": "monologue"})
    assert forced_mono.style_for(DEBATE_SCRIPT) == STYLE_MONOLOGUE


def test_unknown_style_value_behaves_like_auto(tmp_path: Path) -> None:
    gen = generator(tmp_path, cover={"chrome_path": str(tmp_path / "chrome"), "style": "retro"})
    assert gen.style_for(MONOLOGUE_SCRIPT) == STYLE_MONOLOGUE


def test_default_is_still_the_debate_cover(tmp_path: Path) -> None:
    """没形态信息时不许换脸：默认仍是旧版辩论封面。"""
    assert generator(tmp_path).style_for({}) == STYLE_DEBATE


# ---------------------------------------------------------------------------
# 独白版式：换了什么
# ---------------------------------------------------------------------------

def test_monologue_cover_switches_the_visual_language(tmp_path: Path) -> None:
    page = html(tmp_path, MONOLOGUE_SCRIPT)
    shown = body(page)
    assert 'data-style="monologue"' in page
    assert "一人讲透一家公司" in shown          # 品牌副标换成单人叙事
    assert "本期讲透" in shown                  # 钩子标签
    assert 'class="strip ' in shown             # 通栏细走势带取代图板
    # 辩论封面的记认必须全部消失：多空副标、图板、本期看点
    assert "多空对话" not in shown
    assert 'class="chart' not in shown
    assert "本期看点" not in shown


def test_debate_cover_is_untouched(tmp_path: Path) -> None:
    shown = body(html(tmp_path, DEBATE_SCRIPT))
    assert "多空对话" in shown
    assert 'class="chart ' in shown
    assert "本期看点" in shown
    assert "一人讲透一家公司" not in shown
    assert 'data-style="monologue"' not in shown


def test_monologue_cover_keeps_the_section_rail(tmp_path: Path) -> None:
    """几节、讲多久、一个人讲完——独白封面的身份行。"""
    page = html(tmp_path, MONOLOGUE_SCRIPT)
    assert "<b>6</b> 节" in page
    assert "约 3 分钟" in page          # 187.9 秒
    assert "一个人讲完" in page
    page_wide = html(tmp_path, MONOLOGUE_SCRIPT, size="landscape")
    for mark in ("01", "02", "03", "04", "05", "06"):
        assert f"<i>{mark}</i>" in page_wide
    assert "<i>07</i>" not in page_wide


def test_duration_text_prefers_the_scripted_estimate(tmp_path: Path) -> None:
    gen = generator(tmp_path)
    assert gen._duration_text({"estimated_seconds": 187.9}, []) == "约 3 分钟"
    # 没有估算就按字数折（4.8 字/秒）：576 字 ≈ 2 分钟
    assert gen._duration_text({}, [{"line": "字" * 288}, {"line": "字" * 288}]) == "约 2 分钟"
    # 空剧本不硬凑一个「0 分钟」
    assert gen._duration_text({}, []) == ""


def test_section_rail_is_capped(tmp_path: Path) -> None:
    many = {**MONOLOGUE_SCRIPT,
            "turns": [{"speaker": "narrator", "line": f"第 {i} 段。"} for i in range(1, 13)]}
    page = html(tmp_path, many, size="landscape")
    assert f"<i>{SECTION_MARK_LIMIT:02d}</i>" in page
    assert f"<i>{SECTION_MARK_LIMIT + 1:02d}</i>" not in page
    assert "<i>…</i>" in page


def test_single_chapter_monologue_hides_the_count(tmp_path: Path) -> None:
    """只有一节时不写「1 节」——那不叫分节讲述，写出来像个 bug。"""
    page = html(tmp_path, {**MONOLOGUE_SCRIPT, "turns": [{"speaker": "narrator", "line": "一句话讲完。"}]})
    assert "<b>1</b> 节" not in page
    assert "一个人讲完" in page


# ---------------------------------------------------------------------------
# 共用纪律：两种风格都必须守的内容红线
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", [MONOLOGUE_SCRIPT, DEBATE_SCRIPT], ids=["monologue", "debate"])
def test_cover_copy_stays_compliance_safe(tmp_path: Path, script: dict[str, Any]) -> None:
    shown = body(html(tmp_path, script))
    for banned in ("免责", "不构成", "投资建议", "看多", "看空", "AI生成", "买入", "卖出"):
        assert banned not in shown


@pytest.mark.parametrize("script", [MONOLOGUE_SCRIPT, DEBATE_SCRIPT], ids=["monologue", "debate"])
def test_both_styles_keep_price_colour_and_spotlight(tmp_path: Path, script: dict[str, Any]) -> None:
    page = html(tmp_path, script)
    shown = body(page)
    assert 'class="quote up"' in shown       # A股红涨
    assert "--up: #ff7188" in page and "--down: #5ee0a7" in page
    if script.get("spotlight"):
        assert "直销占比" in shown and "45.7%" in shown


def test_monologue_cover_degrades_without_history(tmp_path: Path) -> None:
    empty = {**STOCK, "technical": {}}
    gen = generator(tmp_path)
    width, height = COVER_PRESETS["portrait"]
    page = gen._html(empty, MONOLOGUE_SCRIPT, "600519", "portrait", width, height)
    assert 'class="spark-line"' not in page   # 没数据就不画曲线，只留网格
    assert "一人讲透一家公司" in page          # 版式仍在


def test_monologue_tone_tint_is_injected_inline(tmp_path: Path) -> None:
    """氛围色深浅仍由涨跌幅决定（内联变量），独白封面没有静默降级。"""
    page = html(tmp_path, MONOLOGUE_SCRIPT)
    assert "--tint-a: rgba(255,90,120," in page
    assert "--tint-chip: rgba(255,113,136," in page


# ---------------------------------------------------------------------------
# 出图链路：风格要真的落到 PNG 上
# ---------------------------------------------------------------------------

def test_generate_renders_the_monologue_cover(tmp_path: Path) -> None:
    """风格不能只停在字符串上：出图那条链路真的把独白模板交给了截图器。"""
    seen: dict[str, str] = {}

    def shooter(chrome: str, page: Path, png: Path, size: tuple[int, int], timeout: float) -> bool:
        seen["html"] = page.read_text(encoding="utf-8")
        png.write_bytes(png_bytes(*size) + b"\x00" * 64)
        return True

    chrome = tmp_path / "chrome"
    chrome.write_text("")
    gen = CoverGenerator({"cover": {"chrome_path": str(chrome)},
                          "dialogue": {"format": "monologue"}}, screenshotter=shooter)
    rendered = gen.generate(STOCK, MONOLOGUE_SCRIPT, "600519", ["portrait", "wide"], tmp_path, "600519")
    assert set(rendered) == {"portrait", "wide"}
    assert "一人讲透一家公司" in seen["html"]
    assert all(path.is_file() and path.suffix == ".png" for path in rendered.values())
