"""Generate natural, business-focused stock conversations with Bailian."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping, Sequence

from stocktalk.modules.arcs import (Arc, ArcChoice, Beat, beats_by_key,
                                    resolve_speakers, select_arc)


class DialogueGenerationError(RuntimeError):
    """Raised when Bailian cannot return a usable dialogue script."""


Runner = Callable[[Sequence[str], float], str]


# Measured on real renders: a 1331-character script came out at 275s of speech,
# i.e. ~4.8 Chinese characters per second including inter-turn pauses.  The
# prompt turns a duration target into a character budget with this rate, because
# asking a model for "a 90-second dialogue" reliably overshoots.
SPEECH_CHARS_PER_SECOND = 4.8

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


class DialogueGenerator:
    """Return ordered turns, not artificial paired debate rounds."""

    def __init__(self, config: Mapping[str, Any] | None = None, runner: Runner | None = None) -> None:
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
        self._runner = runner or self._run_subprocess

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

    def generate(self, stock_data: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(stock_data, Mapping):
            raise TypeError("stock_data must be a mapping")
        self._require_bailian_cli()
        payload = self._compact_data(stock_data)
        choice = self.select_arc(payload)
        raw = self._runner(self._command(self._system_prompt(choice.arc),
                                         self._user_prompt(payload, choice.arc)),
                           self.config.timeout_seconds)
        return self._normalize_script(self._decode_response(raw), payload, choice)

    def _command(self, system: str, message: str) -> list[str]:
        return ["bl", "text", "chat", "--model", self.config.model, "--system", system, "--message", message,
                "--max-tokens", str(self.config.max_tokens), "--temperature", str(self.config.temperature),
                "--output", "json", "--quiet"]

    def _system_prompt(self, arc: Arc | None = None) -> str:
        choice = self._choice_for(arc)
        bull = self._character("bull", "增长派", "擅长类比、未来叙事和增长逻辑；承认数据边界")
        bear = self._character("bear", "审计派", "擅长数据引用、风险揭示和历史比较；追问叙事")
        return (
            "你是财经短视频编导，为对投资感兴趣的普通观众写一段两人聊公司的中文对话。只依据提供的数据发言；"
            "不编造数字、新闻或事实，不预测涨跌，不给出买卖或仓位建议，不承诺收益。\n"
            f"看多角色：{bull['name']}，人设：{bull['persona']}。\n看空角色：{bear['name']}，人设：{bear['persona']}。\n"
            "\n内容要求（重要）：\n"
            "1. 以“讲懂这家公司”为主线：先说清它到底卖什么、靠什么赚钱、客户是谁、这门生意怎么运转；"
            "再说它在所处行业里的位置、竞争格局、行业天花板与政策环境。\n"
            "   数据里的「公司档案」来自交易所 F10 原文，是讲这门生意的第一手依据，必须据此展开：\n"
            "   · 主营业务、主营构成（各产品/地区/销售模式的收入占比与毛利率）就是这门生意的骨架——"
            "哪块业务撑起收入、哪块最赚钱、直销和经销各占多少，都要讲到具体数字；\n"
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
            "5. 真实资讯是重要素材：数据里的 news/资讯 是该股票最近的公开新闻、研报和快讯标题，"
            "挑与生意最相关的 2-4 条自然融进对话（说清日期和信息类型，如\u201c九月初有研报提到……\u201d）；"
            "只能复述标题和摘要里已有的信息，不得脑补细节、不得据此预测股价；没有资讯或都不相关就完全不提。\n"
            "6. 多用通俗类比和生活化举例把生意讲透（如把经销网络比作水管、把预收款比作客户先排队交钱）；"
            "关键数字要说清楚\u201c意味着什么\u201d，而不是只报数字。\n"
            "7. 每轮对话都要给出画面指引：从 line 中提取 2-4 个关键词或短语（公司名、业务词、财务指标名、"
            "日期或区间、行业词、新闻事件词），按对话顺序填入 visual 数组；只允许出现 line 里说到的词。\n"
            "8. 台词要有情绪起伏和口语节奏：多使用反问、设问、惊讶、质疑、认同后转折；"
            "在最想强调的词和数字上自然用上\u201c！\u201d\u201c？\u201d\u201c……\u201d\u201c——\u201d等标点，"
            "但不要每句都感叹，该克制时克制，做到有张有弛。\n"
            + "\n" + self._arc_section(choice) + "\n"
            "\n反套话（重要）：每一期的开场都必须是新的。禁止把这些当开场套式——"
            "“咱们先看 XX 到底卖啥”“说白了”“这家公司靠什么赚钱”式的设问、“XX 这门生意”式的名词解释。"
            "这些意思可以在正文里讲，但第一句必须从具体事实、具体数字或一个反直觉的判断切入，"
            "不要出处介绍、不要背景铺垫。收尾同样禁止“总的来说/说到底”式的空转，要落到一个具体变量上。\n"
            "\n对话节奏（重要）：长度由内容决定，该长则长、该短则短。允许某一轮只有几个字"
            "（如“嗯”“有道理”“但是——”“等等，这个不对”），用来表示认可、打断或迟疑；"
            "也允许一方连续追问、另一方长答后短驳。不要为了凑字数把每一轮都写满，也不要每轮等长。\n"
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
             "visual": ["line 中提到的关键词1", "关键词2"]}],
            "sides": {"bull": "看多一方的一句话立场（只讲生意层面的分歧）",
                      "bear": "看空一方的一句话立场"},
            "hook": "下一期要盯的一个可验证的具体变量",
            "question": "抛给观众的一个生意层面二选一问题",
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
            "这是一条长视频，不是快讯：观众交出的是几分钟的注意力，每一轮都要让他多懂一点这门生意。"
            "多轮短交锋，别让一个人一口气讲太久：一轮只讲一个点，讲完就把话头递出去。"
            "但每一轮仍必须有新信息、新数字或新角度，严禁把同一个意思换句话再说一遍。"
            "素材和数据多就往上限展开，素材少就收得住（不低于下限），不要为凑时长注水，也不要意犹未尽地草草收尾。"
            "至少一半的篇幅围绕公司业务和行业本身（生意模式、行业格局、竞争与需求），技术信号最多作为一句带过的佐证。"
            "开头几轮就把“这家公司靠什么赚钱”讲明白，让观众听完能多懂一门生意，而不是听了一段行情点评。"
            f"本期必须按这条骨架展开（节拍顺序与分工见系统提示，第 1 拍就是开场）：{'→'.join(beat_keys)}。"
            "每一拍的对话都要落到具体数字或具体环节上，讲清“这意味着什么”，不要停在结论式的形容。"
            "涉及管理层时只能用公司档案里给出的姓名与职务，不评价个人能力、不推测动机；公司动作只说新闻里写了的，并带上出处和时间。"
            "收尾要自然，不要戛然而止。"
            "另外必须产出三个用于和观众互动的字段（本期片尾会把它们打在画面上，必须经得起核对）："
            "hook —— 下一期要盯的一个可验证的具体变量（如“下一份财报的外销收入占比”“越南基地的实际产出”），"
            "要能被下一期直接核对，不要写“继续关注”“拭目以待”这类空话；"
            "question —— 抛给观众的一个问题，必须是生意层面的二选一（如“你更信渠道还是产能”），"
            "观众能用一句话回答；禁止询问买卖、点位、涨跌、能不能涨；"
            "sides —— 多空各一句立场，只讲生意层面的分歧（渠道、产能、价格能不能传导、谁会替代谁），"
            "一句话说完；禁止出现点位、目标价、买卖动作、收益暗示。"
            "输出结构必须匹配：\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n股票数据：\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        )

    def _normalize_script(self, response: Any, stock_data: Mapping[str, Any],
                          choice: ArcChoice | None = None) -> dict[str, Any]:
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
            # 节拍声明为短回应、或者模型自己就写了极短的一句，都按"短回应"处理：
            # 这类轮次不换画面图板（一句话的认可没必要把图板重画一遍）。
            short = bool(beat and beat.short) or len(line) <= self.config.short_line_chars
            if beat and beat.speaker and beat.speaker != speaker:
                notes.append(f"[第{index}轮] 骨架指定由 {beat.speaker} 说这段（{beat.key}），模型派给了 {speaker}")
            visuals = self._clean_visuals(entry.get("visual"))
            # 模型没管住字数时，把超长台词拆成同说话人的续轮——一个字都不丢，
            # 而且正好贴合"轮次多一点、每次讲短一点"的节奏（2026-09-22 用户反馈）。
            pieces = self._split_line(line)
            for offset, piece in enumerate(pieces):
                turns.append({"speaker": speaker, "line": piece,
                              "beat": beat.key if beat else self._clean_beat(entry.get("beat")),
                              # 只有首段能算短回应；续轮是正常发言，别跳过图板
                              "short": short and offset == 0,
                              "visual": visuals,
                              "character_name": self._character(speaker, speaker, "")["name"]})
        quote = stock_data.get("quote") if isinstance(stock_data.get("quote"), Mapping) else {}
        code, name = str(stock_data.get("code") or quote.get("code") or ""), str(quote.get("name") or stock_data.get("code") or "股票")
        char_count = sum(len(turn["line"]) for turn in turns)
        return {"stock_code": code, "stock_name": name, "title": self._clean_title(response.get("title"), name, code),
                "turns": turns,
                # 本期用了哪条骨架、哪些节拍因为没数据被剪掉、模型哪里没照着走。
                # 结构"是否真的变了"要可核对，不能只看 prompt 写了什么。
                "arc": choice.report() if choice else None,
                "arc_notes": notes,
                "hook": self._clean_text(response.get("hook"), 60),
                "question": self._clean_text(response.get("question"), 60),
                "sides": self._clean_sides(response.get("sides")),
                "char_count": char_count,
                # 按实测语速换算的自估时长。长片最容易出的偏差是模型多写几百字，
                # 把它留在结果里，pipeline 就能在渲染前提醒（而不是渲完才发现超长）。
                "estimated_seconds": round(char_count / SPEECH_CHARS_PER_SECOND, 1)}

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
        超长交给 :meth:`_split_line` 拆成续轮，内容一个字都不丢。"""
        return re.sub(r"\s+", "", str(value or ""))

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
        """Keep up to 4 non-empty keyword phrases extracted from the turn line."""
        items = value if isinstance(value, list) else []
        visuals: list[str] = []
        for item in items:
            text = re.sub(r"\s+", " ", str(item)).strip()[:24]
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

    @staticmethod
    def _run_subprocess(argv: Sequence[str], timeout: float) -> str:
        try: completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc: raise DialogueGenerationError(f"Failed to run Bailian chat: {exc}") from exc
        if completed.returncode != 0: raise DialogueGenerationError(f"Bailian chat failed: {completed.stderr.strip() or completed.stdout.strip() or 'unknown error'}")
        return completed.stdout

    @staticmethod
    def _require_bailian_cli() -> None:
        if not shutil.which("bl"): raise DialogueGenerationError("Bailian CLI 'bl' is not installed or not on PATH")


def generate_dialogue(stock_data: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return DialogueGenerator(config).generate(stock_data)
