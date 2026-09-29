"""验证「多年财务图表池」：真实 F10 下 5 张新图的渲染与论点绑定。

数据源是 300024（机器人）的**真实** F10「财务分析」块（已由
StockDataClient 解析成 年度/季度/科目变动），台词是照着这批数据写的，
用来检查「讲到哪年、哪张表，画面就出哪张图」。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocktalk.modules.hyperframes_builder import HyperFramesBuilder  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "_finance_pool_300024"
F10 = json.loads(Path("/tmp/f10_300024.json").read_text(encoding="utf-8"))
FINANCIALS = json.loads((ROOT / "output" / "300024_20260928_213016.json").read_text(encoding="utf-8"))[
    "approved_script"].get("stock_data", {}) or {}

LINES = [
    # 首段是开场（画面固定给 K 线主图），所以新图从第二段开始各占一块图板。
    ("bull", "今天聊的这家公司，五年报表摊开来看，有几个地方挺值得琢磨。"),
    ("bull", "这几年归母净利润从 2021 年亏五亿六千二百万，到 2022、2023 年勉强转正，"
             "2024、2025 又连亏两年，一年比一年深。"),
    ("bear", "销售毛利率从 2021 年的百分之七点五七，爬到 2024 年的百分之十四点六二，"
             "去年又掉回百分之十二点四八，中间那两年的改善没守住。"),
    ("bull", "净资产收益率连续三年是负的，股东回报这条路这几年没走通。"),
    ("bear", "经营活动现金流去年才转正一点六一亿，投资活动净额又翻成负的，"
             "筹资活动还要往里补三个亿。"),
    ("bull", "存货四十点五一亿，比期初多三成，短期借款半年多了五成，资产和负债两头都在变。"),
]

MARKERS = {
    "profit_trend": "业绩走势",
    "margin_trend": "毛利率走势",
    "roe_trend": "股东回报",
    "cashflow_statement": "现金流三张账",
    "balance_sheet": "资产与负债",
    "working_capital": "营运占用",
    "composition": "收入构成",
    "mindmap": "业务拆解",
    "ranking": "行业里站哪",
    "waterfall": "利润阶梯",
    "timeline": "最近发生的事",
    "kline": "真实日K",
}


def main() -> None:
    config = {"video": {"output_dir": str(OUT), "project_dir": str(OUT / "project"),
                        "canvas": "horizontal"}}
    builder = HyperFramesBuilder(config)
    stock_data = {
        "code": "300024",
        "quote": {"name": "机器人", "price": 12.91, "change_pct": -2.34},
        "f10": F10,
        "financials": FINANCIALS or {"报告期": "2026-06-30", "营业收入(亿元)": 14.51,
                                     "经营现金流(亿元)": -4.33, "应收账款(亿元)": 15.93,
                                     "存货(亿元)": 40.51, "净利润(亿元)": -1.89},
        "kline": [{"date": f"2026-08-{day:02d}", "close": 12.0 + (day % 7) * 0.2,
                   "open": 12.1 + (day % 7) * 0.2, "high": 12.4 + (day % 7) * 0.2,
                   "low": 11.8 + (day % 7) * 0.2, "volume": 1000 + day} for day in range(1, 29)],
    }
    script = {"turns": [{"speaker": who, "line": line} for who, line in LINES]}
    audio = {"segments": [], "total_duration": 72.0}

    print("=== 论点绑定 ===")
    for who, line in LINES:
        topic, title = builder._turn_argument(line)
        print(f"  {topic:<18}{title:<10}{line[:34]}")

    print("=== 单图渲染（真实数据）===")
    ctx = {"bars": stock_data["kline"], "quote": stock_data["quote"], "keywords": [], "argument": "",
           "f10": F10, "financials": stock_data["financials"], "news": {}}
    for name in MARKERS:
        body = builder._render_chart(name, ctx)
        head = re.search(r"font-size:24[^>]*>([^<]*)<", body or "") or re.search(r">([^<]{2,20})<", body or "")
        print(f"  {name:<20}{'—' if not body else f'{len(body):>6} chars':<12}"
              f"{head.group(1) if head else ''}")

    project = builder.build(stock_data, script, audio)
    html = (project.directory / "index.html").read_text(encoding="utf-8")
    boards = re.findall(r'data-slot="visual"[^>]*data-topic="([^"]+)"[^>]*>(.*?)</div>', html, re.S)
    print("=== 成片图板 ===")
    for topic, svg in boards:
        marker = next((label for key, label in MARKERS.items() if label in svg), "?")
        title = re.search(r'letter-spacing:2px">([^<]*)<', svg)
        print(f"  {topic:<18}{title.group(1) if title else '?':<10}{marker}")

    vertical = dict(config)
    vertical["video"] = dict(config["video"], canvas="vertical",
                             project_dir=str(OUT / "project_vertical"))
    vproject = HyperFramesBuilder(vertical).build(stock_data, script, audio)
    print("\nH:", project.directory)
    print("V:", vproject.directory)


if __name__ == "__main__":
    main()
