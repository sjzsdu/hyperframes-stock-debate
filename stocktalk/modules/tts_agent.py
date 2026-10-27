"""谈股论金对话脚本的 TTS 编排（provider 可插拔，默认百炼 CLI）。

每句对话单独渲染，以便视频时间轴准确切换活跃说话者。
字幕时间是从生成的音频中导出的，而不是根据行中字符数估算的。

本模块只负责业务：逐句 prosody（语速/音调/SSML break）、静音裁剪、
时长统计与字幕对齐。传输（CLI 子进程 / DashScope WebSocket）在
``stocktalk.modules.providers``，按 ``tts.provider`` 配置挑选。
"""

from __future__ import annotations

import math
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping

from stocktalk.modules.arcs import resolve_speakers
from stocktalk.modules.dialogue_generator import EMOTION_DIRECTIVES
from stocktalk.modules.numbers import to_display
from stocktalk.modules.providers import TTSJob, TTSProviderError, create_tts_provider


class TTSSynthesisError(RuntimeError):
    """Raised when the TTS provider cannot produce a usable audio segment."""


class TTSAgent:
    """Generate independent bull/bear voice tracks and aligned SRT subtitles."""

    def __init__(self, config: Mapping[str, Any], tts_provider: Any = None):
        self.config = config
        self.tts_config = config.get("tts", {})
        self.characters = config.get("characters", {})
        # 传输层：显式注入 > 工厂（tts.provider，auto 按模型名分流，缺省=旧行为）。
        self._provider = tts_provider if tts_provider is not None else create_tts_provider(self.tts_config)
        self._supports_prosody = bool(getattr(self._provider, "supports_line_prosody", True))
        # 说话的角色由配置决定，不写死：硬编码 {"bull","bear"} 会让配置里新增的
        # 角色在这一步被静默丢掉（台词消失且不报错），是最难查的一类坑。
        # 默认两名角色始终可用，配置只覆盖其中一个时不会误判。
        self.speakers = resolve_speakers(self.characters)
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
        self._provider.check_ready()  # 依赖齐备性（CLI/包/密钥）在入口查一次
        audio_dir = self.output_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        turns = [(str(turn["speaker"]), str(turn["line"]), str(turn.get("emotion", "")))
                 for turn in self._turns(script)]
        jobs = [(character, line, emotion, audio_dir / f"{index:02d}_{character}.{self._audio_format}")
                for index, (character, line, emotion) in enumerate(turns, start=1)]
        if self._supports_prosody:
            rendered = [self._synthesize_line_cli(line, character, output_path, emotion)
                        for character, line, emotion, output_path in jobs]
        else:
            rendered = self._synthesize_all_ws(jobs)

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

    def _turns(self, script: Mapping[str, Any]) -> List[Dict[str, str]]:
        """Read the natural-turn contract, with legacy round support for old files."""
        turns = script.get("turns", [])
        if isinstance(turns, list):
            return [{"speaker": str(item.get("speaker")), "line": str(item.get("line")),
                     "emotion": str(item.get("emotion") or "")}
                    for item in turns if isinstance(item, Mapping) and item.get("speaker") in self.speakers and item.get("line")]
        result: List[Dict[str, str]] = []
        for round_data in script.get("rounds", []):
            if isinstance(round_data, Mapping):
                for speaker in self.speakers:
                    line = TTSAgent._line_from(round_data.get(speaker))
                    if line: result.append({"speaker": speaker, "line": line, "emotion": ""})
        return result

    def _line_instruction(self, character: str, emotion: str) -> str | None:
        """本句的演绎指令：角色基础指令 + 按轮情绪短语（2026-10-07）。

        通道开关语义与旧版一致：CLI 路径要 ``supports_instruction`` 显式开启
        （系统 _v3 音色传 instruction 会 428）；WebSocket 设计音色的
        ``voice_direction`` 一直直传、无需开关。情绪短语只在
        ``supports_instruction`` 通道激活时叠加——豆包 context_texts 每次调用
        独立传，逐句合成的架构正好一句一个情绪。
        """
        cfg = self.characters.get(character, {}) or {}
        if cfg.get("supports_instruction"):
            parts = [str(cfg.get("instruction") or "").strip()]
            directive = EMOTION_DIRECTIVES.get(emotion, "")
            if directive:
                parts.append(directive)
        else:
            parts = [str(cfg.get("voice_direction") or "").strip()]
        merged = "；".join(p for p in parts if p)
        return merged or None

    @property
    def _audio_format(self) -> str:
        value = str(self.tts_config.get("output_format", "wav")).lower()
        if value not in {"mp3", "pcm", "wav", "opus"}:
            raise ValueError(f"Unsupported audio format: {value}")
        return value

    def _synthesize_all_ws(
        self, jobs: List[tuple[str, str, str, Path]]
    ) -> List[Dict[str, Any]]:
        """实时设计音色：同一角色复用一条持久 WebSocket（见 dashscope_ws_tts）。

        这条路径不做逐句 prosody：设计音色只接受会话级语速/音调，
        会话参数在 provider 建连时一次性下发。
        """
        for character, _, _, _ in jobs:
            voice_id = (self.characters.get(character, {}) or {}).get("voice_id")
            if not voice_id or voice_id == "default":
                raise TTSSynthesisError(f"No voice_id configured for {character}")

        tts_jobs: List[TTSJob] = []
        for character, line, emotion, output_path in jobs:
            cfg = self.characters.get(character, {}) or {}
            tts_jobs.append(TTSJob(
                text=line, voice=str(cfg.get("voice_id")), out_path=output_path,
                audio_format=self._audio_format,
                speed=float(cfg.get("speed", self.tts_config.get("speed", 1.0))),
                pitch=float(cfg.get("pitch", self.tts_config.get("pitch", 1.0))),
                volume=self.tts_config.get("volume", 1.0),
                instruction=self._line_instruction(character, emotion),
                label=character,
            ))
        try:
            self._provider.synthesize_lines(tts_jobs)
        except TTSProviderError as exc:
            raise TTSSynthesisError(str(exc)) from exc

        segments: List[Dict[str, Any]] = []
        for character, line, _, output_path in jobs:
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
        # Keep a character's timbre stable across separately synthesized lines.
        # Emotion still changes cadence, but the caller can tune the very small
        # allowed movement instead of letting punctuation create a new voice.
        pitch_limit = max(0.0, float(self.tts_config.get("prosody_pitch_delta", 0.02)))
        speed_limit = max(0.0, float(self.tts_config.get("prosody_speed_delta", 0.03)))
        pitch_delta = rate_delta = 0.0
        if "？" in line or "?" in line:
            pitch_delta += pitch_limit          # questions lift
            rate_delta -= min(speed_limit, 0.02)
        if "！" in line or "!" in line:
            pitch_delta += pitch_limit          # emphasis push
            rate_delta += speed_limit
        if any(word in line for word in ("风险", "警惕", "回撤", "不确定", "小心", "危险", "亏损", "隐患")):
            pitch_delta -= min(pitch_limit, 0.01)  # warnings sink and slow
            rate_delta -= speed_limit
        # 不再把「说白了」列进来（2026-10-27）：它已经在 copy_guard 的套话清单里，
        # 台词里出现它是缺陷、不会再出现在成片里，也就不必为它单独放慢语速。
        if any(word in line for word in ("你看", "打个比方", "也就是说", "举个例子")):
            rate_delta -= min(speed_limit, 0.02)  # explanatory asides ease off
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

    def _synthesize_line_cli(self, line: str, character: str, output_path: Path,
                             emotion: str = "") -> Dict[str, Any]:
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
        job = TTSJob(
            text=synth_text, voice=str(voice_id), out_path=output_path,
            audio_format=self._audio_format,
            speed=speed, pitch=pitch, volume=self.tts_config.get("volume"),
            use_ssml=use_ssml,
            sample_rate=self.tts_config.get("sample_rate"),
            seed=self.tts_config.get("seed"),
            instruction=self._line_instruction(character, emotion),
            label=character,
        )
        try:
            self._provider.synthesize_lines([job])
        except TTSProviderError as exc:
            raise TTSSynthesisError(str(exc)) from exc

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
            # SRT 是显示层：口播念汉字完整读法，字幕出阿拉伯数字。
            blocks.append(
                f"{index}\n{self._format_srt_time(float(segment['start_time']))} --> "
                f"{self._format_srt_time(float(segment['end_time']))}\n{to_display(str(segment['line']))}"
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
