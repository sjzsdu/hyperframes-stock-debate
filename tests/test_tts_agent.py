from __future__ import annotations

import struct
import subprocess
from pathlib import Path

from stocktalk.modules.tts_agent import TTSAgent


def _write_wav(path: Path, sample_rate: int = 24000, channels: int = 1, bits: int = 16, frames: int = 48000) -> None:
    """Write a minimal PCM WAV, optionally with a corrupt chunk-size header."""
    data = bytes([0] * (channels * (bits // 8) * frames))
    with path.open("wb") as handle:
        handle.write(b"RIFF")
        handle.write(struct.pack("<I", 0x7FFFFFFF))  # corrupt RIFF size
        handle.write(b"WAVE")
        handle.write(b"fmt ")
        handle.write(struct.pack("<I", 16))
        handle.write(struct.pack("<HH", 1, channels))
        handle.write(struct.pack("<I", sample_rate))
        handle.write(struct.pack("<I", sample_rate * channels * (bits // 8)))
        handle.write(struct.pack("<HH", channels * (bits // 8), bits))
        handle.write(b"data")
        handle.write(struct.pack("<I", 0x7FFFFFFF))  # corrupt data size
        handle.write(data)


def test_synthesis_passes_configured_voice_to_bailian(tmp_path: Path, monkeypatch) -> None:
    config = {
        "output": {"dir": str(tmp_path)},
        "characters": {"bull": {"name": "多头", "voice_id": "longfeifei_v3"}},
    }
    agent = TTSAgent(config)
    output = tmp_path / "line.wav"
    command: list[str] = []

    def fake_run(args, **_kwargs):
        command.extend(args)
        output.write_bytes(b"audio")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(agent, "_audio_duration", lambda _path: 1.25)

    result = agent._synthesize_line("测试语音", "bull", output)

    assert command[command.index("--voice") + 1] == "longfeifei_v3"
    assert result["duration"] == 1.25


def test_wav_duration_ignores_corrupt_chunk_sizes(tmp_path: Path) -> None:
    """Bailian writes 0x7FFFFFFF chunk sizes; duration must come from real bytes."""
    audio = tmp_path / "corrupt.wav"
    _write_wav(audio, sample_rate=24000, frames=48000)  # 2.0s of actual audio
    agent = TTSAgent({"output": {"dir": str(tmp_path)}})
    assert agent._wav_duration(audio) == 2.0
