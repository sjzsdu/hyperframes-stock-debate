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
    # 可选节拍（2026-10-08）：有 needs 但这份数据给不起时，只把这一拍剪掉，
    # **不**让整条骨架失去资格（missing_needs 会跳过它）。资讯拍用这个语义：
    # 用户选题时几乎总带着新闻，但不能因为某天恰好没抓到资讯就把好骨架全废掉。
    optional: bool = False


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
    # 内容形态（2026-10-24 独白扩展）："debate" 双人对辩 / "monologue" 单人独白。
    # 骨架是"这期怎么讲"的节拍序列，而节拍里写死了谁来说——五条对辩骨架的
    # closing 拍全是"由对方接一句"，拿去喂单人片必然穿帮。所以两条形态各走各的
    # 注册表（见 ARCS / MONOLOGUE_ARCS），而不是给对辩骨架加 if。
    format: str = "debate"


# ---------------------------------------------------------------------------
# 骨架注册表
# ---------------------------------------------------------------------------

# 资讯拍（2026-10-08 用户反馈）：用户选题时几乎总挑「当下有新闻的票」，观众也是
# 因为这条新闻点进来的——所以每条骨架都要有一块**正面响应最新资讯**的内容，
# 而不是像以前那样把 news 当可有可无的佐证素材。位置统一放在收束之前：
# 先把生意讲明白，再用最新消息检验前面的判断（印证/矛盾/只是交易异动），
# 观众看完正好知道「今天这条新闻对这门生意意味着什么」。
# optional=True：某天真没抓到资讯就整拍剪掉，不硬凑、也不连累骨架资格。
NEWS_BEAT = Beat(
    "news",
    "正面响应最新资讯：从 news.items 里挑最新、与这门生意最相关的一条，"
    "说清日期与信息类型（公告/研报/快讯/龙虎榜），再把它和前面聊的生意判断连线——"
    "印证了什么、矛盾在哪、或说明这只是交易层面的异动；"
    "只复述标题与摘要里已有的信息，不脑补细节、不据此预测股价",
    needs=("news.items",),
    optional=True,
)

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
        NEWS_BEAT,
        Beat("wrap", "对这门生意做一次小结式收束，顺手把本期最值得观众带走的一两个认知点"
                     "（行业门道、看财报的方法或公司价值逻辑）用一句话钉住，不要戛然而止", speaker="bull"),
        Beat("closing", "对方接一句短回应收束：一句认可、一句保留，或把最关键的分歧钉一次；禁止一方独白结束",
             speaker="bear", short=True, max_chars=24),
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
        NEWS_BEAT,
        Beat("closing", "对方接一句短回应收束：若上一拍是 bull 长篇，由 bear 接（反之亦然）；"
                        "一句认可、一句保留，或把分歧钉一次；禁止一方独白结束",
             short=True, max_chars=24),
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
        NEWS_BEAT,
        Beat("wrap", "收束：这门生意对客户的不可替代性到底有多强，把本期最值得带走的认知点一句话钉住"),
        Beat("closing", "对方接一句短回应收束：若上一拍是 bull 长篇，由 bear 接（反之亦然）；"
                        "一句认可、一句保留，或把分歧钉一次；禁止一方独白结束",
             short=True, max_chars=24),
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
        NEWS_BEAT,
        Beat("closing", "对方接一句短回应收束：若上一拍是 bull 长篇，由 bear 接（反之亦然）；"
                        "一句认可、一句保留，或把分歧钉一次；禁止一方独白结束",
             short=True, max_chars=24),
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
        NEWS_BEAT,
        Beat("wrap", "收束：它在同行里的位置是护城河还是天花板，把本期最值得带走的认知点一句话钉住"),
        Beat("closing", "对方接一句短回应收束：若上一拍是 bull 长篇，由 bear 接（反之亦然）；"
                        "一句认可、一句保留，或把分歧钉一次；禁止一方独白结束",
             short=True, max_chars=24),
    ),
    min_beats=3,
)

ARCS: dict[str, Arc] = {arc.id: arc for arc in (MAINLINE, CRACK, CUSTOMER, ANNUAL, PEERS)}

# 回退骨架：数据太薄、或配置写错时走它。
ARC_FALLBACK: Arc = MAINLINE

# ---------------------------------------------------------------------------
# 独白骨架（2026-10-24）：一个人把一家公司讲透，2-4 分钟。
# ---------------------------------------------------------------------------
# 与对辩骨架的两条结构性差别，注册时就要定死，不能靠运行时补丁：
# 1. **节拍不指派说话人**。Beat.speaker 一律留空（"谁来说都行"在单人形态下
#    没有意义，prompt 侧也不渲染这一句），最终台词的 speaker 统一由
#    dialogue_generator 归一成 narrator。
# 2. **没有"由对方接一句"的 closing 拍**。对辩五条骨架的收尾全靠对方短回应
#    立住"这是一场对话"；独白没有对方，收束要落在一个观众能自己去核对的
#    具体变量上（对应 episode_plan 的 r5_open_verdict）。
# 剪枝与数据依赖（needs / optional）机制完全复用——"有数据才讲，没有就不讲"
# 对独白片比对辩更重要：讲透一家公司靠的是叙事密度，不是篇幅。
DEEP_DIVE = Arc(
    id="deep_dive",
    name="深度拆解",
    summary="这家公司做什么→钱从哪来→账怎么样→护城河与风险→带走什么",
    format="monologue",
    opener="第一句从一个看得见的东西起头（产品、渠道、客户类型、一个具体场景），"
           "不要行业定性开场（「这是一家做XX的公司」）、不要报股价；"
           "前两句里埋一个信息缺口，让人想听下去。",
    beats=(
        Beat("hook", "用一个看得见的具体事实或反差开场，让观众先愣一下；"
                     "只给事实和它的反差，不给结论、不做自我介绍"),
        Beat("business", "说清这家公司卖什么、靠什么赚钱、客户是谁，"
                         "用主营构成里的真实占比和毛利率当骨架"),
        Beat("money", "钱从哪来：这门生意的钱是怎么流转的——谁先掏钱、"
                      "货怎么变成钱、直销和经销各占多少，用真实结构数据讲清运转机制",
             needs=("f10.主营构成.明细|f10.经营评述",)),
        Beat("ledger", "账怎么样：把营收/利润和经营现金流对着看，"
                       "存货与应收各压着多少营收，利润是不是真金白银落袋",
             needs=("financials.净利润(亿元)|financials.经营现金流(亿元)|financials.存货(亿元)|financials.应收账款(亿元)",)),
        Beat("moat", "护城河与风险落在具体环节：同行家数、行业位置、"
                     "替代者、单一客户/单一品类依赖……讲清它凭什么站稳、短板在哪",
             needs=("f10.行业地位.同行家数|board.industry|f10.所属行业|financials.资产负债率(%)")),
        NEWS_BEAT,
        Beat("takeaway", "收束：把本期最值得带走的一两个可复用认知"
                         "（行业门道/看财报的方法/这门生意好在哪脆弱在哪）钉住，"
                         "并落在一个观众能自己去核对的具体变量上；不要戛然而止，"
                         "也不要「等下一份财报来验证」式的空悬念"),
    ),
    min_beats=4,
)

MONOLOGUE_ARCS: dict[str, Arc] = {DEEP_DIVE.id: DEEP_DIVE}
MONOLOGUE_FALLBACK: Arc = DEEP_DIVE

# 独白形态唯一的说话人代号。写死它而不是"从配置里推"：脚本合同
# （``turn["speaker"]``）要跨 TTS / 画面 / 合规 / 发布四层保持同一个 id，
# 而 ``characters.narrator.name`` 是会被人改的展示名——id 稳定、名字可换。
MONOLOGUE_SPEAKER = "narrator"


def arcs_for_format(fmt: str | None = None) -> dict[str, Arc]:
    """按内容形态取骨架注册表。未知形态一律回对辩——不猜、不抛。"""
    return MONOLOGUE_ARCS if str(fmt or "").strip().lower() == "monologue" else ARCS


def fallback_arc(fmt: str | None = None) -> Arc:
    return MONOLOGUE_FALLBACK if str(fmt or "").strip().lower() == "monologue" else ARC_FALLBACK

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
    """这条骨架在这份数据下缺了哪些依赖（用于日志与审查，便于事后解释选择）。

    optional 拍（资讯拍）的缺数据不算在内：它缺席只影响自己被剪掉，
    不该把整条骨架判为不够格——否则某天没抓到资讯，好骨架会全体落选。
    """
    return tuple(group for beat in arc.beats if not beat.optional
                 for group in beat.needs if not satisfies(payload, (group,)))


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
               code: str = "", day: str = "", format: str | None = None) -> ArcChoice:
    """挑一条骨架。

    * ``configured`` 是注册表里的 id 时强制用它（仍然按数据剪枝）；
    * ``configured == "auto"`` 时按数据选：**只有"数据全都能支撑"的骨架才够格**
      ——每个声明了依赖的节拍都必须有数据，且剪枝后还剩够多节拍。够格的有好几条时，
      按 (代码, 日期) 稳定分散，于是"每期不一样"不依赖任何跨运行状态。
    * 没有一条够格就回退六层主线（独白形态回退 deep_dive）。

    ``format`` 决定从哪个注册表里挑（debate / monologue）：两条形态的骨架
    互不参与竞争——对辩骨架的 closing 拍写死了"由对方接一句"，混进独白的
    候选池就是一次静默穿帮。

    注意"够格"必须要求依赖全中，而不只是"剪枝后还剩几拍"：裂痕式里有三个节拍不依赖
    数据（追问/极短回应/留下分歧），如果只看剩余节拍数，一份只有行情数据、连主营构成
    都没有的 payload 也会被判定为"适合裂痕式"，然后丢掉它最关键的"反常识数字开场"。
    这不是"有数据就加"，是硬凑。
    """
    registry = arcs_for_format(format)
    fallback = fallback_arc(format)
    requested = str(configured or "auto").strip()
    if requested and requested != "auto" and requested in registry:
        arc = registry[requested]
        beats = prune_beats(arc, payload)
        if not beats:
            beats = fallback.beats
            return ArcChoice(arc=fallback, beats=beats, forced=True)
        return ArcChoice(arc=arc, beats=beats, forced=True)

    qualified: list[Arc] = []
    skipped: list[tuple[str, tuple[str, ...]]] = []
    for arc in registry.values():
        if arc.id == fallback.id:
            continue  # 回退骨架不参与竞争，只在没人够格时上场
        beats = prune_beats(arc, payload)
        missing = missing_needs(arc, payload)
        if missing or len(beats) < arc.min_beats:
            skipped.append((arc.id, missing or ()))
            continue
        qualified.append(arc)

    if not qualified:
        beats = prune_beats(fallback, payload)
        return ArcChoice(arc=fallback, beats=beats or fallback.beats,
                         eligible=(), skipped=tuple(skipped))

    # 够格的骨架里 optional 拍仍可能因缺数据被剪（资讯拍），所以返回剪枝后的
    # 节拍，而不是原始 beats——否则 prompt 会指挥模型去讲一拍没有数据的新闻。
    picked = qualified[0] if len(qualified) == 1 else _stable_pick(qualified, code, day)
    return ArcChoice(arc=picked, beats=prune_beats(picked, payload),
                     eligible=tuple(arc.id for arc in qualified), skipped=tuple(skipped))


def beats_by_key(beats: Sequence[Beat]) -> tuple[str, ...]:
    return tuple(beat.key for beat in beats)
