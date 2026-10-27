"""Generate natural, business-focused stock conversations via a pluggable LLM.

传输层走 ``stocktalk.modules.providers``（默认百炼 CLI，可配置切换到任何
OpenAI-compatible 端点）；本模块只负责业务：prompt 构造、骨架选择、
响应解包与脚本归一化。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping, Sequence

from stocktalk.modules import copy_guard, sharpness
from stocktalk.modules.episode_planner import (NUMBER_CAP_ANALOGY,
                                               NUMBER_CAP_ANCHOR)
from stocktalk.modules.arcs import (Arc, ArcChoice, Beat, MONOLOGUE_SPEAKER,
                                    beats_by_key, resolve_speakers, select_arc)
from stocktalk.modules.hyperframes_builder import short_business_label
from stocktalk.modules.numbers import repair_spoken_numbers, to_display
from stocktalk.modules.providers import ProviderError, create_llm_client
from stocktalk.modules.providers.bailian_chat import BailianChatClient
from stocktalk.modules.risk_exchange import build as build_risk_exchange
from stocktalk.modules.stock_data import StockDataClient
from stocktalk.modules.story_spotter import spot_stories


class DialogueGenerationError(RuntimeError):
    """Raised when Bailian cannot return a usable dialogue script."""


Runner = Callable[[Sequence[str], float], str]


# Measured on real renders: a 1331-character script came out at 275s of speech,
# i.e. ~4.8 Chinese characters per second including inter-turn pauses.  The
# prompt turns a duration target into a character budget with this rate, because
# asking a model for "a 90-second dialogue" reliably overshoots.
SPEECH_CHARS_PER_SECOND = 4.8

# 片尾风险提示交换轮（2026-10-08 第二版）：一人一句、按个股特征变化。
# 模板池与挑选逻辑见 stocktalk/modules/risk_exchange.py——有数据才用特征模板，
# 合格候选按 (代码, 日期) 稳定分散，同一只票同一天可复现、不同票不撞形。
# dialogue.risk_exchange: false 可整体关闭。
RISK_A = "股市有风险"
RISK_B = "投资需谨慎"

# 内容形态（2026-10-24 独白扩展）：dialogue.format。
#   debate     —— 默认，看多/看空两人对辩（既有行为，一个字不改）
#   monologue  —— 单人独白讲透一家公司（2-4 分钟，见 MonologueGenerator）
FORMAT_DEBATE = "debate"
FORMAT_MONOLOGUE = "monologue"
FORMATS = (FORMAT_DEBATE, FORMAT_MONOLOGUE)


def dialogue_format(config: Mapping[str, Any] | None) -> str:
    """读 dialogue.format；未知值一律回 debate——不猜、不抛，形态是开关不是数据。"""
    source = dict((config or {}).get("dialogue", config or {}) or {})
    value = str(source.get("format", FORMAT_DEBATE) or FORMAT_DEBATE).strip().lower()
    return value if value in FORMATS else FORMAT_DEBATE


# 独白形态下 narrator 角色的兜底值（配置里没写全时补齐）。
# 为什么必须有这一层：TTS 按 characters.<role>.voice_id 取音色，缺角色时
# tts_agent._turns 会把该说话人的全部台词**静默过滤掉**——成片零配音、还不报错，
# 是这条链路上最难查的一类坑。宁可在这里写死一份默认人设，也不赌下游能报错。
NARRATOR_DEFAULTS: dict[str, Any] = {
    "name": "谈股论金",
    "persona": "一个人对着镜头把一家公司掰开讲：先讲清它卖什么、靠什么赚钱，"
               "再讲账、讲护城河与风险，最后给观众留下可复用的认知；"
               "口语化、有节奏，不播报、不堆指标",
    # 豆包 2.0「解说小明」——为"解说腔"做的 A/B 选型（2026-10-07），
    # 独白片要的就是一个人讲透，而不是辩论里的任何一方。
    "voice_id": "zh_male_jieshuoxiaoming_uranus_bigtts",
    "supports_instruction": True,
    "instruction": "用沉稳、有节奏的讲解语气，像把一件事讲给朋友听；"
                   "该停顿处停顿，强调关键数字时放慢，不播音腔",
    "speed": 1.0,
    "pitch": 1.0,
}


def ensure_narrator_character(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """独白形态下补齐 characters.narrator（就地改写 config，幂等）。

    放在生成器和 pipeline 两处入口都调一次：生成器自己要人设（prompt 与
    character_name），TTS 要 voice_id，两边读的是同一份 config 字典，
    在这里补一次就都不缺——否则"prompt 有人名、配音没声音"这种半对齐状态
    会被下游当成正常运行。
    """
    if dialogue_format(config) != FORMAT_MONOLOGUE:
        return config or {}
    source = dict((config or {}).get("characters", {}) or {})
    narrator = dict(source.get(MONOLOGUE_SPEAKER) or {})
    for key, value in NARRATOR_DEFAULTS.items():
        narrator.setdefault(key, value)
    source[MONOLOGUE_SPEAKER] = narrator
    (config or {})["characters"] = source
    return config or {}

# 按轮情绪（2026-10-07）：模型每轮自报台词的当下情绪，TTS 按轮注入演绎指令——
# 「情绪在对话里起伏」是听感从平铺到有戏的最大杠杆（豆包 context_texts 每次调用
# 独立传，逐句合成架构正好一一对应）。词表同时是归一化的校验白名单；
# 短语是 TTS 端的演绎描述（EMOTION_DIRECTIVES 由 tts_agent 消费）。
TURN_EMOTIONS = ("反驳", "质疑", "惊叹", "顿悟", "坚守", "平述")
EMOTION_DIRECTIVES = {
    "反驳": "这一句用反驳的语气，锋芒毕露、寸步不让",
    "质疑": "这一句用怀疑审视的语气，带着不解和追问感",
    "惊叹": "这一句用惊讶、难以置信的语气，像刚看到一个意外的数字",
    "顿悟": "这一句用恍然大悟的语气，像刚刚想通一件事",
    "坚守": "这一句用笃定沉稳的语气，立场坚定不被说服",
    "平述": "",
}

# 套话清单已集中到 ``copy_guard``（2026-10-27 WAN-27：用户点名「这门生意」
# 「把…聊透了」）。清单必须**只有一份**：模板池扫描（谁往片尾模板里写了套话）、
# 成片体检（这期念出来几次）、跨轮复读检测用的是同一批规则，各写一份的话，
# 新加的词只会进其中一处，而漏掉的那一处决定成片。

# 「照抄就变成新套话」的反例清单（2026-10-19 内容迭代）：句子 → 为什么会被念成套话、
# 该往哪儿改。它和 episode_plan 的开法种子配成一对——种子给"照这个动作说话"，
# 反例给"别照这个句式说话"，只讲原则模型读不出指的是什么。
SENTENCE_BLACKLIST: tuple[tuple[str, str], ...] = (
    ("你以为它卖 A，账上最重的其实是 B",
     "每期同构反转，第三期就被观众预判；对单主业公司（八成四是一瓶酒）根本不成立，硬套就是假反转——只在 A≠B 确实成立时用"),
    ("每 100 元收入里 X 元来自 A",
     "固定成换算句式后每期出现两次就还是报数；换算只留给观众没尺度感的指标（费用率、净利率），收入占比直接说「八成四来自一瓶酒」"),
    ("把一笔订单拆开：上游给它 A，它把 B 交付给 C",
     "顾问腔，制造业和白酒会长得一模一样；必须出现具体的人（谁拉走、谁垫货、谁定价）和具体动作"),
    ("这场争论不用猜，下一份 X 看 Y：如果 A…如果 B…",
     "这是 2026-09-29 复盘点名的「看下一期」结语换了件马甲——只留变量和一句判断，不写「如果…如果…」"),
    ("行，就拿交付量说话",
     "它已经当过范例，模型会整期复用；范例每期必须换，且从当期分歧里长出来"),
    ("它卡住的是 X 环节，客户换掉它要付 Y 代价",
     "所有票都变成「卡脖子」叙事；消费品的真壁垒是场合和心智，套上去就假——护城河先分类型（成本/切换成本/心智/牌照），句式随类型换"),
    ("故事讲得通，账上却有一笔不配合",
     "它会变成 R4 的固定开场，第二期就是模板——R4 直接从数字对撞开口，不预告「我要查账」"),
    ("合同负债从八十亿骤降到三十一亿，客户不排队交钱了？",
     "用会计科目开场、一句话报两个数——这是 2026-10-19 真机那期的原话，正是最该消失的开场；改用看得见的东西（产品/渠道/场景）起头"),
    ("批价守住了，再看下一季预收款能不能回流",
     "固定「等下一个信号」的收尾，每期都长成这个形状——收尾按本期叙事线的那条动作收，不预告数据"),
)

# 成片时长目标 2-5 分钟（2026-09-20 用户反馈"内容太短、得不到有效启示"）。
# 2026-09-22 用户反馈：轮次多一点、每人一次讲短一点——短交锋才有辩论的节奏，
# 一人一口气 27 秒太闷。单轮默认 80 字（约 17 秒），轮数按典型 60 字估。
@dataclass(frozen=True)
class DialogueConfig:
    model: str = "qwen3.6-plus"
    min_duration_seconds: int = 120
    max_duration_seconds: int = 300
    max_line_chars: int = 80
    max_tokens: int = 8000
    temperature: float = 0.8
    timeout_seconds: float = 300.0
    # 剧本骨架：注册表 id，或 "auto"（按这只票实际拿得到的数据自动挑）。
    # 见 stocktalk/modules/arcs.py。
    arc: str = "auto"
    # 短回应（"嗯""有道理，但是…"）的字数上限。用户 2026-09-22：
    # 对话该长则长该短则短，有时候只是认可一句。
    short_line_chars: int = 20
    # 本期提示词（CLI `-m/--message` 或配置 dialogue.message）：注入剧本生成
    # prompt 的**选材与侧重参考**——「这期多讲点什么」。它是提示而非新约束：
    # 合规红线、数据真实性、骨架节拍与字数额度都照旧有效（见 _message_section）。
    message: str = ""
    # 片尾风险提示交换轮（2026-10-08 第二版）：false = 关闭。
    risk_exchange: bool = True
    # 内容形态（2026-10-24）。debate 走 DialogueGenerator，monologue 走
    # MonologueGenerator——形态决定 prompt、说话人数、片尾风险提示与画面。
    format: str = FORMAT_DEBATE


# 独白形态的默认额度（2026-10-24 用户决策：独白 120-240 秒）。
# 与对辩的两个关键差别，都是"一个人连讲 2 分钟"逼出来的：
#   1. 单轮上限 220 字（≈46 秒）。对辩的 80 字是"短交锋"节奏，独白没有对手，
#      一轮 80 字会变成十几段没人接话的短句，讲不透一件事；220 字正落在
#      "段落 150-300 字"的中段，超了仍由 _split_line 按句号拆成续轮。
#   2. risk_exchange 关掉：它是"一人一句接力"的双人机制，独白里没有对方可接，
#      硬拼只会让同一个人念两句免责声明。片尾画面的合规提示照常打在 outro 卡上。
MONOLOGUE_DEFAULTS = {
    "min_duration_seconds": 120,
    "max_duration_seconds": 240,
    "max_line_chars": 220,
    "risk_exchange": False,
}


class DialogueGenerator:
    """Return ordered turns, not artificial paired debate rounds."""

    # 形态是类级默认，MonologueGenerator 覆写它。实例方法据此分支，而不是每处
    # 都去读 config——"这一次是几人片"是生成器的身份，不是某个方法的局部判断。
    format: str = FORMAT_DEBATE

    def __init__(self, config: Mapping[str, Any] | None = None, runner: Runner | None = None,
                 llm: Any = None) -> None:
        source = dict((config or {}).get("dialogue", config or {}))
        # 独白形态：额度有一套自己的默认（见 MONOLOGUE_DEFAULTS），配置里写了就用配置。
        # 都放在 dialogue.monologue 这一层下，于是默认配置文件能同时写死两种形态，
        # 而 dialogue.format 只是一个开关，不承载任何额度语义。
        overrides = dict(source.get("monologue") or {}) if self.format == FORMAT_MONOLOGUE else {}
        merged = dict(MONOLOGUE_DEFAULTS) if self.format == FORMAT_MONOLOGUE else {}
        merged.update({key: value for key, value in overrides.items() if value not in (None, "")})
        merged.update({key: value for key, value in source.items()
                       if key not in ("monologue", "format")
                       and (self.format != FORMAT_MONOLOGUE or key not in MONOLOGUE_DEFAULTS)})
        self.config = DialogueConfig(
            model=str(source.get("model", DialogueConfig.model)),
            min_duration_seconds=int(merged.get("min_duration_seconds", DialogueConfig.min_duration_seconds)),
            max_duration_seconds=int(merged.get("max_duration_seconds", DialogueConfig.max_duration_seconds)),
            max_line_chars=int(merged.get("max_line_chars", DialogueConfig.max_line_chars)),
            max_tokens=int(source.get("max_tokens", DialogueConfig.max_tokens)),
            temperature=float(source.get("temperature", DialogueConfig.temperature)),
            timeout_seconds=float(source.get("timeout_seconds", DialogueConfig.timeout_seconds)),
            arc=str(source.get("arc", DialogueConfig.arc) or DialogueConfig.arc),
            short_line_chars=int(source.get("short_line_chars", DialogueConfig.short_line_chars)),
            message=self._normalize_message(source.get("message")),
            risk_exchange=merged.get("risk_exchange",
                                     source.get("risk_exchange", source.get("risk_line", True)))
                          not in (False, "", None, "false", "0"),
            format=self.format,
        )
        # 单轮上限随形态变：独白允许长段落（220），对辩仍卡在 150 以内
        # （"一人一口气 27 秒太闷"是 2026-09-22 的实测结论，不随独白放宽）。
        line_chars_cap = 300 if self.format == FORMAT_MONOLOGUE else 150
        if (self.config.min_duration_seconds < 1 or self.config.max_duration_seconds < self.config.min_duration_seconds
                or not 30 <= self.config.max_line_chars <= line_chars_cap or self.config.max_tokens < 1):
            raise ValueError(f"duration bounds, max_line_chars (30--{line_chars_cap}), and max_tokens must be valid")
        if not 0 < self.config.temperature <= 2:
            raise ValueError("temperature must be in (0, 2]")
        ensure_narrator_character(config)
        self.characters = dict((config or {}).get("characters", {}))
        # 说话的只有这两个角色，但"有哪几个角色"由配置决定而不是写死：
        # 之前 {"bull", "bear"} 硬编码在两处（这里与 tts_agent），新增角色会被
        # 静默丢弃——一个不报错的坑。配置里加了角色就自动可用。
        # 独白形态是唯一例外：脚本合同里的 speaker 必须恒等于 narrator（一个稳定
        # 的 id），因为 TTS / 画面 / 合规三层都靠它对齐；所以这里不是"允许 narrator
        # 参与"，而是"只允许 narrator"。
        self.speakers = ((MONOLOGUE_SPEAKER,) if self.format == FORMAT_MONOLOGUE
                         else resolve_speakers(self.characters))
        self._active: ArcChoice | None = None
        # LLM 传输层：显式注入 > 旧 runner 契约（包成百炼 client，测试无需改动）
        # > 按配置走工厂（dialogue.provider，默认 bailian-cli）。
        if llm is not None:
            self._llm = llm
        elif runner is not None:
            self._llm = BailianChatClient(model=self.config.model, runner=runner,
                                          default_timeout=self.config.timeout_seconds)
        else:
            self._llm = create_llm_client(config, default_timeout=self.config.timeout_seconds)

    def select_arc(self, payload: Mapping[str, Any]) -> ArcChoice:
        """按这只票的数据挑骨架，并记下选择结果供 prompt 与审计复用。

        format 必须跟着传：骨架注册表按形态分家（对辩的 closing 拍写死了
        "由对方接一句"），不传就等于让独白片去和对辩骨架竞争。
        """
        quote = payload.get("quote") if isinstance(payload.get("quote"), Mapping) else {}
        code = str(payload.get("code") or quote.get("code") or "")
        choice = select_arc(payload, configured=self.config.arc, code=code,
                            day=date.today().isoformat(), format=self.format)
        self._active = choice
        return choice

    def _arc_choice(self, payload: Mapping[str, Any] | None = None) -> ArcChoice:
        """当前生效的骨架：已选过的就用它，否则就地选一次。"""
        if self._active is not None:
            return self._active
        return self.select_arc(payload if isinstance(payload, Mapping) else {})

    def generate(self, stock_data: Mapping[str, Any], episode_plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(stock_data, Mapping):
            raise TypeError("stock_data must be a mapping")
        payload = self._compact_data(stock_data)
        # 本期重点（2026-09-29）：按个股数据挑出的讨论角度——每期视频「有个性」
        # 的入口。没有足够数据（新股/块缺失）时就是空表，prompt 里不会出现。
        stories = spot_stories(stock_data)
        if stories:
            payload["本期重点"] = stories
        if episode_plan:
            payload["episode_plan"] = episode_plan
        choice = self.select_arc(payload)
        try:
            raw = self._llm.chat(self._system_prompt(choice.arc),
                                 self._user_prompt(payload, choice.arc),
                                 max_tokens=self.config.max_tokens,
                                 temperature=self.config.temperature,
                                 timeout=self.config.timeout_seconds)
        except ProviderError as exc:
            raise DialogueGenerationError(str(exc)) from exc
        return self._normalize_script(self._decode_response(raw), payload, choice, episode_plan)

    def _system_prompt(self, arc: Arc | None = None) -> str:
        """对辩形态的系统提示（单人独白见 MonologueGenerator）。

        整条提示按形态拆成四层：_attitude_rules（两人态度与正面交锋，对辩专属，
        独白不渲染）→ _content_rules（数据纪律与表达规则，两种形态逐字相同）→
        _shared_opening_rules（反套话 / 开场三秒 / 叙事铺排，只在两三处措辞上
        分叉）→ 各自的节奏与风格。分层不是为了好看：两种形态只要各写一份全文，
        下一次改数字纪律就会只改到其中一条——那是内容静默分叉，没有任何测试能发现。
        """
        choice = self._choice_for(arc)
        # 兜底人设与 config/characters 同调（WAN-29）：多头热、空头冷。配置里没写
        # characters 时（单测、精简配置）提示词也不至于退回两个中性评述员。
        bull = self._character("bull", "增长派",
                               "热情乐观，擅长类比、未来叙事和增长逻辑，说话往前冲；承认数据边界")
        bear = self._character("bear", "审计派",
                               "冷静理性，擅长数据引用、风险揭示和历史比较，慢半拍接话；追问叙事")
        return (
            "你是财经短视频编导，为对投资感兴趣的普通观众写一段两人聊公司的中文对话。只依据提供的数据发言；"
            "不编造数字、新闻或事实，不预测涨跌，不给出买卖或仓位建议，不承诺收益。\n"
            f"看多角色：{bull['name']}，人设：{bull['persona']}。\n看空角色：{bear['name']}，人设：{bear['persona']}。\n"
            + self._attitude_rules()
            + self._content_rules()
            + self._system_tail(choice)
        )

    def _attitude_rules(self) -> str:
        """态度对立与正面交锋（**对辩专属**，独白形态不渲染这一段）。

        WAN-29 用户反馈：成片里「多空双方的态度不够尖锐」——听下来像两个人各自把
        稿子念完，没有对手感。问题不在人设写少了，而在提示词从头到尾没要求过
        「接话」：模型完全可以写两段平行的独白再拼起来，而每一轮单看都合格。

        2026-10-27 第二轮（用户复述"兴冲冲想买进去 / 冷静理性帮她把风险一条条拽住"）
        把这一段从形容词改成**行为**：情绪先于论证、抢话与半句话被打断、先问一个
        具体问题再逐条拆、同一份数据的另一面拆回去。形容词压不住模型的默认写法，
        「看到好消息 → 判断 → 动作」这种先后顺序才是能写出来的东西。

        再一条是反裸报数：数字念出来没人用它，等于没讲。这两类规则都只描述**怎么写**，
        不动数据纪律（_content_rules）与锐度判据（_sharpness_rules）——那两段两种形态
        共用，改一个字独白片就会跟着变风格。
        """
        return (
            "\n态度与交锋（重要）：这是两个人在争一件事，不是两个人轮流念稿。\n"
            "· 多头「兴冲冲」——**情绪先于论证**：看到积极信息先有反应，忍不住当场下判断、给动作"
            "（「这价我先上车」「这个数我认」），而不是先把账念一遍再慢慢推理；"
            "被打断、被泼冷水时先找补或嘴硬（「你先等我说完」「可这块才刚起来啊」），"
            "不是每次被问就点头——认输只留给片尾那一处反转。允许抢话，允许半句话被打断"
            "（「这个毛利——」「你先听我说完」）。\n"
            "· 空头「找茬」——**不抢话**，先问一个很具体的问题（「你先说清楚这个毛利怎么算的」"
            "「这笔钱是一次性的还是年年都有」），再一条条拆：估值撑不撑得住、现金流跟不跟得上、"
            "利润里有多少一次性的、现在在周期的哪个位置。全程不喊不叫、不提音量，"
            "把话递回去的说法是「我不拦你，但你要知道……」。\n"
            "· 碰撞：多头的每一个理由都要被**同一份数据里的另一面**当场拆掉——不是拿别处的常识反驳，"
            "而是他自己引的那个数字本身还有另一半；多头再拿新信息或新角度反击回来。"
            "不许出现某一方连续两轮没人质疑：他说的每一句都得有人接。\n"
            "· 反裸报数：禁止「营收 12.3 亿，同比增长 18%」这类朗读句——数字念出来却没人用它，等于没讲。"
            "数字必须挂在动作、判断或情绪上（「这个毛利率一掉，他那句护城河就站不住了」"
            "「账上多出来的钱是一次性的，拿它撑估值不成立」），并且念出来的数字要被对方接过去用"
            "或当场质疑；某一轮一个数字都没有是允许的，只要那一轮是在交锋。\n"
            "· 必须正面交锋，不许平行独白：除开场轮（第一句还没人说话）和几个字的短回应"
            "（「嗯」「有道理」「等等」）之外，每一次**实质发言**都要**先接住对方刚说过的那句话**——"
            "点名复述他的原话或数字（「你刚才说毛利率九成二，可这个毛利是……」），"
            "再说清自己为什么不同意；被点的这一方下一轮要把这一刀还回去。"
            "这里的「接住」指意思上回应，不是一句固定前缀：不要每轮都用「你刚才说……」开头，"
            "复述可以是半个数字、一句反问，或一句打断。"
            "全片至少两次这样「引用→反驳→回击」的往返还手，不要出现两人各讲一个话题、"
            "谁也没搭理谁的局面。\n"
            "· 必须有一个激变点：全片至少一处真实的**观点反转或立场激变**——"
            "一方被对方摆出的具体数字说服而改口（「……这我认，我上一句得收回来」），"
            "或被逼到立场升级（「好，那我更要问一句……」）。反转必须由**具体的数字或事实**掰过来，"
            "并且让观众听得出是哪一个数字掰的；不许用一句「你说得对」敷衍过去了事。\n"
            "· 矛盾感留到片尾：允许僵持、话不投机、被打断后抢回话头；争到最后可以仍不完全一致，"
            "把分歧钉在最后一个具体问题上，不要收在共识上。\n"
            "· 热情不等于吹票：上头是**他自己的动作**（「我先上车」），不是叫观众买——"
            "不许承诺收益、不许预测涨跌、不许喊别人买；动作只用口语说法，"
            "不要用「加仓／减仓／满仓／清仓」这类仓位词（它们在合规层会被换成别的说法，句子会散）；"
            "空头的质疑同样只落在数据和生意层面——不唱空，不评价个人。\n"
        )

    def _content_rules(self, emotion_close: str = "辩论的情绪就在起伏里") -> str:
        """数据纪律与表达规则（对辩与独白共用）。

        这一段讲的是"怎么只依据数据说话"，与几个人在说无关，所以两种形态共用
        同一份文本；``emotion_close`` 是唯一的分叉点——对辩有对手接话，独白没有。
        """
        return (
            "\n内容要求（重要）：\n"
            "0. 数据里的「本期重点」是按这家公司**这期财务数据**的显著变化挑出的讨论角度"
            "（业绩趋势/毛利率走势/股东回报/现金流/科目异动/收入结构位移）——"
            "它们正是这家公司区别于别家的地方，优先围绕它们展开；"
            "但每个角度都必须落到数据明细里的具体数字与年份，明细里撑不住的角度宁可不讲。\n"
            "1. 以“讲懂这家公司”为主线：先说清它到底卖什么、靠什么赚钱、客户是谁、"
            "钱在它手里怎么转一圈再回来；"
            "再说它在所处行业里的位置、竞争格局、行业天花板与政策环境。\n"
            "   数据里的「公司档案」来自交易所 F10 原文，是讲清上面这些的第一手依据，必须据此展开：\n"
            "   · 主营业务、主营构成（各产品/地区/销售模式的收入占比与毛利率）就是整段叙事的骨架——"
            "哪块业务撑起收入、哪块最赚钱、直销和经销各占多少，都要讲到具体数字；"
            "提到某块业务时，统一用数据里给出的业务名称（已是画面同款简称），"
            "不要自创缩写或换一个叫法——旁白和画面必须用同一个词；\n"
            "   · 所属行业、行业地位（研究行业与同行家数）用来定位它在行业里的位置和同行对比；\n"
            "   · 管理层只提档案里写了姓名和职务的人，不评价能力、不推测想法、不编造言论；\n"
            "   · 经营评述是公司自述，可以复述但要说明这是公司的说法。\n"
            "   board（行业/概念板块）是交易所对这家公司的分类，作为补充：含“白酒概念”就围绕白酒这条线讲，"
            "分类只到行业层面时就只讲行业层面的事实。"
            "档案和 board 里都没有的产品、客户、产能、管理层姓名与言论一律不得编造，宁可说“公开信息只到这一层”。\n"
            "2. 财务与行情数据只用来佐证业务判断：把营收、利润、毛利率、ROE、价格和成交等数字翻译成"
            "经营层面的含义（例如这个数字变动说明谁的日子变难了），不要罗列指标，不要停留在K线、MACD等技术信号本身。\n"
            "3. 讲听众关心的实际问题：这家公司凭什么在行业里站稳、增长从哪里来、钱从哪里赚、和同行比强在哪、"
            "风险藏在哪个环节。\n"
            "4. 有多少数据讲多少内容：充分利用数据里的财务指标、每股指标、利润/资产负债/现金流构成和历史走势；"
            "结合最近的行情节奏（涨跌、量能、位置）自然展开，可以引用带日期的具体数字，但只解读不预测。\n"
            "5. 最新资讯是每期的必答块（2026-10-08）：数据里的 news/资讯 是该股票最近的公开新闻、研报和快讯。"
            "骨架里出现 news 节拍时，必须至少用一轮正面响应最新的一条——说清日期与信息类型"
            "（如\u201c10月8日龙虎榜……\u201d\u201c九月初有研报提到……\u201d），并把它和前面的生意判断连线"
            "（印证了什么/矛盾在哪/或说明只是交易层面的异动），这正是观众点进来的原因；"
            "只能复述标题和摘要里已有的信息，不得脑补细节、不得据此预测股价；"
            "骨架里没有 news 节拍（数据里没给资讯）时才完全不提。\n"
            "6. 多用通俗类比和生活化举例把机制说成人话（如把经销网络比作水管、把预收款比作客户先排队交钱）；"
            "关键数字要说清楚\u201c意味着什么\u201d，而不是只报数字。\n"
            "7. 每轮对话都要给出画面指引：从 line 中提取 2-4 个关键词或短语（公司名、业务词、财务指标名、"
            "日期或区间、行业词、新闻事件词），按对话顺序填入 visual 数组；只允许出现 line 里说到的词；"
            "visual 会直接打在画面上，数字一律用阿拉伯数字（如「应收账款4.51亿」「毛利率18.29%」「9月26日中标」）；"
            "相邻两轮的 visual 数组不得完全相同——每一轮讲了新信息就配新关键词，图板才会随对话推进。\n"
            "8. 台词要有情绪起伏和口语节奏：多使用反问、设问、惊讶、质疑、认同后转折；"
            "在最想强调的词和数字上自然用上\u201c！\u201d\u201c？\u201d\u201c……\u201d\u201c——\u201d等标点，"
            "但不要每句都感叹，该克制时克制，做到有张有弛。\n"
            "8b. 每轮输出一个 emotion 字段，标注这句台词**说出口那一刻**的情绪，"
            "只能取：反驳/质疑/惊叹/顿悟/坚守/平述。它是配音的情绪脚本（配音会据此调整语气），"
            "不是台词的一部分：被驳斥时的回击→反驳，摆事实提疑点→质疑，听到意外的数字→惊叹，"
            "刚想通一个关联→顿悟，守住立场不被说服→坚守，普通陈述→平述。"
            f"同一情绪不要连用超过三轮；全片「平述」超过一半等于没打——{emotion_close}。\n"
            "9. 数字必须写完整读法，禁止缩略：写「四亿五千一百万」不要写「四亿五一」，"
            "写「百分之八十二点七」不要写「八成二七」，写「十八点二九」不要写「十八二九」；"
            "缩略数字配音会念错，观众也听不懂。\n"
            "10. 数字分三级、按**解释单元**计数：一组同口径的对比（「毛利九成二、净利五成」）算 1 个，不是 2 个。"
            "anchor 原值口播且必须绑定「所以呢」——念到数字就要讲清它意味着什么生意变化，"
            "光报数字不给生意含义等于没讲；analogy 只说确定换算后的白话并记录原指标；"
            "visual_only 只上屏、不要念出来。"
            f"额度：全片 anchor ≤{NUMBER_CAP_ANCHOR} 个、analogy ≤{NUMBER_CAP_ANALOGY} 个、visual_only 不限；"
            "**任意连续两轮不得都出现新数字**——一轮报数、下一轮讲它的生意含义。"
            "缺少 source_refs 的事实不得提及。\n"
        )

    def _shared_opening_rules(self, *, tension: str = "顺着质疑把代价和疑点铺深",
                              closing: str = "片尾收束再把分歧留给观众",
                              deduced: str = "从数据交锋里自己拼出来") -> str:
        """反套话 / 开场三秒 / 叙事铺排：两种形态同文，只在三处措辞上分叉。

        对辩的张力来自"对方的追问"、片尾留给观众的是"分歧"、人名从"数据交锋"里
        拼出来；独白没有对方，张力来自讲述人自己的追问，片尾交给观众的是一个
        能自己核对的变量。其余一字不改——改了就是两条形态各自长出一套开场习惯，
        看三期又能被预判。
        """
        return (
            "\n反套话（重要）：每一期的开场都必须是新的。禁止把这些当开场套式——"
            "“咱们先看……”整个句式（不管宾语换成什么，2026-09-27 实锤：“咱们先看谁在给XX掏钱”只换了宾语、骨架照旧）、"
            "“说白了”“这家公司靠什么赚钱”式的设问、“XX 这门生意”式的名词解释。"
            "**“这门生意”这四个字全片都不许出现**：它是把公司抽象成一个名词摆上台面，"
            "观众记住的是这个说法而不是这家公司；要讲生意就直接说是谁在掏钱、钱卡在哪一环、谁替谁垫钱。"
            "同样禁止“把某某聊透了”“讲透了”“今天就聊到这里”这类对节目自己的总结——"
            "它们一旦出现在片尾，等于把最后一轮让给自夸，观众拿到的是零信息。"
            "这些意思可以在正文里讲，但第一句必须从具体事实、具体数字或一个反直觉的判断切入，"
            "不要出处介绍、不要背景铺垫。收尾同样禁止“总的来说/说到底”式的空转，要落到一个具体变量上。\n"
            "\n开场三秒（重要）：第一句不超过 25 个字，必须带一个具体数字或一个反直觉的判断，"
            "让人立刻想问「为什么」；前两句里要埋一个信息缺口——反差、反问或未解的因果，"
            "观众为了补上这个缺口才会把片子看完。不要用行业定性开场（「这是一家做XX的公司」），"
            f"公司是谁、做什么生意，让观众{deduced}。"
            "\n叙事铺排（重要）：最强的反差数字/反常识事实是本期的「底牌」，不许在中段平铺掉——"
            f"开头埋钩（只露一半，让人想问另一半）、中段造张力（{tension}）、"
            "把底牌安排在整体进度的 60%-75% 处揭晓（揭晓那一刻要有情绪：惊叹/沉默/重新评估），"
            f"{closing}。这只改变**已有数据的出场顺序和悬念话术**："
            "不许为了戏剧性夸大、预判或编造任何数字，数据说什么就是什么。\n"
        )

    def _sharpness_rules(self, *, two_sided: bool = True) -> str:
        """锐度要求（对辩与独白共用，只在「谁来反驳」这一处措辞上分叉）。

        用户的原话是「像在报流水账」「看完不想看下一集」，但说不出标准。标准写成
        三条可执行的写作约束：每轮都是**能被反驳的具体判断**、对立面真的对撞、
        全片留一处反常识。``sharpness`` 模块用同一套口径核对成片，于是"够不够锐"
        不是感觉——能指出具体哪一轮还是流水账。
        """
        if two_sided:
            opposition = ("两边必须真的对立：允许认同，但每一方至少要有一次直接推翻对方的具体依据"
                          "（「你这条数说明不了这个」）；「你说得对，不过也要看到另一面」式的和稀泥"
                          "一次都不要有。")
        else:
            opposition = ("你要自己跟自己抬杠：每给出一个判断，就当场替观众追问一句「凭什么」，"
                          "再用数据把它答掉；一路顺着讲到底就是流水账。")
        return (
            "\n锐度（重要）：这不是一期资讯播报，观点要能扎人。"
            "每一轮抛出的都必须是**当场能被反驳的具体判断**——谁在掏钱、凭什么非它不可、"
            "代价压在哪个环节；只说「公司做了什么」是流水账，说出「这么做要付出什么」才是观点。"
            + opposition
            + "全片至少一处反常识：把数字和直觉拧过来（账上赚的和手里收的对不上、越赚钱越缺钱、"
            "同行都在涨它却在掉），但只能是数据里已有的事实，不许为了反差夸大或编造。"
            "观众看完要有一个「原来如此」的瞬间，而不是记住一串指标。\n"
        )

    def _system_tail(self, choice: ArcChoice) -> str:
        """对辩形态的节奏与风格收束（骨架清单 + 反套话 / 开场 / 铺排 + 交替发言）。"""
        return (
            "\n" + self._arc_section(choice) + "\n"
            + self._shared_opening_rules()
            + self._sharpness_rules()
            + "\n对话节奏（重要）：长度由内容决定，该长则长、该短则短。允许某一轮只有几个字"
            "（如“嗯”“有道理”“但是——”“等等，这个不对”），用来表示认可、打断或迟疑；"
            "也允许一方连续追问、另一方长答后短驳。不要为了凑字数把每一轮都写满，也不要每轮等长。"
            "两人必须交替发言：同一说话人连续两轮只允许超长台词被拆成续轮的情况；"
            "一个论点的自然延伸就并进同一轮里说完，不要单独再开一轮。\n"
            "\n风格要求：像两个懂行的人聊天，允许追问、打断（……或破折号）、短暂停顿、跑题后拉回、惊讶/认同/质疑，"
            "及被说服后修正观点。每一轮都必须提供新信息、新角度或新动作，严禁重复已经说过的观点；"
            "数字不是每轮的义务，是拿来互相拆台的弹药——允许某一轮一个数字都没有，"
            "但念出来的每个数字都得被对方接过去用或当场质疑，"
            "「营收 12.3 亿，同比增长 18%」这种念完就没人接的句子一律不要；"
            "鼓励连续 2-3 轮围绕一个话题层层深挖（提出→举例/数字→追问短板→修正），再自然转到下一个话题。"
            "数据存在时引用具体数字；数据缺失就坦诚说缺，不硬编。\n"
            "必须只输出 JSON，不要 Markdown。"
        )

    def _choice_for(self, arc: Arc | None = None,
                    payload: Mapping[str, Any] | None = None) -> ArcChoice:
        """解析出这次要用来渲染 prompt 的骨架（含已按数据剪枝后的节拍）。

        ``generate`` 的正常路径上骨架已经选过（``self._active``），直接用。但审查页
        与单测会直接调 ``_system_prompt``/``_user_prompt``，此时还没选过——这时必须
        **按配置和这份数据就地选一次**，而不是悄悄退回写死的主线：否则配置里写了
        ``arc: annual``，prompt 却仍然是主线，而且轮数下限会按主线的节拍数算。
        """
        if self._active is not None:
            return self._active
        source = payload if isinstance(payload, Mapping) else {}
        quote = source.get("quote") if isinstance(source.get("quote"), Mapping) else {}
        configured = arc.id if isinstance(arc, Arc) else self.config.arc
        return select_arc(source, configured=configured,
                          code=str(source.get("code") or quote.get("code") or ""),
                          day=date.today().isoformat(), format=self.format)

    def _arc_section(self, choice: ArcChoice) -> str:
        """把骨架渲染成给模型的节拍清单——这是"这期怎么讲"的唯一来源。"""
        lines = [f"本期骨架：{choice.arc.name}——{choice.arc.summary}",
                 "严格按下面的节拍顺序展开，每个节拍至少一轮发言；被标为可短的那一拍允许只有几个字："]
        for index, beat in enumerate(choice.beats, 1):
            # 独白骨架的节拍不指派说话人（全部由 narrator 说），"谁来说都行"这句
            # 在单人片里既无信息量又会诱导模型编出第二个角色——直接不渲染。
            if beat.speaker:
                who = f"（由{self._character(beat.speaker, beat.speaker, '')['name']}说）"
            else:
                who = "" if self.format == FORMAT_MONOLOGUE else "（谁来说都行）"
            limit = f"，最多 {beat.max_chars} 字" if beat.max_chars else ""
            lines.append(f"{index}. {beat.intent}{who}{'，这一拍可以说得很短' if beat.short else ''}{limit}")
        if choice.arc.opener and choice.beats and choice.beats[0] is choice.arc.beats[0]:
            # 开场要求是绑在首拍上的：首拍因为没数据被剪掉时不能再提它，
            # 否则 prompt 会让模型去完成一个数据里根本没有的开场。
            lines.append(f"开场要求：{choice.arc.opener}")
        # 节拍的 key 同时是 JSON 里 beat 字段的合法取值，明确写出来，模型不用猜。
        lines.append(f"beat 字段只能从这些值里取：{'、'.join(beats_by_key(choice.beats))}。")
        return "\n".join(lines)

    @staticmethod
    def _normalize_message(value: Any, limit: int = 4000) -> str:
        """规整本期提示词：去掉行尾空白与空行，超长截断并留下可见标记。

        提示词整条进 LLM 上下文，而 prompt 本身已经有骨架说明和几万字数据 JSON，
        所以留一个上限兜底：正常写得下的提示词碰不到它，误传一个 MB 级文件时
        也不至于把上下文挤爆。截断标记会一并写进结果 JSON，事后能看出被截过。
        """
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in str(value or "").splitlines()]
        text = "\n".join(line for line in lines if line)
        if len(text) > limit:
            return text[:limit] + f"\n（提示词超过 {limit} 字，已截断）"
        return text

    def _message_section(self) -> str:
        """把 --message 注入的提示词渲染成 prompt 里的一段（没有就返回空串）。

        位置放在用户提示的末尾、JSON 结构说明之前：它决定"本期从哪里切入、多讲
        什么"，属于**选材与侧重**层面的参考，所以必须明确写出它与硬性要求的关系
        ——数据真实性、合规红线、骨架节拍、轮数与字数额度一律不受它影响。不写这
        一句，模型很容易把操作者的一句话当成新的硬约束（例如被要求"讲讲重组预期"
        就去编一个数据里没有的重组）。
        """
        text = self.config.message
        if not text:
            return ""
        return (
            "\n本期额外提示（操作者指定，用于选材与侧重）：\n"
            f"{text}\n"
            "上面这条提示决定本期从哪里切入、多讲什么；它不改变前面那些硬性要求——"
            "仍只依据提供的数据发言、不编造数字与事件、不预测涨跌、不给买卖或仓位建议，"
            "骨架节拍顺序、轮数与字数预算也照旧。数据撑不住提示里的说法时，按数据本身讲，"
            "并如实说明公开信息只到这一层。\n"
        )

    @staticmethod
    def _minutes(seconds: int) -> int:
        """秒换算成最近的整分钟，给 prompt 里那句「约 X-Y 分钟」用。（120→2、300→5）"""
        return max(1, round(seconds / 60))

    def _audience_rules(self, *, opening: str = "开头几轮", where: str = "交锋里") -> str:
        """观众获得感与三类可复用认知（对辩与独白逐字共用，只有两处措辞分叉）。

        这是这条产品线的核心质量门槛（2026-10-16：看一期要带走两个可复用认知），
        与几个人在说无关，所以必须共用：各写一份的话，下一次改质量门槛就只会
        改到其中一条，另一种形态会在没有通知的情况下悄悄退回旧标准。
        """
        return (
            "至少一半的篇幅围绕公司业务和行业本身（生意模式、行业格局、竞争与需求），技术信号最多作为一句带过的佐证。"
            f"{opening}就把“这家公司靠什么赚钱”讲明白，让观众听完能看懂一个行当的赚钱方式，而不是听了一段行情点评。"
            "观众的获得感是这条视频的命：看完要让人觉得「学到了」，而不只是听了一嘴行情。"
            "每一期至少让观众带走两个可复用的认知，从下面三类里至少覆盖两类："
            "① 行业门道——这个行业的钱怎么流转、产业链上下游谁卡着谁、为什么这类行当毛利高或账期长，讲清一个运转机制；"
            "② 看财报的方法——结合本期真实数字讲一个指标怎么读、容易看漏什么"
            "（比如经营现金流为什么要和净利润对着看、存货和应收压着多少营收说明什么），"
            "让观众下次自己翻开财报也会用这一招；"
            "③ 公司价值——把商业模式翻译成「它凭什么赚钱、哪一环最经不起查」，不要停在概念词上。"
            "讲通用财识可以用行业常识，但涉及本期公司的数字必须原样来自数据；"
            f"知识点要长在{where}自然讲出来，禁止开科普小课堂（「我们来了解一下」这类句式一律不要）。"
        )

    def _interactive_fields(self, include_sides: bool = True) -> str:
        """hook / question（/ sides）互动字段的产出规则。

        两种形态都要 hook 与 question（片尾画面与封面用它们），只有 sides 是
        对辩专属——它是"多空各一句立场"，单人片里没有两个立场可给。
        """
        sides_note = "，与 sides 里的立场相互呼应" if include_sides else ""
        count = "三个" if include_sides else "两个"
        text = (
            f"另外必须产出{count}用于和观众互动的字段（本期片尾会把它们打在画面上，必须经得起核对）："
            "hook —— 下一期要盯的一个可验证的具体变量（如“下一份财报的外销收入占比”“越南基地的实际产出”），"
            "要能被下一期直接核对，不要写“继续关注”“拭目以待”这类空话；"
            "hook 同时是封面文案，鼓励带反差或悬念的措辞（如“九成收入竟靠它”），"
            "但落点必须仍是具体事实，不超过 22 字，禁止买卖词与收益暗示；"
            "question —— 抛给观众的一个问题，必须是生意层面的二选一（如“你更信渠道还是产能”），"
            f"观众能用一句话回答；问题必须从本场内容实际讲到的分歧点提炼{sides_note}，"
            "禁止引入内容里没有出现过的概念或环节（没聊到代工就绝不能问代工）；"
            "禁止询问买卖、点位、涨跌、能不能涨；"
        )
        if include_sides:
            text += (
                "sides —— 多空各一句立场，只讲生意层面的分歧（渠道、产能、价格能不能传导、谁会替代谁），"
                "一句话说完；禁止出现点位、目标价、买卖动作、收益暗示。"
            )
        return text

    @staticmethod
    def _spotlight_rule() -> str:
        """首帧大卡与封面的「冲突数字」规则（纯上屏元素，两种形态共用）。"""
        return (
            "另外必须产出一个 spotlight 字段——首帧大卡与封面的「冲突数字」：从数据里挑本期最反差的一个数字，"
            "label 是指标名（不超过 8 个字，如「经营现金流」），value 是冲突表达（不超过 16 个字，"
            "阿拉伯数字，如「-2.4亿→+8000万」「毛利率38%→31%」）。"
            "它必须原样来自提供的数据，禁止换算、推算或暗示涨跌方向；"
            "它是纯上屏元素（不进配音台词），所以数字直接写阿拉伯写法。\n"
        )

    def _user_prompt(self, payload: Mapping[str, Any], arc: Arc | None = None) -> str:
        choice = self._choice_for(arc, payload)
        min_s, max_s = self.config.min_duration_seconds, self.config.max_duration_seconds
        min_chars = int(min_s * SPEECH_CHARS_PER_SECOND)
        budget_chars = int(max_s * SPEECH_CHARS_PER_SECOND)
        floor_chars = max(30, self.config.max_line_chars // 2)
        # 轮数按「典型单轮」估算，而不是拿极值算：极值配出来的区间太宽
        # （4-22 轮），模型会往中间凑，长片的实际时长反而失控。
        typical_chars = max(40, (floor_chars + self.config.max_line_chars) // 2)
        # 下限同时受骨架约束：每个节拍至少要有一轮发言。
        min_turns = max(len(choice.beats), min_chars // typical_chars)
        max_turns = max(min_turns + 4, budget_chars // typical_chars)
        # --duration-minutes 4 会把上下限压成同一个值，别写成「约 4-4 分钟」。
        minutes = (f"{self._minutes(min_s)}-{self._minutes(max_s)} 分钟" if max_s > min_s
                   else f"{self._minutes(max_s)} 分钟")
        beat_keys = beats_by_key(choice.beats)
        schema = {"title": "股票名（代码）：一句话点出这家公司的看点或它在行业里的位置", "turns": [
            {"speaker": "bull", "line": f"一句完整自然的话，{floor_chars} 到 {self.config.max_line_chars} 字；"
                                        f"说不下就拆成两轮说；短回应可以只有 2-8 字",
             "beat": f"骨架节拍，只能取：{'/'.join(beat_keys)}",
             "emotion": "平述（只能取：反驳/质疑/惊叹/顿悟/坚守/平述，见系统提示 8b）",
             "visual": ["line 中提到的关键词1", "关键词2"],
             "round_id": "本轮所属叙事章节 id，只能取下面五章之一（可省略）"}],
            "sides": {"bull": "看多一方的一句话立场（只讲生意层面的分歧）",
                      "bear": "看空一方的一句话立场"},
            "hook": "下一期要盯的一个可验证的具体变量",
            "question": "抛给观众的一个生意层面二选一问题",
            "spotlight": {"label": "指标名（≤8字）", "value": "冲突数字短语（≤16字，阿拉伯数字）"},
            }
        return (
            f"生成一段成片时长在 {min_s}-{max_s} 秒（约 {minutes}）的中文对话流："
            f"正文总字数控制在 {min_chars}-{budget_chars} 字之间（配音约 4.8 字/秒，超字数就会超时长），"
            f"分 {min_turns}-{max_turns} 轮发言，单轮 {floor_chars}-{self.config.max_line_chars} 字"
            f"（允许出现只有几个字的短回应轮，如“嗯”“有道理”“但是——”，这类短回应不受单轮下限约束）。"
            "单轮字数是节奏参考，不是硬上限：先把句子说完整，再考虑字数。"
            "任何一句话都不许为了压字数砍成半截——宁可多拆一轮，也不能丢掉半句意思；"
            "写完自己检查每轮是否是完整的话，结尾不能是“……的”“……反而”这种断句。"
            "不要编号、不要 Round、不要强制一来一回；"
            "允许一方连续追问、另一方长答后短驳，长短句交错，不要每轮等长。"
            + ("这是一条信息流短视频，不是快讯：每一轮都要让观众多懂一点它是怎么赚钱的，不能用铺垫消耗前几秒。"
               if max_s <= 90 else
               "这是一条长视频，不是快讯：观众交出的是几分钟的注意力，每一轮都要让他多懂一点它是怎么赚钱的。")
            + "多轮短交锋，别让一个人一口气讲太久：一轮只讲一个点，讲完就把话头递出去。"
            # 态度分家与正面交锋（WAN-29）：系统提示里写过一遍，这里在「今天的正文
            # 该怎么写」这一层再收一次口——模型对紧挨着数据 JSON 的这几句最敏感。
            "两人的态度要分家（多头热、空头冷）：除开场轮和几个字的短回应之外，每次实质发言都先接住对方刚说的原话或数字再反驳，"
            "不许写成两段平行独白；全片必须有一次由具体数字掰过来的观点反转，并且把分歧留到片尾。"
            "但每一轮仍必须有新信息、新角度或新动作，严禁把同一个意思换句话再说一遍；"
            "数字不再按轮计：它出现在对撞的关键处（谁在掏钱、代价压在哪、对方哪句站不住），"
            "允许某一轮一个数字都没有，但凡念出来的数字都要被对方接过去用或当场质疑——"
            "禁止「营收 12.3 亿，同比增长 18%」这种把报表念一遍、没人接的句子。"
            "素材和数据多就往上限展开，素材少就收得住（不低于下限），不要为凑时长注水，也不要意犹未尽地草草收尾。"
            + self._audience_rules()
            + f"本期必须按这条骨架展开（节拍顺序与分工见系统提示，第 1 拍就是开场）：{'→'.join(beat_keys)}。"
            "节拍只进不退，每拍大致对应一轮：closing 是唯一收束拍，只允许最后一轮、"
            "由上一轮说话人的对方用一句短回应（不超过25字）完成；骨架各拍走完后不得再新增论证轮——"
            "还想交锋就把内容写进倒数第二拍（wrap 一类的收束拍），绝不要在 closing 之后继续争论。"
            "每一拍的对话都要落到具体数字或具体环节上，讲清“这意味着什么”，不要停在结论式的形容。"
            "涉及管理层时只能用公司档案里给出的姓名与职务，不评价个人能力、不推测动机；公司动作只说新闻里写了的，并带上出处和时间。"
            "收尾必须是对话收束，不要一方独白结束：最后一拍（closing）由上一拍说话人的对方用一句短回应接住"
            "——一句认可、一句保留，或把最关键的分歧再钉一次。"
            "「股市有风险」「投资需谨慎」这两句固定风险提示由程序在收尾之后以一人一句的方式追加，"
            "不要把它们（或同义说法）写进任何台词。"
            "收尾句式每期必须换（2026-09-29 复盘：十期里七期都是「那就看下一期/下一份财报……」收尾，"
            "观众会腻）：禁止再把「那就看下一…」「咱们看下一期……」当结语模板。要盯的变量会打在片尾画面上，"
            "台词点到变量本身即可，说法要从当期的分歧里自然长出来，例如：「行，那就拿交付量说话。」"
            "「这个分歧先记着，等十月十二号的说明会。」「存货压不压得下来，下一张报表见分晓。」"
            "——例子只是示范语气，别整期复用同一句。"
            + self._interactive_fields(include_sides=True)
            + self._spotlight_rule()
            # --message 的注入点：选材与侧重参考，紧挨 JSON 结构说明之前——
            # 数据 JSON 仍留在提示最尾部，模型的"最后读到的是数据"不变。
            + self._message_section()
            + self._episode_section(payload)
            + "输出结构必须匹配：\n"
            + f"{json.dumps(schema, ensure_ascii=False)}\n股票数据：\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        )

    def _lead_label(self, lead: str) -> str:
        """章节 lead（bull/bear）渲染成给模型的一句话——由谁领讲。

        episode_plan 的五章是**叙事**地图（哪一章讲哪条素材、用哪条开法），
        与几个人在说无关，所以独白片复用同一份计划；只有"由谁领讲"这一句
        必须按形态换——原样写"由股市新手领讲"会诱导模型编出第二个角色。
        """
        return f"由{self._character(lead, lead, '')['name']}领讲" if lead else "谁说都行"

    def _seed_opener(self, side: str) -> str:
        """开法种子里"建议由谁起头"的后缀。独白没有可指派的对象。"""
        return f"，建议由{self._character(side, side, '')['name']}起头" if side else ""

    def _episode_section(self, payload: Mapping[str, Any]) -> str:
        """把本期叙事统筹（episode_plan）写成给模型的五章地图。

        计划是**确定性**产出的（素材与数字额度都带 source_refs，不需要模型
        去挑），模型要做的只是照着它把故事讲出来：哪一章讲哪条素材、由谁领、
        这一期用哪条开法、哪些数字能开口播、哪些只许上屏。这里不重复列素材明细——
        它们已经在数据 JSON 里。
        """
        plan = payload.get("episode_plan")
        if not isinstance(plan, Mapping):
            return ""
        rounds = [r for r in (plan.get("rounds") or []) if isinstance(r, Mapping)]
        if not rounds:
            return ""
        cap = ((plan.get("number_budget") or {}).get("cap") or {})
        line = plan.get("story_line") if isinstance(plan.get("story_line"), Mapping) else None
        lines = ["本期叙事统筹（按这个顺序讲，不要中途换题）："]
        if line:
            # 开头与结尾是**同一条线的两端**：只换句式不换线，五期之后观众照样能
            # 预判。这条也是"结尾不固定"唯一能成立的机制。
            lines.append(f"本期叙事线【{line.get('label') or line.get('id')}】：第 1 章和第 5 章是"
                         f"这条线的两端——第 1 章提出的那个东西，第 5 章必须回到它，不许中途换线。")
        for index, chapter in enumerate(rounds, 1):
            lead = str(chapter.get("lead") or "")
            who = self._lead_label(lead)
            spoken = (chapter.get("number_budget") or {}).get("spoken", 2)
            # 这里不再重复 must_cover：它恒等于 purpose，写两遍等于告诉模型这句要读两次。
            lines.append(f"{index}. {chapter.get('id')}（{chapter.get('purpose') or ''}）：{who}；"
                         f"本章最多口播 {spoken} 个解释单元。")
            if chapter.get("id") == "r1_hook":
                lines.append("   **开场硬约束**：前两句不许出现会计科目（合同负债／预收款／应收账款／"
                             "存货／现金流…）、财务比率（毛利率／净利率／市盈率…）、股价、报告期。"
                             "用产品名、渠道名、客户类型，或一句生意的具体场景开场——"
                             "「合同负债从八十亿骤降到三十一亿」这种开场是最该消失的。")
            if chapter.get("id") == "r5_open_verdict":
                lines.append("   **收尾硬约束**：禁止「等下一季度／下一份报告／下次财报来验证」这类固定"
                             "悬念收尾——它以前不是收尾方式之一，是唯一方式。按本章那条收尾动作收，"
                             "落点可以是开头的那个东西、一个观众能自己去看的动作、一句代价判断，"
                             "或一句处境描述；不预告下一个数据。")
            for seed in [s for s in (chapter.get("sentence_modes") or []) if isinstance(s, Mapping)]:
                side = str(seed.get("speaker") or "")
                opener = self._seed_opener(side)
                lines.append(f"   这一期的开法【{seed.get('mode') or ''}】{opener}：{seed.get('text') or ''}")
        # 示范句里的公司名、产品名、数字都属于它诞生的那一期，不是可以照抄的内容。
        lines.append("开法示范只借用它的**修辞动作**（反问/设问/场景代入/算账…），"
                     "里面的公司名、产品名、数字一律换成这只票自己的素材与 source_refs 里的数；"
                     "示范依赖的事实在这只票上不成立时（只有一块业务的公司就没有第二支柱），"
                     "就换一个同类型的修辞动作，不要硬套。")
        lines.append(f"数字纪律：按**解释单元**计数（一组同口径对比算 1 个，如「毛利九成二、净利五成」是 1 个）；"
                     f"全片 anchor ≤{cap.get('anchor', 4)} 个、analogy ≤{cap.get('analogy', 4)} 个、"
                     "visual_only 不限；**任意连续两轮不得都出现新数字**——一轮报数、下一轮讲它的生意含义。"
                     "anchor 原值口播且必须绑定「所以呢」（念到数字就要讲清它意味什么生意变化）；"
                     "analogy 只讲确定换算后的白话；visual_only 只上屏、不要念出来。"
                     "没有 source_refs 的事实一律不讲。")
        lines.append(self._blacklist_section())
        lines.append("每轮可用 round_id 标出它属于哪一章（只能取上面的 id）；不标则由程序按轮次顺序分配。")
        return "\n" + "\n".join(lines) + "\n"

    def _blacklist_section(self) -> str:
        """把「照抄就变成新套话」的句子写成反例段。

        给原则（"不要播报腔"）没用，模型读不出它指的是什么；给反面样例才有约束力。
        """
        lines = ["下面这些句子是被上一轮内容迭代判死的，遇到同一处境也**不许照抄**，"
                 "只看它为什么被判死："]
        for sentence, reason in SENTENCE_BLACKLIST:
            lines.append(f"· 「{sentence}」——{reason}")
        lines.append("类比必须按**行业**生成：同一句「每 100 元收入有 X 元压在货上」讲制造业是资产效率，"
                     "讲白酒就是错的（基酒越放越值钱，那是资产不是积压）。")
        return "\n".join(lines)

    def _normalize_script(self, response: Any, stock_data: Mapping[str, Any],
                          choice: ArcChoice | None = None,
                          episode_plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(response, Mapping):
            raise DialogueGenerationError("Bailian response does not contain a JSON object")
        raw_turns = response.get("turns", response.get("dialogue"))
        if not isinstance(raw_turns, list) or not raw_turns:
            raise DialogueGenerationError("Bailian must return a non-empty turns array")
        choice = choice or self._active
        beats = list(choice.beats) if choice else []
        beat_keys = beats_by_key(beats)
        turns: list[dict[str, Any]] = []
        # 每轮最终台词对应的模型原始条目：章节分配在拆分之后统一做，那时还要按它
        # 读模型自己给的 round_id/visual_id。
        entries: list[Mapping[str, Any]] = []
        notes: list[str] = []
        cursor = 0
        # 片尾风险提示交换轮是双人机制（一人一句接力），独白形态下没有第二个人可接，
        # 且 dialogue.monologue.risk_exchange 默认关闭——这里再挡一层，避免某处配置
        # 单独把它打开后，同一个声音念两句免责声明（合规提示仍打在片尾画面卡上）。
        append_risk = (bool(self.config.risk_exchange) and bool(raw_turns)
                       and self.format != FORMAT_MONOLOGUE)
        for index, entry in enumerate(raw_turns, 1):
            if not isinstance(entry, Mapping):
                raise DialogueGenerationError(f"turn {index} is not an object")
            speaker = str(entry.get("speaker") or entry.get("role") or "").lower()
            # 独白形态：说话人恒等于 narrator。模型偶尔会带回对辩习惯（"bull"/
            # "分析师"），这里归一而不是报错——形态是产品决策，不该因为模型多写
            # 一个词就废掉一整期生成（一整轮 LLM + 数据抓取都要重来）。
            if self.format == FORMAT_MONOLOGUE:
                if speaker != MONOLOGUE_SPEAKER:
                    notes.append(f"[第{index}轮] speaker “{speaker or '空'}” 不是 narrator，"
                                 "已统一归一——独白片只有一个说话人")
                speaker = MONOLOGUE_SPEAKER
            elif speaker not in self.speakers:
                raise DialogueGenerationError(
                    f"turn {index} must name one of {', '.join(self.speakers)} as speaker")
            line = self._clean_line(entry.get("line"))
            if not line:
                raise DialogueGenerationError(f"turn {index} line is empty")
            beat, cursor = self._resolve_beat(entry.get("beat"), beats, cursor, index, notes)
            # 硬约束：closing 一来一回最多 2 轮。骨架走完后模型继续论证时，游标会把
            # 后续轮全压到 closing 拍（2026-09-27 600295 实锤：第 7-11 轮连续 5 个
            # closing）；closing 挂 short 标记，渲染层跳过图板更新，主画面就此空白。
            # 长于该拍字数上限的 closing 轮降级回最近的信息拍，图板照常更新；
            # 真正的短收尾仍归 closing——prompt 约束在前，这里兜底。
            if beat is not None and beat.short and cursor == len(beats) - 1 \
                    and len(line) > max(beat.max_chars or 0, self.config.short_line_chars):
                demoted = next((b for b in reversed(beats[:cursor]) if not b.short), None)
                if demoted is not None:
                    notes.append(f"[第{index}轮] closing 拍出现长台词（{len(line)}字），"
                                 f"降级为 {demoted.key}——closing 最多一来一回")
                    beat = demoted
            # 节拍声明为短回应、或者模型自己就写了极短的一句，都按"短回应"处理：
            # 这类轮次不换画面图板（一句话的认可没必要把图板重画一遍）。
            short = bool(beat and beat.short) or len(line) <= self.config.short_line_chars
            if beat and beat.speaker and beat.speaker != speaker:
                notes.append(f"[第{index}轮] 骨架指定由 {beat.speaker} 说这段（{beat.key}），模型派给了 {speaker}")
            visuals = self._clean_visuals(entry.get("visual"))
            # 按轮情绪：词表外/缺省一律回落「平述」并留痕——TTS 端拿到的 emotion
            # 必须是 EMOTION_DIRECTIVES 里存在的 key，不许未定义行为。
            emotion = str(entry.get("emotion") or "").strip()
            if emotion not in TURN_EMOTIONS:
                if emotion:
                    notes.append(f"[第{index}轮] emotion “{emotion}” 不在词表内，按平述处理")
                emotion = "平述"
            # 模型没管住字数时，把超长台词拆成同说话人的续轮——一个字都不丢，
            # 而且正好贴合"轮次多一点、每次讲短一点"的节奏（2026-09-22 用户反馈）。
            pieces = self._split_line(line)
            for offset, piece in enumerate(pieces):
                turns.append({"speaker": speaker, "line": piece,
                              "beat": beat.key if beat else self._clean_beat(entry.get("beat")),
                              # 只有首段能算短回应；续轮是正常发言，别跳过图板
                              "short": short and offset == 0,
                              "visual": visuals,
                              # 续轮继承同一情绪：它们是同一句话的拆分，语气不该变
                              "emotion": emotion,
                              "character_name": self._character(speaker, speaker, "")["name"]})
                entries.append(entry)
        # 章节分配的轮数基准必须是「最终成片有几轮」：超长台词会被 _split_line 拆成
        # 续轮，拿模型给的轮数当基准时，片尾两轮的 index 会溢出 total，末章判定的
        # {total, total-1} 匹配不上——收尾两句会落进 R4 章，R5 全片 0 轮，规划好的
        # timeline 图一次都不出现。片尾风险提示交换轮是程序追加的，同样要算进来，
        # 算漏了末章也会被正文的倒数第二轮顶掉。
        total_turns = len(turns) + (2 if append_risk else 0)
        # 片尾风险提示交换轮（2026-10-08 第二版）：一人一句的接力——「股市有风险」
        # 由一位角色说出口，「投资需谨慎」由对方接住，像对话的最后一次交锋而不是
        # 贴在片尾的免责声明。措辞按个股特征（行业/主营/股名）从模板池挑：有数据
        # 才用特征模板，合格候选按 (代码, 日期) 稳定分散——同一只票同一天可复现，
        # 不同票、不同日子不撞形（与骨架选择同一哲学）。全程确定性、不烧 LLM：
        # 合规底线一期都不能漏，也不能押在模型自觉上。说话人按「与最后一轮对调」
        # 分配，交替感与 TTS 音色切换天然成立；标 short——两句话不值得重画图板。
        self._risk_choice: dict[str, str] = {}
        if self.config.risk_exchange and turns:
            quote = stock_data.get("quote") if isinstance(stock_data.get("quote"), Mapping) else {}
            risk_code = str(stock_data.get("code") or quote.get("code") or "")
            self._risk_choice = build_risk_exchange(stock_data, code=risk_code,
                                                    day=date.today().isoformat())
            last_speaker = turns[-1]["speaker"]
            first_speaker = next((name for name in self.speakers if name != last_speaker),
                                 last_speaker)
            for offset, (speaker, line) in enumerate(
                    ((first_speaker, self._risk_choice["a"]), (last_speaker, self._risk_choice["b"]))):
                turns.append({"speaker": speaker, "line": line,
                              "beat": "closing", "short": True, "visual": [],
                              "emotion": "平述",
                              "character_name": self._character(speaker, speaker, "")["name"]})
        # 章节标记统一在这里打：此时轮数已经定死（超长台词的续轮、片尾两轮都在里面），
        # index 与 total 用的是同一个基准，末章判定在 0～N 次拆行下都成立。
        # 按"成片实际有几句"分章、不按"模型逻辑上有几轮"，所以**同一句被拆成两段时
        # 前后半段会落进相邻两章**——这是刻意的：观众听到的是句子，换图就是转场。
        # 别把它"修"回跟随父轮，那条语义由
        # SplitAwareChapterTests.test_a_split_line_may_land_in_two_adjacent_chapters_and_that_is_intended
        # 守着：测试名写在这里，是让人删到它时先看到它在护什么。
        for position, turn in enumerate(turns, 1):
            entry = entries[position - 1] if position <= len(entries) else {}
            turn.update(self._turn_metadata(entry, position, episode_plan, total=total_turns))
        quote = stock_data.get("quote") if isinstance(stock_data.get("quote"), Mapping) else {}
        code, name = str(stock_data.get("code") or quote.get("code") or ""), str(quote.get("name") or stock_data.get("code") or "股票")
        char_count = sum(len(turn["line"]) for turn in turns)
        script = {"stock_code": code, "stock_name": name, "title": self._clean_title(response.get("title"), name, code),
                "turns": turns,
                # 内容形态（debate / monologue）：剧本 JSON 是渲染与审查的契约，
                # 画面要据此决定"几人卡司、要不要双栏 CTA"，不能只看 config
                # （旧成片重渲时 config 可能已经换了形态）。
                "format": self.format,
                **({"episode_plan": episode_plan} if episode_plan else {}),
                # 本期用了哪条骨架、哪些节拍因为没数据被剪掉、模型哪里没照着走。
                # 结构"是否真的变了"要可核对，不能只看 prompt 写了什么。
                "arc": choice.report() if choice else None,
                "arc_notes": notes,
                "hook": self._clean_text(response.get("hook"), 60),
                "question": self._clean_text(response.get("question"), 60),
                "sides": self._clean_sides(response.get("sides")),
                # 首帧/封面冲突数字（上屏专用）；模型没给就 None，画面不硬凑
                "spotlight": self._clean_spotlight(response),
                # 片尾风险提示交换（程序追加的两轮）：留痕供审查页核对「这期为什么是这句」
                **({"risk_exchange": self._risk_choice} if self._risk_choice else {}),
                "char_count": char_count,
                # 按实测语速换算的自估时长。长片最容易出的偏差是模型多写几百字，
                # 把它留在结果里，pipeline 就能在渲染前提醒（而不是渲完才发现超长）。
                "estimated_seconds": round(char_count / SPEECH_CHARS_PER_SECOND, 1)}
        script["quality"] = self._style_audit(turns)
        for flag in script["quality"]["flags"]:
            notes.append(f"[文风检查] {flag}")
        # 锐度判定清单（2026-10-27 WAN-27）：把"够不够锐"拆成七条可核对的判定，
        # 与 quality 一起落进剧本 JSON。同一只票改动前后各跑一次，哪几条从红转绿
        # 一眼可见——否则"改好了没有"只能凭印象比较两期节目。
        # 锐度不进 arc_notes：那里是"模型没照骨架走"的偏差记录，混进质量体检会
        # 让「偏差」一栏变性；两项各自落在 quality / sharpness 字段里，审查页分栏看。
        script["sharpness"] = sharpness.evaluate(script)
        if self.config.message:
            # 用了 --message 就要留痕：这期"为什么讲得跟别期不一样"必须能在成片
            # 旁边的 JSON / 审查页里看到原话，否则提示词是一条断掉的回溯链。
            script["message"] = self.config.message
        return script

    @staticmethod
    def _chapter_index(index: int, total: int, chapters: int = 5) -> int:
        """把 turn 流按在总长中的位置切成 chapters 段，返回 0-based 段号。

        必须满足三条：段号随 index 单调不回退、每一段都可能被分到、段与段之间
        只由「turn 在整段对话里的相对位置」决定（与 turn 总数无关地稳定）。
        早先的 ``((index - 1) * 5) // (1 + rounds * 2)`` 是一条随手写的直线：
        turn 数一变就把章节切歪（12 轮时后两章空、24 轮时前两章吃掉一半）。
        """
        count = max(1, int(total or 0))
        chapters = max(1, int(chapters))
        position = min(max(1, int(index)), count)
        # ceil(position * chapters / count) - 1：第 1 轮必在第 0 段，最后一轮必在末段。
        return min(chapters - 1, max(0, -(-position * chapters // count) - 1))

    @classmethod
    def _turn_metadata(cls, entry: Mapping[str, Any], index: int,
                       plan: Mapping[str, Any] | None, total: int = 0) -> dict[str, Any]:
        """给一轮台词打上「属于哪一章、讲哪条素材、配哪块图板」的标记。

        模型显式给了 round_id/visual_id 就以模型为准（它看得见自己写了什么）；
        没给就按 turn 在整段对话中的位置落到对应章节——章节由图板计划持有，
        所以这一处决定了画面按 ``visual_id`` 取图，而不是反过来从台词猜。
        """
        if not isinstance(plan, Mapping):
            return {"round_id": str(entry.get("round_id") or ""), "material_ids": list(entry.get("material_ids") or []),
                    "metric_ids": list(entry.get("metric_ids") or []), "visual_id": str(entry.get("visual_id") or "")}
        rounds = [r for r in (plan.get("rounds") or []) if isinstance(r, Mapping)]
        if not rounds:
            return {"round_id": "", "material_ids": [], "metric_ids": [], "visual_id": ""}
        rid = str(entry.get("round_id") or "")
        selected = next((r for r in rounds if str(r.get("id")) == rid), None)
        if selected is None:
            # 末章只给片尾两轮（风险提示交换轮），不按剩余 turn 均分。
            body = [r for r in rounds if str(r.get("id")) != "r5_open_verdict"] or rounds
            tail = next((r for r in rounds if str(r.get("id")) == "r5_open_verdict"), None)
            if total > len(rounds) and tail is not None:
                last_chapter_turns = {total, total - 1}
                if int(index) in last_chapter_turns:
                    selected = tail
            if selected is None:
                selected = body[min(len(body) - 1, cls._chapter_index(index, total, len(body)))]
        visual = selected.get("visual") if isinstance(selected.get("visual"), Mapping) else {}
        # 这一轮上场的口播数字 = 本章的 anchor + analogy（R2 讲的是毛利率那条 analogy，
        # 只看 anchor 会让它的指标追溯为空）。
        spoken_metrics = [*list(selected.get("anchor_metric_ids") or []),
                          *list(selected.get("analogy_metric_ids") or [])]
        return {"round_id": str(selected.get("id") or ""), "material_ids": list(entry.get("material_ids") or selected.get("material_ids") or []),
                "metric_ids": list(entry.get("metric_ids") or spoken_metrics),
                "visual_id": str(entry.get("visual_id") or visual.get("id") or "")}

    @staticmethod
    def _style_audit(turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """成片的套话/重复度体检——规则集中在 ``copy_guard``（2026-10-27）。

        刻意只报告、不改写：二次改写会伤到事实措辞（数字读法、来源归属），而
        一次体检把命中项与原文摆出来，人就能只改那一句。清单与模板池扫描共用
        同一份，避免"新套话只进了其中一处"。
        """
        return copy_guard.audit(turns)

    @staticmethod
    def _clean_spotlight(response: Mapping[str, Any]) -> dict[str, str] | None:
        """首帧/封面冲突数字（2026-10-07）。

        纯上屏元素，value 统一过 ``to_display`` 转阿拉伯——模型偶尔会把口播
        的完整读法（「四亿五千万」）带进来，上屏必须回到「4.5亿」。
        label/value 任一缺失就返回 None：画面不硬凑一个空壳。
        """
        raw = response.get("spotlight")
        if not isinstance(raw, Mapping):
            return None
        label = re.sub(r"\s+", "", str(raw.get("label") or ""))[:8]
        value = re.sub(r"\s+", "", str(raw.get("value") or ""))[:16]
        if not label or not value:
            return None
        return {"label": label, "value": to_display(value)}

    @staticmethod
    def _resolve_beat(reported: Any, beats: Sequence[Beat], cursor: int, index: int,
                      notes: list[str]) -> tuple[Beat | None, int]:
        """把模型报的 beat 落到骨架的某个节拍上。

        骨架顺序是权威的：游标只前进、不后退。模型漏报或写了个骨架外的名字，
        就沿用当前节拍并记一条备注——宁可结构稳、也不因为一次错字把整期打乱。
        """
        if not beats:
            return None, cursor
        keys = beats_by_key(beats)
        name = str(reported or "").strip()
        if name not in keys:
            notes.append(f"[第{index}轮] beat “{name or '空'}” 不在本期骨架内，"
                         f"归入第{cursor + 1}拍（{keys[cursor]}）")
            return beats[cursor], cursor
        position = keys.index(name)
        if position < cursor:
            notes.append(f"[第{index}轮] beat “{name}” 回退了骨架顺序（已到第{cursor + 1}拍），按当前节拍处理")
            return beats[cursor], cursor
        return beats[position], position

    def _clean_line(self, value: Any) -> str:
        """清洗台词。只去空白，不做字数截断——截断会把句子砍成半截，
        配音照着半句念，听感就是"语音被掐掉"（2026-09-22 000066 实锤）。
        超长交给 :meth:`_split_line` 拆成续轮，内容一个字都不丢。
        数字走口播层修复：模型漏网的「四亿五一」式缩略读法修成「4.51亿」，
        完整读法原样保留（TTS 对完整汉字数字的发音是可靠的）。"""
        return repair_spoken_numbers(re.sub(r"\s+", "", str(value or "")))

    def _split_line(self, value: Any) -> list[str]:
        """把超长台词按标点拆成多轮（每轮 ≤ max_line_chars），保句子的完整。

        优先在句号/问号/叹号/分号处断（完整句边界），其次逗号/顿号/冒号，
        都没有才硬切。软目标靠 prompt 传达，这里只兜底。
        """
        text = re.sub(r"\s+", "", str(value or ""))
        limit = self.config.max_line_chars
        if len(text) <= limit:
            return [text] if text else []
        chunks: list[str] = []
        while len(text) > limit:
            window = text[:limit]
            # 先找完整句边界；最近的句边界太靠前（切出来太碎）就退到句内标点
            cut = max(window.rfind(p) for p in "。！？；…")
            if cut < limit // 2:
                cut = max(window.rfind(p) for p in "，、：,—-")
            if cut < limit // 2:
                cut = limit - 1
            head, text = text[:cut + 1], text[cut + 1:]
            head = head.rstrip("，、；：,")
            if head and head[-1] not in "。！？…":
                head += "。"
            chunks.append(head)
        if text:
            text = text.rstrip("，、；：,")
            if text and text[-1] not in "。！？…":
                text += "。"
            chunks.append(text)
        return chunks

    @staticmethod
    def _clean_beat(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "自然推进")).strip()[:40] or "自然推进"

    @staticmethod
    def _clean_visuals(value: Any) -> list[str]:
        """Keep up to 4 non-empty keyword phrases extracted from the turn line.

        visual 会直接上屏，汉字数字统一转阿拉伯显示（「毛利率十八点二九」→
        「毛利率18.29」），观众扫一眼就读懂，也不会再漏进封面标签。
        """
        items = value if isinstance(value, list) else []
        visuals: list[str] = []
        for item in items:
            text = re.sub(r"\s+", " ", str(item)).strip()[:24]
            text = to_display(text)
            if text and text not in visuals:
                visuals.append(text)
            if len(visuals) == 4:
                break
        return visuals

    @staticmethod
    def _clean_title(value: Any, name: str, code: str) -> str:
        title = re.sub(r"\s+", " ", str(value or "")).strip()
        return title[:60] or f"{name}（{code}）这家公司怎么看"

    @staticmethod
    def _clean_text(value: Any, limit: int) -> str:
        """一句用于片尾的短文案（hook / question），去掉换行、限长。"""
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text[:limit]

    def _clean_sides(self, value: Any) -> dict[str, str]:
        """多空各一句立场；只认配置里的角色，缺的就留空。

        独白形态恒返回空表：sides 是"多空两个立场"，单人片里根本没有第二个立场。
        留空而不是塞一句占位，下游（审查页）会把缺失的一侧显示成「—」。
        """
        if self.format == FORMAT_MONOLOGUE:
            return {}
        source = value if isinstance(value, Mapping) else {}
        return {role: self._clean_text(source.get(role), 60) for role in ("bull", "bear")}

    def _character(self, role: str, default_name: str, default_persona: str) -> dict[str, str]:
        value = self.characters.get(role, {})
        value = value if isinstance(value, Mapping) else {}
        return {"name": str(value.get("name") or default_name), "persona": str(value.get("persona") or default_persona)}

    @staticmethod
    def _recent_sessions(technical: Mapping[str, Any], days: int = 15) -> list[dict[str, str]]:
        """Digest the tail of the real indicator history for the dialogue prompt.

        Only close price, daily change and the provider's own signal words are
        kept — enough for the script to reference the actual recent market
        rhythm without ballooning the prompt with the full indicator payload.
        """
        history = technical.get("history") if isinstance(technical.get("history"), list) else []
        sessions: list[dict[str, str]] = []
        for item in [x for x in history if isinstance(x, Mapping)][-days:]:
            price = item.get("price") if isinstance(item.get("price"), Mapping) else {}
            session: dict[str, str] = {"date": str(item.get("timestamp") or "")}
            if price.get("current") is not None:
                session["close"] = str(price.get("current"))
            if price.get("change_pct") not in (None, 0):
                session["change_pct"] = str(price.get("change_pct"))
            for group, field in (("ma", "trend"), ("macd", "signal"), ("rsi", "signal")):
                block = item.get(group) if isinstance(item.get(group), Mapping) else {}
                word = str(block.get(field) or "")
                if word and word != "neutral":
                    session[f"{group}_{field}"] = word
            if len(session) > 1:
                sessions.append(session)
        return sessions

    @classmethod
    def _compact_data(cls, data: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in ("code", "quote", "technical", "financials", "f10", "board", "stockinfo", "news", "unavailable"):
            if key not in data: continue
            value = data[key]
            if key == "technical" and isinstance(value, Mapping):
                recent = cls._recent_sessions(value)
                value = {n: value[n] for n in ("summary", "count") if n in value}
                if recent: value["近段行情"] = recent
            elif key == "f10" and isinstance(value, Mapping):
                # Send the parsed 公司档案, not the raw F10 tables: the tables are
                # tens of thousands of box-drawing characters that would crowd
                # out everything else while burying the few facts that matter.
                profile = value.get("profile")
                if not isinstance(profile, Mapping) or not profile: continue
                value = dict(profile)
                # 主营构成明细名统一成图板同款简称（2026-09-29 评审：旁白说
                # 「产线集成」、画面写「自动化装配与检测生产线」，同一个东西两个
                # 名字）。prompt 与渲染共用 short_business_label，观众听到和
                # 看到的就是同一个词。
                composition = value.get("主营构成")
                if isinstance(composition, Mapping) and isinstance(composition.get("明细"), list):
                    rows = [dict(row) if isinstance(row, Mapping) else row for row in composition["明细"]]
                    for row in rows:
                        if isinstance(row, Mapping) and row.get("项目"):
                            row["项目"] = short_business_label(str(row["项目"]))
                    value["主营构成"] = dict(composition, 明细=rows)
                # 财务历史整包有 60+ 指标，prompt 只吃最常讨论的一批；图表层
                # 仍用全量（结构化对象按名精确取），互不影响。
                trimmed = StockDataClient.trim_history_for_prompt(profile.get("财务历史"))
                if trimmed:
                    value["财务历史"] = trimmed
                composition_history = value.get("主营构成历史")
                if isinstance(composition_history, list) and len(composition_history) > 3:
                    value["主营构成历史"] = composition_history[:3]
            elif key == "stockinfo" and isinstance(value, Mapping):
                metrics = value.get("metrics")
                if not isinstance(metrics, Mapping) or not metrics: continue
                value = dict(metrics)
            elif key == "board" and isinstance(value, Mapping):
                # 行业与概念是 tongstock 真实给出的分类（block show），是「这公司到底
                # 做什么」最直接的素材——比行情数字更能撑起内容。
                value = {n: [str(x) for x in value[n]][:8] for n in ("industry", "concepts")
                         if isinstance(value.get(n), list) and value[n]}
                if not value: continue
            elif key == "news" and isinstance(value, Mapping):
                digest = cls._news_digest(value)
                if digest: value = {"items": digest}
                else: continue
            result[key] = value
        return result

    @staticmethod
    def _news_digest(news: Mapping[str, Any], limit: int = 8) -> list[dict[str, str]]:
        """Trim real news items to the headline facts usable as script material."""
        items = news.get("items") if isinstance(news.get("items"), list) else []
        digest: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            entry: dict[str, str] = {"title": title[:80]}
            for field in ("type", "source", "publish_time"):
                text = str(item.get(field) or "").strip()
                if text:
                    entry[field] = text
            summary = str(item.get("summary") or "").strip()
            if summary:
                entry["summary"] = summary[:120]
            digest.append(entry)
            if len(digest) == limit:
                break
        return digest

    @staticmethod
    def _decode_response(raw: str) -> Any:
        try: parsed: Any = json.loads(raw)
        except json.JSONDecodeError: parsed = raw
        for _ in range(4):
            if isinstance(parsed, Mapping):
                if "turns" in parsed or "dialogue" in parsed: return parsed
                parsed = next((parsed[k] for k in ("content", "text", "message", "output", "response", "choices") if k in parsed), parsed)
                if isinstance(parsed, list) and parsed: parsed = parsed[0]
            elif isinstance(parsed, str):
                try: parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", parsed.strip(), flags=re.I))
                except json.JSONDecodeError as exc: raise DialogueGenerationError("Bailian returned invalid JSON script") from exc
            else: break
        return parsed


class MonologueGenerator(DialogueGenerator):
    """单人独白：一个人把一家公司讲透（2026-10-24）。

    继承而不是复制整条生成链路：数据压缩、骨架剪枝、数字口播修复、超长拆行、
    章节分配、文风审计、合规审查全部复用——它们与"几个人在说"无关。真正按形态
    分叉的只有三处：谁在说话（prompt 头 + speaker 归一）、怎么收尾（没有对方可
    接）、片尾风险提示（双人接力轮关掉）。
    """

    format: str = FORMAT_MONOLOGUE

    def _system_prompt(self, arc: Arc | None = None) -> str:
        choice = self._choice_for(arc)
        narrator = self._character(MONOLOGUE_SPEAKER, "谈股论金",
                                   "一个人把一家公司掰开讲的财经讲述人")
        return (
            "你是财经短视频编导，为对投资感兴趣的普通观众写一段**一个人把一家公司掰开讲**的中文独白。"
            "只依据提供的数据发言；不编造数字、新闻或事实，不预测涨跌，不给出买卖或仓位建议，不承诺收益。\n"
            f"讲述人：{narrator['name']}，人设：{narrator['persona']}。\n"
            f"全片只有这一个说话人：每一轮的 speaker 都写 {MONOLOGUE_SPEAKER}。"
            "不要虚构第二个角色，也不要写成「一人分饰两角的辩论」——"
            "这是一个人把这家公司讲给观众听，不是把一份对辩剧本一个人念完。\n"
            + self._content_rules(emotion_close="独白没有对手接话，情绪起伏就是唯一的节奏")
            + self._system_tail(choice)
        )

    def _system_tail(self, choice: ArcChoice) -> str:
        """独白形态的收束规则：节奏与风格按"一个人连讲两分钟"重写。"""
        return (
            "\n" + self._arc_section(choice) + "\n"
            + self._shared_opening_rules(
                tension="顺着自己的追问把代价和疑点铺深",
                closing="片尾收束把落点交回那个观众能自己去核对的变量",
                deduced="从你的讲述里自己拼出来",
            )
            + "\n独白节奏（重要）：一个人连讲两三分钟，节奏就是命。长度由内容决定，该长则长、该短则短；"
            "不要把每一段都写成一样长，也不要为了填时长把一句话拆成三段。"
            "允许自问自答、设问、停顿（……）、破折号打断自己的思路——但不许写出第二个说话人，"
            "也不要「有人说」「也有观点认为」式的转述。"
            "一段里把一个点讲完整再往下走，不要东一句西一句。"
            "全片不出现「嗯」「有道理」「但是——」这类对辩用的短回应垫话——没有人在跟你对话。\n"
            "\n风格要求：像一个懂行的人在把一家公司讲给你听，允许自问自答、停顿（……或破折号）、"
            "讲到一半想起一个更关键的点再折回来、惊讶、沉吟。"
            "每一段都必须提供新信息、新数字或新角度，严禁把同一个意思换句话再说一遍；"
            "鼓励连续两三段围绕一个环节层层深挖（是什么→凭什么→代价在哪→同行怎么比），"
            "再自然转到下一个环节。"
            "数据存在时引用具体数字；数据缺失就坦诚说缺，不硬编。\n"
            "必须只输出 JSON，不要 Markdown。"
        )

    def _user_prompt(self, payload: Mapping[str, Any], arc: Arc | None = None) -> str:
        choice = self._choice_for(arc, payload)
        min_s, max_s = self.config.min_duration_seconds, self.config.max_duration_seconds
        min_chars = int(min_s * SPEECH_CHARS_PER_SECOND)
        budget_chars = int(max_s * SPEECH_CHARS_PER_SECOND)
        floor_chars = max(30, self.config.max_line_chars // 2)
        # 典型段落比对辩长得多（220 字上限 vs 对辩 80）：一个人连讲时，80 字一段
        # 会碎成十几段没人接话的短句，讲不透一件事。
        typical_chars = max(60, (floor_chars + self.config.max_line_chars) // 2)
        min_turns = max(len(choice.beats), min_chars // typical_chars)
        max_turns = max(min_turns + 3, budget_chars // typical_chars)
        minutes = (f"{self._minutes(min_s)}-{self._minutes(max_s)} 分钟" if max_s > min_s
                   else f"{self._minutes(max_s)} 分钟")
        beat_keys = beats_by_key(choice.beats)
        narrator = self._character(MONOLOGUE_SPEAKER, "谈股论金", "")
        schema = {"title": "股票名（代码）：一句话点出这家公司的看点或它在行业里的位置", "turns": [
            {"speaker": MONOLOGUE_SPEAKER,
             "line": f"一段完整自然的话，{floor_chars} 到 {self.config.max_line_chars} 字；"
                     "说不下就拆成下一段",
             "beat": f"骨架节拍，只能取：{'/'.join(beat_keys)}",
             "emotion": "平述（只能取：反驳/质疑/惊叹/顿悟/坚守/平述，见系统提示 8b）",
             "visual": ["line 中提到的关键词1", "关键词2"],
             "round_id": "本轮所属叙事章节 id，只能取下面五章之一（可省略）"}],
            "hook": "下一期要盯的一个可验证的具体变量",
            "question": "抛给观众的一个生意层面二选一问题",
            "spotlight": {"label": "指标名（≤8字）", "value": "冲突数字短语（≤16字，阿拉伯数字）"},
            }
        return (
            f"生成一段成片时长在 {min_s}-{max_s} 秒（约 {minutes}）的**单人独白**："
            f"全片由 {narrator['name']} 一个人从头讲到尾，每一轮的 speaker 都写 {MONOLOGUE_SPEAKER}，"
            "不要出现第二个说话人。\n"
            f"正文总字数控制在 {min_chars}-{budget_chars} 字之间（配音约 4.8 字/秒，超字数就会超时长），"
            f"分 {min_turns}-{max_turns} 段，单段 {floor_chars}-{self.config.max_line_chars} 字。"
            "单段字数是节奏参考，不是硬上限：先把句子说完整，再考虑字数。"
            "任何一句话都不许为了压字数砍成半截——宁可多拆一段，也不能丢掉半句意思；"
            "写完自己检查每段是否是完整的话，结尾不能是“……的”“……反而”这种断句。"
            "不要编号、不要 Round。"
            + ("这是一条信息流短视频，不是快讯：每一段都要让观众多懂一点它是怎么赚钱的，不能用铺垫消耗前几秒。"
               if max_s <= 90 else
               "这是一条长视频，不是快讯：观众交出的是几分钟的注意力，每一段都要让他多懂一点它是怎么赚钱的。")
            +             "独白不是一人分饰两角，也不是把一份对辩剧本一个人念完：全片只有一个人在讲，"
            "没有「看多看空的两个立场」要你轮流代言，讲的就是你自己对这家公司的判断——"
            "但理解必须长在数据上，数据说什么就是什么。"
            "素材和数据多就往上限展开，素材少就收得住（不低于下限），不要为凑时长注水，也不要意犹未尽地草草收尾。"
            + self._audience_rules(opening="开头两段", where="叙事里")
            + f"本期必须按这条骨架展开（节拍顺序见系统提示，第 1 拍就是开场）：{'→'.join(beat_keys)}。"
            "节拍只进不退，每拍至少一段；骨架各拍走完后不得再新增论证段——"
            "还想展开就把内容写进倒数第二拍（takeaway 一类的收束拍）。"
            "每一拍都要落到具体数字或具体环节上，讲清“这意味着什么”，不要停在结论式的形容。"
            "涉及管理层时只能用公司档案里给出的姓名与职务，不评价个人能力、不推测动机；公司动作只说新闻里写了的，并带上出处和时间。"
            "收尾由你一个人完成：最后一拍（takeaway）把本期最值得带走的认知钉住，"
            "并落在一个观众能自己去核对的具体变量上，不要戛然而止，也不要「等下一份财报来验证」式的空悬念。"
            "「股市有风险」「投资需谨慎」这两句固定风险提示**不会**由本片播报（片尾画面卡上会打出来），"
            "不要把它们（或同义说法）写进任何台词。"
            "收尾句式每期必须换（2026-09-29 复盘：十期里七期都是「那就看下一期/下一份财报……」收尾，"
            "观众会腻）：禁止再把「那就看下一…」「咱们看下一期……」当结语模板。"
            "要盯的变量会打在片尾画面上，台词点到变量本身即可，说法要从当期讲过的内容里自然长出来，"
            "例如：「渠道里压着多少货、这批货能不能变成钱，是这家公司接下来的真问题。」"
            "——例子只是示范语气，别整期复用同一句。"
            + self._interactive_fields(include_sides=False)
            + self._spotlight_rule()
            # --message 的注入点：选材与侧重参考，紧挨 JSON 结构说明之前——
            # 数据 JSON 仍留在提示最尾部，模型的"最后读到的是数据"不变。
            + self._message_section()
            + self._episode_section(payload)
            + "输出结构必须匹配：\n"
            + f"{json.dumps(schema, ensure_ascii=False)}\n股票数据：\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        )

    def _lead_label(self, lead: str) -> str:
        """独白片没有"谁领讲"：五章仍是有用的叙事地图，但 lead 是双人片的产物。"""
        name = self._character(MONOLOGUE_SPEAKER, "谈股论金", "")["name"]
        return f"本章由{name}一个人从头讲到尾"

    def _seed_opener(self, side: str) -> str:
        """开法种子里的"建议由谁起头"在单人片里没有指派对象。"""
        return ""


def create_dialogue_generator(config: Mapping[str, Any] | None = None, runner: Runner | None = None,
                              llm: Any = None) -> DialogueGenerator:
    """按 ``dialogue.format`` 选生成器（debate → 对辩，monologue → 独白）。

    用工厂而不是让调用方 if：pipeline / 守护进程 / 单测三处都要建生成器，
    形态判断漏掉任何一处，就会出现"prompt 是独白、画面是对辩"这种半对齐。
    """
    cls = MonologueGenerator if dialogue_format(config) == FORMAT_MONOLOGUE else DialogueGenerator
    return cls(config, runner=runner, llm=llm)


def generate_dialogue(stock_data: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return create_dialogue_generator(config).generate(stock_data)
