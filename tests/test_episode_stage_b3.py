"""阶段 B3：叙事线（开头与结尾同源）+ 开收尾的禁用材质。

用户在 2026-10-19 真机那期之后提了两条反馈："开头似乎不够理想"、"结尾也要太
固定模式"。当时的样子是：

* R1 一开口就是「合同负债从八十亿骤降到三十一亿」——会计科目 + 两个数字；
* R5 每期都是「批价守住了，再看下一季预收款能不能回流」——唯一的收尾形状。

B3 的解法不是给句式换措辞，而是把整期换成一条**叙事线**：五条线各自的开口动作
和收尾落点都不同，同一条线同时供给 R1 和 R5，所以开头讲什么、结尾就回到什么。

这个文件锁住三件事：
1. 五条线都存在、互不重复，R1/R5 种子各自唯一；
2. 开口不碰会计科目/财务比率/股价/报告期，收尾不碰"等下季度/下一份报告"；
3. 计划里 R1 与 R5 确实来自同一条线，末章图板随线变且 fallback 不悬空。
"""
from __future__ import annotations

import tempfile
import unittest
from typing import Any

from stocktalk.modules.dialogue_generator import DialogueGenerator
from stocktalk.modules.episode_planner import (CLOSING_BANNED_PHRASES,
                                               OPENING_BANNED_TERMS,
                                               R5_FALLBACKS_BY_TEMPLATE,
                                               ROUND_TEMPLATES, STORY_LINES,
                                               EpisodePlanner, MaterialExtractor,
                                               _rotate_line)

DATA: dict[str, Any] = {
    "code": "600519",
    "quote": {"code": "600519", "name": "贵州茅台", "price": 1432.0, "change_pct": -0.8},
    "f10": {"profile": {
        "主营构成": {"报告期": "2026-06-30", "明细": [
            {"项目": "茅台酒(产品)", "收入": "600.00亿", "收入占比(%)": "84.00", "毛利率(%)": "94.10"},
            {"项目": "其他系列酒(产品)", "收入": "114.00亿", "收入占比(%)": "16.00", "毛利率(%)": "78.20"},
            {"项目": "直销(销售模式)", "收入": "320.00亿", "收入占比(%)": "44.80", "毛利率(%)": "95.30"},
            {"项目": "批发代理(销售模式)", "收入": "394.00亿", "收入占比(%)": "55.20", "毛利率(%)": "89.10"},
            {"项目": "国内(地区)", "收入": "690.00亿", "收入占比(%)": "96.60", "毛利率(%)": "91.80"},
            {"项目": "白酒(行业)", "收入": "704.00亿", "收入占比(%)": "98.50", "毛利率(%)": "91.70"},
        ]},
        "行业地位": {"研究行业": "白酒", "同行家数": 20, "市场表现": {"排名": 1}},
        "财务历史": {"年度": {"报告期": ["2023-12-31", "2024-12-31", "2025-12-31"],
                            "指标": {"归母净利润(亿元)": [747.34, 862.28, 903.10]}}},
    }},
    "news": {"items": [{"title": "公司发布 2026 年半年报", "type": "公告",
                        "publish_time": "2026-08-28"}]},
}


class StoryLineTests(unittest.TestCase):
    """五条叙事线本身：唯一性、开口/收尾的禁用材质。"""

    def test_five_lines_with_distinct_openings_and_closings(self) -> None:
        self.assertEqual(len(STORY_LINES), 5)
        ids = [line["id"] for line in STORY_LINES]
        self.assertEqual(len(set(ids)), 5, ids)
        openings = [line["r1"][2] for line in STORY_LINES]
        closings = [line["r5"][2] for line in STORY_LINES]
        self.assertEqual(len(set(openings)), 5, "五条线的开场必须互不相同")
        self.assertEqual(len(set(closings)), 5, "五条线的收尾必须互不相同")
        for line in STORY_LINES:
            self.assertIn(line["r1"][0], ("bull", "bear"))
            self.assertIn(line["r5"][0], ("bull", "bear"))
            self.assertTrue(line["r1"][1] and line["r5"][1])
            self.assertTrue(line["label"])

    def test_no_opening_uses_an_invisible_material(self) -> None:
        """开场不许用会计科目 / 财务比率 / 股价 / 报告期。

        「合同负债从八十亿骤降到三十一亿」那期的原话就是这一条要挡的东西：
        观众第一耳朵听到的是一个他看不见也摸不着的科目。
        """
        for line in STORY_LINES:
            opening = line["r1"][2]
            hit = [term for term in OPENING_BANNED_TERMS if term in opening]
            self.assertEqual(hit, [], (line["id"], opening, hit))

    def test_no_closing_waits_for_the_next_report(self) -> None:
        """收尾不许是「等下一季度/下一份报告来验证」那个固定悬念。"""
        for line in STORY_LINES:
            closing = line["r5"][2]
            hit = [phrase for phrase in CLOSING_BANNED_PHRASES if phrase in closing]
            self.assertEqual(hit, [], (line["id"], closing, hit))

    def test_every_line_lands_on_a_renderable_template(self) -> None:
        """末章图板随线变，但每条线都必须指向一张真渲染器里的图。

        悬空名和"缺数据"长得一模一样（都静默掉来源卡），所以这里直接比对
        渲染器登记处，见 ``PlanWiresTheLineTests.test_r5_fallbacks_are_all_registered``。
        """
        from stocktalk.modules.hyperframes_builder import HyperFramesBuilder
        builder = HyperFramesBuilder({"video": {"canvas": "horizontal"}})
        ctx: dict[str, Any] = {"bars": [], "quote": {}, "keywords": [], "argument": "business",
                               "f10": {}, "financials": {}, "news": {}}
        registered = set(builder._chart_renderers(ctx))
        for line in STORY_LINES:
            self.assertIn(line["r5_template"], registered, line["id"])


class PlanWiresTheLineTests(unittest.TestCase):
    """plan() 真的把同一条线接到了 R1 和 R5 上。"""

    def test_opening_and_closing_come_from_the_same_line(self) -> None:
        plan = EpisodePlanner().plan(DATA)
        line = plan["story_line"]
        self.assertIsNotNone(line)
        source = next(item for item in STORY_LINES if item["id"] == line["id"])
        chapters = {c["id"]: c for c in plan["rounds"]}
        self.assertEqual(chapters["r1_hook"]["sentence_modes"][0]["text"], source["r1"][2])
        self.assertEqual(chapters["r5_open_verdict"]["sentence_modes"][0]["text"], source["r5"][2])
        # 末章的图板也跟着线走
        self.assertEqual(chapters["r5_open_verdict"]["visual"]["template"],
                         source["r5_template"])

    def test_line_moves_with_the_date_but_repeats_within_a_day(self) -> None:
        first = EpisodePlanner().plan(DATA)["story_line"]["id"]
        again = EpisodePlanner().plan(DATA)["story_line"]["id"]
        self.assertEqual(first, again)
        days = {_rotate_line("600519", f"2026-10-{day:02d}") for day in range(1, 15)}
        self.assertEqual(days, set(range(len(STORY_LINES))), days)

    def test_closing_template_differs_across_lines(self) -> None:
        """结尾画面不许每期都是同一条事件时间线。"""
        templates = {line["r5_template"] for line in STORY_LINES}
        self.assertGreaterEqual(len(templates), 4, templates)
        for template in templates:
            self.assertIn(template, R5_FALLBACKS_BY_TEMPLATE, template)
            # 末级必须是中性的来源卡，且序列里不许出现计划外的悬空名
            self.assertEqual(R5_FALLBACKS_BY_TEMPLATE[template][-1], "source_card", template)

    def test_r5_fallbacks_are_all_registered(self) -> None:
        """末章后退序列里的每个名字都必须有渲染器（B1 踩过的悬空名坑）。"""
        from stocktalk.modules.hyperframes_builder import HyperFramesBuilder
        builder = HyperFramesBuilder({"video": {"canvas": "horizontal"}})
        ctx: dict[str, Any] = {"bars": [], "quote": {}, "keywords": [], "argument": "business",
                               "f10": {}, "financials": {}, "news": {}}
        registered = set(builder._chart_renderers(ctx))
        for template, fallbacks in R5_FALLBACKS_BY_TEMPLATE.items():
            self.assertIn(template, registered, template)
            for name in fallbacks:
                self.assertIn(name, registered, (template, name))


class PromptCarriesTheRulesTests(unittest.TestCase):
    """prompt 里必须真的写着开场/收尾两条硬约束，否则模型读不到。"""

    def _section(self) -> tuple[str, dict[str, Any]]:
        data = dict(DATA)
        data["episode_plan"] = EpisodePlanner().plan(DATA)
        generator = DialogueGenerator({"dialogue": {}}, llm=None)
        return generator._episode_section(data), data["episode_plan"]

    def test_opening_ban_reaches_the_prompt(self) -> None:
        section, _plan = self._section()
        self.assertIn("开场硬约束", section)
        for term in ("会计科目", "财务比率", "报告期"):
            self.assertIn(term, section)

    def test_closing_ban_reaches_the_prompt(self) -> None:
        section, _plan = self._section()
        self.assertIn("收尾硬约束", section)
        self.assertIn("等下一季度", section)
        self.assertIn("不预告下一个数据", section)

    def test_the_line_label_reaches_the_prompt(self) -> None:
        section, plan = self._section()
        self.assertIn("本期叙事线", section)
        self.assertIn(plan["story_line"]["label"], section)


class FakeRowTests(unittest.TestCase):
    """`(行业)` 行既不是产品也不是渠道：不许进产品线，也不许画成业务分支。"""
    def test_industry_rows_do_not_become_product_lines(self) -> None:
        materials = MaterialExtractor().extract(DATA)
        product = next(m for m in materials if m["id"] == "mat.product.0")
        names = [entity["name"] for entity in product["entities"]]
        self.assertEqual(names, ["茅台酒(产品)", "其他系列酒(产品)"])
        self.assertNotIn("白酒(行业)", names)

    def test_mindmap_and_composition_skip_channel_and_industry_rows(self) -> None:
        from stocktalk.modules.hyperframes_builder import HyperFramesBuilder
        builder = HyperFramesBuilder({"video": {"canvas": "horizontal"}})
        body = builder._business_mindmap_body(DATA["quote"], DATA["f10"])
        self.assertIsNotNone(body)
        for fake in ("直销", "批发代理", "国内", "白酒(行业)"):
            self.assertNotIn(fake, body, fake)
        bars = builder._composition_bars_body(DATA["f10"])
        self.assertIsNotNone(bars)
        for fake in ("直销", "批发代理", "国内", "白酒(行业)"):
            self.assertNotIn(fake, bars, fake)
        self.assertIn("茅台酒", body)
        self.assertIn("茅台酒", bars)


class ReviewShowsTheLineTests(unittest.TestCase):
    """审查页要看得见这一期走的是哪条线（否则"开头和结尾是不是一件事"没法核对）。"""

    def test_review_page_names_the_story_line(self) -> None:
        from stocktalk.modules.review import render_review_html
        plan = EpisodePlanner().plan(DATA)
        html = render_review_html(
            tag="x", stock_name="贵州茅台", stock_code="600519",
            created_at="2026-10-19 12:00", turns=10, duration=180.0,
            videos=[], covers=[], canvas_rows=[], title="t", description="d", tags="",
            issues=[], disclaimers="", episode_plan=plan)
        self.assertIn("叙事线", html)
        self.assertIn(plan["story_line"]["label"], html)
        self.assertIn("开头与结尾同源", html)


if __name__ == "__main__":
    unittest.main()
