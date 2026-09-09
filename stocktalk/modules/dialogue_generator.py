"""Generate natural, data-grounded stock conversations with Bailian."""
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


@dataclass(frozen=True)
class DialogueConfig:
    model: str = "qwen3.6-plus"
    min_duration_seconds: int = 120
    max_duration_seconds: int = 300
    max_line_chars: int = 130
    max_tokens: int = 3200
    temperature: float = 0.8
    timeout_seconds: float = 90.0


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
                "--output", "json", "--non-interactive"]

    def _system_prompt(self) -> str:
        bull = self._character("bull", "增长派", "擅长类比、未来叙事和增长逻辑；承认数据边界")
        bear = self._character("bear", "审计派", "擅长数据引用、风险揭示和历史比较；追问叙事")
        return (
            "你是财经短视频编导。只依据提供的数据写中文虚拟人物对话；不编造数字、新闻或事实，"
            "不预测涨跌，不给出买卖或仓位建议，不承诺收益。\n"
            f"看多角色：{bull['name']}，人设：{bull['persona']}。\n看空角色：{bear['name']}，人设：{bear['persona']}。\n"
            "让两人像真人聊天：允许追问、打断（……或破折号）、短暂停顿、跑题后拉回、惊讶/认同/质疑，及被说服后修正观点。"
            "按话题递进：数据表象→商业/技术/心理/经济规律→不确定性、概率或反脆弱；自然融合至少三种视角，数据存在时引用具体数字。"
            "必须只输出 JSON，不要 Markdown。"
        )

    def _user_prompt(self, payload: Mapping[str, Any]) -> str:
        schema = {"title": "股票名（代码）：增长叙事遇上风险定价", "turns": [
            {"speaker": "bull", "line": "30 到 150 字的自然发言；允许极短打断或停顿", "beat": "数据表象",
             "visual_prompt": "具体镜头、图表/数据叠加、人物情绪和转场"}],
            "disclaimer": "内容为虚拟人物观点碰撞，不构成投资建议。"}
        return (
            f"生成约 {self.config.min_duration_seconds // 60}-{self.config.max_duration_seconds // 60} 分钟的自然对话流，"
            f"常规发言 30-{self.config.max_line_chars} 个中文字符；不要编号、不要 Round、不要强制一来一回。"
            "每个 turn 必须含可直接用于视频生成的具体 visual_prompt。输出结构必须匹配：\n"
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
                          "visual_prompt": self._clean_visual(entry.get("visual_prompt"), speaker, line),
                          "character_name": self._character(speaker, speaker, "")["name"]})
        quote = stock_data.get("quote") if isinstance(stock_data.get("quote"), Mapping) else {}
        code, name = str(stock_data.get("code") or quote.get("code") or ""), str(quote.get("name") or stock_data.get("code") or "股票")
        return {"stock_code": code, "stock_name": name, "title": self._clean_title(response.get("title"), name, code),
                "turns": turns, "disclaimer": "内容为虚拟人物观点碰撞，不构成投资建议。"}

    def _clean_line(self, value: Any) -> str:
        line = re.sub(r"\s+", "", str(value or ""))
        return line[:self.config.max_line_chars - 1].rstrip("，。；、") + "。" if len(line) > self.config.max_line_chars else line

    @staticmethod
    def _clean_beat(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "自然推进")).strip()[:40] or "自然推进"

    @staticmethod
    def _clean_visual(value: Any, speaker: str, line: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        role = "绿色增长派人物特写" if speaker == "bull" else "红色审计派人物特写"
        return text[:160] or f"{role}；字幕高亮“{line[:24]}”；叠加相关数据卡片，镜头缓慢推进。"

    @staticmethod
    def _clean_title(value: Any, name: str, code: str) -> str:
        title = re.sub(r"\s+", " ", str(value or "")).strip()
        return title[:60] or f"{name}（{code}）增长叙事遇上风险定价"

    def _character(self, role: str, default_name: str, default_persona: str) -> dict[str, str]:
        value = self.characters.get(role, {})
        value = value if isinstance(value, Mapping) else {}
        return {"name": str(value.get("name") or default_name), "persona": str(value.get("persona") or default_persona)}

    @staticmethod
    def _compact_data(data: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in ("code", "quote", "technical", "financials", "f10", "unavailable"):
            if key not in data: continue
            value = data[key]
            if key == "technical" and isinstance(value, Mapping): value = {n: value[n] for n in ("summary", "count") if n in value}
            elif key == "f10" and isinstance(value, Mapping):
                sections = value.get("sections", {})
                value = {"sections": {str(k): str(v)[:600] for k, v in sections.items()} if isinstance(sections, Mapping) else {}}
            result[key] = value
        return result

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
