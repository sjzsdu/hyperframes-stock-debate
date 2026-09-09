from __future__ import annotations

import subprocess
from pathlib import Path

from stocktalk.modules.tts_agent import TTSAgent


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
