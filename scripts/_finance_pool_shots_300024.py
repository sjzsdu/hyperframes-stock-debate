"""对「财务图表池」重建工程逐段截图（GSAP 中途态用 tl.seek，截图前中和 transform）。

段中点按 hyperframes_builder._segments 的兜底公式（len(line)*0.23，夹在
2.5–30s）复算——这份重建工程没有真实 TTS 时间轴，时长由台词长度合成。
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "_finance_pool_300024"
SHOTS = OUT / "shots"
SHOTS.mkdir(exist_ok=True)

LINES = [
    "今天聊的这家公司，五年报表摊开来看，有几个地方挺值得琢磨。",
    "这几年归母净利润从 2021 年亏五亿六千二百万，到 2022、2023 年勉强转正，2024、2025 又连亏两年，一年比一年深。",
    "销售毛利率从 2021 年的百分之七点五七，爬到 2024 年的百分之十四点六二，去年又掉回百分之十二点四八，中间那两年的改善没守住。",
    "净资产收益率连续三年是负的，股东回报这条路这几年没走通。",
    "经营活动现金流去年才转正一点六一亿，投资活动净额又翻成负的，筹资活动还要往里补三个亿。",
    "存货四十点五一亿，比期初多三成，短期借款半年多了五成，资产和负债两头都在变。",
]

CASES = [("h", "project", (1920, 1080)), ("v", "project_vertical", (1080, 1920))]


def midpoints() -> list[float]:
    cursor, marks = 0.0, []
    for line in LINES:
        duration = max(2.5, min(30.0, len(line) * 0.23))
        marks.append(cursor + duration / 2)
        cursor += duration + 0.25
    return marks


def main() -> None:
    marks = midpoints()
    print("段中点:", [round(m, 1) for m in marks])
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for label, project, (width, height) in CASES:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(f"file://{OUT / project / 'index.html'}")
            # 截图前中和 .clip 上的 transform：否则拍到的是 GSAP 中途态，看起来像版面缺陷。
            page.add_style_tag(content=".clip *{transform:none!important}")
            page.wait_for_timeout(400)
            for index, mark in enumerate(marks):
                page.evaluate(
                    """(t) => { const tl = window.__timelines['stock-debate'];
                         tl.pause(0); tl.seek(t); tl.pause(); }""",
                    mark,
                )
                page.wait_for_timeout(220)
                name = f"{label}_{index:02d}_{round(mark):03d}.png"
                page.screenshot(path=str(SHOTS / name))
                print("shot", name)
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
