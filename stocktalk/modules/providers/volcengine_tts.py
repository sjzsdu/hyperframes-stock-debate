"""火山引擎豆包语音合成（seed-tts-2.0）provider——抖音最流行解说音色的源头。

抖音/剪映生态的解说配音（解说小帅、磁性男声等）底层就是火山引擎的 TTS 引擎；
本实现走大模型语音合成 2.0 的 HTTP 单向流式接口，与抖音同源：

- 端点：``POST https://openspeech.bytedance.com/api/v3/tts/unidirectional``
- 鉴权：``X-Api-Key``（**语音控制台**的 API Key，与 Ark 大模型的 key 不通用）
- 响应：NDJSON，每行 ``{"code":0,"data":"<base64>"}``，``{"code":20000000}`` 结束

情感控制走 ``additions.context_texts``（自然语言，部分 2.0 音色有效）；
语速映射 ``speech_rate``（-50..100，100=2 倍速）。pitch 在该接口无对应参数，
超范围时忽略（豆包 2.0 的情感表达由 context_texts 承担）。

音色 id 语义（characters.voice_id 直接填）：
- ``zh_female_vv_uranus_bigtts``（可爱女生/Vivi，情感响应好）
- ``zh_male_liufei_uranus_bigtts``（刘飞）、``zh_male_m191_uranus_bigtts``（云舟）
- 标准 streaming 音色（BV*，如电影解说小帅 BV411_streaming）属 1.0 资源，
  resource_id 需配 ``seed-tts-1.0``，且不支持 context_texts。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from stocktalk.modules.providers.base import TTSJob, TTSProviderError, register_tts

TTS_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
# 响应里的成功/结束码（NDJSON 协议）
_CODE_CHUNK = 0
_CODE_DONE = 20000000

Urlopen = Callable[..., Any]

_BREAK_TAG = re.compile(r"</?\s*break[^>]*/?\s*>")


def _strip_break_tags(text: str) -> str:
    """去掉 SSML <break> 标记——豆包 2.0 不按本接口的 SSML 字段传这些标签，
    硬传会被读出来。停顿交给模型自己处理（2.0 的韵律足够）。"""
    return _BREAK_TAG.sub("", text)


def _run_subprocess(argv: Sequence[str], timeout: float) -> str:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise TTSProviderError(f"command failed: {completed.stderr.strip() or 'unknown error'}")
    return completed.stdout


class VolcengineTTSProvider:
    """豆包语音合成 2.0（seed-tts-2.0）。构造零副作用，依赖检查在 ``check_ready``。"""

    supports_line_prosody = True

    def __init__(self, *,
                 api_key: str | None = None,
                 api_key_env: str = "VOLCENGINE_TTS_API_KEY",
                 resource_id: str = "seed-tts-2.0",
                 model: str | None = None,
                 urlopen: Urlopen | None = None,
                 default_timeout: float = 120.0) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._resource_id = resource_id
        # model 仅复刻音色需要（seed-tts-2.0-standard），官方音色留空
        self._model = model
        self._urlopen: Urlopen = urlopen or urllib.request.urlopen
        self._default_timeout = float(default_timeout)

    # -- 契约入口 ---------------------------------------------------------

    def check_ready(self) -> None:
        if not self._resolve_api_key():
            raise TTSProviderError(
                f"Volcengine TTS API key is not set: export {self._api_key_env} "
                "(get it from the Volcengine speech console — the Ark/LLM key is a different credential)")

    def synthesize_lines(self, jobs: Sequence[TTSJob]) -> None:
        for job in jobs:
            self._synthesize(job)

    # -- 实现 -------------------------------------------------------------

    def _resolve_api_key(self) -> str:
        if self._api_key is not None:
            return self._api_key
        return os.environ.get(self._api_key_env, "")

    @staticmethod
    def _speech_rate(speed: float) -> int:
        """speed 1.0 → 0；1.07 → 7；0.5 → -50；2.0 → 100（接口范围 [-50, 100]）。"""
        return max(-50, min(100, round((float(speed) - 1.0) * 100)))

    @staticmethod
    def _loudness_rate(volume: float | None) -> int:
        if volume is None:
            return 0
        return max(-50, min(100, round((float(volume) - 1.0) * 100)))

    def _synthesize(self, job: TTSJob) -> None:
        api_key = self._resolve_api_key()
        if not api_key:
            raise TTSProviderError(f"{self._api_key_env} is not set (cannot synthesize {job.label})")

        text = _strip_break_tags(job.text) if job.use_ssml else job.text
        req_params: dict[str, Any] = {
            "text": text,
            "speaker": job.voice,
            "audio_params": {
                "format": job.audio_format if job.audio_format != "pcm" else "wav",
                "sample_rate": int(job.sample_rate or 24000),
                "speech_rate": self._speech_rate(job.speed),
                "loudness_rate": self._loudness_rate(job.volume),
            },
        }
        if self._model:
            req_params["model"] = self._model
        if job.instruction:
            # additions 是「JSON 序列化后的字符串」，不是对象——接口最常见坑
            req_params["additions"] = json.dumps({"context_texts": [job.instruction]}, ensure_ascii=False)

        body = json.dumps({
            "user": {"uid": "stocktalk"},
            "req_params": req_params,
        }, ensure_ascii=False).encode()

        request = urllib.request.Request(
            TTS_ENDPOINT,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": api_key,
                "X-Api-Resource-Id": self._resource_id,
                "X-Api-Request-Id": str(uuid.uuid4()),
            },
            method="POST",
        )
        try:
            with self._urlopen(request, timeout=self._default_timeout) as resp:
                raw = resp.read().decode()
        except Exception as exc:  # noqa: BLE001 — 统一转成 provider 错误
            raise TTSProviderError(f"Volcengine TTS request failed for {job.label}: {exc}") from exc

        chunks: list[bytes] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TTSProviderError(f"Volcengine TTS returned a malformed NDJSON line for {job.label}") from exc
            code = item.get("code")
            if code == _CODE_CHUNK:
                # code=0 的行可能是元数据（sentence/phonemes，data 为 null），
                # 只有带 data 的行才是音频块
                if item.get("data"):
                    chunks.append(base64.b64decode(item["data"]))
            elif code == _CODE_DONE:
                break
            else:
                message = item.get("message") or item.get("header", {}).get("message") or item
                raise TTSProviderError(f"Volcengine TTS error for {job.label} (code={code}): {message}")

        audio = b"".join(chunks)
        if not audio:
            raise TTSProviderError(f"Volcengine TTS produced no audio for {job.label}")
        out_path: Path = job.out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)


@register_tts("volcengine-doubao")
def volcengine_tts(source: Mapping[str, Any]) -> VolcengineTTSProvider:
    options = dict(source.get("options") or {})
    return VolcengineTTSProvider(
        api_key=options.get("api_key"),
        api_key_env=str(options.get("api_key_env", "VOLCENGINE_TTS_API_KEY")),
        resource_id=str(options.get("resource_id", "seed-tts-2.0")),
        model=options.get("model"),
    )
