#!/usr/bin/env python3
"""Orchestrate StockTalk's stock-data-to-video generation pipeline."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, TypeVar

import yaml
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from stocktalk.modules.compliance import ComplianceAgent
from stocktalk.modules.dialogue_generator import DialogueGenerator
from stocktalk.modules.hyperframes_builder import HyperFramesBuilder
from stocktalk.modules.stock_data import StockDataClient, StockDataConfig
from stocktalk.modules.tts_agent import TTSAgent


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config") / "default.yaml"
T = TypeVar("T")


class PipelineError(RuntimeError):
    """A concise, user-facing failure from a named pipeline stage."""


def _merge_config(defaults: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge a user config onto the bundled defaults."""
    merged = dict(defaults)
    for key, value in overrides.items():
        merged[key] = _merge_config(merged[key], value) if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping) else value
    return merged


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load bundled defaults, optionally overlaid by a user config file."""
    with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as f:
        defaults = yaml.safe_load(f) or {}
    if path is None or not Path(path).exists():
        return defaults
    with Path(path).open(encoding="utf-8") as f:
        return _merge_config(defaults, yaml.safe_load(f) or {})


class Pipeline:
    """End-to-end StockTalk video generation pipeline."""

    def __init__(self, config: dict[str, Any] | None = None, *, console: Console | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config or {}
        self.console = console or Console()
        self._sleep = sleep
        self.stock_client = StockDataClient(StockDataConfig(
            retries=self.config.get("stock_data", {}).get("retries", 2),
            timeout_seconds=self.config.get("stock_data", {}).get("timeout_seconds", 20.0),
        ))
        self.dialogue_gen = DialogueGenerator(self.config)
        self.compliance = ComplianceAgent(self.config)
        self.tts = TTSAgent(self.config)
        self.builder = HyperFramesBuilder(self.config)
        self.output_dir = Path(self.config.get("output", {}).get("dir", "output"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _retry(self, stage: str, operation: Callable[[], T]) -> T:
        """Retry a remote LLM/TTS operation with capped exponential backoff."""
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                return operation()
            except Exception as exc:  # Concrete remote-client errors vary by provider.
                last_error = exc
                if attempt == 3:
                    break
                delay = 2 ** (attempt - 1)
                self.console.print(f"[yellow]{stage} failed; retrying in {delay}s ({attempt}/3)…[/yellow]")
                self._sleep(delay)
        raise PipelineError(f"{stage} failed after 3 attempts: {last_error}") from last_error

    @staticmethod
    def _silent_timeline(script: Mapping[str, Any], srt_path: Path) -> dict[str, Any]:
        """Create subtitle timing for a silent render when TTS is unavailable."""
        segments: list[dict[str, Any]] = []
        cursor = 0.0
        for round_data in script.get("rounds", []):
            if not isinstance(round_data, Mapping):
                continue
            for character in ("bull", "bear"):
                entry = round_data.get(character)
                line = str(entry.get("line", "") if isinstance(entry, Mapping) else entry or "").strip()
                if not line:
                    continue
                duration = max(2.5, min(9.0, len(line) * 0.23))
                segments.append({"character": character, "line": line, "start_time": cursor,
                                 "end_time": cursor + duration, "duration": duration, "audio_path": None})
                cursor += duration + 0.25
        lines = []
        for index, segment in enumerate(segments, 1):
            lines.append(f"{index}\n{TTSAgent._format_srt_time(segment['start_time'])} --> "
                         f"{TTSAgent._format_srt_time(segment['end_time'])}\n{segment['line']}")
        srt_path.write_text("\n\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return {"segments": segments, "srt_path": str(srt_path), "total_duration": segments[-1]["end_time"] if segments else 0.0, "fallback": "silent"}

    def run(self, stock_code: str, stock_name: str | None = None, *, render: bool = True,
            preview: bool = False) -> dict[str, Any]:
        """Generate the project and, unless disabled, render its MP4 output."""
        if preview:
            render = False
        tag = f"{stock_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        project_dir = self.output_dir / tag
        t0 = time.monotonic()
        columns = (SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), TimeElapsedColumn(), TimeRemainingColumn())

        with Progress(*columns, console=self.console, transient=False) as progress:
            task = progress.add_task("Fetching stock data", total=6)
            try:
                stock_data = self.stock_client.get_stock_data(stock_code)
                if stock_name:
                    stock_data.setdefault("quote", {})["name"] = stock_name
            except Exception as exc:
                raise PipelineError(f"Stock data retrieval failed: {exc}") from exc
            progress.advance(task)

            progress.update(task, description="Generating debate script (may take ~1 min)")
            script = self._retry("Dialogue generation", lambda: self.dialogue_gen.generate(stock_data))
            progress.advance(task)

            progress.update(task, description="Reviewing compliance")
            try:
                approved = self.compliance.review(script)
            except Exception as exc:
                raise PipelineError(f"Compliance review failed: {exc}") from exc
            progress.advance(task)

            progress.update(task, description="Synthesizing voice (may take ~1 min)")
            try:
                audio = self._retry("TTS synthesis", lambda: self.tts.synthesize(approved))
            except PipelineError as exc:
                srt_path = self.output_dir / f"{tag}.srt"
                self.console.print(f"[yellow]{exc}; continuing with silent video and subtitles.[/yellow]")
                audio = self._silent_timeline(approved, srt_path)
            progress.advance(task)

            progress.update(task, description="Building HyperFrames project")
            try:
                self.builder.video_config["project_dir"] = str(project_dir)
                project = self.builder.build(stock_data, approved, audio)
            except Exception as exc:
                raise PipelineError(f"HyperFrames build failed: {exc}") from exc
            progress.advance(task)

            video_path: Path | None = None
            if render:
                progress.update(task, description="Rendering MP4 (may take several minutes)")
                try:
                    video_path = self.builder.render_mp4(project, self.output_dir / f"{tag}.mp4")
                except Exception as exc:
                    raise PipelineError(f"MP4 render failed: {exc}") from exc
            else:
                progress.update(task, description="HTML project ready (render skipped)")
            progress.advance(task)

        result = {"stock_code": stock_code, "stock_name": stock_data.get("quote", {}).get("name", stock_name or ""),
                  "tag": tag, "script": script, "approved_script": approved, "audio": audio,
                  "srt": audio.get("srt_path"), "project_dir": str(project_dir),
                  "video_path": str(video_path) if video_path else None, "rendered": bool(video_path),
                  "preview": preview, "elapsed_seconds": round(time.monotonic() - t0, 1)}
        result_path = self.output_dir / f"{tag}.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self.console.print(f"[green]Done:[/green] {result_path}")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="StockTalk — AI debate video pipeline")
    parser.add_argument("stock_code", help="Six-digit A-share code, e.g. 600519")
    parser.add_argument("--name", default=None, help="Override stock name")
    parser.add_argument("--config", default=None, help="YAML config file")
    parser.add_argument("--no-render", action="store_true", help="Generate the HTML project but skip MP4 rendering")
    parser.add_argument("--preview", action="store_true", help="Generate only the HTML preview project (implies --no-render)")
    args = parser.parse_args()
    try:
        Pipeline(load_config(args.config)).run(args.stock_code, args.name, render=not args.no_render, preview=args.preview)
    except PipelineError as exc:
        print(f"StockTalk failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
