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

from stocktalk.modules.arcs import (Arc, ArcChoice, Beat, beats_by_key,
                                    resolve_speakers, select_arc)
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

# Deterministic style audit: these are not hard bans (a quote may legitimately
# contain one), but repeated use is a strong signal of canned AI prose.  Keep
# the audit observable in the run JSON so a human can decide whether to rewrite
# a line instead of hiding a quality failure behind another full generation.
STYLE_CLICHES = ("说白了", "总的来说", "值得注意的是", "不难发现", "在当前市场环境下",
                 "我们先看", "接下来我们", "归根结底")
STYLE_IDIOMS = ("一针见血", "锦上添花", "顺势而为", "乘风破浪", "稳中有进", "大有可为",
                "水涨船高", "如火如荼", "不言而喻", "毋庸置疑")

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


class DialogueGenerator:
    """Return ordered turns, not artificial paired debate rounds."""

    def __init__(self, config: Mapping[str, Any] | None = None, runner: Runner | None = None,
                 llm: Any = None) -> None:
        source = dict((config or {}).get("dialogue", config or {}))
        self.config = DialogueConfig(
            model=str(source.get("model", DialogueConfig.model)),
            min_duration_seconds=int(source.get("min_duration_seconds", DialogueConfig.min_duration_seconds)),
            max_duration_seconds=int(source.get("max_duration_seconds", DialogueConfig.max_duration_seconds)),
            max_line_chars=int(source.get("max_line_chars", DialogueConfig.max_line_chars)),
            max_tokens=int(source.get("max_tokens", DialogueConfig.max_tokens)),
            temperature=float(source.get("temperature", DialogueConfig.temperature)),
            timeout_seconds=float(source.get("timeout_seconds", DialogueConfig.timeout_seconds)),
            arc=str(source.get("arc", DialogueConfig.arc) or DialogueConfig.arc),
            short_line_chars=int(source.get("short_line_chars", DialogueConfig.short_line_chars)),
            message=self._normalize_message(source.get("message")),
            risk_exchange=source.get("risk_exchange", source.get("risk_line", True)) not in (False, "", None, "false", "0"),
        )
        if (self.config.min_duration_seconds < 1 or self.config.max_duration_seconds < self.config.min_duration_seconds
                or not 30 <= self.config.max_line_chars <= 150 or self.config.max_tokens < 1):
            raise ValueError("duration bounds, max_line_chars (30--150), and max_tokens must be valid")
        if not 0 < self.config.temperature <= 2:
            raise ValueError("temperature must be in (0, 2]")
        self.characters = dict((config or {}).get("characters", {}))
        # 说话的只有这两个角色，但"有哪几个角色"由配置决定而不是写死：
        # 之前 {"bull", "bear"} 硬编码在两处（这里与 tts_agent），新增角色会被
        # 静默丢弃——一个不报错的坑。配置里加了角色就自动可用。
        self.speakers = resolve_speakers(self.characters)
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
        """按这只票的数据挑骨架，并记下选择结果供 prompt 与审计复用。"""
        quote = payload.get("quote") if isinstance(payload.get("quote"), Mapping) else {}
        code = str(payload.get("code") or quote.get("code") or "")
        choice = select_arc(payload, configured=self.config.arc, code=code,
                            day=date.today().isoformat())
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
        choice = self._choice_for(arc)
        bull = self._character("bull", "增长派", "擅长类比、未来叙事和增长逻辑；承认数据边界")
        bear = self._character("bear", "审计派", "擅长数据引用、风险揭示和历史比较；追问叙事")
        return (
            "你是财经短视频编导，为对投资感兴趣的普通观众写一段两人聊公司的中文对话。只依据提供的数据发言；"
            "不编造数字、新闻或事实，不预测涨跌，不给出买卖或仓位建议，不承诺收益。\n"
            f"看多角色：{bull['name']}，人设：{bull['persona']}。\n看空角色：{bear['name']}，人设：{bear['persona']}。\n"
            "\n内容要求（重要）：\n"
            "0. 数据里的「本期重点」是按这家公司**这期财务数据**的显著变化挑出的讨论角度"
            "（业绩趋势/毛利率走势/股东回报/现金流/科目异动/收入结构位移）——"
            "它们正是这家公司区别于别家的地方，优先围绕它们展开；"
            "但每个角度都必须落到数据明细里的具体数字与年份，明细里撑不住的角度宁可不讲。\n"
            "1. 以“讲懂这家公司”为主线：先说清它到底卖什么、靠什么赚钱、客户是谁、这门生意怎么运转；"
            "再说它在所处行业里的位置、竞争格局、行业天花板与政策环境。\n"
            "   数据里的「公司档案」来自交易所 F10 原文，是讲这门生意的第一手依据，必须据此展开：\n"
            "   · 主营业务、主营构成（各产品/地区/销售模式的收入占比与毛利率）就是这门生意的骨架——"
            "哪块业务撑起收入、哪块最赚钱、直销和经销各占多少，都要讲到具体数字；"
            "提到某块业务时，统一用数据里给出的业务名称（已是画面同款简称），"
            "不要自创缩写或换一个叫法——旁白和画面必须用同一个词；\n"
            "   · 所属行业、行业地位（研究行业与同行家数）用来定位它在行业里的位置和同行对比；\n"
            "   · 管理层只提档案里写了姓名和职务的人，不评价能力、不推测想法、不编造言论；\n"
            "   · 经营评述是公司自述，可以复述但要说明这是公司的说法。\n"
            "   board（行业/概念板块）是交易所对这家公司的分类，作为补充：含“白酒概念”就围绕白酒这门生意讲，"
            "分类只到行业层面时就只讲行业层面的事实。"
            "档案和 board 里都没有的产品、客户、产能、管理层姓名与言论一律不得编造，宁可说“公开信息只到这一层”。\n"
            "2. 财务与行情数据只用来佐证业务判断：把营收、利润、毛利率、ROE、价格和成交等数字翻译成生意层面的含义"
            "（例如意味着什么生意变化），不要罗列指标，不要停留在K线、MACD等技术信号本身。\n"
            "3. 聊听众关心的实际问题：这家公司凭什么在行业里站稳、增长从哪里来、钱从哪里赚、和同行比强在哪、"
            "风险藏在哪个环节。\n"
            "4. 有多少数据聊多少内容：充分利用数据里的财务指标、每股指标、利润/资产负债/现金流构成和历史走势；"
            "结合最近的行情节奏（涨跌、量能、位置）自然展开，可以引用带日期的具体数字，但只解读不预测。\n"
            "5. 最新资讯是每期的必答块（2026-10-08）：数据里的 news/资讯 是该股票最近的公开新闻、研报和快讯。"
            "骨架里出现 news 节拍时，必须至少用一轮正面响应最新的一条——说清日期与信息类型"
            "（如\u201c10月8日龙虎榜……\u201d\u201c九月初有研报提到……\u201d），并把它和前面的生意判断连线"
            "（印证了什么/矛盾在哪/或说明只是交易层面的异动），这正是观众点进来的原因；"
            "只能复述标题和摘要里已有的信息，不得脑补细节、不得据此预测股价；"
            "骨架里没有 news 节拍（数据里没给资讯）时才完全不提。\n"
            "6. 多用通俗类比和生活化举例把生意讲透（如把经销网络比作水管、把预收款比作客户先排队交钱）；"
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
            "同一情绪不要连用超过三轮；全片「平述」超过一半等于没打——辩论的情绪就在起伏里。\n"
            "9. 数字必须写完整读法，禁止缩略：写「四亿五千一百万」不要写「四亿五一」，"
            "写「百分之八十二点七」不要写「八成二七」，写「十八点二九」不要写「十八二九」；"
            "缩略数字配音会念错，观众也听不懂。\n"
            "10. 数字分三级执行：anchor 原值口播且绑定所以呢；analogy 只说确定换算后的白话并记录原指标；visual_only 只上屏不口播。全片最多6个核心数字、单轮最多2个，缺少 source_refs 的事实不得提及。\n"
            + "\n" + self._arc_section(choice) + "\n"
            "\n反套话（重要）：每一期的开场都必须是新的。禁止把这些当开场套式——"
            "“咱们先看……”整个句式（不管宾语换成什么，2026-09-27 实锤：“咱们先看谁在给XX掏钱”只换了宾语、骨架照旧）、"
            "“说白了”“这家公司靠什么赚钱”式的设问、“XX 这门生意”式的名词解释。"
            "这些意思可以在正文里讲，但第一句必须从具体事实、具体数字或一个反直觉的判断切入，"
            "不要出处介绍、不要背景铺垫。收尾同样禁止“总的来说/说到底”式的空转，要落到一个具体变量上。\n"
            "\n开场三秒（重要）：第一句不超过 25 个字，必须带一个具体数字或一个反直觉的判断，"
            "让人立刻想问「为什么」；前两句里要埋一个信息缺口——反差、反问或未解的因果，"
            "观众为了补上这个缺口才会把片子看完。不要用行业定性开场（「这是一家做XX的公司」），"
            "公司是谁、做什么生意，让观众从数据交锋里自己拼出来。"
            "\n叙事铺排（重要）：最强的反差数字/反常识事实是本期的「底牌」，不许在中段平铺掉——"
            "开头埋钩（只露一半，让人想问另一半）、中段造张力（顺着质疑把代价和疑点铺深）、"
            "把底牌安排在整体进度的 60%-75% 处揭晓（揭晓那一刻要有情绪：惊叹/沉默/重新评估），"
            "片尾收束再把分歧留给观众。这只改变**已有数据的出场顺序和悬念话术**："
            "不许为了戏剧性夸大、预判或编造任何数字，数据说什么就是什么。\n"
            "\n对话节奏（重要）：长度由内容决定，该长则长、该短则短。允许某一轮只有几个字"
            "（如“嗯”“有道理”“但是——”“等等，这个不对”），用来表示认可、打断或迟疑；"
            "也允许一方连续追问、另一方长答后短驳。不要为了凑字数把每一轮都写满，也不要每轮等长。"
            "两人必须交替发言：同一说话人连续两轮只允许超长台词被拆成续轮的情况；"
            "一个论点的自然延伸就并进同一轮里说完，不要单独再开一轮。\n"
            "\n风格要求：像两个懂行的人聊天，允许追问、打断（……或破折号）、短暂停顿、跑题后拉回、惊讶/认同/质疑，"
            "及被说服后修正观点。每一轮都必须提供新信息、新数字或新角度，严禁重复已经说过的观点；"
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
                          day=date.today().isoformat())

    def _arc_section(self, choice: ArcChoice) -> str:
        """把骨架渲染成给模型的节拍清单——这是"这期怎么讲"的唯一来源。"""
        lines = [f"本期骨架：{choice.arc.name}——{choice.arc.summary}",
                 "严格按下面的节拍顺序展开，每个节拍至少一轮发言；被标为可短的那一拍允许只有几个字："]
        for index, beat in enumerate(choice.beats, 1):
            who = f"（由{self._character(beat.speaker, beat.speaker, '')['name']}说）" if beat.speaker else "（谁来说都行）"
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
        schema = {"title": "股票名（代码）：一句话点出这门生意或行业看点", "turns": [
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
            + ("这是一条信息流短视频，不是快讯：每一轮都要让观众多懂一点这门生意，不能用铺垫消耗前几秒。"
               if max_s <= 90 else
               "这是一条长视频，不是快讯：观众交出的是几分钟的注意力，每一轮都要让他多懂一点这门生意。")
            + "多轮短交锋，别让一个人一口气讲太久：一轮只讲一个点，讲完就把话头递出去。"
            "但每一轮仍必须有新信息、新数字或新角度，严禁把同一个意思换句话再说一遍。"
            "素材和数据多就往上限展开，素材少就收得住（不低于下限），不要为凑时长注水，也不要意犹未尽地草草收尾。"
            "至少一半的篇幅围绕公司业务和行业本身（生意模式、行业格局、竞争与需求），技术信号最多作为一句带过的佐证。"
            "开头几轮就把“这家公司靠什么赚钱”讲明白，让观众听完能多懂一门生意，而不是听了一段行情点评。"
            "观众的获得感是这条视频的命：看完要让人觉得「学到了」，而不只是听了一嘴行情。"
            "每一期至少让观众带走两个可复用的认知，从下面三类里至少覆盖两类："
            "① 行业门道——这个行业的钱怎么流转、产业链上下游谁卡着谁、为什么这类生意毛利高或账期长，讲清一个运转机制；"
            "② 看财报的方法——结合本期真实数字讲一个指标怎么读、容易看漏什么"
            "（比如经营现金流为什么要和净利润对着看、存货和应收压着多少营收说明什么），"
            "让观众下次自己翻开财报也会用这一招；"
            "③ 公司价值——把商业模式翻译成「这门生意好在哪、脆弱在哪」，不要停在概念词上。"
            "讲通用财识可以用行业常识，但涉及本期公司的数字必须原样来自数据；"
            "知识点要长在交锋里自然讲出来，禁止开科普小课堂（「我们来了解一下」这类句式一律不要）。"
            f"本期必须按这条骨架展开（节拍顺序与分工见系统提示，第 1 拍就是开场）：{'→'.join(beat_keys)}。"
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
            "另外必须产出三个用于和观众互动的字段（本期片尾会把它们打在画面上，必须经得起核对）："
            "hook —— 下一期要盯的一个可验证的具体变量（如“下一份财报的外销收入占比”“越南基地的实际产出”），"
            "要能被下一期直接核对，不要写“继续关注”“拭目以待”这类空话；"
            "hook 同时是封面文案，鼓励带反差或悬念的措辞（如“九成收入竟靠它”），"
            "但落点必须仍是具体事实，不超过 22 字，禁止买卖词与收益暗示；"
            "question —— 抛给观众的一个问题，必须是生意层面的二选一（如“你更信渠道还是产能”），"
            "观众能用一句话回答；问题必须从本场对话实际交锋的分歧点提炼，与 sides 里的立场相互呼应，"
            "禁止引入对话里没有出现过的概念或环节（对话没聊到代工就绝不能问代工）；"
            "禁止询问买卖、点位、涨跌、能不能涨；"
            "sides —— 多空各一句立场，只讲生意层面的分歧（渠道、产能、价格能不能传导、谁会替代谁），"
            "一句话说完；禁止出现点位、目标价、买卖动作、收益暗示。"
            "另外必须产出一个 spotlight 字段——首帧大卡与封面的「冲突数字」：从数据里挑本期最反差的一个数字，"
            "label 是指标名（不超过 8 个字，如「经营现金流」），value 是冲突表达（不超过 16 个字，"
            "阿拉伯数字，如「-2.4亿→+8000万」「毛利率38%→31%」）。"
            "它必须原样来自提供的数据，禁止换算、推算或暗示涨跌方向；"
            "它是纯上屏元素（不进配音台词），所以数字直接写阿拉伯写法。\n"
            # --message 的注入点：选材与侧重参考，紧挨 JSON 结构说明之前——
            # 数据 JSON 仍留在提示最尾部，模型的"最后读到的是数据"不变。
            + self._message_section()
            + self._episode_section(payload)
            + "输出结构必须匹配：\n"
            + f"{json.dumps(schema, ensure_ascii=False)}\n股票数据：\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        )

    def _episode_section(self, payload: Mapping[str, Any]) -> str:
        """把本期叙事统筹（episode_plan）写成给模型的五章地图。

        计划是**确定性**产出的（素材与数字额度都带 source_refs，不需要模型
        去挑），模型要做的只是照着它把故事讲出来：哪一章讲哪条素材、哪些数字
        能开口播、哪些只许上屏。这里不重复列素材明细——它们已经在数据 JSON
        里；只给章节顺序与数字纪律，免得模型把五章讲成一条财务播报。
        """
        plan = payload.get("episode_plan")
        if not isinstance(plan, Mapping):
            return ""
        rounds = [r for r in (plan.get("rounds") or []) if isinstance(r, Mapping)]
        if not rounds:
            return ""
        lines = ["本期叙事统筹（按这个顺序讲，不要中途换题）："]
        for index, chapter in enumerate(rounds, 1):
            budget = chapter.get("number_budget") or {}
            lines.append(f"{index}. {chapter.get('id')}（{chapter.get('purpose') or ''}）："
                         f"围绕 {chapter.get('must_cover') or ['本期素材']} 展开；"
                         f"该章最多口播 {budget.get('spoken', 2)} 个数字。")
        lines.append("anchor 数字按原值口播并讲清'所以呢'；analogy 只讲换算后的白话；"
                     "visual_only 只上屏、不要念出来。没有 source_refs 的事实一律不讲。")
        lines.append("每轮可用 round_id 标出它属于哪一章（只能取上面的 id）；不标则由程序按轮次顺序分配。")
        return "\n" + "\n".join(lines) + "\n"

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
        notes: list[str] = []
        cursor = 0
        # 章节分配要按「最终会有多少轮台词」算，而不是只算模型给的轮数：片尾
        # 风险提示交换轮是程序追加的，算漏了末章就会被正文的倒数第二轮顶掉。
        # （turns 为空时不会追加，那种情况 raw_turns 也为空，整个生成会先失败。）
        append_risk = bool(self.config.risk_exchange) and bool(raw_turns)
        total_turns = len(raw_turns) + (2 if append_risk else 0)
        for index, entry in enumerate(raw_turns, 1):
            if not isinstance(entry, Mapping):
                raise DialogueGenerationError(f"turn {index} is not an object")
            speaker = str(entry.get("speaker") or entry.get("role") or "").lower()
            if speaker not in self.speakers:
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
                metadata = self._turn_metadata(entry, index, episode_plan, total=total_turns)
                turns.append({"speaker": speaker, "line": piece,
                              "beat": beat.key if beat else self._clean_beat(entry.get("beat")),
                              # 只有首段能算短回应；续轮是正常发言，别跳过图板
                              "short": short and offset == 0,
                              "visual": visuals,
                              # 续轮继承同一情绪：它们是同一句话的拆分，语气不该变
                              "emotion": emotion,
                              "character_name": self._character(speaker, speaker, "")["name"],
                              **metadata})
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
                              "character_name": self._character(speaker, speaker, "")["name"],
                              **self._turn_metadata({}, len(turns) + 1, episode_plan, total=total_turns)})
        quote = stock_data.get("quote") if isinstance(stock_data.get("quote"), Mapping) else {}
        code, name = str(stock_data.get("code") or quote.get("code") or ""), str(quote.get("name") or stock_data.get("code") or "股票")
        char_count = sum(len(turn["line"]) for turn in turns)
        script = {"stock_code": code, "stock_name": name, "title": self._clean_title(response.get("title"), name, code),
                "turns": turns,
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
        return {"round_id": str(selected.get("id") or ""), "material_ids": list(entry.get("material_ids") or selected.get("material_ids") or []),
                "metric_ids": list(entry.get("metric_ids") or selected.get("anchor_metric_ids") or []),
                "visual_id": str(entry.get("visual_id") or visual.get("id") or "")}

    @staticmethod
    def _style_audit(turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Expose lightweight, deterministic human-speech quality signals.

        This intentionally reports instead of rewriting: a second LLM pass can
        damage factual wording, while a reviewer can use the flags to target
        only the offending lines.
        """
        lines = [str(turn.get("line") or "").strip() for turn in turns if str(turn.get("line") or "").strip()]
        joined = "\n".join(lines)
        cliche_hits = [phrase for phrase in STYLE_CLICHES if joined.count(phrase)]
        idiom_count = sum(joined.count(phrase) for phrase in STYLE_IDIOMS)
        repeated = sum(1 for left, right in zip(lines, lines[1:]) if left == right)
        flags: list[str] = []
        if cliche_hits:
            flags.append("套话：" + "、".join(cliche_hits))
        if idiom_count > max(1, len(lines) // 8):
            flags.append(f"熟语密度偏高（{idiom_count}处）")
        if repeated:
            flags.append(f"相邻台词重复（{repeated}处）")
        return {"cliche_count": sum(joined.count(phrase) for phrase in STYLE_CLICHES),
                "cliche_hits": cliche_hits, "idiom_count": idiom_count,
                "repeated_adjacent_lines": repeated,
                "short_turn_ratio": round(sum(len(line) <= 20 for line in lines) / len(lines), 3) if lines else 0.0,
                "flags": flags}

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
        return title[:60] or f"{name}（{code}）这门生意怎么看"

    @staticmethod
    def _clean_text(value: Any, limit: int) -> str:
        """一句用于片尾的短文案（hook / question），去掉换行、限长。"""
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text[:limit]

    def _clean_sides(self, value: Any) -> dict[str, str]:
        """多空各一句立场；只认配置里的角色，缺的就留空。"""
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


def generate_dialogue(stock_data: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return DialogueGenerator(config).generate(stock_data)
