"""Generate natural, business-focused stock conversations with Bailian."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


class DialogueGenerationError(RuntimeError):
    """Raised when Bailian cannot return a usable dialogue script."""


Runner = Callable[[Sequence[str], float], str]


# Measured on real renders: a 1331-character script came out at 275s of speech,
# i.e. ~4.8 Chinese characters per second including inter-turn pauses.  The
# prompt turns a duration target into a character budget with this rate, because
# asking a model for "a 90-second dialogue" reliably overshoots.
SPEECH_CHARS_PER_SECOND = 4.8


@dataclass(frozen=True)
class DialogueConfig:
    model: str = "qwen3.6-plus"
    min_duration_seconds: int = 70
    max_duration_seconds: int = 110
    max_line_chars: int = 90
    max_tokens: int = 2500
    temperature: float = 0.8
    timeout_seconds: float = 180.0


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
        )
        if (self.config.min_duration_seconds < 1 or self.config.max_duration_seconds < self.config.min_duration_seconds
                or not 30 <= self.config.max_line_chars <= 150 or self.config.max_tokens < 1):
            raise ValueError("duration bounds, max_line_chars (30--150), and max_tokens must be valid")
        if not 0 < self.config.temperature <= 2:
            raise ValueError("temperature must be in (0, 2]")
        self.characters = dict((config or {}).get("characters", {}))
        self._runner = runner or self._run_subprocess

    def generate(self, stock_data: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(stock_data, Mapping):
            raise TypeError("stock_data must be a mapping")
        self._require_bailian_cli()
        payload = self._compact_data(stock_data)
        raw = self._runner(self._command(self._system_prompt(), self._user_prompt(payload)), self.config.timeout_seconds)
        return self._normalize_script(self._decode_response(raw), payload)

    def _command(self, system: str, message: str) -> list[str]:
        return ["bl", "text", "chat", "--model", self.config.model, "--system", system, "--message", message,
                "--max-tokens", str(self.config.max_tokens), "--temperature", str(self.config.temperature),
                "--output", "json", "--quiet"]

    def _system_prompt(self) -> str:
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
            "\n风格要求：像两个懂行的人聊天，允许追问、打断（……或破折号）、短暂停顿、跑题后拉回、惊讶/认同/质疑，"
            "及被说服后修正观点。每一轮都必须提供新信息、新数字或新角度，严禁重复已经说过的观点；"
            "鼓励连续 2-3 轮围绕一个话题层层深挖（提出→举例/数字→追问短板→修正），再自然转到下一个话题。"
            "按话题递进：这门生意是什么→行业里的位置→钱怎么赚、财务是否印证→风险与不确定性。"
            "数据存在时引用具体数字；数据缺失就坦诚说缺，不硬编。\n"
            "必须只输出 JSON，不要 Markdown。"
        )

    def _user_prompt(self, payload: Mapping[str, Any]) -> str:
        min_s, max_s = self.config.min_duration_seconds, self.config.max_duration_seconds
        min_chars = int(min_s * SPEECH_CHARS_PER_SECOND)
        budget_chars = int(max_s * SPEECH_CHARS_PER_SECOND)
        floor_chars = max(30, self.config.max_line_chars // 2)
        min_turns = max(4, min_chars // self.config.max_line_chars)
        max_turns = max(min_turns + 2, budget_chars // floor_chars)
        schema = {"title": "股票名（代码）：一句话点出这门生意或行业看点", "turns": [
            {"speaker": "bull", "line": f"{floor_chars} 到 {self.config.max_line_chars} 字的自然发言；允许极短打断或停顿",
             "beat": "生意本质",
             "visual": ["line 中提到的关键词1", "关键词2"]}],
            }
        return (
            f"生成一段成片时长在 {min_s}-{max_s} 秒之间的中文对话流："
            f"正文总字数控制在 {min_chars}-{budget_chars} 字之间（配音约 4.8 字/秒，超字数就会超时长），"
            f"分 {min_turns}-{max_turns} 轮发言，单轮 {floor_chars}-{self.config.max_line_chars} 字。"
            "素材和数据多就往上限展开，素材少就收得住（不低于下限），不要为凑时长注水，也不要意犹未尽地草草收尾。"
            "不要编号、不要 Round、不要强制一来一回；"
            "允许一方连续追问、另一方长答后短驳，长短句交错，不要每轮等长。"
            "至少一半的篇幅围绕公司业务和行业本身（生意模式、行业格局、竞争与需求），技术信号最多作为一句带过的佐证。"
            "开头几轮就把“这家公司靠什么赚钱”讲明白，让观众听完能多懂一门生意，而不是听了一段行情点评。"
            "涉及管理层时只能用公司档案里给出的姓名与职务，不评价个人能力、不推测动机；公司动作只说新闻里写了的，并带上出处和时间。"
            "内容要充分展开：覆盖业务模式、行业格局、盈利来源、财务印证、风险与不确定性等多个层面，"
            "避免车轱辘话和重复观点，每一轮都提供新的信息或新的角度。"
            "收尾要自然：对这次聊到的生意做一个小结式收束，不要戛然而止。"
            "输出结构必须匹配：\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n股票数据：\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        )

    def _normalize_script(self, response: Any, stock_data: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(response, Mapping):
            raise DialogueGenerationError("Bailian response does not contain a JSON object")
        raw_turns = response.get("turns", response.get("dialogue"))
        if not isinstance(raw_turns, list) or not raw_turns:
            raise DialogueGenerationError("Bailian must return a non-empty turns array")
        turns = []
        for index, entry in enumerate(raw_turns, 1):
            if not isinstance(entry, Mapping):
                raise DialogueGenerationError(f"turn {index} is not an object")
            speaker = str(entry.get("speaker") or entry.get("role") or "").lower()
            if speaker not in {"bull", "bear"}:
                raise DialogueGenerationError(f"turn {index} must name bull or bear as speaker")
            line = self._clean_line(entry.get("line"))
            if not line:
                raise DialogueGenerationError(f"turn {index} line is empty")
            turns.append({"speaker": speaker, "line": line, "beat": self._clean_beat(entry.get("beat")),
                          "visual": self._clean_visuals(entry.get("visual")),
                          "character_name": self._character(speaker, speaker, "")["name"]})
        quote = stock_data.get("quote") if isinstance(stock_data.get("quote"), Mapping) else {}
        code, name = str(stock_data.get("code") or quote.get("code") or ""), str(quote.get("name") or stock_data.get("code") or "股票")
        return {"stock_code": code, "stock_name": name, "title": self._clean_title(response.get("title"), name, code), "turns": turns}

    def _clean_line(self, value: Any) -> str:
        line = re.sub(r"\s+", "", str(value or ""))
        return line[:self.config.max_line_chars - 1].rstrip("，。；、") + "。" if len(line) > self.config.max_line_chars else line

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
