# Language: 中文
"""封面槽位选择：抖音 2026-11 双封面弹窗的比例对齐（2026-11-07 事故驱动）。

现象：--republish 发出去的视频，抖音上封面只剩中间一条 —— 4:3 横槽被塞了 3:4 竖图，
平台按 4:3 裁掉上下，品牌/股票名/价格和底部标题全没了。
"""
import unittest

from uploader.douyin_uploader.main import pick_cover_for_step

PORTRAIT = "/tmp/cover-portrait.png"   # 3:4
LANDSCAPE = "/tmp/cover-landscape.png"  # 4:3


class CoverSlotTests(unittest.TestCase):
    def test_landscape_step_uses_the_4x3_cover(self):
        """弹窗默认停在「横封面（4:3）」——这时必须喂 landscape。"""
        path, label = pick_cover_for_step("landscape", PORTRAIT, LANDSCAPE)
        self.assertEqual(path, LANDSCAPE)
        self.assertIn("横版", label)

    def test_vertical_step_uses_the_3x4_cover(self):
        path, label = pick_cover_for_step("vertical", PORTRAIT, LANDSCAPE)
        self.assertEqual(path, PORTRAIT)
        self.assertIn("竖版", label)

    def test_legacy_single_slot_still_uses_portrait(self):
        """旧版弹窗只有一个 3:4 竖槽，历史行为要保住。"""
        path, _ = pick_cover_for_step("legacy", PORTRAIT, LANDSCAPE)
        self.assertEqual(path, PORTRAIT)

    def test_missing_matching_cover_is_skipped_not_substituted(self):
        """横槽缺 4:3 图时宁可跳过，也不要把竖图塞进去被裁。"""
        path, _ = pick_cover_for_step("landscape", PORTRAIT, None)
        self.assertIsNone(path)


if __name__ == "__main__":
    unittest.main()
