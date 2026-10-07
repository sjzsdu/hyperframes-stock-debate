"""按配置构造 provider 实例的工厂。

配置契约：

- ``dialogue.provider`` / ``tts.provider``：实现名（见注册表）。
- LLM 缺省 ``bailian-cli``（历史行为）；TTS 缺省 ``auto``——按模型名推断，
  与旧版「vd-realtime → WebSocket 直连，其余 → CLI」的分流完全一致。
- provider 专属参数放 ``dialogue.options`` / ``tts.options``
  （如 openai-compatible 的 ``base_url``、``api_key_env``）。
"""
from __future__ import annotations

from typing import Any, Mapping

from stocktalk.modules.providers.base import (
    LLM_CLIENTS,
    LLMProviderError,
    TTS_PROVIDERS,
    TTSProviderError,
)

DEFAULT_LLM_PROVIDER = "bailian-cli"
DEFAULT_TTS_PROVIDER = "bailian-cli"


def create_llm_client(config: Mapping[str, Any] | None = None,
                      default_timeout: float = 300.0):
    """从 ``config["dialogue"]``（或扁平的 dialogue 配置）构造 LLMClient。"""
    dialogue = (config or {}).get("dialogue", config or {})
    if not isinstance(dialogue, Mapping):
        raise LLMProviderError("dialogue 配置必须是映射")
    name = str(dialogue.get("provider") or DEFAULT_LLM_PROVIDER).strip().lower()
    builder = LLM_CLIENTS.get(name)
    if builder is None:
        raise LLMProviderError(
            f"未知的 LLM provider: {name}（可选：{', '.join(sorted(LLM_CLIENTS))}）")
    return builder(dialogue, default_timeout=default_timeout)


def create_tts_provider(tts_config: Mapping[str, Any] | None = None):
    """从 ``config["tts"]`` 构造 TTSProvider；``provider: auto`` 按模型名推断。"""
    tts = dict(tts_config or {})
    name = str(tts.get("provider") or "auto").strip().lower()
    if name == "auto":
        model = str(tts.get("model", ""))
        name = "dashscope-ws" if ("vd-realtime" in model or "tts-vd" in model) else DEFAULT_TTS_PROVIDER
    builder = TTS_PROVIDERS.get(name)
    if builder is None:
        raise TTSProviderError(
            f"未知的 TTS provider: {name}（可选：{', '.join(sorted(TTS_PROVIDERS))}）")
    return builder(tts)


__all__ = ["create_llm_client", "create_tts_provider",
           "DEFAULT_LLM_PROVIDER", "DEFAULT_TTS_PROVIDER"]
