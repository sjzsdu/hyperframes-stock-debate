"""可插拔的 AI 能力提供方（LLM 对话 / TTS 语音合成）。

契约在 ``base.py``，工厂在 ``factory.py``，实现在各自模块里。
import 本包即完成全部实现的注册。新增供应商：写一个实现模块 +
``@register_llm`` / ``@register_tts`` 登记 + 在这里补一行 import，
核心链路（dialogue_generator / tts_agent / pipeline）零改动。
"""
from stocktalk.modules.providers.base import (
    LLM_CLIENTS,
    LLMProviderError,
    LLMClient,
    ProviderError,
    TTS_PROVIDERS,
    TTSJob,
    TTSProvider,
    TTSProviderError,
    register_llm,
    register_tts,
)
from stocktalk.modules.providers.factory import create_llm_client, create_tts_provider

# 实现 import 触发注册（顺序无关，注册表在 base 里）
from stocktalk.modules.providers import bailian_chat  # noqa: F401,E402
from stocktalk.modules.providers import openai_chat  # noqa: F401,E402
from stocktalk.modules.providers import bailian_tts  # noqa: F401,E402
from stocktalk.modules.providers import dashscope_ws_tts  # noqa: F401,E402
from stocktalk.modules.providers import volcengine_tts  # noqa: F401,E402

__all__ = [
    "ProviderError", "LLMProviderError", "TTSProviderError",
    "LLMClient", "TTSProvider", "TTSJob",
    "LLM_CLIENTS", "TTS_PROVIDERS", "register_llm", "register_tts",
    "create_llm_client", "create_tts_provider",
]
