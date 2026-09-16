"""谈股论金对话脚本的百炼 TTS 编排。

每句对话单独渲染，以便视频时间轴准确切换活跃说话者。
字幕时间是从生成的音频中导出的，而不是根据行中字符数估算的。
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping

try:
    import websockets
except ImportError:
    websockets = None


class TTSSynthesisError(RuntimeError):
    """Raised when Bailian cannot produce a usable audio segment."""


class TTSAgent:
    """Generate independent bull/bear voice tracks and aligned SRT subtitles."""

    def __init__(self, config: Mapping[str, Any]):
        self.config = config
        self.tts_config = config.get("tts", {})
        self.characters = config.get("characters", {})
        self.output_dir = Path(
            config.get("output", {}).get(
                "dir", config.get("video", {}).get("output_dir", "./output")
            )
        )

    def synthesize(self, script: Mapping[str, Any]) -> Dict[str, Any]:
        """Synthesize the script in display order and return its media timeline.

        ``script['turns']`` is an ordered list of speaker/line mappings.  A synthesis failure is not hidden: an
        estimated timestamp would put later subtitles out of sync.
        """
        if not self._is_websocket_model:
            self._require_bailian_cli()
        if self._is_websocket_model and websockets is None:
            raise TTSSynthesisError("websockets package is required for qwen3-tts-vd-realtime models (pip install websockets)")
        audio_dir = self.output_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        turns = [(str(turn["speaker"]), str(turn["line"])) for turn in self._turns(script)]
        jobs = [(character, line, audio_dir / f"{index:02d}_{character}.{self._audio_format}")
                for index, (character, line) in enumerate(turns, start=1)]
        if self._is_websocket_model:
            rendered = self._synthesize_all_ws(jobs)
        else:
            rendered = [self._synthesize_line_cli(line, character, output_path)
                        for character, line, output_path in jobs]

        segments: List[Dict[str, Any]] = []
        cursor = 0.0
        pause = max(0.0, float(self.tts_config.get("pause_between_segments", 0)))
        for segment in rendered:
            segment["start_time"] = cursor
            segment["end_time"] = cursor + segment["duration"]
            segments.append(segment)
            cursor = segment["end_time"] + pause

        srt_path = self._generate_srt(segments)
        return {
            "segments": segments,
            "srt_path": str(srt_path),
            "total_duration": segments[-1]["end_time"] if segments else 0.0,
        }

    @staticmethod
    def _turns(script: Mapping[str, Any]) -> List[Dict[str, str]]:
        """Read the natural-turn contract, with legacy round support for old files."""
        turns = script.get("turns", [])
        if isinstance(turns, list):
            return [{"speaker": str(item.get("speaker")), "line": str(item.get("line"))}
                    for item in turns if isinstance(item, Mapping) and item.get("speaker") in {"bull", "bear"} and item.get("line")]
        result: List[Dict[str, str]] = []
        for round_data in script.get("rounds", []):
            if isinstance(round_data, Mapping):
                for speaker in ("bull", "bear"):
                    line = TTSAgent._line_from(round_data.get(speaker))
                    if line: result.append({"speaker": speaker, "line": line})
        return result

    @property
    def _audio_format(self) -> str:
        value = str(self.tts_config.get("output_format", "wav")).lower()
        if value not in {"mp3", "pcm", "wav", "opus"}:
            raise ValueError(f"Unsupported Bailian audio format: {value}")
        return value

    @property
    def _is_websocket_model(self) -> bool:
        """Design-voice models (qwen3-tts-vd-realtime) require WebSocket synthesis."""
        model = str(self.tts_config.get("model", "cosyvoice-v3-flash"))
        return "vd-realtime" in model or "tts-vd" in model

    def _synthesize_all_ws(
        self, jobs: List[tuple[str, str, Path]]
    ) -> List[Dict[str, Any]]:
        """Synthesize every line over one persistent WebSocket per character.

        Design voices drift in timbre when each line opens a fresh session: the
        server re-seeds prosody per connection.  Keeping a single connection per
        speaker and issuing ``input_text_buffer.commit`` per line yields separate
        audio files while sharing one voice/session context, so the same speaker
        sounds consistent across the whole video.
        """
        if websockets is None:
            raise TTSSynthesisError("websockets package is required (pip install websockets)")
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise TTSSynthesisError("DASHSCOPE_API_KEY environment variable is not set")
        for character, _, _ in jobs:
            voice_id = (self.characters.get(character, {}) or {}).get("voice_id")
            if not voice_id or voice_id == "default":
                raise TTSSynthesisError(f"No voice_id configured for {character}")

        try:
            files = asyncio.run(self._run_ws_batch(jobs, api_key))
        except TTSSynthesisError:
            raise
        except Exception as exc:
            raise TTSSynthesisError(f"WebSocket TTS batch failed: {exc}") from exc

        segments: List[Dict[str, Any]] = []
        for (character, line, output_path), raw in zip(jobs, files):
            if not raw:
                raise TTSSynthesisError(f"WebSocket TTS produced no audio for {character}")
            with open(output_path, "wb") as f:
                f.write(self._ensure_wav(raw))
            if self.tts_config.get("trim_silence", True):
                self._trim_and_fade(output_path)
            character_config = self.characters.get(character, {})
            segments.append({
                "character": character,
                "character_name": character_config.get("name", character),
                "line": line,
                "audio_path": str(output_path),
                "duration": self._audio_duration(output_path),
            })
        return segments

    @staticmethod
    def _ensure_wav(raw: bytes, sample_rate: int = 24000) -> bytes:
        """Wrap bare PCM in a RIFF/WAVE header; pass WAV/MP3 streams through.

        The realtime server only emits a RIFF header on the FIRST response of a
        session; later ``commit`` responses arrive as headerless PCM, so every
        segment must be normalised to a standalone playable WAV.
        """
        if raw[:4] == b"RIFF":
            return raw
        return (struct.pack("<4sI4s4sIHHIIHH4sI",
                            b"RIFF", 36 + len(raw), b"WAVE", b"fmt ", 16,
                            1, 1, sample_rate, sample_rate * 2, 2, 16,
                            b"data", len(raw)) + raw)

    async def _run_ws_batch(
        self, jobs: List[tuple[str, str, Path]], api_key: str
    ) -> List[bytes]:
        model = str(self.tts_config.get("model", "qwen3-tts-vd-realtime-2025-12-16"))
        ws_url = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={model}"
        timeout = float(self.tts_config.get("websocket_timeout_seconds", 30))
        volume = self.tts_config.get("volume", 1.0)
        try:
            volume = max(0, min(100, round(float(volume) * 100 if float(volume) <= 1 else float(volume))))
        except (TypeError, ValueError):
            volume = 100

        async def connect_character(character: str):
            cfg = self.characters.get(character, {})
            speed = float(cfg.get("speed", self.tts_config.get("speed", 1.0)))
            pitch = float(cfg.get("pitch", self.tts_config.get("pitch", 1.0)))
            instructions = str(cfg.get("voice_direction", "")).strip() or None
            ws = await websockets.connect(ws_url, additional_headers={"Authorization": f"Bearer {api_key}"})
            try:
                await ws.recv()  # session.created
                # Uniform PCM on the wire: only the session's first response
                # carries a RIFF header, so we normalise every segment ourselves.
                wire_format = "pcm" if self._audio_format in {"wav", "pcm"} else self._audio_format
                session_config: dict[str, Any] = {
                    "voice": cfg.get("voice_id"),
                    "response_format": wire_format,
                    "sample_rate": 24000,
                    "mode": "commit",
                    "speech_rate": speed,
                    "pitch_rate": pitch,
                    "volume": volume,
                    "language_type": "zh",
                }
                if instructions:
                    session_config["instructions"] = instructions
                await ws.send(json.dumps({"type": "session.update", "session": session_config}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if reply.get("type") == "error":
                    raise TTSSynthesisError(f"WebSocket session error ({character}): {reply.get('error', {}).get('message', reply)}")
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
                    raise TTSSynthesisError(f"WebSocket synthesis error: {data.get('error', {}).get('message', data)}")
            return b"".join(chunks)

        connections: dict[str, Any] = {}
        results: List[bytes] = []
        try:
            for character, line, _ in jobs:
                if character not in connections:
                    connections[character] = await connect_character(character)
                results.append(await synth_line(connections[character], line))
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

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    def _apply_prosody(self, line: str, character_config: Mapping[str, Any]) -> tuple[str, float, float, bool]:
        """Return (synth_text, speed, pitch, use_ssml) for one spoken line.

        System voices (cosyvoice-v3-flash) reject SSML emphasis/prosody tags,
        but accept <break>.  So cadence comes from two levers: per-line
        pitch/rate derived from punctuation and intent, plus a couple of
        rhetorical pauses at clause boundaries.  The original line is returned
        untouched to the caller for subtitles.
        """
        base_pitch = float(character_config.get("pitch", self.tts_config.get("pitch", 1.0)))
        base_rate = float(character_config.get("speed", self.tts_config.get("speed", 1.0)))
        pitch_delta = rate_delta = 0.0
        if "？" in line or "?" in line:
            pitch_delta += 0.06          # questions lift
            rate_delta -= 0.02
        if "！" in line or "!" in line:
            pitch_delta += 0.05          # emphasis push
            rate_delta += 0.04
        if any(word in line for word in ("风险", "警惕", "回撤", "不确定", "小心", "危险", "亏损", "隐患")):
            pitch_delta -= 0.02          # warnings sink and slow
            rate_delta -= 0.06
        if any(word in line for word in ("你看", "打个比方", "说白了", "也就是说", "举个例子")):
            rate_delta -= 0.03          # explanatory asides ease off
        pitch = round(self._clamp(base_pitch + pitch_delta, 0.82, 1.22), 3)
        speed = round(self._clamp(base_rate + rate_delta, 0.92, 1.18), 3)
        synth_text, use_ssml = self._with_breaks(line)
        return synth_text, speed, pitch, use_ssml

    @staticmethod
    def _with_breaks(line: str) -> tuple[str, bool]:
        """Insert a few real SSML pauses for natural rhythm (system voices only
        support <break>, not emphasis/prosody).  The first one or two clause
        commas become short pauses; ellipsis/dash become longer ones.
        """
        text = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace("……", '<break time="300ms"/>')
        text = text.replace("——", '<break time="260ms"/>')
        comma_count = 0

        def pause_comma(_match: re.Match[str]) -> str:
            nonlocal comma_count
            comma_count += 1
            return '<break time="150ms"/>' if comma_count <= 2 else "，"

        text = re.sub("，", pause_comma, text)

        def pause_terminal(match: re.Match[str]) -> str:
            # Keep the punctuation; add a short beat only when the line continues.
            if match.end() < len(text):
                return match.group(0) + '<break time="130ms"/>'
            return match.group(0)

        text = re.sub("[？！]", pause_terminal, text)
        if "<break" in text:
            return f"<speak>{text}</speak>", True
        return line, False

    def _synthesize_line_cli(self, line: str, character: str, output_path: Path) -> Dict[str, Any]:
        character_config = self.characters.get(character, {})
        voice_id = character_config.get("voice_id")
        if not voice_id or voice_id == "default":
            raise TTSSynthesisError(
                f"No Bailian TTS voice configured for {character}. "
                "Set characters.<name>.voice_id to a voice returned by "
                "`bl speech synthesize --list-voices --model cosyvoice-v3-flash`."
            )
        # Per-line prosody: questions lift, exclamations push, risk lines slow
        # down; strategic <break> tags add rhetorical rhythm.  The original line
        # is kept untouched for subtitles.
        synth_text, speed, pitch, use_ssml = self._apply_prosody(line, character_config)
        command = [
            "bl", "speech", "synthesize",
            "--text", synth_text,
            "--model", str(self.tts_config.get("model", "cosyvoice-v3-flash")),
            "--format", self._audio_format,
            "--out", str(output_path),
        ]
        command.extend(["--voice", str(voice_id)])
        if use_ssml:
            command.append("--enable-ssml")

        sample_rate = self.tts_config.get("sample_rate")
        if sample_rate:
            command.extend(["--sample-rate", str(sample_rate)])
        command.extend(["--rate", str(speed)])
        volume = self.tts_config.get("volume")
        if volume is not None:
            volume_f = float(volume)
            command.extend(["--volume", str(round(volume_f * 100) if volume_f <= 1 else round(volume_f))])
        if pitch is not None and 0.5 <= float(pitch) <= 2.0:
            command.extend(["--pitch", str(pitch)])
        # Some cloned/designed voices support natural-language instructions.
        # CosyVoice v3 system voices currently reject this parameter (428), so
        # voice_direction remains useful documentation unless explicitly opted in.
        instruction = character_config.get("instruction") if character_config.get("supports_instruction") else None
        if instruction:
            command.extend(["--instruction", str(instruction)])
        seed = self.tts_config.get("seed")
        if seed is not None:
            command.extend(["--seed", str(seed)])
        command.extend(["--language", "zh"])

        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TTSSynthesisError(f"Failed to run Bailian TTS for {character}: {exc}") from exc
        if completed.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no audio file was created"
            raise TTSSynthesisError(f"Bailian TTS failed for {character}: {detail}")

        if self.tts_config.get("trim_silence", True):
            self._trim_and_fade(output_path)

        return {
            "character": character,
            "character_name": character_config.get("name", character),
            "line": line,
            "audio_path": str(output_path),
            "duration": self._audio_duration(output_path),
        }

    def _trim_and_fade(self, path: Path) -> None:
        """Trim only edge silence and add tiny fades so adjacent turns do not click.

        TTS providers leave a variable 0.1–0.6 second tail.  A fixed timeline gap
        therefore still sounds irregular.  Detecting the real speech bounds keeps
        internal rhetorical pauses intact while making every hand-off consistent.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or path.suffix.lower() != ".wav":
            return
        duration = self._audio_duration(path)
        probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path), "-af", "silencedetect=noise=-48dB:d=0.04", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        events = probe.stderr
        leading = 0.0
        first_start = re.search(r"silence_start:\s*0(?:\.0+)?\b", events)
        if first_start:
            match = re.search(r"silence_end:\s*([0-9.]+)", events[first_start.start():])
            if match:
                leading = float(match.group(1))
        trailing = duration
        starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", events)]
        if starts and "silence_end:" not in events[events.rfind("silence_start:"):]:
            trailing = starts[-1]
        keep_lead = max(0.0, float(self.tts_config.get("leading_silence", 0.025)))
        keep_tail = max(0.0, float(self.tts_config.get("trailing_silence", 0.06)))
        start = max(0.0, leading - keep_lead)
        end = min(duration, trailing + keep_tail)
        if end - start < 0.2 or (start < 0.01 and duration - end < 0.02):
            return
        fade = min(max(0.0, float(self.tts_config.get("fade_seconds", 0.025))), (end - start) / 4)
        temp = path.with_name(f".{path.stem}.polished{path.suffix}")
        filters = f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(0, end-start-fade):.3f}:d={fade:.3f}"
        polished = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.4f}", "-t", f"{end-start:.4f}", "-i", str(path), "-af", filters, str(temp)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if polished.returncode == 0 and temp.is_file() and temp.stat().st_size:
            temp.replace(path)
        elif temp.exists():
            temp.unlink()

    def _audio_duration(self, path: Path) -> float:
        if path.suffix.lower() == ".wav":
            duration = self._wav_duration(path)
            if duration > 0:
                return duration

        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            try:
                duration = float(probe.stdout.strip())
                if duration > 0 and math.isfinite(duration):
                    return duration
            except ValueError:
                pass
        raise TTSSynthesisError(f"Unable to determine duration of generated audio: {path}")

    @classmethod
    def _wav_duration(cls, path: Path) -> float:
        """Compute WAV duration, tolerating corrupt chunk-size fields.

        Bailian's CLI writes 0x7FFFFFFF for both the RIFF and ``data`` chunk
        sizes, which makes Python's :mod:`wave` report a ~12.5 hour file for a
        few seconds of audio.  Walk the chunk list ourselves and, when a
        declared size exceeds what the file can physically contain, fall back
        to the bytes actually present after the ``data`` header.
        """
        try:
            with path.open("rb") as handle:
                header = handle.read(12)
                if len(header) != 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
                    return 0.0
                sample_rate = 0
                channels = 0
                frame_width = 0
                data_start = 0
                data_size = 0
                while True:
                    chunk = handle.read(8)
                    if len(chunk) != 8:
                        break
                    chunk_id, chunk_size = chunk[0:4], struct.unpack("<I", chunk[4:8])[0]
                    if chunk_id == b"fmt ":
                        fmt = handle.read(16)
                        if len(fmt) != 16:
                            break
                        channels = struct.unpack("<H", fmt[2:4])[0]
                        sample_rate = struct.unpack("<I", fmt[4:8])[0]
                        bits = struct.unpack("<H", fmt[14:16])[0]
                        frame_width = max(1, channels * (bits // 8))
                        leftover = chunk_size - 16
                        handle.seek(leftover, 1) if leftover > 0 else None
                    elif chunk_id == b"data":
                        data_start = handle.tell()
                        data_size = chunk_size
                        break
                    else:
                        handle.seek(chunk_size + (chunk_size % 2), 1)
                if not sample_rate or not frame_width or not data_start:
                    return 0.0
                handle.seek(0, 2)
                file_size = handle.tell()
                available = file_size - data_start
                if data_size > available or data_size == 0x7FFFFFFF:
                    data_size = available
                frames = data_size // frame_width
                duration = frames / sample_rate
                return duration if duration > 0 and math.isfinite(duration) else 0.0
        except (OSError, ValueError, EOFError):
            return 0.0

    def _generate_srt(self, segments: List[Mapping[str, Any]]) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        srt_path = self.output_dir / "subtitles.srt"
        blocks = []
        for index, segment in enumerate(segments, start=1):
            blocks.append(
                f"{index}\n{self._format_srt_time(float(segment['start_time']))} --> "
                f"{self._format_srt_time(float(segment['end_time']))}\n{segment['line']}"
            )
        srt_path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
        return srt_path

    @staticmethod
    def _line_from(entry: Any) -> str:
        if isinstance(entry, Mapping):
            entry = entry.get("line", "")
        return str(entry).strip() if entry else ""

    @staticmethod
    def _format_srt_time(seconds: float) -> str:
        milliseconds = max(0, int(round(seconds * 1000)))
        hours, milliseconds = divmod(milliseconds, 3_600_000)
        minutes, milliseconds = divmod(milliseconds, 60_000)
        whole_seconds, milliseconds = divmod(milliseconds, 1_000)
        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"

    @staticmethod
    def _require_bailian_cli() -> None:
        if not shutil.which("bl"):
            raise TTSSynthesisError("Bailian CLI 'bl' is not installed or not on PATH")
