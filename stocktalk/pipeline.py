#!/usr/bin/env python3
"""StockTalk pipeline — orchestrates the full debate-video generation flow.

Usage:
    python -m stocktalk.pipeline 600519 --name 贵州茅台
    python -m stocktalk.pipeline 000001 --config config.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

from stocktalk.modules.stock_data import StockDataClient, StockDataConfig
from stocktalk.modules.dialogue_generator import DialogueGenerator
from stocktalk.modules.compliance import ComplianceAgent
from stocktalk.modules.tts_agent import TTSAgent
from stocktalk.modules.hyperframes_builder import HyperFramesBuilder


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config") / "default.yaml"


def _merge_config(defaults: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge a user config onto the bundled defaults."""
    merged = dict(defaults)
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load bundled defaults, optionally overlaid by a user config file."""
    with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as f:
        defaults = yaml.safe_load(f) or {}
    if path is None:
        return defaults
    p = Path(path)
    if not p.exists():
        return defaults
    with p.open(encoding="utf-8") as f:
        return _merge_config(defaults, yaml.safe_load(f) or {})


class Pipeline:
    """End-to-end StockTalk video generation pipeline."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.stock_client = StockDataClient(StockDataConfig(
            retries=self.config.get("stock_data", {}).get("retries", 2),
            timeout_seconds=self.config.get("stock_data", {}).get("timeout_seconds", 20.0),
        ))
        self.dialogue_gen = DialogueGenerator(self.config)
        self.compliance = ComplianceAgent(self.config)
        self.tts = TTSAgent(self.config)
        self.builder = HyperFramesBuilder(self.config)
        output_dir = self.config.get("output", {}).get("dir", "output")
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(self, stock_code: str, stock_name: str | None = None) -> dict[str, Any]:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag = f"{stock_code}_{ts}"
        print(f"[StockTalk] pipeline start  code={stock_code}  tag={tag}")

        t0 = time.monotonic()
        stock_data = self.stock_client.get_stock_data(stock_code)
        if stock_name:
            stock_data.setdefault("quote", {})["name"] = stock_name
        print(f"[StockTalk] stock data fetched  ({time.monotonic() - t0:.1f}s)")

        script = self.dialogue_gen.generate(stock_data)
        print(f"[StockTalk] dialogue generated  turns={len(script.get('turns', []))}")

        approved = self.compliance.review(script)
        violations = approved.get("violations", [])
        if violations:
            print(f"[StockTalk] compliance: {len(violations)} violations fixed")
        else:
            print("[StockTalk] compliance: clean")

        audio = self.tts.synthesize(approved)
        srt_path = self.tts.write_srt(audio, self.output_dir / f"{tag}.srt")
        print(f"[StockTalk] TTS complete  segments={len(audio.get('segments', []))}")

        project_dir = self.output_dir / tag
        project = self.builder.build(
            stock_data=stock_data, script=approved, audio=audio, output_dir=project_dir,
        )
        print(f"[StockTalk] HyperFrames project written to {project_dir}")

        result = {
            "stock_code": stock_code,
            "stock_name": stock_data.get("quote", {}).get("name", stock_name or ""),
            "tag": tag,
            "script": script,
            "approved_script": approved,
            "audio": audio,
            "srt": str(srt_path),
            "project_dir": str(project_dir),
            "elapsed_seconds": round(time.monotonic() - t0, 1),
        }
        result_path = self.output_dir / f"{tag}.json"
        with result_path.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[StockTalk] done  result={result_path}")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="StockTalk — AI debate video pipeline")
    parser.add_argument("stock_code", help="Six-digit A-share code, e.g. 600519")
    parser.add_argument("--name", default=None, help="Override stock name")
    parser.add_argument("--config", default=None, help="YAML config file")
    args = parser.parse_args()
    config = load_config(args.config)
    pipeline = Pipeline(config)
    pipeline.run(args.stock_code, args.name)


if __name__ == "__main__":
    main()
