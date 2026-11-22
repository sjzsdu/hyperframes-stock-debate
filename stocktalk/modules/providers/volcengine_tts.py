"""火山引擎豆包语音合成（seed-tts-2.0）provider——抖音最流行解说音色的源头。

抖音/剪映生态的解说配音（解说小帅、磁性男声等）底层就是火山引擎的 TTS 引擎；
本实现走大模型语音合成 2.0 的 HTTP 单向流式接口，与抖音同源：

- 端点：``POST https://openspeech.bytedance.com/api/v3/tts/unidirectional``
- 鉴权：``X-Api-Key``（**语音控制台**的 API Key，与 Ark 大模型的 key 不通用）
- 响应：NDJSON，每行 ``{"code":0,"data":"<base64>"}``，``{"code":20000000}`` 结束

情感控制走 ``additions.context_texts``（自然语言，部分 2.0 音色有效）；
语速映射 ``speech_rate``（-50..100，100=2 倍速）；音量映射 ``loudness_rate``；
音调映射 ``additions.post_process.pitch``（**半音**，-12..12；TTSJob 的倍率 pitch
按 12*log2(ratio) 换算，1.0 不传）。

音色 id 语义（characters.voice_id 直接填）：
- ``zh_female_vv_uranus_bigtts``（可爱女生/Vivi，情感响应好）
- ``zh_male_liufei_uranus_bigtts``（刘飞）、``zh_male_m191_uranus_bigtts``（云舟）
- 标准 streaming 音色（BV*，如电影解说小帅 BV411_streaming）属 1.0 资源，
  resource_id 需配 ``seed-tts-1.0``，且不支持 context_texts。
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
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


def _is_transient_network_error(exc: Exception) -> bool:
    """只把「传输层瞬时故障」判为可重试。

    2026-11-19：本机 Clash TUN + fake-ip（openspeech.bytedance.com → 198.18.x.x），
    代理重连时会抛 ``URLError([SSL: UNEXPECTED_EOF_WHILE_READING])``。这类错误
    重试通常就能过；而 4xx（鉴权/参数/额度）重试多少次都不会成功，必须立刻
    抛出，让上层按 TTS 硬失败中止（拒绝发布静音成片）。
    """
    # HTTPError 是 URLError 的子类，先单独判定：只有 429/5xx 值得等，4xx 直接放弃
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, (urllib.error.URLError, ssl.SSLError, TimeoutError,
                        ConnectionError, ConnectionResetError, OSError)):
        return True
    return False


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
                 default_timeout: float = 120.0,
                 network_retries: int = 3,
                 network_backoff: float = 1.0,
                 sleep: Callable[[float], None] | None = None) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._resource_id = resource_id
        # model 仅复刻音色需要（seed-tts-2.0-standard），官方音色留空
        self._model = model
        self._urlopen: Urlopen = urlopen or urllib.request.urlopen
        self._default_timeout = float(default_timeout)
        # 传输层抖动（代理重连 / TLS EOF）在单句粒度重试：整批重来会把已经
        # 合成好的句子全部重新请求，既浪费又更容易再次撞上抖动窗口。
        self._network_retries = max(0, int(network_retries))
        self._network_backoff = max(0.0, float(network_backoff))
        self._sleep = sleep or time.sleep

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

    @staticmethod
    def _pitch_semitones(pitch: float | None) -> int:
        """倍率 pitch → 半音（接口范围 [-12, 12]）：1.12 → +2；1.0 → 0（不传）。"""
        if not pitch or float(pitch) == 1.0:
            return 0
        return max(-12, min(12, round(12 * math.log2(float(pitch)))))

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
        # additions 是「JSON 序列化后的字符串」，不是对象——接口最常见坑
        additions: dict[str, Any] = {}
        if job.instruction:
            additions["context_texts"] = [job.instruction]
        semitones = self._pitch_semitones(job.pitch)
        if semitones:
            additions["post_process"] = {"pitch": semitones}
        if additions:
            req_params["additions"] = json.dumps(additions, ensure_ascii=False)

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
        # 单句级传输重试：代理重连 / TLS EOF 是瞬时的，等一下再发通常就通。
        # 业务错误（4xx）不重试、耗尽仍失败的照样抛 TTSProviderError —— 上层
        # 依旧按 TTS 硬失败中止，绝不降级成静音成片（2026-10-06 事故红线）。
        attempt = 0
        while True:
            try:
                with self._urlopen(request, timeout=self._default_timeout) as resp:
                    raw = resp.read().decode()
                break
            except Exception as exc:  # noqa: BLE001 — 统一转成 provider 错误
                if not _is_transient_network_error(exc) or attempt >= self._network_retries:
                    raise TTSProviderError(f"Volcengine TTS request failed for {job.label}: {exc}") from exc
                delay = self._network_backoff * (2 ** attempt)
                attempt += 1
                print(
                    f"[tangulunjin] TTS 网络抖动，{delay:g}s 后重试 "
                    f"({attempt}/{self._network_retries}): {exc}",
                    file=sys.stderr,
                )
                self._sleep(delay)

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
        network_retries=int(options.get("network_retries", 3)),
        network_backoff=float(options.get("network_backoff", 1.0)),
    )
