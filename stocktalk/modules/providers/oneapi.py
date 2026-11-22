"""Single dialogue gateway, bounded I/O, no replay after an ambiguous POST."""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
import subprocess
import sys
from typing import Any, Callable, Mapping

from .base import LLMProviderError, register_llm

BASE_URL = "https://oneapi.wnbgj.com/v1"
KEY_ENV = "ONEAPI_API_KEY"


class OneAPIRequestError(LLMProviderError):
    def __init__(self, code: str, *, outcome_unknown: bool = False):
        self.code = code
        self.outcome_unknown = outcome_unknown
        super().__init__(f"{code}: " + {
            "CONFIG_MISSING": "请在服务进程设置 ONEAPI_API_KEY。",
            "CONFIG_INVALID": "请迁移 dialogue.provider/options 到 oneapi。",
            "UPSTREAM_AUTH": "网关拒绝授权，请检查令牌和授权范围。",
            "UPSTREAM_TIMEOUT": "网关超时；结果可能未知，请核对后显式重试。",
            "MODEL_UNAVAILABLE": "模型不在可用清单，请刷新并重选。",
            "QUOTA_EXCEEDED": "网关额度不足，请联系维护方。",
            "RATE_LIMITED": "网关限流，请稍后显式重试。",
            "UPSTREAM_ERROR": "网关请求失败，请检查服务状态。",
        }.get(code, "请求失败。"))

    def public(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "outcome_unknown": self.outcome_unknown}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OneAPIRequestError("UPSTREAM_ERROR")


class OneAPIClient:
    def __init__(self, model: str = "", *, base_url: str = BASE_URL, api_key_env: str = KEY_ENV,
                 urlopen: Callable[..., Any] | None = None, default_timeout: float = 60):
        if base_url.rstrip("/") != BASE_URL or api_key_env != KEY_ENV:
            raise OneAPIRequestError("CONFIG_INVALID")
        self.model, self.base_url, self.api_key_env = model, BASE_URL, KEY_ENV
        self._urlopen = urlopen
        self.timeout = min(float(default_timeout), 60.0)

    def _request(self, route: str, payload: dict[str, Any] | None = None, timeout: float = 5) -> dict[str, Any]:
        key = os.environ.get(KEY_ENV)
        if not key:
            raise OneAPIRequestError("CONFIG_MISSING")
        request = urllib.request.Request(BASE_URL + route,
                    data=json.dumps(payload).encode() if payload is not None else None,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        deadline = time.monotonic() + timeout

        def read():
            assert self._urlopen is not None
            with self._urlopen(request, timeout=min(timeout, 5)) as response:
                # Read incrementally so an upstream that trickles bytes cannot extend the budget.
                body = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    part = response.read(65536)
                    if not part:
                        break
                    body.extend(part)
                    if len(body) > 8 * 1024 * 1024:
                        raise ValueError("response exceeds limit")
                return json.loads(body)

        try:
            if self._urlopen is None:
                # An isolated transport process gives socket/header/body handling a
                # hard deadline. subprocess.run kills and reaps it on timeout.
                completed = subprocess.run([sys.executable, "-m", "stocktalk.modules.providers.oneapi_http"],
                    input=json.dumps({"route": route, "payload": payload, "timeout": timeout}),
                    text=True, capture_output=True, timeout=timeout, check=True)
                envelope = json.loads(completed.stdout)
                if "error" in envelope:
                    raise OneAPIRequestError(envelope["error"]["code"],
                                             outcome_unknown=envelope["error"]["outcome_unknown"])
                return envelope["data"]
            result = read()
            if not isinstance(result, dict):
                raise ValueError("invalid envelope")
            return result
        except urllib.error.HTTPError as exc:
            code = {401: "UPSTREAM_AUTH", 403: "UPSTREAM_AUTH", 429: "RATE_LIMITED"}.get(exc.code, "UPSTREAM_ERROR")
            # Use protocol codes only; never echo upstream messages or headers.
            try:
                error_code = json.loads(exc.read(65536)).get("error", {}).get("code")
                code = {"insufficient_quota": "QUOTA_EXCEEDED", "model_not_found": "MODEL_UNAVAILABLE"}.get(error_code, code)
            except (ValueError, AttributeError, TypeError):
                pass
            raise OneAPIRequestError(code) from None
        except (TimeoutError, socket.timeout, subprocess.TimeoutExpired):
            raise OneAPIRequestError("UPSTREAM_TIMEOUT", outcome_unknown=payload is not None) from None
        except OneAPIRequestError:
            raise
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            raise OneAPIRequestError("UPSTREAM_ERROR", outcome_unknown=payload is not None) from None

    def models(self) -> list[str]:
        data = self._request("/models")
        try:
            models = sorted({row["id"] for row in data["data"] if isinstance(row["id"], str) and row["id"]})
            return models
        except (KeyError, TypeError):
            raise OneAPIRequestError("UPSTREAM_ERROR") from None

    def chat(self, system: str, message: str, *, max_tokens=None, temperature=None, timeout=None) -> str:
        payload: dict[str, Any] = {"model": self.model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": message}]}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        data = self._request("/chat/completions", payload, min(float(timeout or self.timeout), self.timeout))
        try:
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError()
            return content
        except (KeyError, IndexError, TypeError):
            raise OneAPIRequestError("UPSTREAM_ERROR", outcome_unknown=True) from None


@register_llm("oneapi")
def oneapi(source: Mapping[str, Any], default_timeout: float = 60) -> OneAPIClient:
    options = dict(source.get("options") or {})
    if set(options) - {"base_url", "api_key_env"}:
        raise OneAPIRequestError("CONFIG_INVALID")
    return OneAPIClient(str(source.get("model", "")), base_url=options.get("base_url", BASE_URL),
                        api_key_env=options.get("api_key_env", KEY_ENV), default_timeout=default_timeout)


def validate_dialogue(config: Mapping[str, Any]) -> None:
    source = config.get("dialogue", config)
    if source.get("provider", "oneapi") != "oneapi":
        raise OneAPIRequestError("CONFIG_INVALID")
    oneapi(source)
