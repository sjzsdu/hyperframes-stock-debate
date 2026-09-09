"""Bailian TTS orchestration for StockTalk dialogue scripts.

Each dialogue line is rendered separately so the video timeline can switch the
active speaker accurately.  Subtitle timings are derived from the generated
audio, rather than estimated from the number of characters in a line.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import wave
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
        self.output_dir = Path(config.get("video", {}).get("output_dir", "./output"))

    def synthesize(self, script: Mapping[str, Any]) -> Dict[str, Any]:
        """Synthesize the script in display order and return its media timeline.

        ``script['rounds']`` is expected to contain ``bull`` and/or ``bear``
        mappings with a ``line`` field.  A synthesis failure is not hidden: an
        estimated timestamp would put later subtitles out of sync.
        """
        self._require_bailian_cli()
        audio_dir = self.output_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        segments: List[Dict[str, Any]] = []
        cursor = 0.0
        pause = max(0.0, float(self.tts_config.get("pause_between_segments", 0)))
        for round_index, round_data in enumerate(script.get("rounds", []), start=1):
            if not isinstance(round_data, Mapping):
                continue
            for character in ("bull", "bear"):
                entry = round_data.get(character)
                line = self._line_from(entry)
                if not line:
                    continue
                segment = self._synthesize_line(
                    line=line,
                    character=character,
                    output_path=audio_dir / f"{round_index:02d}_{character}.{self._audio_format}",
                )
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

    @property
    def _audio_format(self) -> str:
        value = str(self.tts_config.get("output_format", "wav")).lower()
        if value not in {"mp3", "pcm", "wav", "opus"}:
            raise ValueError(f"Unsupported Bailian audio format: {value}")
        return value

    def _synthesize_line(self, line: str, character: str, output_path: Path) -> Dict[str, Any]:
        character_config = self.characters.get(character, {})
        voice_id = character_config.get("voice_id")
        command = [
            "bl", "speech", "synthesize",
            "--text", line,
            "--model", str(self.tts_config.get("model", "cosyvoice-v3-flash")),
            "--format", self._audio_format,
            "--out", str(output_path),
        ]
        if voice_id and voice_id != "default":
            command.extend(["--voice", str(voice_id)])

        sample_rate = self.tts_config.get("sample_rate")
        if sample_rate:
            command.extend(["--sample-rate", str(sample_rate)])
        if self.tts_config.get("speed") is not None:
            command.extend(["--rate", str(self.tts_config["speed"])])
        if self.tts_config.get("volume") is not None:
            volume = float(self.tts_config["volume"])
            command.extend(["--volume", str(round(volume * 100) if volume <= 1 else round(volume))])
        # Bailian accepts a pitch multiplier (0.5--2.0); the stock config's
        # zero means neutral and must therefore be omitted.
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
            try:
                with wave.open(str(path), "rb") as audio:
                    duration = audio.getnframes() / audio.getframerate()
                if duration > 0:
                    return duration
            except (wave.Error, EOFError, ZeroDivisionError):
                pass

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
