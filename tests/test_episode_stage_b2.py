"""阶段 B2：素材池收成（渠道/地区）+ 三张新 SVG 的契约测试。

三条主线——
1. 素材池：渠道/地区行单独成一类、和产品线同一套 source_refs 纪律，子公司/
   具名品牌/具名竞品继续 missing；
2. 三张新图（``product_portfolio`` / ``channel_flow`` / ``ledger_pair``）横竖
   两版都出得来，且只认 ``data_binding`` 绑好的素材；
3. **缺数据时降级到哪**：这是 B2 最容易被做成半成品的地方——新图画不出时
   必须退到"同一件事的另一种画法"，末级 source_card，绝不退回 K 线。
"""
from __future__ import annotations

import tempfile
import unittest
from typing import Any

from stocktalk.modules.episode_planner import (ROUND_FALLBACKS, ROUND_TEMPLATES,
                                               ROUND_VISUAL_BINDINGS,
                                               EpisodePlanner, MaterialExtractor)
from stocktalk.modules.hyperframes_builder import HyperFramesBuilder

# 产品行 + 渠道/地区行（F10 原样混在同一张 明细 表里）。
FULL: dict[str, Any] = {
    "code": "600519",
    "quote": {"code": "600519", "name": "贵州茅台", "price": 1432.0, "change_pct": -0.8},
    "f10": {"profile": {
        "主营构成": {"报告期": "2026-06-30", "明细": [
            {"项目": "茅台酒(产品)", "收入": "600.00亿", "收入占比(%)": "84.00", "毛利率(%)": "94.10"},
            {"项目": "其他系列酒(产品)", "收入": "114.00亿", "收入占比(%)": "16.00", "毛利率(%)": "78.20"},
            {"项目": "直销(销售模式)", "收入": "320.00亿", "收入占比(%)": "44.80", "毛利率(%)": "95.30"},
            {"项目": "批发代理(销售模式)", "收入": "394.00亿", "收入占比(%)": "55.20", "毛利率(%)": "89.10"},
            {"项目": "国内(地区)", "收入": "690.00亿", "收入占比(%)": "96.60", "毛利率(%)": "91.80"},
            {"项目": "国外(地区)", "收入": "24.00亿", "收入占比(%)": "3.40", "毛利率(%)": "91.20"},
        ]},
        "行业地位": {"研究行业": "白酒", "同行家数": 20, "市场表现": {"排名": 1}},
        "财务历史": {"年度": {"报告期": ["2023-12-31", "2024-12-31", "2025-12-31"],
                            "指标": {"归母净利润(亿元)": [747.34, 862.28, 903.10],
                                    "销售毛利率(%)": [91.96, 91.93, 91.83]}}},
    }},
    "news": {"items": [{"title": "公司发布 2026 年半年报", "type": "公告",
                        "publish_time": "2026-08-28"}]},
}


def _without_channel_rows() -> dict[str, Any]:
    """同一份数据，去掉渠道/地区行——大多数票长这样就该退到现有图。"""
    import copy
    data = copy.deepcopy(FULL)
    rows = data["f10"]["profile"]["主营构成"]["明细"]
    data["f10"]["profile"]["主营构成"]["明细"] = [
        row for row in rows if not str(row["项目"]).endswith(("(销售模式)", "(地区)"))]
    return data


class MaterialPoolTests(unittest.TestCase):
    """素材池：4 类可用的（产品线 / 渠道地区 / 行业位置 / 事件 / 财务证据）+ 3 类缺来源。"""

    def test_channel_rows_become_their_own_source_backed_material(self) -> None:
        materials = MaterialExtractor().extract(FULL)
        channel = next(m for m in materials if m["id"] == "mat.channel.0")
        self.assertEqual(channel["category"], "channel_region")
        self.assertEqual(channel["availability"], "ready")
        self.assertTrue(channel["source_refs"], channel["id"])
        self.assertTrue(channel["metrics"], channel["id"])
        for metric in channel["metrics"]:
            # 和产品线同一套纪律：每条指标自带来源，来源是明细表的那一行那个字段
            self.assertTrue(metric["source_refs"], metric)
            self.assertTrue(str(metric["source_refs"][0]["path"]).startswith(
                "f10.profile.主营构成.明细["), metric)
        names = {str(e["name"]) for e in channel["entities"]}
        self.assertTrue({"直销(销售模式)", "国内(地区)"} & names, names)
        self.assertEqual({str(e["role"]) for e in channel["entities"]}, {"channel", "region"})

    def test_channel_rows_are_not_counted_as_product_lines(self) -> None:
        """渠道行混进产品卡组，卡片上就会出现「直销」这种假产品线。"""
        materials = MaterialExtractor().extract(FULL)
        product = next(m for m in materials if m["id"] == "mat.product.0")
        channel = next(m for m in materials if m["id"] == "mat.channel.0")
        product_names = {str(e["name"]) for e in product["entities"]}
        channel_names = {str(e["name"]) for e in channel["entities"]}
        self.assertEqual(product_names, {"茅台酒(产品)", "其他系列酒(产品)"})
        self.assertTrue(channel_names and not (product_names & channel_names))
        # 指标 id 前缀各自分开，图上改名靠它把名称和数字对上
        self.assertTrue(all(str(m["id"]).startswith("m.product.")
                            for m in product["metrics"]))
        self.assertTrue(all(str(m["id"]).startswith("m.channel.")
                            for m in channel["metrics"]))

    def test_relations_without_a_structured_source_stay_rejected(self) -> None:
        plan = EpisodePlanner().plan(FULL)
        rejected = {item["id"] for item in plan["rejected_materials"]}
        self.assertEqual(rejected, {"mat.subsidiary.missing", "mat.brand_channel.missing",
                                    "mat.competition_named.missing"})
        ready = {str(m["id"]) for m in plan["materials"] if m["availability"] == "ready"}
        self.assertFalse(ready & rejected)
        self.assertTrue(all(w.endswith(":missing_source")
                            for w in plan["warnings"] if w.startswith(("subsidiary", "brand", "competition"))))

    def test_channel_material_is_available_but_claims_no_spoken_metric(self) -> None:
        """渠道素材进池（画面要用），但不抢口播额度——口播取材仍是 ROUND_MATERIALS。"""
        plan = EpisodePlanner().plan(FULL)
        self.assertIn("mat.channel.0", plan["selected_material_ids"])
        spoken = [mid for chapter in plan["rounds"]
                  for mid in [*chapter["anchor_metric_ids"], *chapter["analogy_metric_ids"]]]
        self.assertFalse([mid for mid in spoken if mid.startswith("m.channel.")], spoken)


class VisualBindingTests(unittest.TestCase):
    """图板契约：模板换到三张新图上，且没有悬空名字。"""

    def test_the_three_new_charts_are_what_the_chapters_ask_for(self) -> None:
        self.assertEqual(ROUND_TEMPLATES, {"r1_hook": "product_portfolio",
                                           "r2_mechanism": "channel_flow",
                                           "r3_moat_pressure": "ranking",
                                           "r4_ledger_test": "ledger_pair",
                                           "r5_open_verdict": "timeline"})

    def test_no_fallback_name_is_left_dangling(self) -> None:
        """B1 踩过的坑：阶段 B 的名字在阶段 A 时全掉 source_card——因为那些名字
        当时根本没注册，和"有这张图但缺数据"长得一模一样。"""
        builder = HyperFramesBuilder({"video": {"canvas": "horizontal"}})
        registered = set(builder._chart_renderers({}))
        planned = set(ROUND_TEMPLATES.values())
        for names in ROUND_FALLBACKS.values():
            planned.update(names)
        self.assertTrue(planned <= registered, planned - registered)
        for names in ROUND_FALLBACKS.values():
            self.assertEqual(names[-1], "source_card", names)   # 末级
            self.assertNotIn("kline", names, names)             # 绝不退回 K 线

    def test_data_binding_follows_the_mat_metric_contract(self) -> None:
        plan = EpisodePlanner().plan(FULL)
        by_id = {str(m["id"]): m for m in plan["materials"]}
        bound = {chapter["id"]: chapter["visual"]["data_binding"] for chapter in plan["rounds"]}
        # R2「生意怎么转」的画面要看厂商→渠道→客户，所以它是唯一绑两类素材的章
        self.assertEqual(bound["r2_mechanism"]["material_ids"],
                         ["mat.product.0", "mat.channel.0"])
        self.assertEqual(bound["r1_hook"]["material_ids"], ["mat.product.0"])
        self.assertEqual(bound["r4_ledger_test"]["material_ids"], ["mat.financial.0"])
        for chapter_id, binding in bound.items():
            self.assertIn(chapter_id, ROUND_VISUAL_BINDINGS)
            metric_ids = set(binding["metric_ids"])
            expected = {str(m["id"]) for material_id in binding["material_ids"]
                        for m in (by_id[material_id].get("metrics") or [])}
            self.assertEqual(metric_ids, expected, chapter_id)

    def test_the_last_chapter_ships_what_to_verify(self) -> None:
        """「待验证什么」只挂在事件线那条收尾上。

        B3 之后末章收尾有五种（回扣 / 观察动作 / 代价判断 / 处境描述 / 挂到
        一个落点），只有最后一种会输出"待验证"标签——每期都以一个待验证的数
        收尾，本身就是被嫌弃的那个固定模式。所以这里验的是**契约**：verify
        非空 ⟺ 末章图板是 timeline。
        """
        plan = EpisodePlanner().plan(FULL)
        verify = {chapter["id"]: chapter["visual"].get("verify") for chapter in plan["rounds"]}
        last = next(c for c in plan["rounds"] if c["id"] == "r5_open_verdict")
        if last["visual"]["template"] == "timeline":
            self.assertEqual(verify["r5_open_verdict"], "下一份报告里的归母净利润")
        else:
            self.assertEqual(verify["r5_open_verdict"], "", last["visual"]["template"])
        # 只有末章带这一条：别的章没有"留给下一份报告"这层意思
        self.assertEqual([rid for rid, text in verify.items() if text and rid != "r5_open_verdict"], [])
        # 素材全无时不许编一条待验证项
        empty = EpisodePlanner().plan({"code": "000000", "quote": {"code": "000000"}})
        last_empty = next(c for c in empty["rounds"] if c["id"] == "r5_open_verdict")
        if last_empty["visual"]["template"] == "timeline":
            self.assertEqual(last_empty["visual"]["verify"], "下一份报告")
        else:
            self.assertEqual(last_empty["visual"]["verify"], "")


class NewChartTests(unittest.TestCase):
    """三张新图横竖两版都出；缺数据时降级到哪。"""

    def _builder(self, canvas: str) -> HyperFramesBuilder:
        return HyperFramesBuilder({"video": {"canvas": canvas, "output_dir": tempfile.mkdtemp()}})

    def _ctx(self, plan: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        return {"bars": [{"close": 10.0 + i} for i in range(40)],
                "quote": data.get("quote") or {}, "keywords": [], "argument": "business",
                "f10": data.get("f10") or {}, "financials": data.get("financials") or {},
                "news": data.get("news") or {}, "episode_plan": plan}

    def test_three_new_charts_render_in_both_layouts(self) -> None:
        """横竖两版是两条画布（图表区 1480px / 1082px），卡片宽度得跟着走。"""
        titles = {"vis.r1_hook": "卖的是什么 · 产品卡组",
                  "vis.r2_mechanism": "货怎么到客户手上 · 渠道链路",
                  "vis.r4_ledger_test": "账有没有配合 · 两数对照"}
        plan = EpisodePlanner().plan(FULL)
        for canvas in ("horizontal", "vertical"):
            builder = self._builder(canvas)
            for visual_id, title in titles.items():
                body, name = builder._turn_chart("business", self._ctx(plan, FULL), "", visual_id)
                self.assertIn(title, body, (canvas, visual_id))
                self.assertNotEqual(name, "source_card", (canvas, visual_id))
                # 竖版图表区更窄：卡片不能越过右边界
                if canvas == "vertical":
                    self.assertLessEqual(max(float(x) for x in _xs(body)), 1126.0,
                                         (canvas, visual_id))

    def test_new_charts_render_from_the_plan_even_when_ctx_is_thin(self) -> None:
        """图只认计划的 data_binding：计划带着数，图就该出。

        ctx.f10 空而计划有数时退化成 source_card，等于把"画面服从统筹"又丢回去
        看渲染那一刻手边有什么。
        """
        builder = self._builder("horizontal")
        plan = EpisodePlanner().plan(FULL)
        ctx = {"bars": [], "quote": {}, "keywords": [], "argument": "business",
               "f10": {}, "financials": {}, "news": {}, "episode_plan": plan}
        for visual_id, name in (("vis.r1_hook", "product_portfolio"),
                                ("vis.r2_mechanism", "channel_flow"),
                                ("vis.r4_ledger_test", "ledger_pair")):
            _, shown = builder._turn_chart("business", ctx, "", visual_id)
            self.assertEqual(shown, name, visual_id)

    def test_channel_flow_degrades_to_the_business_chart(self) -> None:
        """大多数票没有渠道/地区行：channel_flow 画不出 → 退到业务拆解。"""
        builder = self._builder("horizontal")
        data = _without_channel_rows()
        plan = EpisodePlanner().plan(data)
        self.assertNotIn("mat.channel.0", plan["selected_material_ids"])
        _, name = builder._turn_chart("business", self._ctx(plan, data), "", "vis.r2_mechanism")
        self.assertEqual(name, "mindmap")

    def test_product_portfolio_degrades_when_there_is_only_one_product(self) -> None:
        """只有一块业务时不叫"卡组"：退到业务拆解（它还能用题材标签出分支）。"""
        one_product = {"code": "000003", "quote": {"code": "000003", "name": "薄产品"},
                       "f10": {"profile": {
                           "主营构成": {"报告期": "2026-06-30", "明细": [
                               {"项目": "只有一块业务", "收入占比(%)": "100.00", "毛利率(%)": "20.00"}]},
                           "题材标签": {"概念": ["汽车电子", "储能"]}}}}
        builder = self._builder("horizontal")
        plan = EpisodePlanner().plan(one_product)
        _, name = builder._turn_chart("business", self._ctx(plan, one_product), "", "vis.r1_hook")
        self.assertEqual(name, "mindmap")

    def test_ledger_pair_degrades_when_there_is_only_one_period(self) -> None:
        """只有一期就不叫"对照"——两期才谈得上变了没有。"""
        one_period = {"code": "000004", "quote": {"code": "000004", "name": "一期"},
                      "f10": {"profile": {"财务历史": {"年度": {
                          "报告期": ["2025-12-31"],
                          "指标": {"归母净利润(亿元)": [903.10]}}}}}}
        builder = self._builder("horizontal")
        plan = EpisodePlanner().plan(one_period)
        body, name = builder._turn_chart("business", self._ctx(plan, one_period), "",
                                         "vis.r4_ledger_test")
        self.assertEqual(name, "source_card")
        self.assertNotIn("真实日K", body)
        self.assertIn("仅展示已核验素材", body)

    def test_channel_flow_draws_a_half_chain_when_one_side_is_missing(self) -> None:
        """缺一侧就画半条链：那一格写「未披露」，而不是把两头直接连起来。"""
        half = FULL["f10"]["profile"]["主营构成"]["明细"]
        half = [row for row in half if not str(row["项目"]).endswith("(地区)")]
        data = {"code": "600519", "quote": {"code": "600519", "name": "贵州茅台"},
                "f10": {"profile": {"主营构成": {"报告期": "2026-06-30", "明细": half}}}}
        builder = self._builder("horizontal")
        plan = EpisodePlanner().plan(data)
        body, name = builder._turn_chart("business", self._ctx(plan, data), "", "vis.r2_mechanism")
        self.assertEqual(name, "channel_flow")
        self.assertIn("未披露", body)          # 缺失那一侧是明确写出来的
        self.assertIn("stroke-dasharray", body)  # 半链是虚线，不是实心箭头
        self.assertNotIn("国内(地区)", body)

    def test_the_verification_label_reaches_the_timeline(self) -> None:
        """走「最近发生的事」这条线时，末章才挂「待验证」标签。

        其它四条线的收尾（回扣 / 观察动作 / 代价判断 / 处境描述）都不挂——
        每期都以一个待验证的数收尾，正是被嫌弃的那个固定模式。
        """
        from unittest import mock

        import stocktalk.modules.episode_planner as ep
        time_index = [i for i, line in enumerate(ep.STORY_LINES)
                      if line["r5_template"] == "timeline"][0]
        with mock.patch.object(ep, "_rotate_line", return_value=time_index):
            plan = EpisodePlanner().plan(FULL)
            body, name = self._builder("horizontal")._turn_chart(
                "business", self._ctx(plan, FULL), "", "vis.r5_open_verdict")
        self.assertEqual(name, "timeline")
        self.assertIn("◆ 待验证：下一份报告里的归母净利润", body)

    def test_portfolio_never_draws_a_row_the_plan_did_not_bind(self) -> None:
        """计划绑了哪几块业务，卡组上就只许有那几块。

        容错的判据是"这一章没绑到东西"（计划丢了），不是"F10 的行比计划多"。
        用数量比较的话，B3 按这期只讲两块业务去裁 `metric_ids` 时，没绑定的
        业务会自己爬回卡组——画面和口播对不上，而且看起来很"正常"。
        """
        import copy
        data = copy.deepcopy(FULL)
        data["f10"]["profile"]["主营构成"]["明细"].extend([
            {"项目": "冰淇淋(产品)", "收入占比(%)": "2.00", "毛利率(%)": "40.00"},
            {"项目": "红酒(产品)", "收入占比(%)": "1.00", "毛利率(%)": "35.00"},
        ])
        plan = EpisodePlanner().plan(data)
        r1 = next(r for r in plan["rounds"] if r["id"] == "r1_hook")
        binding = r1["visual"]["data_binding"]
        # 只留前两块业务的指标（收入占比 + 产品线毛利率各一条）：这期只讲这两块。
        binding["metric_ids"] = [mid for mid in binding["metric_ids"]
                                 if mid.startswith(("m.product.0.", "m.product.1."))]
        body, name = self._builder("horizontal")._turn_chart(
            "business", self._ctx(plan, data), "", "vis.r1_hook")
        # 卡组仍然出得来（两块业务够成"组"），但只许有绑定的那两块。
        self.assertEqual(name, "product_portfolio")
        self.assertIn("茅台酒", body)
        self.assertIn("其他系列酒", body)
        self.assertNotIn("冰淇淋", body)
        self.assertNotIn("红酒", body)

    def test_channel_flow_never_draws_rows_the_plan_did_not_bind(self) -> None:
        """同一把尺子量渠道链路：绑定里没有渠道行，就不许从 F10 把渠道行捞回来。

        R2 同时绑了产品线与渠道/地区两类素材；计划一旦只留产品线（这期不讲
        渠道），画面不该自己补一条"厂商→渠道→客户"，那等于把统筹又交回渲染层。
        """
        plan = EpisodePlanner().plan(FULL)
        r2 = next(r for r in plan["rounds"] if r["id"] == "r2_mechanism")
        binding = r2["visual"]["data_binding"]
        # 计划这章不改讲渠道：素材层就不绑 mat.channel.0（不是只留指标）。
        binding["material_ids"] = [mid for mid in binding["material_ids"]
                                   if mid != "mat.channel.0"]
        body, name = self._builder("horizontal")._turn_chart(
            "business", self._ctx(plan, FULL), "", "vis.r2_mechanism")
        # 渠道行全被计划排除 → 这张图画不出 → 按该章 fallback 退到业务拆解；
        # 关键是不许"从 F10 把渠道行捞回来"重新拼一条渠道链路。
        self.assertEqual(name, "mindmap")
        self.assertNotIn("渠道链路", body)


def _xs(svg: str) -> list[float]:
    """SVG 里每个 rect 的右边界（x + width），用来查卡片有没有越出图表区。"""
    import re
    return [float(x) + float(w)
            for x, w in re.findall(r'<rect[^>]*\bx="([-\d.]+)"[^>]*\bwidth="([-\d.]+)"', svg)] or [0.0]


if __name__ == "__main__":
    unittest.main()
