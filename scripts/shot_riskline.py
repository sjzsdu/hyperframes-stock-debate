"""Seek 时间轴截片尾两帧：CTA 图板（结尾前）与 outro 卡（最后一帧）。"""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path("/Users/juzhongsun/Codes/pythons/hyperframes-stock-debate/output/_preview_riskline/hyperframes")
OUT = ROOT.parent

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        await page.goto(f"file://{ROOT}/index.html")
        await page.wait_for_timeout(800)
        # 纪律：注入 transform 归零，避免 GSAP 中途态假缺陷
        await page.add_style_tag(content=".clip * { transform: none !important; }")
        duration = await page.evaluate("parseFloat(document.getElementById('root').dataset.duration)")
        print("duration:", duration)
        tl = "window.__timelines['stock-debate']"
        # 帧 1：CTA 图板（outro 卡进场前 1 秒）
        t1 = duration - 1.0 - 2.4 - 0.5
        await page.evaluate(f"(() => {{ const t = {tl}; t.pause(0); t.seek({t1}); }})()")
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(OUT / "frame_cta.png"))
        # 帧 2：最后一帧（outro 卡）
        await page.evaluate(f"(() => {{ const t = {tl}; t.seek({duration - 0.2}); }})()")
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(OUT / "frame_outro.png"))
        await browser.close()

asyncio.run(main())
