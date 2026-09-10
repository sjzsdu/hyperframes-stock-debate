"""Bailian TTS orchestration for StockTalk dialogue scripts.

Each dialogue line is rendered separately so the video timeline can switch the
active speaker accurately.  Subtitle timings are derived from the generated
audio, rather than estimated from the number of characters in a line.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping


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
        self._require_bailian_cli()
        audio_dir = self.output_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        segments: List[Dict[str, Any]] = []
        cursor = 0.0
        pause = max(0.0, float(self.tts_config.get("pause_between_segments", 0)))
        for turn_index, turn in enumerate(self._turns(script), start=1):
            character, line = str(turn["speaker"]), str(turn["line"])
            segment = self._synthesize_line(line=line, character=character,
                output_path=audio_dir / f"{turn_index:02d}_{character}.{self._audio_format}")
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

    def _synthesize_line(self, line: str, character: str, output_path: Path) -> Dict[str, Any]:
        character_config = self.characters.get(character, {})
        voice_id = character_config.get("voice_id")
        if not voice_id or voice_id == "default":
            raise TTSSynthesisError(
                f"No Bailian TTS voice configured for {character}. "
                "Set characters.<name>.voice_id to a voice returned by "
                "`bl speech synthesize --list-voices --model cosyvoice-v3-flash`."
            )
        command = [
            "bl", "speech", "synthesize",
            "--text", line,
            "--model", str(self.tts_config.get("model", "cosyvoice-v3-flash")),
            "--format", self._audio_format,
            "--out", str(output_path),
        ]
        command.extend(["--voice", str(voice_id)])

        sample_rate = self.tts_config.get("sample_rate")
        if sample_rate:
            command.extend(["--sample-rate", str(sample_rate)])
        # Voice normalization: always pass speed/pitch/volume to ensure
        # consistent prosody across all segments and both speakers.
        speed = self.tts_config.get("speed")
        if speed is not None:
            command.extend(["--rate", str(speed)])
        volume = self.tts_config.get("volume")
        if volume is not None:
            volume_f = float(volume)
            command.extend(["--volume", str(round(volume_f * 100) if volume_f <= 1 else round(volume_f))])
        pitch = self.tts_config.get("pitch")
        if pitch is not None and 0.5 <= float(pitch) <= 2.0:
            command.extend(["--pitch", str(pitch)])

        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TTSSynthesisError(f"Failed to run Bailian TTS for {character}: {exc}") from exc
        if completed.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no audio file was created"
            raise TTSSynthesisError(f"Bailian TTS failed for {character}: {detail}")

        return {
            "character": character,
            "character_name": character_config.get("name", character),
            "line": line,
            "audio_path": str(output_path),
            "duration": self._audio_duration(output_path),
        }

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
