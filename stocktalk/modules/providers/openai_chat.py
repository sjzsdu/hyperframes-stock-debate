"""OpenAI-compatible HTTP 对话实现（stdlib urllib，不引入新依赖）。

适用于任何暴露 ``POST {base_url}/chat/completions`` 且响应为
``choices[0].message.content`` 的端点（OpenAI / DeepSeek / Moonshot /
vLLM / Ollama 的兼容层……）。鉴权走环境变量，key 不落配置文件。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from stocktalk.modules.providers.base import LLMProviderError, register_llm

Urlopen = Callable[..., Any]


class OpenAIChatClient:
    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1",
                 api_key_env: str = "OPENAI_API_KEY", default_timeout: float = 300.0,
                 urlopen: Urlopen | None = None) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self._default_timeout = float(default_timeout)
        self._urlopen = urlopen or urllib.request.urlopen

    def chat(self, system: str, message: str, *,
             max_tokens: int | None = None,
             temperature: float | None = None,
             timeout: float | None = None) -> str:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise LLMProviderError(f"environment variable {self.api_key_env} is not set")
        payload: dict[str, Any] = {"model": self.model, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ]}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        effective_timeout = float(timeout if timeout is not None else self._default_timeout)
        try:
            with self._urlopen(request, timeout=effective_timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise LLMProviderError(f"OpenAI-compatible chat failed: {exc}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(
                f"OpenAI-compatible chat returned an unexpected body: {data!r}") from exc
        if not isinstance(content, str):
            raise LLMProviderError(
                f"OpenAI-compatible chat returned a non-string message: {content!r}")
        return content


@register_llm("openai-compatible")
def openai_chat(source: Mapping[str, Any], default_timeout: float = 300.0) -> OpenAIChatClient:
    options = dict(source.get("options") or {})
    return OpenAIChatClient(
        model=str(source.get("model", "gpt-4o-mini")),
        base_url=str(options.get("base_url", "https://api.openai.com/v1")),
        api_key_env=str(options.get("api_key_env", "OPENAI_API_KEY")),
        default_timeout=default_timeout,
    )
