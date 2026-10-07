"""百炼 CLI（``bl speech synthesize``）的语音实现：逐行子进程调用。"""
from __future__ import annotations

import shutil
import subprocess
from typing import Any, Mapping, Sequence

from stocktalk.modules.providers.base import TTSJob, TTSProviderError, register_tts


class BailianTTSProvider:
    """CosyVoice 系统音色走 CLI；支持逐句 prosody（语速/音调/SSML break）。"""

    supports_line_prosody = True

    def __init__(self, model: str = "cosyvoice-v3-flash") -> None:
        self.model = model

    def synthesize_lines(self, jobs: Sequence[TTSJob]) -> None:
        for job in jobs:
            self._synthesize(job)

    def check_ready(self) -> None:
        """编排层入口调用一次；逐句路径不重复检查（便于测试注入）。"""
        if not shutil.which("bl"):
            raise TTSProviderError("Bailian CLI 'bl' is not installed or not on PATH")

    def _synthesize(self, job: TTSJob) -> None:
        who = f" for {job.label}" if job.label else ""
        command = [
            "bl", "speech", "synthesize",
            "--text", job.text,
            "--model", self.model,
            "--format", job.audio_format,
            "--out", str(job.out_path),
            "--voice", job.voice,
        ]
        if job.use_ssml:
            command.append("--enable-ssml")
        if job.sample_rate:
            command.extend(["--sample-rate", str(job.sample_rate)])
        command.extend(["--rate", str(job.speed)])
        if job.volume is not None:
            volume_f = float(job.volume)
            command.extend(["--volume", str(round(volume_f * 100) if volume_f <= 1 else round(volume_f))])
        if job.pitch is not None and 0.5 <= float(job.pitch) <= 2.0:
            command.extend(["--pitch", str(job.pitch)])
        if job.instruction:
            command.extend(["--instruction", str(job.instruction)])
        if job.seed is not None:
            command.extend(["--seed", str(job.seed)])
        command.extend(["--language", job.language])

        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TTSProviderError(f"Failed to run Bailian TTS{who}: {exc}") from exc
        if completed.returncode != 0 or not job.out_path.is_file() or job.out_path.stat().st_size == 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no audio file was created"
            raise TTSProviderError(f"Bailian TTS failed{who}: {detail}")


@register_tts("bailian-cli")
def bailian_tts(source: Mapping[str, Any]) -> BailianTTSProvider:
    return BailianTTSProvider(model=str(source.get("model", "cosyvoice-v3-flash")))
