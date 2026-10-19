"""阶段 A：叙事统筹（episode_plan）的契约测试。

覆盖三件事——
1. planner 产出的五章与图板必须落在**现有渲染器**上（阶段 B 那六张新 SVG 启用前
   不许出现，否则整片会静默掉成来源卡）；
2. turn 元字段（round_id/visual_id）的分配必须单调、非空、末章留给片尾；
3. 渲染侧真的按 ``visual_id`` 取图——这是阶段 A 的核心，半成品漏的就是这一环。
"""
from __future__ import annotations

import tempfile
import unittest
from typing import Any

from stocktalk.modules.dialogue_generator import DialogueGenerator
from stocktalk.modules.episode_planner import (ROUND_DEFS, EpisodePlanner,
                                               MaterialExtractor)
from stocktalk.modules.hyperframes_builder import HyperFramesBuilder

# 所有可能出现在计划里的模板名（现有 12 类渲染器 + 中性来源卡 + 阶段 B 的六张新图）。
_CANDIDATE_RENDERERS: tuple[str, ...] = (
    "kline", "mindmap", "composition", "waterfall", "timeline", "working_capital",
    "ranking", "profit_trend", "margin_trend", "roe_trend", "cashflow_statement",
    "balance_sheet", "source_card", "product_portfolio", "channel_flow",
    "value_chain", "competition_map", "event_impact", "ledger_pair",
)

STOCK_DATA: dict[str, Any] = {
    "code": "605058",
    "quote": {"code": "605058", "name": "澳弘电子", "price": 21.35, "change_pct": 1.2},
    "financials": {"报告期": "2026-08-28", "营业收入(亿元)": 7.73, "营业利润(亿元)": 0.73,
                   "净利润(亿元)": 0.66, "存货(亿元)": 4.24, "应收账款(亿元)": 3.94},
    "f10": {"profile": {
        "主营业务": "印制电路板的研发、生产和销售",
        "主营构成": {"报告期": "2026-06-30", "明细": [
            {"项目": "印刷线路板(产品)", "收入": "6.54亿", "收入占比(%)": "84.62", "毛利率(%)": "11.98"},
            {"项目": "其他业务(产品)", "收入": "1.19亿", "收入占比(%)": "15.38", "毛利率(%)": "94.58"},
        ]},
        "行业地位": {"研究行业": "元器件", "同行家数": 66,
                   "市场表现": {"排名": 1}, "公司规模": {"排名": 52}},
        "财务历史": {"年度": {"报告期": ["2021-12-31", "2022-12-31", "2023-12-31",
                                      "2024-12-31", "2025-12-31"],
                            "指标": {"归母净利润(亿元)": [1.20, 1.05, 0.90, 1.10, 1.35],
                                    "销售毛利率(%)": [24.10, 21.30, 18.60, 16.20, 17.40]}}},
    }},
    "news": {"items": [{"title": "公司发布 2026 年半年报", "type": "公告",
                        "publish_time": "2026-08-28", "source": "交易所"}]},
    "kline": [],
}


def _renderer_names(builder: HyperFramesBuilder) -> set[str]:
    """现有渲染器的名字集合（阶段 A 的 visual.template 只能取这里面的值）。

    判据是"这张图能不能真出图"，而不是某个常量表：缺数据的图会返回 None，
    所以拿一份有数据的上下文逐个试，能出图的才是可用模板。
    """
    ctx = {"bars": [{"close": 10.0 + i} for i in range(40)], "quote": {"name": "澳弘电子"},
           "keywords": [], "argument": "business",
           "f10": STOCK_DATA["f10"], "financials": STOCK_DATA["financials"],
           "news": STOCK_DATA["news"]}
    known = {name for name in _CANDIDATE_RENDERERS if builder._render_chart(name, ctx)}
    assert "composition" in known and "source_card" in known, known
    # 已注册的渲染器全集（含这张票没数据、因而画不出来的那些）：fallback 里出现
    # 的名字必须是"真有这张图"，而不是阶段 B 才做的新模板名。
    registered = set(known) | {"balance_sheet", "cashflow_statement"}
    return known, registered


class EpisodePlannerTests(unittest.TestCase):
    def test_round_templates_resolve_to_existing_renderers(self) -> None:
        """阶段 A 不许指向阶段 B 才有的模板：否则五章全掉成来源卡。"""
        builder = HyperFramesBuilder({"video": {"canvas": "horizontal"}})
        known, registered = _renderer_names(builder)
        plan = EpisodePlanner().plan(STOCK_DATA)
        self.assertEqual([r["id"] for r in plan["rounds"]], [r[0] for r in ROUND_DEFS])
        for chapter in plan["rounds"]:
            visual = chapter["visual"]
            self.assertIn(visual["template"], known, visual)
            for name in visual["fallback"]:
                self.assertIn(name, registered, visual)
            # 末级必须是中性来源卡，绝不再退回 K 线
            self.assertEqual(visual["fallback"][-1], "source_card")
            self.assertNotIn("kline", visual["fallback"])

    def test_number_budget_counts_real_spoken_metrics(self) -> None:
        """used 是实际上场的 anchor+analogy 数，不是写死的 0。"""
        plan = EpisodePlanner().plan(STOCK_DATA)
        expected = sum(len(r["anchor_metric_ids"]) + len(r["analogy_metric_ids"])
                       for r in plan["rounds"])
        self.assertGreater(expected, 0)
        self.assertEqual(plan["number_budget"]["used"], expected)
        # 全片上限只在 prompt 的数字纪律里：计划里再躺一个不生效的 total 会让人误判
        self.assertNotIn("total", plan["number_budget"])

    def test_spoken_metric_is_never_shared_between_chapters(self) -> None:
        """同一条指标不许在两章各念一遍、额度计两次。

        R1/R2 都取产品素材的 anchors[:1]，念的是同一个「收入占比」。
        """
        plan = EpisodePlanner().plan(STOCK_DATA)
        spoken = [mid for chapter in plan["rounds"]
                  for mid in [*chapter["anchor_metric_ids"], *chapter["analogy_metric_ids"]]]
        self.assertEqual(len(spoken), len(set(spoken)), spoken)
        by_round = {c["id"]: [*c["anchor_metric_ids"], *c["analogy_metric_ids"]]
                    for c in plan["rounds"]}
        # R1 露原值、R2 讲"哪块最赚钱"，两章拿到的必须是不同的指标
        self.assertTrue(by_round["r1_hook"])
        self.assertTrue(by_round["r2_mechanism"])
        self.assertFalse(set(by_round["r1_hook"]) & set(by_round["r2_mechanism"]))

    def test_product_line_exposes_a_gross_margin_analogy(self) -> None:
        """毛利率只有 visual_only 时，R2 讲不了「哪块最赚钱」。"""
        material = next(m for m in MaterialExtractor().extract(STOCK_DATA)
                        if m["id"] == "mat.product.0")
        policies = {m["label"]: m["spoken_policy"] for m in material["metrics"]}
        self.assertEqual(policies["收入占比"], "anchor")
        self.assertEqual(policies["毛利率"], "analogy")

    def test_material_metrics_are_source_backed_and_missing_stays_rejected(self) -> None:
        materials = MaterialExtractor().extract(STOCK_DATA)
        ready = {m["id"] for m in materials if m["availability"] == "ready"}
        self.assertIn("mat.product.0", ready)
        self.assertIn("mat.financial.0", ready)
        for material in materials:
            if material["availability"] == "missing":
                continue
            self.assertTrue(material["source_refs"], material["id"])
            for metric in material["metrics"]:
                self.assertTrue(metric["source_refs"], metric)
                self.assertIn(metric["spoken_policy"], ("anchor", "analogy", "visual_only"))
        plan = EpisodePlanner().plan(STOCK_DATA)
        rejected = {item["id"] for item in plan["rejected_materials"]}
        self.assertEqual(rejected, {"mat.subsidiary.missing", "mat.brand_channel.missing",
                                    "mat.competition_named.missing"})
        self.assertTrue(any(w.endswith("missing_source") for w in plan["warnings"]))
        # 缺来源的素材绝不能进正文
        self.assertFalse(set(plan["selected_material_ids"]) & rejected)

    def test_periods_match_the_metric_series(self) -> None:
        """两期财务指标必须各自带上自己的报告期，不许拼成一段假序列。"""
        plan = EpisodePlanner().plan(STOCK_DATA)
        financial = next(m for m in plan["materials"] if m["id"] == "mat.financial.0")
        periods = {m["period"] for m in financial["metrics"]}
        self.assertTrue(periods <= {"2024-12-31", "2025-12-31"}, periods)


class TurnMetadataTests(unittest.TestCase):
    """turn 流 → 五章的分配：比例切段、单调不回退、末章留给片尾两轮。"""

    def _gen(self) -> DialogueGenerator:
        return DialogueGenerator({"dialogue": {"model": "m", "risk_exchange": True}}, llm=object())

    def test_chapter_index_is_proportional_and_monotonic(self) -> None:
        for total in (9, 12, 17, 24, 30):
            seen = [DialogueGenerator._chapter_index(i, total, 5) for i in range(1, total + 1)]
            self.assertEqual(seen, sorted(seen), (total, seen))
            self.assertEqual(seen[0], 0)
            self.assertEqual(seen[-1], 4)
            # 四章正文每章至少分到一轮（turn 数够时）
            if total >= 5:
                self.assertEqual(set(seen), {0, 1, 2, 3, 4}, (total, seen))

    def test_metadata_falls_back_to_proportional_chapters(self) -> None:
        plan = EpisodePlanner().plan(STOCK_DATA)
        gen = self._gen()
        total = 12
        order = [gen._turn_metadata({}, i, plan, total=total)["round_id"] for i in range(1, total + 1)]
        rounds = [r["id"] for r in plan["rounds"]]
        # 单调不回退（末章除外，它由最后两轮持有）
        body = [rounds.index(r) for r in order[:-2]]
        self.assertEqual(body, sorted(body), order)
        self.assertEqual(order[-2:], ["r5_open_verdict"] * 2)
        # 每章都带上了自己的图板 id
        for index, rid in enumerate(order, 1):
            meta = gen._turn_metadata({}, index, plan, total=total)
            chapter = next(r for r in plan["rounds"] if r["id"] == rid)
            self.assertEqual(meta["visual_id"], chapter["visual"]["id"])
            self.assertEqual(meta["material_ids"], chapter["material_ids"])

    def test_explicit_round_id_wins(self) -> None:
        plan = EpisodePlanner().plan(STOCK_DATA)
        meta = self._gen()._turn_metadata({"round_id": "r3_moat_pressure"}, 1, plan, total=12)
        self.assertEqual(meta["round_id"], "r3_moat_pressure")
        self.assertEqual(meta["visual_id"], "vis.r3_moat_pressure")


class SplitAwareChapterTests(unittest.TestCase):
    """章节分配必须走 ``_normalize_script`` 的真实拆行路径。

    直接喂 ``_turn_metadata`` 会绕过 ``_split_line``：轮数基准一旦按拆分前的轮数
    算，片尾两轮的 index 就溢出 total，末章的 ``{total, total-1}`` 匹配不上——
    收尾两句落进 R4 章，R5 全片 0 轮，规划好的 timeline 图一次都不出现。
    """

    LONG = ("这门生意到底靠什么赚钱，数据摆在这里，收入占比和毛利率一起看才说得清楚，"
            "别只盯着营收规模。")

    def _turns(self, long_count: int) -> list[dict[str, Any]]:
        gen = DialogueGenerator({"dialogue": {"max_line_chars": 40, "risk_exchange": True}},
                                llm=object())
        plan = EpisodePlanner().plan(STOCK_DATA)
        raw = [{"speaker": "bull" if i % 2 == 0 else "bear",
                "line": self.LONG if i < long_count else "那这一块呢，真的撑得住吗？"}
               for i in range(10)]
        script = gen._normalize_script({"turns": raw}, STOCK_DATA, None, plan)
        return list(script["turns"])

    def test_final_two_turns_hold_the_last_chapter_whatever_the_split_count(self) -> None:
        for long_count in (0, 1, 2, 5, 10):
            turns = self._turns(long_count)
            # 10 轮正文 + long_count 次拆行产生的续轮 + 片尾风险提示两轮
            self.assertEqual(len(turns), 12 + long_count, long_count)
            order = [turn["round_id"] for turn in turns]
            self.assertEqual(order[-2:], ["r5_open_verdict"] * 2, (long_count, order))
            self.assertEqual([turn["visual_id"] for turn in turns[-2:]],
                             ["vis.r5_open_verdict"] * 2, (long_count, order))
            # 末章只归片尾两轮；正文按位置单调不回退，五章一章不少
            body = order[:-2]
            self.assertNotIn("r5_open_verdict", body, (long_count, order))
            self.assertEqual(body, sorted(body, key=lambda rid: ["r1_hook", "r2_mechanism",
                                                                 "r3_moat_pressure",
                                                                 "r4_ledger_test"].index(rid)),
                             (long_count, order))
            self.assertEqual(set(order), {rid for rid, *_ in ROUND_DEFS}, (long_count, order))


class PlanDrivesChartTests(unittest.TestCase):
    """渲染侧：有 episode_plan 时，画面由 visual_id 决定，不由台词关键词决定。"""

    def _builder(self, directory: str) -> HyperFramesBuilder:
        return HyperFramesBuilder({"video": {"canvas": "horizontal", "output_dir": directory}})

    def _ctx(self, builder: HyperFramesBuilder, plan: dict[str, Any] | None) -> dict[str, Any]:
        ctx: dict[str, Any] = {"bars": [{"close": 10.0 + i} for i in range(40)],
                               "quote": {"name": "澳弘电子"}, "keywords": [],
                               "argument": "business", "f10": STOCK_DATA["f10"],
                               "financials": STOCK_DATA["financials"],
                               "news": STOCK_DATA["news"]}
        if plan is not None:
            ctx["episode_plan"] = plan
        return ctx

    def test_visual_id_selects_the_planned_chart_not_the_keyword_chart(self) -> None:
        builder = self._builder(tempfile.mkdtemp())
        plan = EpisodePlanner().plan(STOCK_DATA)
        # 台词被判成"同业竞争"（旧路由会出 ranking）；计划里 r1 是收入构成。
        ctx = self._ctx(builder, plan)
        ctx["argument"] = "peers"
        _, name = builder._turn_chart("peers", ctx, "", "vis.r1_hook")
        self.assertEqual(name, "composition")

    def test_planned_chart_falls_back_only_inside_its_own_chain(self) -> None:
        builder = self._builder(tempfile.mkdtemp())
        plan = EpisodePlanner().plan(STOCK_DATA)
        # 数据全空：计划里的 composition 出不了 → 退到该章自己的 fallback。
        ctx = {"bars": [], "quote": {}, "keywords": [], "argument": "business",
               "f10": {}, "financials": {}, "news": {}, "episode_plan": plan}
        body, name = builder._turn_chart("business", ctx, "", "vis.r1_hook")
        self.assertEqual(name, "source_card")
        self.assertNotIn("真实日K", body)   # 绝不退回 K 线
        self.assertIn("仅展示已核验素材", body)

    def test_plan_reaches_the_rendered_composition(self) -> None:
        """端到端：script 里的 episode_plan 真正驱动了每一块图板。"""
        with tempfile.TemporaryDirectory() as temp:
            plan = EpisodePlanner().plan(STOCK_DATA)
            ids = [r["id"] for r in plan["rounds"]]
            # 首轮不重画图板（开场那块是行情板），所以 r1 的图安排在第 6 轮出现。
            order = ids + [ids[0]]
            turns = [{"speaker": "bull" if i % 2 else "bear",
                      "line": f"第{i}轮：这门生意到底靠什么赚钱，数据摆在这里。",
                      "beat": "business", "emotion": "平述",
                      "round_id": rid, "visual_id": f"vis.{rid}"}
                     for i, rid in enumerate(order, 1)]
            script = {"stock_code": "605058", "stock_name": "澳弘电子", "turns": turns,
                      "episode_plan": plan}
            project = self._builder(temp).build_project(STOCK_DATA, script, {})
            content = project.composition_path.read_text(encoding="utf-8")
            # 计划里 r1=收入构成、r2=业务拆解、r3=行业排名、r4=净利走势、r5=资讯时间线
            self.assertIn("钱从哪来 · 收入构成", content)
            self.assertIn("澳弘电子业务拆解", content)
            self.assertIn("行业里站哪 · 排名", content)
            self.assertIn("业绩走势 · 归母净利润", content)
            self.assertIn("最近发生的事", content)
            self.assertNotIn("暂无K线数据", content)
