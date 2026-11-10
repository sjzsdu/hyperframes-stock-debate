# Language: 中文
"""百家号标题填写：编辑区被引导浮层遮挡时的降级路径（2026-11-10 事故驱动）。

现象：发布在 `_fill_title` 的 `title_field.click()` 卡死，报
`<div role="tooltip" class="cheetah-popover-inner"> … intercepts pointer events`，
两次尝试共计 ~4 分钟后判失败；日志里连「标题已填写」都没有。

根因：百家号 2026-11 上线的「智能文案」气泡 + AI 助手引导浮层压在标题编辑区上，
Escape 关不掉。修复要点：先摘浮层 → 点不中就 JS 聚焦 → 用 fill() 写入
（fill 不依赖鼠标命中，且会整体替换编辑器自带的文件名）。
"""
import unittest
from types import SimpleNamespace

from playwright.async_api import TimeoutError as PWTimeoutError

from uploader.baijiahao_uploader.main import BaiJiaHaoVideo

TITLE = "药明康德（603259）：研发外包的溢价与账本博弈"


class _FakeLocator:
    def __init__(self, click_blocked: bool):
        self.click_blocked = click_blocked
        self.clicked = False
        self.focused = False
        self.value = "603259_20261110_162203"  # 编辑器默认带上文件名

    async def wait_for(self, **kwargs):
        return None

    async def click(self, **kwargs):
        if self.click_blocked:
            raise PWTimeoutError("cheetah-popover-inner intercepts pointer events")
        self.clicked = True

    async def evaluate(self, script):
        self.focused = True

    async def fill(self, value):
        self.value = value  # fill 是整体替换

    async def inner_text(self):
        return self.value


class _FakePage:
    def __init__(self, locator, popovers=3):
        self._locator = locator
        self._popovers = popovers
        self.removed_script = None

    def locator(self, selector):
        return SimpleNamespace(first=self._locator)

    async def evaluate(self, script):
        self.removed_script = script
        return self._popovers

    async def wait_for_timeout(self, ms):
        return None

    async def keyboard(self):
        raise AssertionError("不应再依赖 Ctrl+A/Backspace 清空内容")


def _video():
    return BaiJiaHaoVideo(TITLE, "/tmp/x.mp4", [], "/tmp/cookie.json",
                          thumbnail_path="/tmp/cover.png", headless=True)


class BaiJiaHaoFillTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_fills_title_even_when_click_is_blocked(self):
        loc = _FakeLocator(click_blocked=True)
        page = _FakePage(loc)

        await _video()._fill_title(page)

        self.assertTrue(loc.focused, "点不中时应退回 JS 聚焦")
        self.assertEqual(loc.value, TITLE, "fill 应整体替换编辑器里的文件名")

    async def test_clears_popovers_before_clicking(self):
        loc = _FakeLocator(click_blocked=False)
        page = _FakePage(loc, popovers=3)

        await _video()._fill_title(page)

        self.assertTrue(loc.clicked)
        self.assertIn("cheetah-popover", page.removed_script)
        self.assertIn("creating-brain-guider", page.removed_script)

    async def test_short_title_is_padded_to_the_platform_minimum(self):
        video = BaiJiaHaoVideo("短标题", "/tmp/x.mp4", [], "/tmp/cookie.json",
                               thumbnail_path="/tmp/cover.png", headless=True)
        loc = _FakeLocator(click_blocked=False)

        await video._fill_title(_FakePage(loc))

        self.assertGreater(len(loc.value), 8)
        self.assertTrue(loc.value.startswith("短标题"))

    async def test_title_is_clipped_to_30_chars(self):
        long_title = "药明康德" * 12
        video = BaiJiaHaoVideo(long_title, "/tmp/x.mp4", [], "/tmp/cookie.json",
                               thumbnail_path="/tmp/cover.png", headless=True)
        loc = _FakeLocator(click_blocked=False)

        await video._fill_title(_FakePage(loc))

        self.assertEqual(len(loc.value), video.max_title_length)


if __name__ == "__main__":
    unittest.main()
