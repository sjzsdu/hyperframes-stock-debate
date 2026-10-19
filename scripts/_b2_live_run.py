"""B2 真机：真调 LLM 生成一期五章口播，并把实际出图序列打出来。

用法：uv run --extra dev python scripts/_b2_live_run.py 600519
（依赖 /tmp/b2_plan_<code>.json —— 由 scripts/_b2_real_plan.py 落盘）
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

from stocktalk.pipeline import load_config
from stocktalk.modules.dialogue_generator import DialogueGenerator
from stocktalk.modules.episode_planner import EpisodePlanner
from stocktalk.modules.hyperframes_builder import HyperFramesBuilder
from stocktalk.modules.providers.factory import create_llm_client


def main() -> int:
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    blob = json.load(open(f"/tmp/b2_plan_{code}.json", encoding="utf-8"))
    stock_data = blob["stock_data"]
    plan = EpisodePlanner().plan(stock_data)

    config = load_config()
    llm = create_llm_client(config)
    generator = DialogueGenerator(config, llm=llm)
    script = generator.generate(stock_data, plan)
    script["episode_plan"] = plan

    out = Path(tempfile.mkdtemp())
    builder = HyperFramesBuilder({"video": {"canvas": "horizontal", "output_dir": str(out)}})
    project = builder.build_project(stock_data, script, {})
    html = project.composition_path.read_text(encoding="utf-8")

    print(f"=== 真机 {code} {stock_data.get('quote', {}).get('name', '')} ===")
    print(f"轮数 {len(script['turns'])}，时长目标 {config['dialogue']['min_duration_seconds']}-"
          f"{config['dialogue']['max_duration_seconds']}s")
    chapters: dict[str, list[str]] = {}
    order: list[str] = []
    for turn in script["turns"]:
        rid = str(turn.get("round_id") or "")
        if rid not in chapters:
            chapters[rid] = []
            order.append(rid)
        chapters[rid].append(f"{turn['speaker']}：{turn['line']}")
    for rid in order:
        print(f"\n--- {rid} ---")
        for line in chapters[rid]:
            print("  " + line)

    print("\n=== 实际出图序列（按成片 HTML 里出现的顺序）===")
    titles = re.findall(r'font-size:24">([^<]{4,40})</text>', html)
    seen: list[str] = []
    for title in titles:
        if title not in seen:
            seen.append(title)
    for title in seen:
        print("  " + title)
    print("\nproject:", project.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
