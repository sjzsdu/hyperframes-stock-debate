"""片尾风险提示交换轮（2026-10-08 第二版）：一人一句，按个股特征变化。

用户定调：①「股市有风险」「投资需谨慎」拆给两位角色一人一句，像对话收束
的最后一来一回，不像机器贴的免责声明；②绝对不能千篇一律——要根据个股的
特征（行业、主营业务、股名）做变化，消除 AI 味。

设计原则与骨架（arcs.py）一脉相承：

* 每条交换（``RiskExchange``）声明 ``needs`` 数据依赖（压平 payload 的点路径，
  复用 ``arcs.satisfies``）——**有数据才用这一条，没有就退回通用池**，不硬凑；
* 合格的有好几条时，按 (代码, 日期) **稳定分散**挑一条——同一只票同一天可复现，
  不同票、不同日子天然不撞形，不依赖任何跨运行状态；
* 两个锚点短语（``RISK_A``/``RISK_B``）是合规底线，句式再怎么变都必须原样保留；
* 全程确定性、不烧 LLM：风险提示一期都不能漏，不能把它押在模型自觉或网络请求上。

合规自查：模板禁用 买入/卖出/推荐/保证/肯定/一定/绝对/将会 等词
（与 compliance 的 DEFAULT_FORBIDDEN_WORDS / ABSOLUTE_PATTERNS 对齐），
上线前有断言兜底（tests/test_risk_exchange.py）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping

from stocktalk.modules.arcs import satisfies

# 合规锚点：这两句是片尾风险提示的底线，模板怎么变都不能丢。
RISK_A = "股市有风险"
RISK_B = "投资需谨慎"

# 交换轮的单句字数上限：超过就整条弃用（横版字幕一行 ≤22 字，留余量）。
MAX_LINE_CHARS = 20


@dataclass(frozen=True)
class RiskExchange:
    """一条风险提示交换：``a`` 先说（带「股市有风险」），``b`` 接（带「投资需谨慎」）。

    ``needs`` 与骨架节拍同构：压平 payload 的点路径，条目 AND、单条内 ``|`` OR。
    模板里的 ``{industry}`` ``{item}`` ``{name}`` 由个股数据填充；格式化后超长
    或占位符缺失就整条弃用，落到下一条合格候选。
    """

    id: str
    a: str
    b: str
    needs: tuple[str, ...] = ()


def _industry(payload: Mapping[str, Any]) -> str:
    """所属行业取细分类（"食品饮料-酿酒" → "酿酒"），太长的由长度护栏拦下。"""
    raw = payload.get("f10") if isinstance(payload.get("f10"), Mapping) else {}
    value = str(raw.get("所属行业") or "").strip()
    return value.split("-")[-1].strip() if value else ""


def _item(payload: Mapping[str, Any]) -> str:
    """主营业务构成里占比第一的业务名，去掉 "(产品)" 这类括注。"""
    f10 = payload.get("f10") if isinstance(payload.get("f10"), Mapping) else {}
    detail = f10.get("主营构成") if isinstance(f10.get("主营构成"), Mapping) else {}
    rows = detail.get("明细") if isinstance(detail.get("明细"), list) else []
    for row in rows:
        if isinstance(row, Mapping) and str(row.get("项目") or "").strip():
            return re.sub(r"[（(].*?[)）]", "", str(row["项目"])).strip()
    return ""


def _name(payload: Mapping[str, Any]) -> str:
    quote = payload.get("quote") if isinstance(payload.get("quote"), Mapping) else {}
    return str(quote.get("name") or "").strip()


# 候选池：条件命中的才有资格上场。通用三条（无 needs）永远兜底。
EXCHANGES: tuple[RiskExchange, ...] = (
    RiskExchange("industry", "聊完{industry}这门生意，股市有风险。",
                 "投资需谨慎，先看懂生意再谈其他。", needs=("f10.所属行业",)),
    RiskExchange("item", "{item}再能赚钱，股市有风险。",
                 "投资需谨慎，一期业绩说明不了一切。", needs=("f10.主营构成.明细",)),
    RiskExchange("name", "把{name}聊透了，股市有风险。",
                 "投资需谨慎，判断留给自己。", needs=("quote.name",)),
    RiskExchange("generic-signoff", "今天就聊到这里，股市有风险。",
                 "投资需谨慎，别急着替市场下结论。"),
    RiskExchange("generic-numbers", "数字讲完了，股市有风险。",
                 "投资需谨慎，钱包自己做主。"),
    RiskExchange("generic-stance", "观点摆完了，股市有风险。",
                 "投资需谨慎，生意比行情耐用。"),
)

# 模板占位符 → 特征提取函数。占位符取不到值（空串）就整条弃用。
TRAITS = (("industry", _industry), ("item", _item), ("name", _name))


def _format(exchange: RiskExchange, payload: Mapping[str, Any]) -> tuple[str, str] | None:
    """填占位符并做长度护栏；任何一步不满足就返回 None（这条候选出局）。"""
    values = {}
    for key, extractor in TRAITS:
        if f"{{{key}}}" in exchange.a or f"{{{key}}}" in exchange.b:
            value = extractor(payload)
            if not value:
                return None
            values[key] = value
    try:
        a, b = exchange.a.format(**values), exchange.b.format(**values)
    except (KeyError, IndexError, ValueError):
        return None
    if len(a) > MAX_LINE_CHARS or len(b) > MAX_LINE_CHARS:
        return None
    if RISK_A not in a or RISK_B not in b:
        return None
    return a, b


def build(payload: Mapping[str, Any], *, code: str = "", day: str = "") -> dict[str, str]:
    """挑一条合格的风险提示交换，返回 ``{"id", "a", "b"}``。

    资格 = 数据依赖全中 + 占位符填得上 + 长度合规；合格的多条按
    (代码, 日期) 稳定挑一条。**特征模板加权 ×2**：用户要求「根据个股的特征做
    变化」，行业/主营/股名模板够格时应该占主导，通用池只做调剂和兜底。
    """
    candidates: list[tuple[RiskExchange, str, str]] = []
    for exchange in EXCHANGES:
        if not satisfies(payload, exchange.needs):
            continue
        formatted = _format(exchange, payload)
        if formatted is not None:
            candidates.append((exchange, *formatted))
    if not candidates:  # 理论上到不了（通用池无依赖），防御性兜底
        fallback = EXCHANGES[-1]
        candidates.append((fallback, fallback.a, fallback.b))
    trait_hits = [item for item in candidates if item[0].needs]
    generic_hits = [item for item in candidates if not item[0].needs]
    # 特征模板双倍权重：够格时 2/3 概率落在带个股特征的句子上
    pool = sorted(trait_hits * 2 + generic_hits, key=lambda item: (item[0].id, item[1]))
    digest = hashlib.sha1(f"{code}:{day}".encode("utf-8")).hexdigest()
    exchange, a, b = pool[int(digest, 16) % len(pool)]
    return {"id": exchange.id, "a": a, "b": b}
