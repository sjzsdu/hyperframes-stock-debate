"""对重建的 300024 图板逐段截图（GSAP 中途态用 tl.seek，截图前注入 transform 中和）。"""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "_rebind_300024"
SHOTS = OUT / "shots"
SHOTS.mkdir(exist_ok=True)

# (标签, 工程目录, 画布, 秒点)——秒点取每段中段
CASES = [
    ("h_t3_margin", "project", (1920, 1080), [46, 70, 82, 100, 110]),
    ("v_t3_margin", "project_vertical", (1080, 1920), [46, 70, 110]),
]


def main() -> None:
    meta = json.loads((ROOT / "output" / "300024_20260928_213016.json").read_text(encoding="utf-8"))
    segs = meta["audio"]["segments"]
    print("turn midpoints:", [round(s["start_time"] + s["duration"] / 2) for s in segs])
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for label, proj, (w, h), times in CASES:
            page = browser.new_page(viewport={"width": w, "height": h})
            page.goto(f"file://{OUT / proj / 'index.html'}")
            page.add_style_tag(content=".clip *{transform:none!important}")
            page.wait_for_timeout(400)
            for t in times:
                page.evaluate(
                    """(t) => { const tl = window.__timelines['stock-debate'];
                         tl.pause(0); tl.seek(t); tl.pause(); }""",
                    t,
                )
                page.wait_for_timeout(250)
                page.screenshot(path=str(SHOTS / f"{label}_{t:03d}.png"))
                print("shot", f"{label}_{t:03d}.png")
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
