"""剧本骨架（narrative arc）：把"这期怎么讲"从 prompt 字符串里搬出来变成数据。

背景（2026-09-22 用户反馈与评审）：原先把六层主线写死在
``dialogue_generator._user_prompt`` 的字符串里，结果是每一期视频的节拍序列、
开场句式、收尾方式都完全一样——看三就疲。改法是让"骨架"成为一种可注册的数据：

* ``Beat`` —— 一个叙事节拍：讲什么（intent）、谁来讲（speaker）、需要哪些数据（needs）。
* ``Arc``  —— 一条骨架：若干 Beat 的顺序，外加收紧开场套话的要求。
* ``select_arc`` —— **按这只票实际拿得到的数据挑骨架**：有数据才加这一段，没有就不加，
  不硬凑、也不用随机数凑花样（用户 2026-09-22 明确要求：骨架依据个股的数据内容来定）。

选中的骨架会写进脚本（``script["arc"]``），所以"这期为什么长这样"是可追溯的，
审查页也能显示出来。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Beat:
    """一个叙事节拍。

    ``needs`` 是数据依赖，写成"压平后 payload 的点路径"，一条内用 ``|`` 表示
    「任一满足即可」，条目之间是「都要满足」。缺数据的节拍会被整段剪掉
    （``prune_beats``）——这就是"有就加，没有数据就不要"的落地点。
    """

    key: str
    intent: str
    speaker: str | None = None
    needs: tuple[str, ...] = ()
    # 短回应节拍：允许只有几个字（"嗯""有道理，但是…"），画面不换图板。
    # 用户 2026-09-22："对话有长有短，有时候只是说一个'嗯'，对对方观点的认可而已"。
    short: bool = False
    max_chars: int | None = None


@dataclass(frozen=True)
class Arc:
    """一条骨架：节拍的顺序，加上开场约束。"""

    id: str
    name: str
    summary: str
    beats: tuple[Beat, ...]
    # 开场额外约束（叠加在通用反套话规则之上）
    opener: str = ""
    # 剪枝后至少剩这么多节拍，这条骨架才还算成立
    min_beats: int = 3


# ---------------------------------------------------------------------------
# 骨架注册表
# ---------------------------------------------------------------------------

# 六层主线：改动前唯一的那条线，保留为**回退**（数据太薄、或没有骨架可用时走它）。
# 保持 6 个节拍不变，这样默认配置下的轮数区间与改动前一致。
MAINLINE = Arc(
    id="mainline",
    name="六层主线",
    summary="生意本质→行业位置→财务印证→市场预期→风险→小结，最稳的兜底结构",
    beats=(
        Beat("business", "先说清这家公司卖什么、靠什么赚钱、客户是谁，用主营构成里的真实占比和毛利率作为骨架"),
        Beat("industry", "讲它在所处行业里的位置、竞争格局、行业天花板与政策环境", speaker="bear"),
        Beat("financial", "用营收/利润/毛利率/ROE 等真实数字印证或推翻前面的生意判断，把数字翻译成生意含义"),
        Beat("risk", "把风险与不确定性落到某个具体环节（存货、应收、外销依赖、产能爬坡等）", speaker="bear"),
        Beat("valuation", "只讲市场预期与估值处在什么位置的事实，不做买卖判断"),
        Beat("wrap", "对这门生意做一次小结式收束，不要戛然而止", speaker="bull"),
    ),
)

# 裂痕式：反常识数字开场，冲突最强，适合抢前 3 秒。
CRACK = Arc(
    id="crack",
    name="裂痕式",
    summary="反常识数字开场→追问代价→数据核对→极短认可→留下分歧",
    opener="第一句必须直接抛出一个反直觉的真实数字或事实（例如毛利率远高于/低于同行、"
           "九成收入来自单一产品），不要任何铺垫、不要出处介绍、不要'咱们先看/先聊聊'。",
    beats=(
        Beat("odd", "第一句就把一个反直觉的真实数字摆出来，让听的人先愣一下；只给数字和它的反差，不给结论",
             speaker="bull", needs=("f10.主营构成.明细|financials.净利润(亿元)|f10.行业地位.同行家数",)),
        Beat("challenge", "追问这个数字背后的代价或代价来源：它是怎么来的、要付出什么", speaker="bear"),
        Beat("books", "用另一组真实财务数据核对，被说服就当场修正自己前面的说法，不要嘴硬",
             speaker="bull", needs=("financials.营业收入(亿元)|financials.经营现金流(亿元)",)),
        Beat("concede", "极短回应：一句认可、或一句更硬的追问，允许只有几个字", speaker="bear",
             short=True, max_chars=20),
        Beat("fork", "把两个人在哪里仍然不一致讲清楚——不劝和、不给结论，把分歧留给观众"),
    ),
    min_beats=3,
)

# 客户视角：全程不碰行情，最贴近日常认知。
CUSTOMER = Arc(
    id="customer",
    name="客户视角",
    summary="谁在买→为什么买→会不会续买→谁能替代→生意小结",
    opener="从'谁在掏钱买'这个角度切入，不要先报股价、不要先讲行业分类。",
    beats=(
        Beat("who", "说清客户到底是谁（经销还是直销、国内还是海外、哪类终端），用主营构成里的地区/销售模式占比说话",
             speaker="bull", needs=("f10.主营构成.明细|board.concepts|f10.题材标签.概念",)),
        Beat("why", "解释客户为什么选它而不是别家：公司自述的理由可以直接引，但要说明这是公司的说法",
             speaker="bear", needs=("f10.经营评述|f10.题材标签.概念",)),
        Beat("again", "客户会不会继续买——看需求、提价能力、渠道库存这些能被验证的环节",
             speaker="bull", needs=("f10.经营评述|f10.所属行业",)),
        Beat("swap", "谁可能把客户抢走：同类产品、替代品、客户自建，用真实数据说", speaker="bear",
             needs=("f10.行业地位.同行家数|board.industry",)),
        Beat("wrap", "收束：这门生意对客户的不可替代性到底有多强"),
    ),
    min_beats=3,
)

# 年报逐条：最硬核，适合 B 站与长片。
ANNUAL = Arc(
    id="annual",
    name="年报逐条",
    summary="收入结构→毛利来源→现金流质量→钱压在哪→隐患",
    opener="像拆年报一样逐条来，每条都先给数字再给含义，不要泛泛而谈地形容。",
    beats=(
        Beat("revenue", "拆收入结构：哪块业务撑起收入、各占多少，用主营构成的真实占比",
             speaker="bull", needs=("f10.主营构成.明细",)),
        Beat("margin", "拆毛利来源：哪块业务最赚钱、毛利率差距有多大，注意口径要与主营构成一致",
             speaker="bear", needs=("f10.主营构成.明细",)),
        Beat("cash", "现金流质量：经营现金流与净利润的比对，利润是不是真金白银落袋",
             speaker="bull", needs=("financials.经营现金流(亿元)|financials.净利润(亿元)",)),
        Beat("occupied", "钱压在哪：存货与应收账款各相当于多少营收，货有没有变成钱",
             speaker="bear", needs=("financials.存货(亿元)|financials.应收账款(亿元)",)),
        Beat("shadow", "隐患：这条账里最容易被忽略的一处，明明数据摆着但容易看漏"),
    ),
    min_beats=3,
)

# 同行对比：用 F10 行业分析的排名与同行家数做锚。
PEERS = Arc(
    id="peers",
    name="同行对比",
    summary="同行家数→规模位置→毛利差距→排名证据→小结",
    opener="开场就点出它在同行里排第几、和谁比，把对比关系先立起来。",
    beats=(
        Beat("peers_count", "先把同行家数和研究行业报出来，给观众一个尺度感",
             speaker="bear", needs=("f10.行业地位.同行家数",)),
        Beat("scale", "同一门生意里它的规模排在哪一档：总市值、收入体量与同行比",
             speaker="bull", needs=("stockinfo.总市值(亿元)|stockinfo.市值(亿元)",)),
        Beat("margin_gap", "毛利差距：它的毛利率与主营构成里其他业务/同业的差距意味着什么",
             speaker="bear", needs=("f10.主营构成.明细",)),
        Beat("rank", "用 F10 行业分析里的真实排名做证据（市场表现/公司规模/估值水平/财务状况），"
                     "没上榜的维度就说没上榜，不猜",
             speaker="bull", needs=("f10.行业地位.市场表现|f10.行业地位.公司规模|f10.行业地位.估值水平|f10.行业地位.财务状况",)),
        Beat("wrap", "收束：它在同行里的位置是护城河还是天花板"),
    ),
    min_beats=3,
)

ARCS: dict[str, Arc] = {arc.id: arc for arc in (MAINLINE, CRACK, CUSTOMER, ANNUAL, PEERS)}

# 回退骨架：数据太薄、或配置写错时走它。
ARC_FALLBACK: Arc = MAINLINE

# 开箱即用的说话角色。配置里可以再加角色，但这两个始终允许——配置只覆盖其中一个
# （比如只改 bull 的人设）时，不该把另一个角色判为非法。
DEFAULT_SPEAKERS: tuple[str, ...] = ("bull", "bear")


def resolve_speakers(characters: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """配置里声明的角色 ∪ 默认两名角色，顺序稳定。"""
    declared = tuple(str(key) for key in (characters or {}))
    return tuple(dict.fromkeys((*DEFAULT_SPEAKERS, *declared)))


# ---------------------------------------------------------------------------
# 数据可用性判定
# ---------------------------------------------------------------------------

def _present(value: Any) -> bool:
    """数据点是否存在且非空。0 与 False 视为存在（数字本身就是信息）。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple, set)):
        return len(value) > 0
    return True


def has_path(payload: Mapping[str, Any], path: str) -> bool:
    """按点路径检查 payload 里有没有这份数据。

    路径按"压平后的 payload"书写（就是 prompt 里真正看到的那份），所以
    ``f10`` 下面直接是解析后的档案字段，而不是 F10 原始分块。
    """
    node: Any = payload
    for segment in str(path).split("."):
        if not segment:
            continue
        if not isinstance(node, Mapping) or segment not in node:
            return False
        node = node[segment]
    return _present(node)


def satisfies(payload: Mapping[str, Any], needs: Sequence[str]) -> bool:
    """``needs`` 条目之间是 AND，单条内用 ``|`` 分隔表示 OR。"""
    for group in needs:
        alternatives = [part.strip() for part in str(group).split("|") if part.strip()]
        if alternatives and not any(has_path(payload, part) for part in alternatives):
            return False
    return True


def prune_beats(arc: Arc, payload: Mapping[str, Any]) -> tuple[Beat, ...]:
    """剪掉这份数据撑不起来的节拍——"有就加，没有数据就不要"。"""
    return tuple(beat for beat in arc.beats if satisfies(payload, beat.needs))


def missing_needs(arc: Arc, payload: Mapping[str, Any]) -> tuple[str, ...]:
    """这条骨架在这份数据下缺了哪些依赖（用于日志与审查，便于事后解释选择）。"""
    return tuple(group for beat in arc.beats for group in beat.needs
                 if not satisfies(payload, (group,)))


# ---------------------------------------------------------------------------
# 选择
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArcChoice:
    """一次骨架选择的结果，连同它是怎么被选出来的。"""

    arc: Arc
    beats: tuple[Beat, ...]
    eligible: tuple[str, ...] = ()
    skipped: tuple[tuple[str, tuple[str, ...]], ...] = ()
    forced: bool = False

    @property
    def id(self) -> str:
        return self.arc.id

    def report(self) -> dict[str, Any]:
        """写进脚本的审计信息：这期用了哪条骨架、为什么没用别的。"""
        return {
            "id": self.arc.id,
            "name": self.arc.name,
            "forced": self.forced,
            "beats": [beat.key for beat in self.beats],
            "pruned": [beat.key for beat in self.arc.beats if beat not in self.beats],
            "eligible": list(self.eligible),
            "skipped": {arc_id: list(needs) for arc_id, needs in self.skipped},
        }


def _stable_pick(candidates: Sequence[Arc], code: str, day: str) -> Arc:
    """在同样合格的骨架里按 (代码, 日期) 稳定地挑一条。

    同一只票同一天结果稳定（可复现、不会跑两次两个样），不同票之间分散开来
    ——这样"每期不一样"不依赖任何跨运行状态。
    """
    ordered = sorted(candidates, key=lambda arc: arc.id)
    digest = hashlib.sha1(f"{code}:{day}".encode("utf-8")).hexdigest()
    return ordered[int(digest, 16) % len(ordered)]


def select_arc(payload: Mapping[str, Any], *, configured: str = "auto",
               code: str = "", day: str = "") -> ArcChoice:
    """挑一条骨架。

    * ``configured`` 是注册表里的 id 时强制用它（仍然按数据剪枝）；
    * ``configured == "auto"`` 时按数据选：**只有"数据全都能支撑"的骨架才够格**
      ——每个声明了依赖的节拍都必须有数据，且剪枝后还剩够多节拍。够格的有好几条时，
      按 (代码, 日期) 稳定分散，于是"每期不一样"不依赖任何跨运行状态。
    * 没有一条够格就回退六层主线。

    注意"够格"必须要求依赖全中，而不只是"剪枝后还剩几拍"：裂痕式里有三个节拍不依赖
    数据（追问/极短回应/留下分歧），如果只看剩余节拍数，一份只有行情数据、连主营构成
    都没有的 payload 也会被判定为"适合裂痕式"，然后丢掉它最关键的"反常识数字开场"。
    这不是"有数据就加"，是硬凑。
    """
    requested = str(configured or "auto").strip()
    if requested and requested != "auto" and requested in ARCS:
        arc = ARCS[requested]
        beats = prune_beats(arc, payload)
        if not beats:
            beats = ARC_FALLBACK.beats
            return ArcChoice(arc=ARC_FALLBACK, beats=beats, forced=True)
        return ArcChoice(arc=arc, beats=beats, forced=True)

    qualified: list[Arc] = []
    skipped: list[tuple[str, tuple[str, ...]]] = []
    for arc in ARCS.values():
        if arc.id == ARC_FALLBACK.id:
            continue  # 回退骨架不参与竞争，只在没人够格时上场
        beats = prune_beats(arc, payload)
        missing = missing_needs(arc, payload)
        if missing or len(beats) < arc.min_beats:
            skipped.append((arc.id, missing or ()))
            continue
        qualified.append(arc)

    if not qualified:
        beats = prune_beats(ARC_FALLBACK, payload)
        return ArcChoice(arc=ARC_FALLBACK, beats=beats or ARC_FALLBACK.beats,
                         eligible=(), skipped=tuple(skipped))

    picked = qualified[0] if len(qualified) == 1 else _stable_pick(qualified, code, day)
    return ArcChoice(arc=picked, beats=picked.beats,
                     eligible=tuple(arc.id for arc in qualified), skipped=tuple(skipped))


def beats_by_key(beats: Sequence[Beat]) -> tuple[str, ...]:
    return tuple(beat.key for beat in beats)
