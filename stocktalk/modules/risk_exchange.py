"""片尾风险提示轮（2026-11-15 第三版）：一轮一句，让位内容收束。

版本沿革：

* 2026-10-08 第二版「一人一句接力」两轮制；2026-10-27 WAN-27 重写句池。
* 2026-11-15 用户反馈「听多了有点反感这个结尾」——根因是**形态恒定**：每期
  片尾永远是「XX，股市有风险」→「投资需谨慎，XX」两轮接力，80 期后这个
  形态本身成了条件反射式反感源；且风险轮永远占「最后一句话」，把内容收束
  （closing 短回应）的余味浇灭，结尾的情绪价值被合规播音占据。

第三版改三件事（合规零风险：上屏三处——outro 卡 ``.outro-risk``、CTA 图板
脚注——是模板硬编码，不依赖口播轮）：

1. **两轮合一**：两个锚点并进一句话、一位角色说完——时长减半，形态不再是
   「接力」套路；
2. **让位收尾**：风险轮插到最后一轮（内容收束）**之前**——「丑话说在前头」，
   节目的最后一句话还给观点，不是免责声明；
3. **句式翻新**：「账/数字/分歧摆在这儿」三兄弟退役（10-27 重写产物，20 天
   就被听腻），generic 池改「递棒」语义——风险轮说完把话头递给下一位下结论。

设计原则与骨架（arcs.py）一脉相承：

* 每条（``RiskExchange``）声明 ``needs`` 数据依赖（压平 payload 的点路径，
  复用 ``arcs.satisfies``）——**有数据才用这一条，没有就退回通用池**，不硬凑；
* 合格的有好几条时，按 (代码, 日期) **稳定分散**挑一条——同一只票同一天可复现，
  不同票、不同日子天然不撞形，不依赖任何跨运行状态；
* 两个锚点短语（``RISK_A``/``RISK_B``）是合规底线，句式再怎么变都必须原样保留
  （同一轮里先后出现）；
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

from stocktalk.modules import copy_guard
from stocktalk.modules.arcs import satisfies

# 合规锚点：这两句是片尾风险提示的底线，模板怎么变都不能丢。
RISK_A = "股市有风险"
RISK_B = "投资需谨慎"

# 一轮制单句字数上限：一句话要装「特征前缀 + 两个锚点」，比旧 a/b 各一句长，
# 放宽到横版字幕一行的硬顶 22 字；超过就整条弃用。
MAX_LINE_CHARS = 22


@dataclass(frozen=True)
class RiskExchange:
    """一条风险提示：``line`` 一轮说完（含「股市有风险」「投资需谨慎」两锚点）。

    ``needs`` 与骨架节拍同构：压平 payload 的点路径，条目 AND、单条内 ``|`` OR。
    模板里的 ``{industry}`` ``{item}`` ``{name}`` 由个股数据填充；格式化后超长
    或占位符缺失就整条弃用，落到下一条合格候选。
    """

    id: str
    line: str
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
#
# 句式设计（2026-11-15 第三版）：风险轮插在内容收束之前，语义是「丑话说在
# 前头、把话头递给对方下结论」——前缀接住论证（账/数字/分歧/行当），尾巴
# 两个锚点，说完自然让位。旧的「X摆在这儿」族（账/数字/分歧摆在这儿）已列
# 进 tests/test_risk_exchange.py 的回归哨兵，别复辟。
# 新增模板必须过 copy_guard 的闸门（这里 + tests/test_risk_exchange.py 双层）。
EXCHANGES: tuple[RiskExchange, ...] = (
    RiskExchange("industry", "{industry}水深，股市有风险，投资需谨慎。",
                 needs=("f10.所属行业",)),
    RiskExchange("item", "{item}难做，股市有风险，投资需谨慎。",
                 needs=("f10.主营构成.明细",)),
    RiskExchange("name", "{name}这张账，股市有风险，投资需谨慎。",
                 needs=("quote.name",)),
    RiskExchange("generic-ledger", "丑话说在前头，股市有风险，投资需谨慎。"),
    RiskExchange("generic-numbers", "数字是死的，股市有风险，投资需谨慎。"),
    RiskExchange("generic-fork", "分歧慢慢消化，股市有风险，投资需谨慎。"),
)

# 模板占位符 → 特征提取函数。占位符取不到值（空串）就整条弃用。
TRAITS = (("industry", _industry), ("item", _item), ("name", _name))


def _format(exchange: RiskExchange, payload: Mapping[str, Any]) -> str | None:
    """填占位符并做长度护栏；任何一步不满足就返回 None（这条候选出局）。"""
    values = {}
    for key, extractor in TRAITS:
        if f"{{{key}}}" in exchange.line:
            value = extractor(payload)
            if not value:
                return None
            values[key] = value
    try:
        line = exchange.line.format(**values)
    except (KeyError, IndexError, ValueError):
        return None
    if len(line) > MAX_LINE_CHARS:
        return None
    if RISK_A not in line or RISK_B not in line:
        return None
    # 套话闸门（2026-10-27 WAN-27）：模板池是"每期都说一遍"的地方，一句话写错
    # 就是整季复读。命中就整条弃用，落到下一条候选——成片先安全，问题由
    # tests/test_risk_exchange.py 报出来，让人去改模板而不是让观众听出来。
    if copy_guard.source_violations(line):
        return None
    return line


def build(payload: Mapping[str, Any], *, code: str = "", day: str = "") -> dict[str, str]:
    """挑一条合格的风险提示，返回 ``{"id", "line"}``。

    资格 = 数据依赖全中 + 占位符填得上 + 长度合规；合格的多条按
    (代码, 日期) 稳定挑一条。**特征模板加权 ×2**：用户要求「根据个股的特征做
    变化」，行业/主营/股名模板够格时应该占主导，通用池只做调剂和兜底。
    """
    candidates: list[tuple[RiskExchange, str]] = []
    for exchange in EXCHANGES:
        if not satisfies(payload, exchange.needs):
            continue
        formatted = _format(exchange, payload)
        if formatted is not None:
            candidates.append((exchange, formatted))
    if not candidates:  # 理论上到不了（通用池无依赖），防御性兜底
        fallback = EXCHANGES[-1]
        candidates.append((fallback, fallback.line))
    trait_hits = [item for item in candidates if item[0].needs]
    generic_hits = [item for item in candidates if not item[0].needs]
    # 特征模板双倍权重：够格时 2/3 概率落在带个股特征的句子上
    pool = sorted(trait_hits * 2 + generic_hits, key=lambda item: (item[0].id, item[1]))
    digest = hashlib.sha1(f"{code}:{day}".encode("utf-8")).hexdigest()
    exchange, line = pool[int(digest, 16) % len(pool)]
    return {"id": exchange.id, "line": line}
