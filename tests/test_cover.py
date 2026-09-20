"""封面图生成测试：Chrome 探测、降级策略、合规文案、封面参数下发。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stocktalk.modules.cover import COVER_PRESETS, CoverGenerator, _png_size

PNG_HEADER = b"\x89PNG\r\n\x1a\n"

def png_bytes(width: int, height: int) -> bytes:
    """A minimal but structurally correct PNG header (signature + IHDR)."""
    return PNG_HEADER + (13).to_bytes(4, "big") + b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")


SCRIPT: dict[str, Any] = {
    "title": "拓普集团（601689）：Tier 0.5 的生意到底靠什么赚钱",
    "turns": [
        {"speaker": "bull", "line": "单车价值量在提升", "visual": ["Tier 0.5", "机器人"]},
        {"speaker": "bear", "line": "估值不便宜，要留意风险", "visual": ["估值"]},
    ],
}

STOCK: dict[str, Any] = {
    "quote": {"code": "601689", "name": "拓普集团", "price": 68.42, "change_pct": 3.17,
              "amount": 2.13e9, "volume": 314000.0},
    "technical": {"history": [{"timestamp": f"2026-08-{d:02d}", "price": {"current": 55 + d * 0.4}}
                              for d in range(1, 25)],
                  "summary": {"signal": "多头排列"}},
}


def fake_shooter(outputs: dict[str, bytes | Exception]):
    """A screenshotter that "renders" a real PNG header for the requested size."""
    def shooter(chrome: str, html_path: Path, png: Path, size: tuple[int, int], timeout: float) -> bool:
        if isinstance(outputs.get(html_path.name), Exception):
            raise outputs[html_path.name]
        width, height = size
        png.write_bytes(png_bytes(width, height) + b"\x00" * 64)
        return True
    return shooter


def make_generator(tmp_path: Path, chrome: str | None = "chrome", **kwargs: Any) -> CoverGenerator:
    if chrome is not None and not Path(chrome).is_file():
        binary = tmp_path / chrome
        binary.write_text("")
        chrome = str(binary)
    return CoverGenerator({"cover": {"chrome_path": chrome or ""}}, **kwargs)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def test_chrome_resolves_configured_path(tmp_path: Path) -> None:
    binary = tmp_path / "chrome"
    binary.write_text("")
    gen = CoverGenerator({"cover": {"chrome_path": str(binary)}})
    assert gen.chrome_path() == str(binary)
    assert gen.available


def test_chrome_missing_means_covers_are_skipped(tmp_path: Path) -> None:
    gen = CoverGenerator({"cover": {"chrome_path": "/definitely/not/chrome"}})
    assert gen.chrome_path() is None
    assert not gen.available
    assert gen.generate(STOCK, SCRIPT, "601689", ["portrait"], tmp_path, "t") == {}


def test_disabled_config_generates_nothing(tmp_path: Path) -> None:
    gen = CoverGenerator({"cover": {"enabled": False}})
    assert gen.generate(STOCK, SCRIPT, "601689", ["portrait"], tmp_path, "t") == {}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_generate_renders_every_requested_size(tmp_path: Path) -> None:
    gen = make_generator(tmp_path, screenshotter=fake_shooter({}))
    out = gen.generate(STOCK, SCRIPT, "601689", ["portrait", "wide"], tmp_path, "601689_1")
    assert set(out) == {"portrait", "wide"}
    for size, path in out.items():
        assert path.name == f"601689_1.cover-{size}.png"
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_one_failed_size_does_not_block_the_others(tmp_path: Path) -> None:
    def shooter(chrome, html_path, png, size, timeout):
        if size == (1080, 1440):  # portrait fails, wide must still render
            return False
        return fake_shooter({})(chrome, html_path, png, size, timeout)

    gen = make_generator(tmp_path, screenshotter=shooter)
    out = gen.generate(STOCK, SCRIPT, "601689", ["portrait", "wide"], tmp_path, "601689_1")
    assert set(out) == {"wide"}


def test_wrong_dimensions_are_rejected(tmp_path: Path) -> None:
    def shooter(chrome, html_path, png, size, timeout):
        return fake_shooter({})(chrome, html_path, png, (800, 600), timeout)

    gen = make_generator(tmp_path, screenshotter=shooter)
    assert gen.generate(STOCK, SCRIPT, "601689", ["portrait"], tmp_path, "601689_1") == {}


def test_unknown_size_is_ignored(tmp_path: Path) -> None:
    gen = make_generator(tmp_path, screenshotter=fake_shooter({}))
    assert gen.generate(STOCK, SCRIPT, "601689", ["square"], tmp_path, "t") == {}


def test_presets_cover_the_platform_ratios() -> None:
    width, height = COVER_PRESETS["portrait"]
    assert abs(width / height - 3 / 4) < 1e-6
    width, height = COVER_PRESETS["wide"]
    assert abs(width / height - 16 / 9) < 1e-6


# ---------------------------------------------------------------------------
# Copy / compliance
# ---------------------------------------------------------------------------

def test_hook_drops_the_redundant_name_prefix() -> None:
    html = make_generator(Path("/tmp"))._html(STOCK, SCRIPT, "601689", "portrait", 1080, 1440)
    assert "Tier 0.5 的生意到底靠什么赚钱" in html
    assert "拓普集团（601689）：Tier" not in html


def test_hook_neutralises_advice_language() -> None:
    script = {"title": "拓普集团（601689）：建议买入的三个理由", "turns": []}
    html = make_generator(Path("/tmp"))._html(STOCK, script, "601689", "portrait", 1080, 1440)
    assert "建议买入" not in html


def test_cover_never_shows_compliance_or_role_labels() -> None:
    html = make_generator(Path("/tmp"))._html(STOCK, SCRIPT, "601689", "portrait", 1080, 1440)
    for banned in ("免责", "不构成", "投资建议", "看多", "看空", "AI生成"):
        assert banned not in html


def test_cover_never_shows_trade_signals() -> None:
    """技术信号的「卖出/买入」是买卖信号（合规红线），不许上封面。"""
    stock = {**STOCK, "technical": {**STOCK["technical"], "summary": {"signal": "卖出"}}}
    html = make_generator(Path("/tmp"))._html(stock, SCRIPT, "601689", "portrait", 1080, 1440)
    assert "卖出" not in html


def test_quote_tone_follows_a_share_colour_convention() -> None:
    gen = make_generator(Path("/tmp"))
    up = gen._html({**STOCK, "quote": {**STOCK["quote"], "change_pct": 1.2}}, SCRIPT, "601689",
                   "portrait", 1080, 1440)
    down = gen._html({**STOCK, "quote": {**STOCK["quote"], "change_pct": -1.2}}, SCRIPT, "601689",
                     "portrait", 1080, 1440)
    assert 'class="quote up"' in up
    assert 'class="quote down"' in down


def test_sparkline_draws_only_real_closes() -> None:
    html = make_generator(Path("/tmp"))._html(STOCK, SCRIPT, "601689", "portrait", 1080, 1440)
    assert "spark-line" in html
    assert "candle" not in html  # 只画真实收盘曲线，不合成 K 线


def test_sparkline_degrades_to_grid_without_history() -> None:
    empty = {"quote": {"code": "601689", "name": "拓普集团"}, "technical": {}}
    html = make_generator(Path("/tmp"))._html(empty, SCRIPT, "601689", "portrait", 1080, 1440)
    assert 'class="spark-line"' not in html  # 只剩网格，没有曲线


def test_png_size_reads_ihdr(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(png_bytes(1080, 1440))
    assert _png_size(path) == (1080, 1440)
    path.write_bytes(b"not a png")
    assert _png_size(path) is None
