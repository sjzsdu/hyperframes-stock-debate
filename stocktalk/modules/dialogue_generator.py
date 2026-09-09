"""Generate a short, data-grounded bull-versus-bear video script with Bailian.

The module deliberately treats the model response as untrusted input.  It
validates and normalizes it into the small contract consumed by ``TTSAgent``:
each round contains a ``bull.line`` followed by a ``bear.line``.
"""

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
    """Runtime limits for one short-form debate script."""

    model: str = "qwen3.6-plus"
    rounds: int = 3
    max_line_chars: int = 56
    max_tokens: int = 1600
    temperature: float = 0.65
    timeout_seconds: float = 90.0


class DialogueGenerator:
    """Call ``bl text chat`` and return a renderer-ready Chinese debate script."""

    def __init__(self, config: Mapping[str, Any] | None = None, runner: Runner | None = None) -> None:
        source = dict((config or {}).get("dialogue", config or {}))
        self.config = DialogueConfig(
            model=str(source.get("model", DialogueConfig.model)),
            rounds=int(source.get("rounds", DialogueConfig.rounds)),
            max_line_chars=int(source.get("max_line_chars", DialogueConfig.max_line_chars)),
            max_tokens=int(source.get("max_tokens", DialogueConfig.max_tokens)),
            temperature=float(source.get("temperature", DialogueConfig.temperature)),
            timeout_seconds=float(source.get("timeout_seconds", DialogueConfig.timeout_seconds)),
        )
        if self.config.rounds < 1 or self.config.max_line_chars < 8 or self.config.max_tokens < 1:
            raise ValueError("rounds, max_line_chars, and max_tokens must be positive")
        if not 0 < self.config.temperature <= 2:
            raise ValueError("temperature must be in (0, 2]")
        self.characters = dict((config or {}).get("characters", {}))
        self._runner = runner or self._run_subprocess

    def generate(self, stock_data: Mapping[str, Any]) -> dict[str, Any]:
        """Generate a script from ``StockDataClient.get_stock_data`` output."""
        if not isinstance(stock_data, Mapping):
            raise TypeError("stock_data must be a mapping")
        self._require_bailian_cli()
        payload = self._compact_data(stock_data)
        raw = self._runner(self._command(self._system_prompt(), self._user_prompt(payload)), self.config.timeout_seconds)
        response = self._decode_response(raw)
        return self._normalize_script(response, payload)

    def _command(self, system: str, message: str) -> list[str]:
        return [
            "bl", "text", "chat", "--model", self.config.model,
            "--system", system, "--message", message,
            "--max-tokens", str(self.config.max_tokens),
            "--temperature", str(self.config.temperature),
            "--output", "json", "--non-interactive",
        ]

    def _system_prompt(self) -> str:
        bull = self._character("bull", "股市新手", "乐观冲动，但只讨论已给出的数据")
        bear = self._character("bear", "股市老登", "理性批判，质疑数据边界和风险")
        return (
            "你是财经短视频编导。只依据提供的数据写中文虚拟人物辩论；不编造数字、新闻或事实，"
            "不预测涨跌，不给出买卖或仓位建议，不承诺收益。\n"
            f"看多角色：{bull['name']}，人设：{bull['persona']}。\n"
            f"看空角色：{bear['name']}，人设：{bear['persona']}。\n"
            "必须只输出 JSON，不要 Markdown。"
        )

    def _user_prompt(self, payload: Mapping[str, Any]) -> str:
        schema = {
            "title": "股票名（代码）观点碰撞",
            "rounds": [
                {"bull": {"line": "不超过指定字数", "visual_prompt": "一句画面提示"},
                 "bear": {"line": "不超过指定字数", "visual_prompt": "一句画面提示"}}
            ],
            "disclaimer": "内容为虚拟人物观点碰撞，不构成投资建议。",
        }
        return (
            f"基于以下股票数据生成恰好 {self.config.rounds} 轮对话。每位角色每句不超过 "
            f"{self.config.max_line_chars} 个中文字符；每轮先 bull 再 bear。输出结构必须匹配："
            f"\n{json.dumps(schema, ensure_ascii=False)}\n股票数据：\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        )

    def _normalize_script(self, response: Any, stock_data: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(response, Mapping):
            raise DialogueGenerationError("Bailian response does not contain a JSON object")
        raw_rounds = response.get("rounds")
        if not isinstance(raw_rounds, list) or len(raw_rounds) != self.config.rounds:
            raise DialogueGenerationError(f"Bailian must return exactly {self.config.rounds} rounds")
        rounds = []
        for index, raw_round in enumerate(raw_rounds, start=1):
            if not isinstance(raw_round, Mapping):
                raise DialogueGenerationError(f"round {index} is not an object")
            normalized = {}
            for role in ("bull", "bear"):
                entry = raw_round.get(role)
                if not isinstance(entry, Mapping):
                    raise DialogueGenerationError(f"round {index} has no {role} entry")
                line = self._clean_line(entry.get("line"))
                if not line:
                    raise DialogueGenerationError(f"round {index} {role} line is empty")
                normalized[role] = {
                    "line": line,
                    "visual_prompt": self._clean_visual(entry.get("visual_prompt"), line),
                    "character_name": self._character(role, role, "")["name"],
                }
            rounds.append(normalized)
        quote = stock_data.get("quote") if isinstance(stock_data.get("quote"), Mapping) else {}
        code = str(stock_data.get("code") or quote.get("code") or "")
        name = str(quote.get("name") or code or "股票")
        title = self._clean_title(response.get("title"), name, code)
        return {
            "stock_code": code,
            "stock_name": name,
            "title": title,
            "rounds": rounds,
            "disclaimer": "内容为虚拟人物观点碰撞，不构成投资建议。",
        }

    def _clean_line(self, value: Any) -> str:
        line = re.sub(r"\s+", "", str(value or ""))
        if len(line) > self.config.max_line_chars:
            line = line[: self.config.max_line_chars - 1].rstrip("，。；、") + "。"
        return line

    @staticmethod
    def _clean_visual(value: Any, fallback: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text[:80] or f"字幕突出显示：{fallback}"

    @staticmethod
    def _clean_title(value: Any, name: str, code: str) -> str:
        title = re.sub(r"\s+", " ", str(value or "")).strip()
        return title[:60] or f"{name}（{code}）观点碰撞"

    def _character(self, role: str, default_name: str, default_persona: str) -> dict[str, str]:
        value = self.characters.get(role, {})
        if not isinstance(value, Mapping):
            value = {}
        return {"name": str(value.get("name") or default_name), "persona": str(value.get("persona") or default_persona)}

    @staticmethod
    def _compact_data(data: Mapping[str, Any]) -> dict[str, Any]:
        """Keep prompts bounded while retaining the normalized decision context."""
        result: dict[str, Any] = {}
        for key in ("code", "quote", "technical", "financials", "f10", "unavailable"):
            if key not in data:
                continue
            value = data[key]
            if key == "technical" and isinstance(value, Mapping):
                value = {name: value[name] for name in ("summary", "count") if name in value}
            elif key == "f10" and isinstance(value, Mapping):
                sections = value.get("sections", {})
                value = {"sections": {str(k): str(v)[:600] for k, v in sections.items()} if isinstance(sections, Mapping) else {}}
            result[key] = value
        return result

    @staticmethod
    def _decode_response(raw: str) -> Any:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        for _ in range(4):
            if isinstance(parsed, Mapping):
                if "rounds" in parsed:
                    return parsed
                parsed = next((parsed[key] for key in ("content", "text", "message", "output", "response", "choices") if key in parsed), parsed)
                if isinstance(parsed, list) and parsed:
                    parsed = parsed[0]
                continue
            if isinstance(parsed, str):
                candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", parsed.strip(), flags=re.IGNORECASE)
                try:
                    parsed = json.loads(candidate)
                    continue
                except json.JSONDecodeError as exc:
                    raise DialogueGenerationError("Bailian returned invalid JSON script") from exc
            break
        return parsed

    @staticmethod
    def _run_subprocess(argv: Sequence[str], timeout: float) -> str:
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DialogueGenerationError(f"Failed to run Bailian chat: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise DialogueGenerationError(f"Bailian chat failed: {detail}")
        return completed.stdout

    @staticmethod
    def _require_bailian_cli() -> None:
        if not shutil.which("bl"):
            raise DialogueGenerationError("Bailian CLI 'bl' is not installed or not on PATH")


def generate_dialogue(stock_data: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Convenience entry point for the pipeline."""
    return DialogueGenerator(config).generate(stock_data)
