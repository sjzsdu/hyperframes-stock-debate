"""AI 能力提供方的契约层：接口、请求/结果类型、错误类型与注册表。

设计边界（开闭原则的落点）：

- ``LLMClient.chat`` 返回 provider 的**原始响应文本**。信封解包
  （choices/content/message/...）与脚本 JSON 解析留在编排层
  （``dialogue_generator._decode_response``）——那条解包链本就是多信封通吃的，
  下沉到 provider 只会多一层重复解析，还会让容错行为分叉。
- ``TTSProvider.synthesize_lines`` 负责把每条 :class:`TTSJob` 渲染成
  ``out_path`` 上的音频文件；成功无返回值，失败抛 :class:`TTSProviderError`。
  静音裁剪、时长统计、字幕对齐留在编排层（TTSAgent）。
- 新增供应商 = 写一个实现模块 + ``@register_llm`` / ``@register_tts`` 登记 +
  在 ``providers/__init__`` import 一次，核心链路零改动。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


class ProviderError(RuntimeError):
    """AI provider 调用失败的统一基类（编排层负责转成各自的对外异常）。"""


class LLMProviderError(ProviderError):
    """LLM 对话补全失败。"""


class TTSProviderError(ProviderError):
    """TTS 语音合成失败。"""


class LLMClient(Protocol):
    """文本对话补全的最小契约。

    实现自己负责：传输（子进程/HTTP）、鉴权、可用性检查（缺 CLI/缺 key
    在调用时抛 :class:`LLMProviderError`，不放在构造期——构造必须零副作用）。
    """

    def chat(self, system: str, message: str, *,
             max_tokens: int | None = None,
             temperature: float | None = None,
             timeout: float | None = None) -> str:
        """返回 provider 的原始响应文本（含信封，由上层解包）。"""


@dataclass(frozen=True)
class TTSJob:
    """一条待合成的语音。编排层负责填充；各 provider 只解释自己支持的参数。"""

    text: str                       # 合成文本（可能是 SSML，见 use_ssml）
    voice: str                      # 音色 id，语义由 provider 解释
    out_path: Path                  # 音频落盘路径（provider 必须写出一个非空文件）
    audio_format: str = "wav"       # 容器格式（ws 实现自己决定线上格式）
    speed: float = 1.0
    pitch: float = 1.0
    volume: float | None = None
    use_ssml: bool = False
    sample_rate: int | None = None
    seed: int | None = None
    instruction: str | None = None  # 自然语言指令（仅部分音色支持）
    language: str = "zh"
    label: str = ""                 # 报错用的人类可读标识（通常是角色名）


class TTSProvider(Protocol):
    """语音合成的最小契约。

    ``supports_line_prosody`` 是能力声明：实时设计音色只接受会话级参数，
    逐句语速/音调/SSML 会失效甚至报错——编排层据此决定是否做逐句 prosody。
    """

    supports_line_prosody: bool

    def check_ready(self) -> None:
        """依赖齐备性检查（CLI/包/密钥），编排层入口调用一次。

        不放在 ``synthesize_lines`` 里：测试与高级用法可能直接逐句调用，
        或注入自己的传输，重复检查只会让注入路径也被环境卡住。
        """

    def synthesize_lines(self, jobs: Sequence[TTSJob]) -> None:
        """合成全部任务并把音频写到各 ``out_path``。失败抛 TTSProviderError。"""


# ---------------------------------------------------------------------------
# 注册表：provider 名 → 构造器。工厂（factory.py）按配置在这里查表。
# ---------------------------------------------------------------------------

LLMBuilder = Callable[..., object]
TTSBuilder = Callable[..., object]

LLM_CLIENTS: dict[str, LLMBuilder] = {}
TTS_PROVIDERS: dict[str, TTSBuilder] = {}


def register_llm(name: str) -> Callable[[LLMBuilder], LLMBuilder]:
    def decorator(builder: LLMBuilder) -> LLMBuilder:
        LLM_CLIENTS[name] = builder
        return builder
    return decorator


def register_tts(name: str) -> Callable[[TTSBuilder], TTSBuilder]:
    def decorator(builder: TTSBuilder) -> TTSBuilder:
        TTS_PROVIDERS[name] = builder
        return builder
    return decorator


__all__ = [
    "ProviderError", "LLMProviderError", "TTSProviderError",
    "LLMClient", "TTSJob", "TTSProvider",
    "LLM_CLIENTS", "TTS_PROVIDERS", "register_llm", "register_tts",
    "Mapping",
]
