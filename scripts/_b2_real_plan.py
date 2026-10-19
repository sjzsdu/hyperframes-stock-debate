"""B2 真机素材：拉一只票的真实数据，打一份 episode_plan，把五章图板贴出来。

用法：uv run --extra dev python scripts/_b2_real_plan.py 600519
"""
from __future__ import annotations

import json
import sys

from stocktalk.modules.episode_planner import EpisodePlanner
from stocktalk.modules.stock_data import StockDataClient, StockDataConfig


def main() -> int:
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    client = StockDataClient(StockDataConfig())
    data = client.get_stock_data(code)
    plan = EpisodePlanner().plan(data)
    with open(f"/tmp/b2_plan_{code}.json", "w", encoding="utf-8") as handle:
        json.dump({"stock_data": data, "plan": plan}, handle, ensure_ascii=False, indent=1)
    print(f"=== {code} {data.get('quote', {}).get('name', '')} ===")
    print("selected:", plan["selected_material_ids"])
    print("warnings:", plan["warnings"])
    print("number_budget:", plan["number_budget"])
    for chapter in plan["rounds"]:
        visual = chapter["visual"]
        print(f"  {chapter['id']:<18} template={visual['template']:<18}"
              f" fallback={' → '.join(visual['fallback'])}"
              f" | 素材={visual['data_binding']['material_ids']}"
              f" | 图={_shown(data, chapter)}"
              f" | verify={visual.get('verify') or '-'}")
    return 0


def _shown(data: dict, chapter: dict) -> str:
    """真按渲染器跑一遍，看这一章最后落到哪张图（不是念计划里的名字）。"""
    import tempfile
    from stocktalk.modules.hyperframes_builder import HyperFramesBuilder
    builder = HyperFramesBuilder({"video": {"canvas": "horizontal",
                                            "output_dir": tempfile.mkdtemp()}})
    ctx = {"bars": data.get("kline") or data.get("price_history") or [],
           "quote": data.get("quote") or {}, "keywords": [], "argument": "business",
           "f10": data.get("f10") or {}, "financials": data.get("financials") or {},
           "news": data.get("news") or {}, "episode_plan": {"rounds": [chapter],
                                                            "materials": _materials(data)}}
    _, name = builder._turn_chart("business", ctx, "", chapter["visual"]["id"])
    return name


def _materials(data: dict) -> list:
    from stocktalk.modules.episode_planner import MaterialExtractor
    return MaterialExtractor().extract(data)


if __name__ == "__main__":
    raise SystemExit(main())
