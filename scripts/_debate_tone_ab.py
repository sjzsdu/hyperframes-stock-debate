#!/usr/bin/env python3
"""对辩「态度与碰撞」改前 / 改后对照（2026-10-27 WAN-29 第二轮）。

用户要的是**感觉**，prompt diff 他判断不了，所以这个脚本只做一件事：
同一份数据、同一个风险点，在指定的一份代码上真跑一条生成，把前几轮打印出来。
改前/改后各跑一次，两份输出并排看就能判断"是不是一个人在急着上车、另一个人在
一条条拽住他"。

用法（改前用旧 commit 的树，改后用工作树）：

    PYTHONPATH=/path/to/old-tree python3 scripts/_debate_tone_ab.py --label before
    PYTHONPATH=/path/to/new-tree python3 scripts/_debate_tone_ab.py --label after

数据刻意做成"一面好看、另一面难看"：净利润同比 +41%，但经营现金流为负、
毛利率掉了 6.5 个点、应收账款同比 +62%、非经常性损益占净利两成多。
这样"同一份数据里的另一面"天然存在——如果两边只是各念一段账，一读就露。
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from stocktalk.modules.dialogue_generator import DialogueGenerator

# 固定成一条骨架，避免两次生成因为"自动挑骨架"走了不同的节拍而无法对照。
CONFIG: dict[str, Any] = {
    "dialogue": {
        "provider": "bailian-cli",
        "model": "qwen3.6-plus",
        "arc": "mainline",
        "min_duration_seconds": 120,
        "max_duration_seconds": 150,
        "max_line_chars": 80,
        "max_tokens": 4000,
        "temperature": 0.8,
        "timeout_seconds": 300,
    },
    "characters": {},
}

# 同一个风险点：**赚到的钱没有收回来**（利润表好看、现金流是负的、应收在堆）。
PAYLOAD: dict[str, Any] = {
    "code": "300433",
    "quote": {"name": "蓝思科技", "price": 24.36, "change_pct": 3.41},
    "stockinfo": {"总市值(亿元)": 1180},
    "financials": {
        "营业收入(亿元)": {"2025Q3": 462.3, "2024Q3": 391.8, "同比": "+18.0%"},
        "净利润(亿元)": {"2025Q3": 38.6, "2024Q3": 27.4, "同比": "+40.9%"},
        "毛利率(%)": {"2025Q3": 21.4, "2024Q3": 27.9},
        "经营现金流(亿元)": {"2025Q3": -12.4, "2024Q3": 21.8},
        "应收账款(亿元)": {"2025Q3": 96.2, "2024Q3": 59.4, "同比": "+62.0%"},
        "存货(亿元)": {"2025Q3": 68.5, "2024Q3": 58.1, "同比": "+17.9%"},
        "非经常性损益(亿元)": {"2025Q3": 9.1},
        "ROE(%)": {"2025Q3": 8.6, "2024Q3": 7.1},
    },
    "f10": {
        "所属行业": "消费电子零部件",
        "主营构成": {"明细": [
            {"名称": "玻璃防护屏", "收入占比": "48%", "毛利率": "24.1%"},
            {"名称": "金属结构件", "收入占比": "27%", "毛利率": "17.3%"},
            {"名称": "组装业务", "收入占比": "25%", "毛利率": "9.8%"},
        ]},
        "行业地位": {"同行家数": 42, "行业排名": "第3"},
        "经营评述": "公司称报告期内主要客户订单增加、产品结构改善，组装业务放量。",
    },
    "board": {"industry": "消费电子", "concepts": ["苹果概念", "折叠屏"]},
    "news": {"items": [
        {"date": "2025-10-20", "type": "研报",
         "summary": "某券商上调公司盈利预测，理由是新机备货量大增。"},
    ]},
}


def main() -> int:
    parser = argparse.ArgumentParser(description="对辩态度改前/改后对照")
    parser.add_argument("--label", default="sample", help="输出标签，如 before / after")
    parser.add_argument("--turns", type=int, default=6, help="打印前几轮（默认 6）")
    args = parser.parse_args()

    generator = DialogueGenerator(CONFIG)
    script = generator.generate(PAYLOAD)
    turns = list(script.get("turns") or [])

    print(f"[{args.label}] {script.get('title') or ''}")
    print(f"[{args.label}] 骨架={script.get('arc') or '-'} 轮数={len(turns)} "
          f"锐度={json.dumps(script.get('sharpness', {}).get('passed'), ensure_ascii=False)}"
          f"/{json.dumps(script.get('sharpness', {}).get('total'), ensure_ascii=False)}")
    for index, turn in enumerate(turns[: args.turns], 1):
        who = turn.get("character_name") or turn.get("speaker")
        emotion = turn.get("emotion") or "-"
        print(f"{index}. {who}［{emotion}］：{turn.get('line')}")
    sides = script.get("sides") or {}
    if sides:
        print(f"[{args.label}] 立场：多方={sides.get('bull')} / 空方={sides.get('bear')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
