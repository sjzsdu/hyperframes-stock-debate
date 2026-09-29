"""数据故事发现器：显著性挑选必须数据驱动，缺数据就空表，绝不编故事。"""
import unittest

from stocktalk.modules.story_spotter import spot_stories


def _annual(periods: list[str], metrics: dict[str, list]) -> dict:
    return {"年度": {"报告期": periods, "指标": metrics}}


def _stock_data(history: dict, changes: dict | None = None,
                composition_history: list | None = None) -> dict:
    profile: dict = {"财务历史": history}
    if changes is not None:
        profile["财务历史"]["科目变动"] = changes
    if composition_history is not None:
        profile["主营构成历史"] = composition_history
    return {"f10": {"profile": profile}}


YEARS = ["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31"]


class StorySpotterTests(unittest.TestCase):
    def test_flip_years_become_the_headline(self):
        """首尾都是负数也可能中间翻过盈——300024 的真实形态。"""
        history = _annual(YEARS, {"归母净利润(亿元)": [-5.62, 0.45, 0.49, -1.94, -3.98]})
        stories = spot_stories(_stock_data(history))
        headline = next(s["headline"] for s in stories if s["kind"] == "profit_trend")
        self.assertIn("2 年盈利、3 年亏损", headline)

    def test_margin_swing_below_two_points_is_not_a_story(self):
        history = _annual(YEARS, {"销售毛利率(%)": [11.0, 11.4, 11.8, 11.6, 12.0]})
        stories = spot_stories(_stock_data(history))
        self.assertFalse(any(s["kind"] == "margin_trend" for s in stories))

    def test_cashflow_deficit_with_financing_backfill(self):
        history = _annual(YEARS, {
            "经营活动现金流量净额(亿元)": [1.0, -4.03, -2.23, -0.42, -3.5],
            "筹资活动现金流量净额(亿元)": [1.1, -2.54, -0.55, 3.73, 4.2]})
        stories = spot_stories(_stock_data(history))
        cash = next(s for s in stories if s["kind"] == "cashflow")
        self.assertIn("净流出 3.50 亿", cash["headline"])
        self.assertIn("筹资净流入 4.20 亿", cash["headline"])

    def test_balance_changes_rank_by_magnitude_with_threshold(self):
        changes = {"报告期": "2026-06-30", "明细": [
            {"科目": "存货", "本期(亿元)": 40.51, "上期(亿元)": 30.89, "变动率(%)": 31.13},
            {"科目": "短期借款", "本期(亿元)": 6.06, "上期(亿元)": 4.02, "变动率(%)": 50.58},
            {"科目": "管理费用", "本期(亿元)": 1.58, "上期(亿元)": 1.54, "变动率(%)": 2.38},
            {"科目": "商誉", "本期(亿元)": 9.0, "上期(亿元)": 1.0, "变动率(%)": 800.0},
        ]}
        stories = spot_stories(_stock_data(_annual(YEARS, {}), changes=changes))
        kinds = [s["kind"] for s in stories if s["kind"] == "balance"]
        self.assertEqual(len(kinds), 2)                    # 白名单外（商誉）与小异动（管理费用）不进
        self.assertIn("短期借款", stories[0]["headline"])  # |变动率| 最大者排最前

    def test_structure_shift_needs_five_points(self):
        composition_history = [
            {"报告期": "2025-12-31", "明细": [{"项目": "工业机器人(产品)", "收入占比(%)": "36.2", "毛利率(%)": "15.6"}]},
            {"报告期": "2024-12-31", "明细": [{"项目": "工业机器人(产品)", "收入占比(%)": "29.1", "毛利率(%)": "17.4"}]},
        ]
        stories = spot_stories(_stock_data(_annual(YEARS, {}),
                                           composition_history=composition_history))
        self.assertIn("工业机器人", stories[0]["headline"])

    def test_no_data_yields_no_stories(self):
        self.assertEqual(spot_stories({"f10": {"profile": {}}}), [])
        self.assertEqual(spot_stories({}), [])


if __name__ == "__main__":
    unittest.main()
