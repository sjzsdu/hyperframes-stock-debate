"""确定性素材归一与「五章叙事统筹」。

阶段 A 的范围：把个股数据里**有来源**的事实抽成可追溯的素材对象，按五个固定
叙事章节分配素材、数字额度与图板，产出一份 ``episode_plan``。全程确定性
（不新增 LLM 调用，与骨架选择同一哲学：同一只票同一天结果可复现）。

两条硬纪律：
1. 素材的每一个 ``fact``/``metric`` 都必须带 ``source_refs``；没有可靠结构化
   来源的关系（控股子公司、具名品牌、具名竞品）一律 ``availability="missing"``
   并进 ``rejected_materials``，不许从经营评述里抽一句就认定关系。
2. ``visual.template`` 必须解析到**现有渲染器**上（见
   ``hyperframes_builder._chart_renderers``）。阶段 B2 起这三张新 SVG
   （``product_portfolio`` / ``channel_flow`` / ``ledger_pair``）已在渲染器里，
   所以它们可以出现在 ``template`` 上；**没注册的名字不许进 fallback**——
   悬空的名字会和"缺数据"一样静默掉到来源卡（阶段 B1 踩过：阶段 B 的名字
   在阶段 A 时五章全掉 ``source_card``）。
"""
from __future__ import annotations

import hashlib
from datetime import date
from typing import Any, Mapping, Sequence

from stocktalk.modules.business_rows import (
    CHANNEL_ROW_SUFFIXES as _CHANNEL_ROW_SUFFIXES,
)
from stocktalk.modules.business_rows import (
    NON_PRODUCT_ROW_SUFFIXES as _NON_PRODUCT_ROW_SUFFIXES,
)
from stocktalk.modules.story_spotter import spot_stories

# 五章骨架：(id, 目的, 主讲方, 素材类别)
ROUND_DEFS: tuple[tuple[str, str, str, str], ...] = (
    ("r1_hook", "反差开门", "bull", "product"),
    ("r2_mechanism", "生意怎么转", "bull", "product"),
    ("r3_moat_pressure", "护城河与反作用力", "bear", "competition"),
    ("r4_ledger_test", "用账验证故事", "bear", "financial_proof"),
    ("r5_open_verdict", "留下可验证结局", "bull", "news_event"),
)

# 章 → 中央图。这一张是"这章最该看到什么"，不是装饰性轮换。
# 阶段 B2：r1/r2/r4 换到三张新 SVG 上（产品卡组 / 渠道链路 / 两数对照）；
# r3 的行业位置与 r5 的事件线没有对应的新图，留在现有渲染器上——为了换图
# 而换图等于把"图和这章讲的事对得上"这条又丢掉。
ROUND_TEMPLATES: dict[str, str] = {
    "r1_hook": "product_portfolio",     # 产品卡组：名称 + 占比 + 毛利
    "r2_mechanism": "channel_flow",     # 厂商 → 渠道 → 客户：生意怎么转
    "r3_moat_pressure": "ranking",      # 行业地位：同行家数与真实排名
    "r4_ledger_test": "ledger_pair",    # 两数对照：账有没有配合故事
    "r5_open_verdict": "timeline",      # 资讯时间线：下期要核对什么
}

# 章 → 指定图缺数据时的后退序列。两条纪律：
# 1. 末级一律 source_card：宁可出一张中性的来源卡，也不退回"从台词关键词猜
#    一张图"、更不退回万能 K 线；
# 2. 序列里的每个名字都必须真在渲染器里（阶段 B1 踩过：写了阶段 B 的名字，
#    阶段 A 时五章全掉 source_card）。新图换上来时，它替掉的那张旧图进
#    序列第一位——新图缺数据时退到"同一件事的另一种画法"，不是退到无关图。
ROUND_FALLBACKS: dict[str, tuple[str, ...]] = {
    "r1_hook": ("composition", "mindmap", "source_card"),
    "r2_mechanism": ("mindmap", "composition", "source_card"),
    "r3_moat_pressure": ("composition", "mindmap", "source_card"),
    "r4_ledger_test": ("profit_trend", "cashflow_statement", "balance_sheet",
                       "working_capital", "source_card"),
    "r5_open_verdict": ("composition", "source_card"),
}

# 章 → 素材类别对应的 material id。素材没抽到时该章退化到第一个可用素材，
# 章节本身不消失（五章是叙事结构，不是数据的函数）。
# 这一张表是**口播数字**的取材（R2 取产品线毛利率由它决定，阶段 A/B1 定过，
# 不要动）；画面要用的素材见 ROUND_VISUAL_BINDINGS。
ROUND_MATERIALS: dict[str, str] = {
    "product": "mat.product.0",
    "channel_region": "mat.channel.0",
    "competition": "mat.competition.0",
    "financial_proof": "mat.financial.0",
    "news_event": "mat.event.0",
}

# 章 → 图板上要绑哪些素材（mat 契约的 ``data_binding.material_ids``）。
# 与 ROUND_MATERIALS 分开是故意的：R2 口播仍取产品线毛利率（B1 定死），但
# 它这一章的画面要看到"厂商→渠道→客户"，所以额外绑渠道/地区素材；该素材
# 抽不到时 channel_flow 返回 None，按上面的序列退回 mindmap。
ROUND_VISUAL_BINDINGS: dict[str, tuple[str, ...]] = {
    "r1_hook": ("product",),
    "r2_mechanism": ("product", "channel_region"),
    "r3_moat_pressure": ("competition",),
    "r4_ledger_test": ("financial_proof",),
    "r5_open_verdict": ("news_event",),
}

# 主营构成里「渠道/地区」那几行：F10 把产品行、销售模式行、地区行混在同一张
# 明细表里，只能靠行名后缀区分（``直销(销售模式)`` / ``华东(地区)``）。
# 这是阶段 B2 唯一新接的源，和产品线同一套 source_refs 纪律。
# 口径定义在 ``business_rows``：``hyperframes_builder`` 的兜底渲染器也要用同一套，
# 两边各写一份迟早会漂（阶段 B3 已踩过一次）。
CHANNEL_ROW_SUFFIXES: tuple[str, ...] = _CHANNEL_ROW_SUFFIXES

# 主营构成里**不是产品线**的那几行的全部后缀。渠道/地区之外还有 ``(行业)``：
# F10 会把「汽车零部件(行业)」和真正的产品行挤在同一张表里，之前它被当成
# 产品线画上卡组，第一张卡就是行业名（2026-10-19 记的已知项）。
NON_PRODUCT_ROW_SUFFIXES: tuple[str, ...] = _NON_PRODUCT_ROW_SUFFIXES


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
    "r2_mechanism": (("bull", "链路拆解", "一瓶酒出厂以后要过三道手：经销商、批价、终端。每过一道，价格和风险就换个人扛。"),
                     ("bear", "追问", "四成三自己卖，那经销商还剩下什么用？"),
                     ("bull", "算账", "同一瓶酒，自己卖和给经销商卖，差的不是价格，是谁替它垫钱、谁替它压货。")),
    "r3_moat_pressure": (("bear", "替代场景", "能替掉它的不是别的酒，是“这顿饭不喝了”。"),
                         ("bull", "反向守", "请客不会消失，只会换地方：商务少了，宴席还在。"),
                         ("bear", "压力测试", "假设批价跌两成，经销商还会不会替它囤货？")),
    "r4_ledger_test": (("bear", "数字对撞", "毛利九成二，净利五成——中间那四成，谁拿走了？"),
                       ("bull", "反向解释", "被税拿走的那部分，是它替所有想进场的人先交的门票。"),
                       ("bear", "一句话钉", "毛利率是过去的价格权，净利率才是今天的成本权。")),
}
# 开场（R1）与收尾（R5）不再各带一组种子：它们由下面的 STORY_LINES 成对供给。
# 只给一节配种子、另一节按章轮换，就是"开头和结尾各讲一件事"——那正是
# 2026-10-19 用户反馈的「结尾太固定」的根：结尾从来没跟着开头走。

# 五条叙事线：**同一期只走一条线，开头与结尾同源**。
# 这是「开头不够理想、结尾太固定」的正面解法：
#   * 开头：五条线的开场动作互不相同（场景代入 / 顺着一只手问 / 反常识判断 /
#     把镜头拉远 / 从最近的一件事说起），而且**没有一条用会计科目、财务比率、
#     股价、报告期开场**——「合同负债从八十亿骤降到三十一亿」那种开场，是这条
#     视频最该消失的东西；
#   * 结尾：五条线收在不同地方（闭环回扣 / 一个能自己去看的动作 / 一句代价判断 /
#     只说处境不给结论 / 挂到一个能看见的落点），**没有一条是「等下季度报告
#     验证」**那种固定悬念——它以前不是收尾方式之一，是唯一方式；
#   * 配对：开头讲什么，结尾就回到什么。只换句式不换线的话，五期之后观众照样
#     能预判——"不固定"要靠整期换题，不是换措辞。
#   * 画面：末章图板随线走（回扣产品卡组 / 渠道链路 / 行业排名 / 事件线 /
#     收入构成），不再每期都是同一条事件时间线。
# 每条示范句里的公司名、产品名、数字仍然只借用修辞动作，prompt 会明说这一点。
STORY_LINES: tuple[dict[str, Any], ...] = (
    {"id": "thing",
     "label": "先把它看清楚：这东西到底是谁在买",
     "r1": ("bull", "场景代入",
            "先别急着看纸面：这个东西每天出现在谁的桌上、谁的货架上？先把它看清楚。"),
     "r5": ("bear", "闭环回扣",
            "回到开头那个东西——它今天还站在原来的位置上吗？这一期我们没能回答的就是这一点。"),
     "r5_template": "product_portfolio",
     "r5_materials": ("product",)},
    {"id": "hands",
     "label": "谁在替它把货送到你面前",
     "r1": ("bear", "顺着一只手问",
            "这东西到你手里之前，经过了几只手？每过一只手，它就换一次价、换一个人扛风险。"),
     "r5": ("bull", "给一个能自己去看的动作",
            "下次进店别先问价格，看它摆在货架第几层、有没有人在补货——那一眼比任何预测都准。"),
     "r5_template": "channel_flow",
     "r5_materials": ("product", "channel_region")},
    {"id": "price",
     "label": "它凭什么卖这个价",
     "r1": ("bull", "反常识判断",
            "同样的东西，换个包装、换个说法，价格能差好几倍——差的那截不是用料，是别人愿意为它多付的理由。"),
     "r5": ("bear", "一句代价判断",
            "守住这个价，得一直有人替它买单：场合在，价就在；场合换了人，价就得重新谈。"),
     "r5_template": "composition",
     "r5_materials": ("product",)},
    {"id": "crowd",
     "label": "这条街上还有多少人做同一件事",
     "r1": ("bear", "把镜头拉远",
            "别只盯着这一家：这条街上做同一件事的人有多少，决定了它日子能过多舒服。"),
     "r5": ("bull", "只说处境不给结论",
            "现在的处境是：人还是那些人，位置还是那个位置，但门口在换客人。往哪边走，这一期说不清。"),
     "r5_template": "ranking",
     "r5_materials": ("competition",)},
    {"id": "time",
     "label": "最近发生的这件事说明了什么",
     "r1": ("bull", "从最近的一件事说起",
            "最近这家公司出了一件事，它比任何纸面上的数字都更能说明接下来会怎样。"),
     "r5": ("bear", "挂到一个能看见的落点",
            "这件事的影响不会先写在纸面上，它会先出现在订单和排产里——那才是要盯的地方。"),
     "r5_template": "timeline",
     "r5_materials": ("news_event",)},
)

# 末章图板随叙事线走，所以后退序列也得按模板各配一份：末级一律 source_card，
# 序列里不许出现没注册的名字（阶段 B1 的坑）。
R5_FALLBACKS_BY_TEMPLATE: dict[str, tuple[str, ...]] = {
    "product_portfolio": ("composition", "mindmap", "source_card"),
    "channel_flow": ("mindmap", "composition", "source_card"),
    "ranking": ("composition", "mindmap", "source_card"),
    "timeline": ("composition", "source_card"),
    "composition": ("mindmap", "source_card"),
}

# 开头两句的禁用材质（2026-10-19 反馈「开头不够理想」的落地口径）。
# 这不是"不许提数字"，是"不许用看不见的东西开场"：会计科目、财务比率、股价、
# 报告期都在这里；产品名、渠道名、客户类型、一句生意的具体场景不在。
OPENING_BANNED_TERMS: tuple[str, ...] = (
    "合同负债", "预收款", "预收账款", "应收账款", "应付账款", "存货", "商誉",
    "货币资金", "经营现金流", "现金流", "归母净利润", "净利润", "毛利率", "净利率",
    "营业收入", "营收", "资产负债率", "市盈率", "市净率", "ROE", "PE", "PB",
    "股价", "收盘", "涨跌", "市值", "K线", "报告期", "年报", "季报", "半年报",
)

# 收尾的禁用句式（「结尾太固定」的落地口径）：核心是把"等下季度/下一份报告
# 来验证"这一类固定悬念判死——它以前是唯一收尾方式。
CLOSING_BANNED_PHRASES: tuple[str, ...] = (
    "下一季度", "下个季度", "下一份报告", "下一份年报", "下一期", "下次财报",
    "等这个数", "等数据出来", "年报见", "到时候再看",
)


def _rotate_seed(seeds: Sequence[tuple[str, str, str]], code: str, day: str, rid: str) -> int:
    """按 ``(code, day, rid)`` 稳定选出本章这一期的种子下标。

    同一只票同一天两次规划必须拿到同一组（可复现），换一天要换一组（不撞选题节奏），
    且每章**只发一条**——15 条一次全塞给模型等于没给，模型只会抄最近的几条。
    """
    if not seeds:
        return -1
    digest = hashlib.sha256(f"{code}|{day}|{rid}".encode("utf-8")).hexdigest()
    return int(digest, 16) % len(seeds)


def _rotate_line(code: str, day: str) -> int:
    """按 ``(code, day)`` 稳定选出这一期的叙事线下标。

    和 ``_rotate_seed`` 同一套哲学（同一天可复现、换一天换一条），但它是
    **整期**的维度：一条线同时决定开头和结尾，所以五条线轮完，同一只票的
    五期开头是五种不同的动作、结尾是五种不同的落点。
    """
    if not STORY_LINES:
        return -1
    digest = hashlib.sha256(f"{code}|{day}|storyline".encode("utf-8")).hexdigest()
    return int(digest, 16) % len(STORY_LINES)


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


def _verification_target(by_id: Mapping[str, Any]) -> str:
    """末章「待验证什么」：下一份报告里该盯的那条指标。

    只从**已抽到**的素材里挑（财务证据优先，其次是产品线），挑不出就只说
    「下一份报告」——宁可空着一个字也不编一条待验证项：编一个待验证项和编
    一个数字是同一条红线，而且它比数字更难被发现。
    """
    for material_id in ("mat.financial.0", "mat.product.0"):
        material = by_id.get(material_id)
        if not isinstance(material, Mapping):
            continue
        for metric in material.get("metrics") or []:
            if not isinstance(metric, Mapping):
                continue
            if metric.get("spoken_policy") not in ("anchor", "analogy"):
                continue
            label = str(metric.get("label") or "").strip()
            if label:
                return f"下一份报告里的{label}"
    return "下一份报告"


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
        product_rows: list[tuple[int, str, Mapping[str, Any]]] = []
        channel_rows: list[tuple[int, str, Mapping[str, Any]]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            name = str(row.get("项目") or row.get("名称") or row.get("业务") or "").strip()
            if not name:
                continue
            # 渠道/地区行（`(销售模式)` / `(地区)`）单独成一类素材：它们是"货怎么
            # 到客户手上"的那一层，和产品行不是同一件事，混在一起会让产品卡组上
            # 出现「直销」这种假产品线。
            if name.endswith(CHANNEL_ROW_SUFFIXES):
                channel_rows.append((index, name, row))
            elif name.endswith(NON_PRODUCT_ROW_SUFFIXES):
                # `(行业)` 行既不是产品线也不是渠道：F10 把它和产品行挤在同一张
                # 表里，当成产品线画上卡组，第一张卡就会是行业名（例如拓普的
                # 「汽车零部件」）——观众会以为那是它卖的东西。跳过不抽。
                continue
            else:
                product_rows.append((index, name, row))

        def _row_metrics(prefix: str, slot: int, source_index: int, row: Mapping[str, Any],
                         margin_label: str) -> list[dict[str, Any]]:
            """一条明细行的指标。``slot`` 是它在这类素材里的序号（``entities``
            与指标 id 共用，图上靠它把名称和数字对上），``source_index`` 是它在
            F10 明细表里的原始下标（``source_refs`` 用它指回那一行）——两者在有
            渠道行夹在产品行中间时并不相等，各归各的。
            """
            built: list[dict[str, Any]] = []
            # 口径与画面上的收入构成图一致：`收入占比(%)`/`毛利率(%)` 是 F10 原文字段。
            # 毛利率走 analogy：它是"每 100 元收入有 X 元是毛利"这种确定换算，
            # 只上屏不口播的话，R2「生意怎么转」就讲不出哪块最赚钱。
            # 标签必须和财务素材的公司级口径**字面就要分得开**：两条都叫「毛利率」时，
            # 模型会顺手把公司级的那条（visual_only）念出来。
            for key, label, unit, policy in (("收入占比(%)", "收入占比", "%", "anchor"),
                                             ("毛利率(%)", margin_label, "%", "analogy")):
                value = _number(row.get(key))
                if value is None:
                    continue
                built.append({"id": f"m.{prefix}.{slot}.{label}", "label": label, "value": value,
                              "unit": unit, "period": as_of, "spoken_policy": policy,
                              "source_refs": _ref(f"f10.profile.主营构成.明细[{source_index}].{key}", as_of)})
            return built

        items = [{"name": name,
                  "metrics": _row_metrics("product", slot, index, row, "产品线毛利率")}
                 for slot, (index, name, row) in enumerate(product_rows)]
        if items:
            materials.append({"id": "mat.product.0", "category": "product", "title": "主力产品线",
                              "fact": "、".join(x["name"] for x in items),
                              "entities": [{"name": x["name"], "role": "product"} for x in items],
                              "metrics": [m for x in items for m in x["metrics"]],
                              "source_refs": _ref("f10.profile.主营构成.明细", as_of),
                              "visual_candidates": ["product_portfolio", "composition", "mindmap"],
                              "availability": "ready"})

        # 1b. 渠道/地区：同一张明细表里的 `(销售模式)` / `(地区)` 行。
        # 与产品线同一套 source_refs 纪律——逐字段带来源，缺字段就是不抽这条指标，
        # 绝不用经营评述里的一句话补一条"渠道关系"。
        channel_items = [
            {"name": name, "role": "region" if name.endswith("(地区)") else "channel",
             "metrics": _row_metrics("channel", slot, index, row, "渠道毛利率")}
            for slot, (index, name, row) in enumerate(channel_rows)]
        if channel_items:
            materials.append({"id": "mat.channel.0", "category": "channel_region",
                              "title": "渠道与地区",
                              "fact": "、".join(x["name"] for x in channel_items),
                              "entities": [{"name": x["name"], "role": x["role"]}
                                           for x in channel_items],
                              "metrics": [m for x in channel_items for m in x["metrics"]],
                              "source_refs": _ref("f10.profile.主营构成.明细", as_of),
                              "visual_candidates": ["channel_flow", "composition"],
                              "availability": "ready"})

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
        # 本期的叙事线：它同时决定开头（R1）和结尾（R5），所以这两章不再各自
        # 按章轮换——分开轮换就是"开头讲一件事、结尾讲另一件事"。
        line_index = _rotate_line(code, as_of)
        line = STORY_LINES[line_index] if line_index >= 0 else None
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
            # 这一章一条能念的指标都没捞到时，审查页上必须看得出来：静默退化长得
            # 跟"这章本来就没数字"一模一样，等于悄悄少讲一层，事后查不出来。
            # 判空只能拿**能念的**指标算：素材里躺着一堆 visual_only 不等于有数字
            # 可讲（财务素材常常只有一条 visual_only 的毛利率）。判错了就把告警
            # 指向反的方向——该去补素材的人跑去调章节分配。
            speakable = [m for m in metrics if m.get("spoken_policy") in ("anchor", "analogy")]
            # 末章豁免：ROUND_NUMBER_POLICY 里 r5_open_verdict 是空的，它本来就不要求
            # 新数字这一层。只豁免"末章无新数字"这一种退化，**不覆盖前面的章**——
            # 同一份薄样本里 R3/R4 必须照报（见
            # EmptyPoolWarningTests.test_chapter_without_spoken_metric_is_recorded）。
            empty_pool = not (take_anchors or take_analogies) and bool(ROUND_NUMBER_POLICY.get(rid))
            if empty_pool:
                # 分两种：素材压根没有可念的指标，还是可念的指标被前面的章节取完了——
                # 前者要补素材，后者要调章节分配，处理办法不一样。
                warnings.append(f"{rid}:{'spoken_metrics_exhausted' if speakable else 'no_spoken_metric_source'}")
            # 图板上要用的素材：这一章的画面讲哪件事，就绑那件事的素材。
            # 口播素材（mids）和画面素材分开——R2 念的是产品线毛利率，画面画的
            # 是厂商→渠道→客户，两者不是同一份数据。末章的画面随叙事线走。
            visual_materials = ((line or {}).get("r5_materials") or ROUND_VISUAL_BINDINGS.get(rid, (category,))
                                if rid == "r5_open_verdict"
                                else ROUND_VISUAL_BINDINGS.get(rid, (category,)))
            bound_ids = [ROUND_MATERIALS[name] for name in visual_materials
                         if ROUND_MATERIALS.get(name) in by_id]
            bound_metric_ids = [str(m["id"])
                                for bound in bound_ids
                                for m in (by_id[bound].get("metrics") or [])
                                if isinstance(m, Mapping) and m.get("id")]
            # 末章的图板也随线走：回扣产品卡组 / 渠道链路 / 行业排名 / 事件线 /
            # 收入构成。每期都是同一条事件时间线，本身就是"结尾太固定"的一部分。
            template = ROUND_TEMPLATES[rid]
            fallbacks = ROUND_FALLBACKS[rid]
            if rid == "r5_open_verdict" and line:
                template = str(line.get("r5_template") or template)
                fallbacks = R5_FALLBACKS_BY_TEMPLATE.get(template, fallbacks)
            if rid in ("r1_hook", "r5_open_verdict"):
                # 开头与结尾的种子来自同一条线：这两章不按章轮换。
                seed = (line or {}).get("r1" if rid == "r1_hook" else "r5")
                if seed:
                    sentence_modes = [{"id": f"{rid}.{line['id']}",
                                       "speaker": seed[0], "mode": seed[1], "text": seed[2]}]
                else:
                    sentence_modes = []
            else:
                seeds = ROUND_SENTENCE_SEEDS.get(rid, ())
                seed_index = _rotate_seed(seeds, code, as_of, rid)
                sentence_modes = ([{"id": f"{rid}.seed{seed_index + 1}",
                                    "speaker": seeds[seed_index][0],
                                    "mode": seeds[seed_index][1],
                                    "text": seeds[seed_index][2]}]
                                  if seed_index >= 0 else [])
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
                                      "template": template,
                                      # mat/metric 契约：图只认这里绑好的素材与指标，
                                      # 画不出就整个退回 fallback（不许渲染器自己
                                      # 去 f10 里另找一张表——那等于绕开统筹）。
                                      "data_binding": {"material_ids": bound_ids,
                                                       "metric_ids": bound_metric_ids},
                                      "fallback": list(fallbacks),
                                      # 末章「待验证什么」：只挂在事件线上。别的收尾
                                      # 方式（回扣 / 观察动作 / 代价判断 / 处境描述）
                                      # 一律不挂——"每期都以一个待验证的数收尾"本身
                                      # 就是被嫌弃的那个固定模式。
                                      "verify": (_verification_target(by_id)
                                                 if rid == "r5_open_verdict" and template == "timeline"
                                                 else "")},
                           # 本章这一期要用的那条开法种子（每章一条，不把全部种子塞给模型）。
                           "sentence_modes": sentence_modes,
                           "transition": ""})

        missing = [m for m in materials if m.get("availability") == "missing"]
        quote = stock_data.get("quote") if isinstance(stock_data.get("quote"), Mapping) else {}
        return {
            "version": 1,
            "stock_code": str(stock_data.get("code") or quote.get("code") or ""),
            "as_of": as_of,
            # 本期走的叙事线：开头和结尾都由它决定，审查页要能看出这一期讲的是哪条线。
            "story_line": ({"id": line["id"], "label": line["label"]} if line else None),
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
