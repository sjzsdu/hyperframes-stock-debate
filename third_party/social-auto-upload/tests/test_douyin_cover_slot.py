# Language: 中文
"""封面槽位选择：抖音 2026-11 双封面弹窗的比例对齐（2026-11-07 事故驱动）。

现象：--republish 发出去的视频，抖音上封面只剩中间一条 —— 4:3 横槽被塞了 3:4 竖图，
平台按 4:3 裁掉上下，品牌/股票名/价格和底部标题全没了。

后续（2026-11-08）：只修「比例对齐」还不够 —— 只设横槽时，抖音用横图自动生成竖封面，
手机端显示的就是桌面版。两槽必须都显式设，且横在前竖在后。
"""
import unittest

from uploader.douyin_uploader.main import cover_slot_plan, pick_cover_for_step

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


class CoverSlotPlanTests(unittest.TestCase):
    """两槽都要设：只设横槽时手机端看到的是横图自动生成的竖封面（2026-11-08 用户报障）。"""

    def test_both_slots_are_planned_landscape_first(self):
        plan = cover_slot_plan(PORTRAIT, LANDSCAPE)
        self.assertEqual([(s, p) for s, p, _, _ in plan],
                         [(0, LANDSCAPE), (1, PORTRAIT)])
        self.assertEqual([step for _, _, _, step in plan], ["landscape", "vertical"])

    def test_landscape_must_come_before_vertical(self):
        """顺序错了会白干：后设横封面会让抖音重新生成竖封面、冲掉手工设的竖版。"""
        plan = cover_slot_plan(PORTRAIT, LANDSCAPE)
        self.assertLess(plan[0][0], plan[1][0])

    def test_portrait_only_still_targets_the_vertical_slot(self):
        plan = cover_slot_plan(PORTRAIT, None)
        self.assertEqual(plan, [(1, PORTRAIT, "竖版封面", "vertical")])

    def test_landscape_only_still_targets_the_horizontal_slot(self):
        plan = cover_slot_plan(None, LANDSCAPE)
        self.assertEqual(plan, [(0, LANDSCAPE, "横版封面", "landscape")])

    def test_no_covers_means_no_work(self):
        self.assertEqual(cover_slot_plan(None, None), [])


if __name__ == "__main__":
    unittest.main()
