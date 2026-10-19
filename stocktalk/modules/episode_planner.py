"""确定性素材归一与「五章叙事统筹」。

阶段 A 的范围：把个股数据里**有来源**的事实抽成可追溯的素材对象，按五个固定
叙事章节分配素材、数字额度与图板，产出一份 ``episode_plan``。全程确定性
（不新增 LLM 调用，与骨架选择同一哲学：同一只票同一天结果可复现）。

两条硬纪律：
1. 素材的每一个 ``fact``/``metric`` 都必须带 ``source_refs``；没有可靠结构化
   来源的关系（控股子公司、具名品牌、具名竞品）一律 ``availability="missing"``
   并进 ``rejected_materials``，不许从经营评述里抽一句就认定关系。
2. 阶段 A 的 ``visual.template`` 必须解析到**现有渲染器**上（见
   ``hyperframes_builder._render_chart``）；阶段 B 那六张新 SVG 启用前，这里
   不许出现它们——否则整片会静默掉成来源卡。
"""
from __future__ import annotations

import hashlib
from datetime import date
from typing import Any, Mapping, Sequence

from stocktalk.modules.story_spotter import spot_stories

# 五章骨架：(id, 目的, 主讲方, 素材类别)
ROUND_DEFS: tuple[tuple[str, str, str, str], ...] = (
    ("r1_hook", "反差开门", "bull", "product"),
    ("r2_mechanism", "生意怎么转", "bull", "product"),
    ("r3_moat_pressure", "护城河与反作用力", "bear", "competition"),
    ("r4_ledger_test", "用账验证故事", "bear", "financial_proof"),
    ("r5_open_verdict", "留下可验证结局", "bull", "news_event"),
)

# 章 → 中央图。阶段 A 只能用现有渲染器；这一张是"这章最该看到什么"，
# 不是装饰性轮换。
ROUND_TEMPLATES: dict[str, str] = {
    "r1_hook": "composition",          # 收入构成：哪块业务撑起收入
    "r2_mechanism": "mindmap",         # 业务拆解：产品/渠道与毛利率
    "r3_moat_pressure": "ranking",     # 行业地位：同行家数与真实排名
    "r4_ledger_test": "profit_trend",  # 多年净利走势：账有没有配合故事
    "r5_open_verdict": "timeline",     # 资讯时间线：下期要核对什么
}

# 章 → 指定图缺数据时的后退序列。末级一律 source_card：宁可出一张中性的
# 来源卡，也不退回"从台词关键词猜一张图"、更不退回万能 K 线。
ROUND_FALLBACKS: dict[str, tuple[str, ...]] = {
    "r1_hook": ("mindmap", "source_card"),
    "r2_mechanism": ("composition", "source_card"),
    "r3_moat_pressure": ("composition", "mindmap", "source_card"),
    "r4_ledger_test": ("cashflow_statement", "balance_sheet", "working_capital", "source_card"),
    "r5_open_verdict": ("composition", "source_card"),
}

# 章 → 素材类别对应的 material id。素材没抽到时该章退化到第一个可用素材，
# 章节本身不消失（五章是叙事结构，不是数据的函数）。
ROUND_MATERIALS: dict[str, str] = {
    "product": "mat.product.0",
    "competition": "mat.competition.0",
    "financial_proof": "mat.financial.0",
    "news_event": "mat.event.0",
}

# 章 → 本章优先念哪一级数字（按顺序取，取到就停；取不到就退下一级）。
# R1 只露半张底牌，要的是原值；R2「生意怎么转」要讲的是哪块最赚钱，所以先拿
# 产品毛利率那条 analogy 的白话换算，而不是把 R1 的收入占比再念一遍。
ROUND_NUMBER_POLICY: dict[str, tuple[str, ...]] = {
    "r1_hook": ("anchor",),
    "r2_mechanism": ("analogy", "anchor"),
    "r3_moat_pressure": ("analogy", "anchor"),
    "r4_ledger_test": ("anchor",),
    "r5_open_verdict": (),
}

# 章 → 本章最多口播几个「解释单元」（一组同口径的对比算 1 个）。
# 口径取自 product 05:57 那版：条数不是病因，"连续两句报数"和"数字后面没有生意
# 含义"才是。R4 是全片的揭底，允许 3 个；其余章节只给 1-2 个。
ROUND_SPOKEN_BUDGET: dict[str, int] = {
    "r1_hook": 1,
    "r2_mechanism": 1,
    "r3_moat_pressure": 2,
    "r4_ledger_test": 3,
    "r5_open_verdict": 1,
}

# 全片分级额度上限：anchor/analogy 各 ≤4，visual_only 不限（上屏表格可以密，
# 观众自己看得懂）。这两个常量同时是计划里 cap 与 prompt 第 10 条的**唯一来源**
# ——两处各写一遍等于给下一次口径漂移留口子（A 批的 NUMBER_BUDGET_TOTAL 就是教训）。
NUMBER_CAP_ANCHOR = 4
NUMBER_CAP_ANALOGY = 4

# 章 → 台词开法种子（多方/空方、修辞动作、示范句）。每条示范句里的公司名、产品名
# 与数字都是**那一期**的说法，只借用它的修辞动作，不是可以照抄的内容——下发时
# prompt 里会明说这一点。
ROUND_SENTENCE_SEEDS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "r1_hook": (("bull", "事实反差", "一千四百多亿的营收，八成四来自同一瓶酒。"),
                ("bear", "反向设问", "如果明天这桌饭不喝它了，这家公司还剩下什么？"),
                ("bull", "场景代入", "先想一个画面：一桌饭，主位旁边那个红盒子——它为什么总在那儿？")),
    "r2_mechanism": (("bull", "链路拆解", "一瓶酒出厂以后要过三道手：经销商、批价、终端。每过一道，价格和风险就换个人扛。"),
                     ("bear", "追问", "四成三自己卖，那经销商还剩下什么用？"),
                     ("bull", "算账", "同一瓶酒，自己卖和给经销商卖，差的不是价格，是谁替它垫钱、谁替它压货。")),
    "r3_moat_pressure": (("bear", "替代场景", "能替掉它的不是别的酒，是“这顿饭不喝了”。"),
                         ("bull", "反向守", "请客不会消失，只会换地方：商务少了，宴席还在。"),
                         ("bear", "压力测试", "假设批价跌两成，经销商还会不会替它囤货？")),
    "r4_ledger_test": (("bear", "数字对撞", "毛利九成二，净利五成——中间那四成，谁拿走了？"),
                       ("bull", "反向解释", "被税拿走的那部分，是它替所有想进场的人先交的门票。"),
                       ("bear", "一句话钉", "毛利率是过去的价格权，净利率才是今天的成本权。")),
    "r5_open_verdict": (("bull", "只给变量", "下一份年报只盯一个数：直销占比。"),
                        ("bear", "给阈值", "四成三是条线。掉下去，说明它自己卖不动了。"),
                        ("bull", "短收", "行，等这个数。")),
}


def _rotate_seed(seeds: Sequence[tuple[str, str, str]], code: str, day: str, rid: str) -> int:
    """按 ``(code, day, rid)`` 稳定选出本章这一期的种子下标。

    同一只票同一天两次规划必须拿到同一组（可复现），换一天要换一组（不撞选题节奏），
    且每章**只发一条**——15 条一次全塞给模型等于没给，模型只会抄最近的几条。
    """
    if not seeds:
        return -1
    digest = hashlib.sha256(f"{code}|{day}|{rid}".encode("utf-8")).hexdigest()
    return int(digest, 16) % len(seeds)


def _ref(path: str, as_of: str | None = None) -> list[dict[str, str]]:
    """一条来源引用。``as_of`` 缺省今天——F10 报告期缺失时宁可标今天也不空着。"""
    return [{"dataset": "stock_data", "path": path, "as_of": as_of or date.today().isoformat()}]


def _number(value: Any) -> float | None:
    """F10 里的数字大多带单位或百分号（"62.31"、"14.62%"），统一成 float。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace("%", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class MaterialExtractor:
    """把 tongstock 的 payload 变成带 ``source_refs`` 的素材对象。"""

    def extract(self, stock_data: Mapping[str, Any],
                stories: list[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
        f10 = stock_data.get("f10") if isinstance(stock_data.get("f10"), Mapping) else {}
        profile = f10.get("profile") if isinstance(f10.get("profile"), Mapping) else {}
        materials: list[dict[str, Any]] = []

        # 1. 产品线：主营构成明细（项目/收入占比/毛利率），逐字段带来源。
        composition = profile.get("主营构成") if isinstance(profile.get("主营构成"), Mapping) else {}
        rows = composition.get("明细") if isinstance(composition.get("明细"), list) else []
        as_of = str(composition.get("报告期") or date.today().isoformat())
        items: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            name = str(row.get("项目") or row.get("名称") or row.get("业务") or "").strip()
            if not name:
                continue
            metrics: list[dict[str, Any]] = []
            # 口径与画面上的收入构成图一致：`收入占比(%)`/`毛利率(%)` 是 F10 原文字段。
            # 毛利率走 analogy：它是"每 100 元收入有 X 元是毛利"这种确定换算，
            # 只上屏不口播的话，R2「生意怎么转」就讲不出哪块最赚钱。
            # 标签必须和财务素材的公司级口径**字面就要分得开**：两条都叫「毛利率」时，
            # 模型会顺手把公司级的那条（visual_only）念出来。
            for key, label, unit, policy in (("收入占比(%)", "收入占比", "%", "anchor"),
                                             ("毛利率(%)", "产品线毛利率", "%", "analogy")):
                value = _number(row.get(key))
                if value is None:
                    continue
                metrics.append({"id": f"m.product.{index}.{label}", "label": label, "value": value,
                                "unit": unit, "period": as_of, "spoken_policy": policy,
                                "source_refs": _ref(f"f10.profile.主营构成.明细[{index}].{key}", as_of)})
            items.append({"name": name, "metrics": metrics})
        if items:
            materials.append({"id": "mat.product.0", "category": "product", "title": "主力产品线",
                              "fact": "、".join(x["name"] for x in items),
                              "entities": [{"name": x["name"], "role": "product"} for x in items],
                              "metrics": [m for x in items for m in x["metrics"]],
                              "source_refs": _ref("f10.profile.主营构成.明细", as_of),
                              "visual_candidates": ["composition", "mindmap"], "availability": "ready"})

        # 2. 行业位置：同行家数与研究行业（真实名次亦在这块，阶段 A 只用家数）。
        industry = profile.get("行业地位") if isinstance(profile.get("行业地位"), Mapping) else {}
        if industry.get("同行家数") is not None or industry.get("研究行业"):
            metrics = []
            peers = _number(industry.get("同行家数"))
            if peers is not None:
                metrics.append({"id": "m.competition.peers", "label": "同行家数", "value": peers, "unit": "家",
                                "period": as_of, "spoken_policy": "analogy",
                                "source_refs": _ref("f10.profile.行业地位.同行家数", as_of)})
            materials.append({"id": "mat.competition.0", "category": "competition", "title": "行业位置",
                              "fact": str(industry.get("研究行业") or "公开行业"), "entities": [],
                              "metrics": metrics, "source_refs": _ref("f10.profile.行业地位", as_of),
                              "visual_candidates": ["ranking", "composition"], "availability": "ready"})

        # 3. 财务证据：多年年度序列的最后两期（同口径、带报告期）。
        history = profile.get("财务历史") if isinstance(profile.get("财务历史"), Mapping) else {}
        annual = history.get("年度") if isinstance(history.get("年度"), Mapping) else {}
        metric_map = annual.get("指标") if isinstance(annual.get("指标"), Mapping) else {}
        periods = [str(p) for p in (annual.get("报告期") or []) if str(p)]
        # 财务证据里的 `销售毛利率(%)` 是**公司级**口径，与产品线的「产品线毛利率」
        # 属于两件事，且它是 visual_only——只上屏、不许口播（它就是最容易被顺手
        # 念出来的那类数字）。
        wanted = (("归母净利润(亿元)", "归母净利润", "亿元", "anchor"),
                  ("经营活动现金流量净额(亿元)", "经营现金流", "亿元", "anchor"),
                  ("销售毛利率(%)", "公司销售毛利率", "%", "visual_only"))
        metrics = []
        for key, label, unit, policy in wanted:
            values = metric_map.get(key)
            if not isinstance(values, list) or not values:
                continue
            # 报告期与指标一一对应；对不上就标今天，绝不把两期拼成一段假序列。
            for offset, value in enumerate(values[-2:], start=max(0, len(values) - 2)):
                number = _number(value)
                if number is None:
                    continue
                period = periods[offset] if len(periods) == len(values) and offset < len(periods) else as_of
                metrics.append({"id": f"m.ledger.{len(metrics)}", "label": label, "value": number,
                                "unit": unit, "period": period, "spoken_policy": policy,
                                "source_refs": _ref(f"f10.profile.财务历史.年度.指标.{key}[{offset}]", period)})
        if metrics:
            materials.append({"id": "mat.financial.0", "category": "financial_proof", "title": "财务证据",
                              "fact": "同口径财务指标对照", "entities": [], "metrics": metrics,
                              "source_refs": _ref("f10.profile.财务历史.年度", as_of),
                              "visual_candidates": ["profit_trend", "cashflow_statement", "working_capital"],
                              "availability": "ready"})

        # 4. 最新事件：只取一条，日期与信息类型都在引用里。
        news = stock_data.get("news") if isinstance(stock_data.get("news"), Mapping) else {}
        news_items = news.get("items") if isinstance(news.get("items"), list) else []
        if news_items and isinstance(news_items[0], Mapping) and news_items[0].get("title"):
            materials.append({"id": "mat.event.0", "category": "news_event", "title": "最新事件",
                              "fact": str(news_items[0]["title"]), "entities": [], "metrics": [],
                              "source_refs": _ref("news.items[0]", str(news_items[0].get("publish_time") or as_of)),
                              "visual_candidates": ["timeline"], "availability": "ready"})

        # 5. 没有可靠结构化来源的关系：一律 missing，进 rejected_materials。
        for category, title in (("subsidiary", "控股子公司"), ("brand_channel", "具名品牌"),
                                ("competition_named", "具名竞品")):
            materials.append({"id": f"mat.{category}.missing", "category": category, "title": title,
                              "fact": "", "entities": [], "metrics": [], "source_refs": [],
                              "visual_candidates": [], "availability": "missing"})
        return materials


class EpisodePlanner:
    """按五个固定章节分配素材、数字额度与图板，产出 ``episode_plan``。"""

    def __init__(self, extractor: MaterialExtractor | None = None) -> None:
        self.extractor = extractor or MaterialExtractor()

    def plan(self, stock_data: Mapping[str, Any]) -> dict[str, Any]:
        # 选题显著性（story_spotter 只给角度字符串，不带 id/source_refs，所以
        # 它只当"讲什么更值得讲"的排序输入，素材本身仍由 extractor 产出自证来源）。
        stories = spot_stories(stock_data)
        materials = self.extractor.extract(stock_data, stories)
        available = [m for m in materials if m.get("availability") == "ready"]
        selected = [str(m["id"]) for m in available]
        by_id = {str(m["id"]): m for m in available}

        rounds: list[dict[str, Any]] = []
        warnings: list[str] = []
        # 同一条指标全片只上场一次：章节之间各取 anchors[:1] 会取到同一条，
        # 同一个数字念两遍、额度计两次（R1/R2 共用产品素材的「收入占比」就是这么撞的）。
        claimed: set[str] = set()
        anchor_used = analogy_used = 0
        code = str(stock_data.get("code") or (stock_data.get("quote") or {}).get("code") or "")
        as_of = date.today().isoformat()
        for rid, purpose, lead, category in ROUND_DEFS:
            material_id = ROUND_MATERIALS.get(category)
            if material_id not in by_id:
                material_id = selected[0] if selected else None
            mids = [material_id] if material_id else []
            material = by_id.get(material_id or "", {})
            metrics = [m for m in (material.get("metrics") or []) if isinstance(m, Mapping)]
            # 数字三级：anchor 原值口播，analogy 讲确定换算后的白话，visual_only 只上屏。
            anchors = [str(m["id"]) for m in metrics if m.get("spoken_policy") == "anchor"]
            analogies = [str(m["id"]) for m in metrics if m.get("spoken_policy") == "analogy"]
            # 按本章的口径偏好挑一条上场：已被别的章拿走的指标不再重复计额度。
            pools = {"anchor": [i for i in anchors if i not in claimed],
                     "analogy": [i for i in analogies if i not in claimed]}
            take_anchors: list[str] = []
            take_analogies: list[str] = []
            for policy in ROUND_NUMBER_POLICY.get(rid, ("anchor", "analogy")):
                pool = pools.get(policy) or []
                if not pool:
                    continue
                picked = pool[:1]
                (take_anchors if policy == "anchor" else take_analogies).extend(picked)
                claimed.update(picked)
                break
            anchor_used += len(take_anchors)
            analogy_used += len(take_analogies)
            # 这一章一条能念的指标都没捞到，审查页上要看得出来：静默退化长得跟
            # "这章本来就没数字"一模一样，等于悄悄少讲一层，事后查不出来。
            # 末章本来就不要求新数字（ROUND_NUMBER_POLICY 为空），不算退化。
            empty_pool = not (take_anchors or take_analogies) and bool(ROUND_NUMBER_POLICY.get(rid))
            if empty_pool:
                # 分两种：素材压根没有可念的指标，还是指标被前面的章节取完了——
                # 前者要补素材，后者要调章节分配，处理办法不一样。
                warnings.append(f"{rid}:{'spoken_metrics_exhausted' if metrics else 'no_spoken_metric_source'}")
            seeds = ROUND_SENTENCE_SEEDS.get(rid, ())
            seed_index = _rotate_seed(seeds, code, as_of, rid)
            rounds.append({"id": rid, "purpose": purpose, "turn_budget": [2, 4], "lead": lead,
                           "material_ids": mids,
                           # 单章口播额度：按「解释单元」计（一组同口径对比算 1 个），
                           # R4 是全片揭底，允许 3 个。
                           "number_budget": {"spoken": ROUND_SPOKEN_BUDGET.get(rid, 2),
                                             "anchor": len(take_anchors),
                                             "analogy": len(take_analogies)},
                           # 这章该讲数字但一条都没取到（审查页据此标红）
                           "no_spoken_metric": empty_pool,
                           "anchor_metric_ids": take_anchors,
                           "analogy_metric_ids": take_analogies,
                           "visual": {"id": f"vis.{rid}",
                                      "template": ROUND_TEMPLATES[rid],
                                      "data_binding": {"material_ids": mids},
                                      "fallback": list(ROUND_FALLBACKS[rid])},
                           # 本章这一期要用的那条开法种子（每章一条，不把 15 条全塞给模型）。
                           "sentence_modes": ([{"id": f"{rid}.seed{seed_index + 1}",
                                                "speaker": seeds[seed_index][0],
                                                "mode": seeds[seed_index][1],
                                                "text": seeds[seed_index][2]}]
                                              if seed_index >= 0 else []),
                           "transition": ""})

        missing = [m for m in materials if m.get("availability") == "missing"]
        quote = stock_data.get("quote") if isinstance(stock_data.get("quote"), Mapping) else {}
        return {
            "version": 1,
            "stock_code": str(stock_data.get("code") or quote.get("code") or ""),
            "as_of": as_of,
            "materials": materials,
            "story_candidates": stories,
            "story": {"angle": stories[0]["headline"] if stories else "公开资料中的业务结构",
                      "thesis": "看多一方要证明业务链条成立",
                      "counter_thesis": "看空一方要验证压力是否会反噬",
                      "reveal_material_id": "mat.financial.0" if "mat.financial.0" in selected else None},
            # used 是实际上场的 anchor/analogy 数，不是写死的 0：数字纪律要能核对。
            # cap 与 prompt 第 10 条同源（NUMBER_CAP_*），两处不会再各说一套。
            "number_budget": {"used": {"anchor": anchor_used, "analogy": analogy_used},
                              "cap": {"anchor": NUMBER_CAP_ANCHOR, "analogy": NUMBER_CAP_ANALOGY}},
            "selected_material_ids": selected,
            "rounds": rounds,
            "rejected_materials": [{"id": str(m["id"]), "reason": "缺少可靠结构化来源"} for m in missing],
            # 缺来源的素材 + 取不到口播数字的章节：两条都得看得见，"没数字可讲"
            # 不能长得跟"正常讲完了"一样。
            "warnings": [f"{m['category']}:missing_source" for m in missing] + warnings,
        }
