"""用已发布的 300024_20260928_213016 期数据重建图板，验证论点→图表绑定。

不重跑 LLM/TTS：直接读该期 json 里的 approved_script 与 audio 时间轴。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocktalk.modules.hyperframes_builder import HyperFramesBuilder  # noqa: E402

TAG = "300024_20260928_213016"
OUT = Path(__file__).resolve().parents[1] / "output" / "_rebind_300024"


def main() -> None:
    meta = json.loads((Path("output") / f"{TAG}.json").read_text(encoding="utf-8"))
    script = meta["approved_script"]
    audio = meta["audio"]
    cfg = {
        "video": {
            "output_dir": str(OUT),
            "project_dir": str(OUT / "project"),
            "canvas": "horizontal",
        },
    }
    builder = HyperFramesBuilder(cfg)
    stock_data = {
        "code": meta["stock_code"],
        "quote": {"name": meta["stock_name"], "price": 12.91, "change_pct": -2.34},
        # 重建验证只关心「论点→图表」的绑定与标题，行情细节用当期成片同款最小集；
        # F10 主营构成取自该期画面上出现过的真实数据（与 2026-06-30 报告期一致）。
        "f10": {"profile": {"主营构成": {"报告期": "2026-06-30", "明细": [
            {"项目": "工业机器人(产品)", "收入": "6.05亿", "收入占比(%)": "41.67", "毛利率(%)": "14.08"},
            {"项目": "半导体装备(产品)", "收入": "2.55亿", "收入占比(%)": "18.58", "毛利率(%)": "27.86"},
            {"项目": "自动化装配与检测生产线(产品)", "收入": "3.57亿", "收入占比(%)": "24.44", "毛利率(%)": "10.54"},
            {"项目": "物流与仓储自动化成(产品)", "收入": "1.27亿", "收入占比(%)": "8.77", "毛利率(%)": "9.03"},
        ]},
            "行业地位": {"研究行业": "自动化设备", "同行家数": 82,
                        "市场表现": {"排名": 28}, "公司规模": {"排名": 14}, "估值水平": {"排名": 79}}},
        },
        "financials": {"报告期": "2026-08-24", "营业收入(亿元)": 14.52,
                       "经营现金流(亿元)": -4.33, "应收账款(亿元)": 15.93,
                       "存货(亿元)": 40.51, "净利润(亿元)": -1.89},
        "kline": [{"date": f"2026-08-{d:02d}", "close": 12.0 + (d % 7) * 0.2,
                   "open": 12.1 + (d % 7) * 0.2, "high": 12.4 + (d % 7) * 0.2,
                   "low": 11.8 + (d % 7) * 0.2, "volume": 1000 + d} for d in range(1, 29)],
    }
    project = builder.build(stock_data, script, audio)
    html = (Path(project.directory) / "index.html").read_text(encoding="utf-8")

    import re
    boards = re.findall(r'data-slot="visual"[^>]*data-topic="([^"]+)"[^>]*>(.*?)</div>', html, re.S)
    print(f"{'论点':<12}{'图板标题':<10}中央图")
    chart_markers = {
        "working_capital": "营运占用",
        "composition": "收入构成",
        "mindmap": "业务拆解",
        "ranking": "行业里站哪",
        "waterfall": "利润阶梯",
        "timeline": "最近发生的事",
        "kline": "真实日K",
    }
    ok = True
    for topic, svg in boards:
        marker = next((m for m in chart_markers.values() if m in svg), "?")
        title = re.search(r'letter-spacing:2px">([^<]*)<', svg)
        print(f"{topic:<12}{title.group(1) if title else '?':<10}{marker}")
    # 竖版也建一份供截图
    vcfg = dict(cfg)
    vcfg["video"] = dict(cfg["video"], canvas="vertical", project_dir=str(OUT / "project_vertical"))
    vb = HyperFramesBuilder(vcfg)
    vproject = vb.build(stock_data, script, audio)
    print("\nH:", project.directory)
    print("V:", vproject.directory)


if __name__ == "__main__":
    main()
