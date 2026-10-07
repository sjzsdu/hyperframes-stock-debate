"""DashScope realtime WebSocket 的语音实现（qwen3-tts-vd 设计音色直连）。

设计音色在逐行新开会话时音色会漂移：服务端每次连接重新播种韵律。因此
同一个 voice 复用一条持久 WebSocket、逐行 ``commit``，各段独立落盘而共享
同一会话上下文。本模块从旧 ``TTSAgent._synthesize_all_ws/_run_ws_batch``
原样搬移，行为（含超时、错误文案、PCM→WAV 归一化）保持一致。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
from pathlib import Path
from typing import Any, Mapping, Sequence

from stocktalk.modules.providers.base import TTSJob, TTSProviderError, register_tts

try:
    import websockets
except ImportError:
    websockets = None


class DashScopeWSTTSProvider:
    """实时设计音色：只接受会话级参数，不支持逐句 prosody。"""

    supports_line_prosody = False

    def __init__(self, model: str = "qwen3-tts-vd-realtime-2025-12-16",
                 timeout: float = 30.0, volume: Any = 1.0,
                 api_key: str | None = None) -> None:
        self.model = model
        self.timeout = float(timeout)
        self._volume = volume
        self._api_key = api_key

    # -- 契约入口 ---------------------------------------------------------

    def check_ready(self) -> None:
        """编排层入口调用一次；依赖缺失在这里给出明确报错。"""
        if websockets is None:
            raise TTSProviderError("websockets package is required (pip install websockets)")
        api_key = self._api_key if self._api_key is not None else os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise TTSProviderError("DASHSCOPE_API_KEY environment variable is not set")

    def synthesize_lines(self, jobs: Sequence[TTSJob]) -> None:
        self.check_ready()
        for job in jobs:
            if not job.voice or job.voice == "default":
                raise TTSProviderError(f"No voice_id configured for {job.label or job.voice}")
        try:
            files = asyncio.run(self._run_batch(list(jobs), api_key))
        except TTSProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 — 旧实现把一切传输错误归一成合成失败
            raise TTSProviderError(f"WebSocket TTS batch failed: {exc}") from exc
        for job, raw in zip(jobs, files):
            if not raw:
                raise TTSProviderError(f"WebSocket TTS produced no audio for {job.label or job.voice}")
            with open(job.out_path, "wb") as handle:
                handle.write(self._ensure_wav(raw))

    # -- 传输 -------------------------------------------------------------

    async def _run_batch(self, jobs: list[TTSJob], api_key: str) -> list[bytes]:
        ws_url = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={self.model}"
        timeout = self.timeout
        volume = self._session_volume()

        async def connect_session(label: str, job: TTSJob):
            ws = await websockets.connect(ws_url, additional_headers={"Authorization": f"Bearer {api_key}"})
            try:
                await ws.recv()  # session.created
                # 线上统一 PCM：会话只有首个响应带 RIFF 头，之后都是无头 PCM，
                # 每段由 _ensure_wav 自行归一化。
                wire_format = "pcm" if job.audio_format in {"wav", "pcm"} else job.audio_format
                session_config: dict[str, Any] = {
                    "voice": job.voice,
                    "response_format": wire_format,
                    "sample_rate": 24000,
                    "mode": "commit",
                    "speech_rate": job.speed,
                    "pitch_rate": job.pitch,
                    "volume": volume,
                    "language_type": job.language,
                }
                if job.instruction:
                    session_config["instructions"] = job.instruction
                await ws.send(json.dumps({"type": "session.update", "session": session_config}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if reply.get("type") == "error":
                    raise TTSProviderError(
                        f"WebSocket session error ({label}): {reply.get('error', {}).get('message', reply)}")
            except Exception:
                await ws.close()
                raise
            return ws

        async def synth_line(ws, line: str) -> bytes:
            await ws.send(json.dumps({"type": "input_text_buffer.append", "text": line}))
            await ws.send(json.dumps({"type": "input_text_buffer.commit"}))
            chunks: list[bytes] = []
            while True:
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                t = data.get("type", "")
                if t == "response.audio.delta":
                    chunks.append(base64.b64decode(data["delta"]))
                elif t == "response.done":
                    break
                elif t == "error":
                    raise TTSProviderError(
                        f"WebSocket synthesis error: {data.get('error', {}).get('message', data)}")
            return b"".join(chunks)

        connections: dict[str, Any] = {}
        results: list[bytes] = []
        try:
            for job in jobs:
                if job.label not in connections:
                    connections[job.label] = await connect_session(job.label, job)
                results.append(await synth_line(connections[job.label], job.text))
        finally:
            for ws in connections.values():
                try:
                    await ws.send(json.dumps({"type": "session.finish"}))
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(ws.close(), timeout=5)
                except Exception:
                    pass
        return results

    def _session_volume(self) -> int:
        try:
            value = float(self._volume)
            return max(0, min(100, round(value * 100 if value <= 1 else value)))
        except (TypeError, ValueError):
            return 100

    # -- 归一化 -----------------------------------------------------------

    @staticmethod
    def _ensure_wav(raw: bytes, sample_rate: int = 24000) -> bytes:
        """把裸 PCM 包上 RIFF/WAVE 头；已是 WAV/MP3 的流原样通过。

        realtime 服务端只在会话首个响应带 RIFF 头，之后的 ``commit`` 响应
        是无头 PCM，因此每段都必须归一化成独立可播放的 WAV。
        """
        if raw[:4] == b"RIFF":
            return raw
        return (struct.pack("<4sI4s4sIHHIIHH4sI",
                            b"RIFF", 36 + len(raw), b"WAVE", b"fmt ", 16,
                            1, 1, sample_rate, sample_rate * 2, 2, 16,
                            b"data", len(raw)) + raw)


@register_tts("dashscope-ws")
def dashscope_ws_tts(source: Mapping[str, Any]) -> DashScopeWSTTSProvider:
    options = dict(source.get("options") or {})
    return DashScopeWSTTSProvider(
        model=str(source.get("model", "qwen3-tts-vd-realtime-2025-12-16")),
        timeout=float(source.get("websocket_timeout_seconds", 30)),
        volume=source.get("volume", 1.0),
        api_key=options.get("api_key"),
    )
