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
from datetime import date, timedelta
from typing import Any
from unittest import mock

from stocktalk.modules import episode_planner
from stocktalk.modules.dialogue_generator import (SENTENCE_BLACKLIST,
                                                  DialogueGenerator)
from stocktalk.modules.episode_planner import (ROUND_DEFS, ROUND_SENTENCE_SEEDS,
                                               EpisodePlanner, MaterialExtractor,
                                               _rotate_seed)
from stocktalk.modules.hyperframes_builder import HyperFramesBuilder

# 所有可能出现在计划里的模板名（现有 12 类渲染器 + 中性来源卡 + 阶段 B2 的三张新图）。
# 六张图那一版（value_chain / competition_map / event_impact）已作废，不在这里。
_CANDIDATE_RENDERERS: tuple[str, ...] = (
    "kline", "mindmap", "composition", "waterfall", "timeline", "working_capital",
    "ranking", "profit_trend", "margin_trend", "roe_trend", "cashflow_statement",
    "balance_sheet", "source_card", "product_portfolio", "channel_flow", "ledger_pair",
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
            # 渠道/地区行和产品行混在同一张表里（F10 原样），靠后缀区分
            {"项目": "直销(销售模式)", "收入": "4.10亿", "收入占比(%)": "53.00", "毛利率(%)": "12.50"},
            {"项目": "经销(销售模式)", "收入": "3.63亿", "收入占比(%)": "47.00", "毛利率(%)": "11.20"},
            {"项目": "华东(地区)", "收入": "3.20亿", "收入占比(%)": "41.40", "毛利率(%)": "12.10"},
            {"项目": "华南(地区)", "收入": "2.10亿", "收入占比(%)": "27.20", "毛利率(%)": "11.60"},
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

    def test_number_budget_counts_real_spoken_metrics_against_the_caps(self) -> None:
        """used 按 anchor/analogy 分级计数，cap 是新口径的分级上限（不再有 total）。"""
        plan = EpisodePlanner().plan(STOCK_DATA)
        expected_anchor = sum(len(r["anchor_metric_ids"]) for r in plan["rounds"])
        expected_analogy = sum(len(r["analogy_metric_ids"]) for r in plan["rounds"])
        self.assertGreater(expected_anchor + expected_analogy, 0)
        self.assertEqual(plan["number_budget"]["used"]["anchor"], expected_anchor)
        self.assertEqual(plan["number_budget"]["used"]["analogy"], expected_analogy)
        self.assertEqual(plan["number_budget"]["cap"], {"anchor": 4, "analogy": 4})
        self.assertNotIn("total", plan["number_budget"])
        self.assertLessEqual(plan["number_budget"]["used"]["anchor"], 4)
        self.assertLessEqual(plan["number_budget"]["used"]["analogy"], 4)

    def test_spoken_budget_allows_three_units_in_the_ledger_chapter(self) -> None:
        """R4 是全片揭底，允许 3 个解释单元；其余章节 1-2 个。"""
        spoken = {r["id"]: r["number_budget"]["spoken"]
                  for r in EpisodePlanner().plan(STOCK_DATA)["rounds"]}
        self.assertEqual(spoken["r4_ledger_test"], 3)
        self.assertEqual(spoken, {"r1_hook": 1, "r2_mechanism": 1, "r3_moat_pressure": 2,
                                  "r4_ledger_test": 3, "r5_open_verdict": 1})

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
        self.assertEqual(policies["产品线毛利率"], "analogy")
        self.assertNotIn("毛利率", policies)

    def test_the_two_gross_margin_policies_stay_distinct(self) -> None:
        """产品线毛利率可念（analogy），公司级销售毛利率只上屏（visual_only）。

        两条口径同片共存（600519 实测 92.28% vs 91.93%），标签必须字面就分得开，
        否则模型会顺手把公司级那条念出来。
        """
        materials = MaterialExtractor().extract(STOCK_DATA)
        labeled = {(str(m["id"]), str(x["label"])): x["spoken_policy"]
                   for m in materials for x in (m.get("metrics") or [])}
        product = [(key, policy) for key, policy in labeled.items()
                   if key[0] == "mat.product.0" and "毛利率" in key[1]]
        ledger = [(key, policy) for key, policy in labeled.items()
                  if key[0] == "mat.financial.0" and "毛利率" in key[1]]
        self.assertTrue(product and ledger, labeled)
        # 没有一条同名的指标：标签各不相同，模型不会认错口径
        labels = [key[1] for key, _ in product + ledger]
        self.assertEqual(len(set(labels)), len(labels), labels)
        self.assertEqual({policy for _, policy in product}, {"analogy"})
        self.assertEqual({policy for _, policy in ledger}, {"visual_only"})

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


class FrozenDate(date):
    """把 ``date.today()`` 钉在 ``FIXED`` 上——``plan()`` 里 ``as_of`` 的唯一来源。

    用它 patch 掉 ``episode_planner.date``，就不必改 ``plan()`` 签名也能验接线：
    日期变了种子组必须跟着变。
    """

    FIXED = date(2026, 10, 1)

    @classmethod
    def today(cls) -> "FrozenDate":
        return cls(cls.FIXED.year, cls.FIXED.month, cls.FIXED.day)


class SentenceModeTests(unittest.TestCase):
    """每章这一期的开法种子：每章一条、按 (code, date) 稳定轮换。"""

    def test_plan_ships_exactly_one_seed_per_chapter(self) -> None:
        """15 条一次全塞给模型等于没结构化：每章只见自己那一章的这一条。"""
        plan = EpisodePlanner().plan(STOCK_DATA)
        shipped = [c["sentence_modes"] for c in plan["rounds"]]
        self.assertEqual([len(s) for s in shipped], [1] * 5, shipped)
        for modes in shipped:
            seed = modes[0]
            self.assertIn(seed["speaker"], ("bull", "bear"))
            self.assertTrue(seed["mode"] and seed["text"])

    def test_rotation_is_deterministic_and_moves_with_the_date(self) -> None:
        """同一 (code, date) 两次规划同组；换一天要换组，不然每期开法都一样。"""
        first = [c["sentence_modes"][0]["id"]
                 for c in EpisodePlanner().plan(STOCK_DATA)["rounds"]]
        again = [c["sentence_modes"][0]["id"]
                 for c in EpisodePlanner().plan(STOCK_DATA)["rounds"]]
        self.assertEqual(first, again)
        by_day = {}
        for offset in range(1, 15):
            day = (date(2026, 10, 1) + timedelta(days=offset)).isoformat()
            by_day[day] = tuple(_rotate_seed(ROUND_SENTENCE_SEEDS[rid], "600519", day, rid)
                                for rid, *_ in ROUND_DEFS)
        # 两周里至少出现两组不同组合；同一天同一票必然复现同一组
        self.assertGreater(len(set(by_day.values())), 1, by_day)
        self.assertEqual(by_day["2026-10-02"],
                         tuple(_rotate_seed(ROUND_SENTENCE_SEEDS[rid], "600519", "2026-10-02", rid)
                               for rid, *_ in ROUND_DEFS))
        # 两周里三条种子都要轮到：否则第三条就是死内容
        used_indices = {index for combo in by_day.values() for index in combo}
        self.assertEqual(used_indices, {0, 1, 2}, by_day)

    def test_plan_feeds_today_into_the_seed_rotation(self) -> None:
        """``plan()`` 必须把今天（``as_of``）接进轮换：同一天同组，换一天换组。

        上一条是直接调 ``_rotate_seed`` 传显式日期，验的是 ``_rotate_seed`` 的性质
        不是 ``plan()`` 的接线——``as_of`` 改成常量它照样绿。这条走 ``plan()``，
        把 ``episode_planner.py`` 里 ``as_of`` 那行改成常量，这条必须红。
        """

        def plan_on(day: date) -> dict[str, Any]:
            FrozenDate.FIXED = day
            return EpisodePlanner().plan(STOCK_DATA)

        day_one, day_two = date(2026, 10, 1), date(2026, 10, 2)
        with mock.patch.object(episode_planner, "date", FrozenDate):
            first, again, moved = plan_on(day_one), plan_on(day_one), plan_on(day_two)
            # as_of 必须是冻结的今天：它同时是种子轮换的输入
            self.assertEqual(first["as_of"], day_one.isoformat())
            self.assertEqual(again["as_of"], day_one.isoformat())
            self.assertEqual(moved["as_of"], day_two.isoformat())
        groups = [[chapter["sentence_modes"][0]["id"] for chapter in plan["rounds"]]
                  for plan in (first, again, moved)]
        self.assertEqual(groups[0], groups[1], groups[0])          # 同一天可复现
        self.assertNotEqual(groups[0], groups[2], (groups[0], groups[2]))


class EmptyPoolWarningTests(unittest.TestCase):
    """某章 anchor 和 analogy 都取不到时必须留下痕迹，不能静默退化。"""

    THIN_DATA: dict[str, Any] = {
        "code": "000000",
        "quote": {"code": "000000", "name": "薄数据"},
        "f10": {"profile": {"主营构成": {"报告期": "2026-06-30", "明细": [
            {"项目": "只有一块业务", "收入占比(%)": "100.00"}]}}},
    }

    def test_chapter_without_spoken_metric_is_recorded(self) -> None:
        """薄样本（连 mat.event.0 都没有）：非末章全都要照报，末章豁免不覆盖它们。

        豁免只写在 ROUND_NUMBER_POLICY 为空的末章上；前面的章再薄也得报。
        """
        plan = EpisodePlanner().plan(self.THIN_DATA)
        self.assertNotIn("mat.event.0", plan["selected_material_ids"])
        self.assertIn("r3_moat_pressure:spoken_metrics_exhausted", plan["warnings"])
        self.assertIn("r4_ledger_test:spoken_metrics_exhausted", plan["warnings"])
        # 末章本来就没有口播额度的预期，不该把它也算成"退化"
        self.assertNotIn("r5_open_verdict:spoken_metrics_exhausted", plan["warnings"])
        self.assertNotIn("r5_open_verdict:no_spoken_metric_source", plan["warnings"])
        reported = {w.split(":")[0] for w in plan["warnings"]}
        for chapter in plan["rounds"]:
            expected = bool((chapter["anchor_metric_ids"] or chapter["analogy_metric_ids"])
                            or chapter["id"] == "r5_open_verdict")
            self.assertIs(chapter["no_spoken_metric"], not expected, chapter["id"])
            # 非末章取不到就一定在告警里——豁免不能往前扩
            if chapter["no_spoken_metric"]:
                self.assertNotEqual(chapter["id"], "r5_open_verdict")
                self.assertIn(chapter["id"], reported)

    def test_full_data_plans_without_emptiness_warnings(self) -> None:
        """数据齐的时候不该出现告警——否则它就是噪音，没人会看。"""
        plan = EpisodePlanner().plan(STOCK_DATA)
        self.assertNotIn("r3_moat_pressure:spoken_metrics_exhausted", plan["warnings"])
        self.assertNotIn("r4_ledger_test:spoken_metrics_exhausted", plan["warnings"])

    def test_no_spoken_metric_source_is_distinguished_from_exhausted(self) -> None:
        """素材本身没有可念的指标 vs 被前面的章节取完：两种告警原因不同。"""
        no_metric = {"code": "000001", "quote": {"code": "000001"},
                     "f10": {"profile": {"行业地位": {"研究行业": "元器件"}}}}
        plan = EpisodePlanner().plan(no_metric)
        self.assertIn("r1_hook:no_spoken_metric_source", plan["warnings"])

    def test_visual_only_metrics_do_not_count_as_speakable(self) -> None:
        """素材里只有 visual_only 指标时必须报"没得讲"，不能报"被取完了"。

        告警文案决定别人是去补素材还是去调章节分配，指反了这条告警就白记了。
        """
        ledger_only_margin = {
            "code": "000002", "quote": {"code": "000002", "name": "只有上屏毛利率"},
            "f10": {"profile": {"财务历史": {"年度": {"报告期": ["2025-12-31"],
                                                     "指标": {"销售毛利率(%)": [91.93]}}}}},
        }
        plan = EpisodePlanner().plan(ledger_only_margin)
        # 财务素材有指标、但没有一条能念：这是"素材没给"，不是"被取完"
        self.assertIn("r4_ledger_test:no_spoken_metric_source", plan["warnings"])
        self.assertNotIn("r4_ledger_test:spoken_metrics_exhausted", plan["warnings"])
        ledger = next(m for m in plan["materials"] if m["id"] == "mat.financial.0")
        policies = {str(x["spoken_policy"]) for x in ledger["metrics"]}
        self.assertEqual(policies, {"visual_only"}, policies)


class EpisodePromptTests(unittest.TestCase):
    """计划下发到 prompt 的那一层：who 领讲、开法种子、数字纪律、反例都要看得见。"""

    def _section(self, stock_data: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        gen = DialogueGenerator({"characters": {"bull": {"name": "新手"}, "bear": {"name": "老手"}}},
                                llm=object())
        plan = EpisodePlanner().plan(stock_data or STOCK_DATA)
        return gen._episode_section({"episode_plan": plan}), plan

    def test_lead_reaches_the_prompt(self) -> None:
        section, _ = self._section()
        # 章对象的 lead 存在 JSON 里还不够——模型看不到 json 就必须猜谁先说
        self.assertIn("由新手领讲", section)     # r1/r2 lead=bull
        self.assertIn("由老手领讲", section)     # r3/r4 lead=bear
        self.assertIn("领讲", section)

    def test_sentence_modes_seed_reaches_the_prompt(self) -> None:
        section, plan = self._section()
        shipped = {c["sentence_modes"][0]["text"] for c in plan["rounds"]}
        for chapter in plan["rounds"]:
            seed = chapter["sentence_modes"][0]
            self.assertIn(seed["mode"], section)
            self.assertIn(seed["text"], section)
        # 其余 10 条不许跟着一起下去：每章只见自己那一章的这一条
        all_texts = {text for rid in ROUND_SENTENCE_SEEDS
                     for _side, _mode, text in ROUND_SENTENCE_SEEDS[rid]}
        self.assertEqual(len(all_texts), 15)
        for text in all_texts - shipped:
            self.assertNotIn(text, section)

    def test_purpose_is_not_repeated_as_must_cover(self) -> None:
        section, plan = self._section()
        # must_cover 恒等于 purpose，写两遍等于让同一句话在章节行里出现两次
        for chapter in plan["rounds"]:
            self.assertNotIn("must_cover", chapter)
            self.assertEqual(section.count(chapter["purpose"]), 1, chapter["id"])

    def test_seed_replacement_constraint_is_in_the_rendered_prompt(self) -> None:
        """示范句必须换成这只票自己的东西——这条约束要靠测试锁，不能只在描述里写着。

        15 条种子是拿 600519 写的（实体与数字都是那一期的），是否留存 JSON 里没关系，
        模型读的是 prompt：替换要求一旦被删掉，别的票就会念出茅台的句子。
        删掉 `_episode_section` 里那四行，这条必须红。
        """
        section, _ = self._section()
        self.assertIn("换成这只票自己的素材与 source_refs 里的数", section)
        self.assertIn("修辞动作", section)
        self.assertIn("不要硬套", section)

    def test_number_discipline_follows_the_agreed_units(self) -> None:
        section, _ = self._section()
        self.assertIn("解释单元", section)
        self.assertIn("anchor ≤4", section)
        self.assertIn("analogy ≤4", section)
        self.assertIn("visual_only 不限", section)
        self.assertIn("任意连续两轮不得都出现新数字", section)
        # 作废的旧口径不许还在
        self.assertNotIn("全片最多6个核心数字", section)

    def test_blacklist_ships_as_counter_examples_with_reasons(self) -> None:
        section, _ = self._section()
        for sentence, reason in SENTENCE_BLACKLIST:
            self.assertIn(sentence, section)
            self.assertIn(reason, section)
        # 行业性错误那条也要跟着一起下去：类比必须按行业生成
        self.assertIn("基酒", section)


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

    def test_a_split_line_may_land_in_two_adjacent_chapters_and_that_is_intended(self) -> None:
        """同一句被拆成两段、前后半段分到相邻两章是**期望行为**，不是 bug。

        分章按"成片实际有几句"，不按"模型逻辑上有几轮"——观众听到的是句子。
        这句被钉在这里，是因为它看起来像错，下一轮很容易被顺手"修"回跟随父轮。
        """
        turns = self._turns(5)
        order = [turn["round_id"] for turn in turns[:-2]]
        rounds = [rid for rid, *_ in ROUND_DEFS]
        # 相邻一定是相邻章节（不跳章），且次序不回退
        for previous, current in zip(order, order[1:]):
            self.assertLessEqual(rounds.index(previous), rounds.index(current), order)
            self.assertLessEqual(rounds.index(current) - rounds.index(previous), 1, order)
        # 五句长句时至少有一次真的跨到了下一章（把上面的语义落到具体数据上）
        self.assertTrue(any(rounds.index(current) - rounds.index(previous) == 1
                            for previous, current in zip(order, order[1:])), order)


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
        # 台词被判成"同业竞争"（旧路由会出 ranking）；计划里 r1 是产品卡组。
        ctx = self._ctx(builder, plan)
        ctx["argument"] = "peers"
        _, name = builder._turn_chart("peers", ctx, "", "vis.r1_hook")
        self.assertEqual(name, "product_portfolio")

    def test_planned_chart_falls_back_only_inside_its_own_chain(self) -> None:
        builder = self._builder(tempfile.mkdtemp())
        # 计划本身也是空的（一只什么都没查到的票）：三张新图都画不出 → 末级来源卡。
        plan = EpisodePlanner().plan({"code": "000000", "quote": {"code": "000000"}})
        ctx = {"bars": [], "quote": {}, "keywords": [], "argument": "business",
               "f10": {}, "financials": {}, "news": {}, "episode_plan": plan}
        for visual_id in ("vis.r1_hook", "vis.r2_mechanism", "vis.r4_ledger_test"):
            body, name = builder._turn_chart("business", ctx, "", visual_id)
            self.assertEqual(name, "source_card", visual_id)
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
            # 计划里 r1=产品卡组、r2=渠道链路、r3=行业排名、r4=两数对照、r5=资讯时间线
            self.assertIn("卖的是什么 · 产品卡组", content)
            self.assertIn("货怎么到客户手上 · 渠道链路", content)
            self.assertIn("行业里站哪 · 排名", content)
            self.assertIn("账有没有配合 · 两数对照", content)
            self.assertIn("最近发生的事", content)
            # 末章那条「待验证什么」必须真的上屏
            self.assertIn("◆ 待验证：下一份报告里的归母净利润", content)
            self.assertNotIn("暂无K线数据", content)
