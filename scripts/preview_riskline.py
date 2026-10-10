"""片尾风险提示的截图验证（2026-10-08）。

构建一个带互动文案的项目，用 Playwright 把时间轴 seek 到片尾，截
outro 卡（收尾卡）与 CTA 图板两帧，核对「股市有风险，投资需谨慎」的上屏效果。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocktalk.modules.hyperframes_builder import HyperFramesBuilder

script = {
    "arc": {"id": "annual", "name": "年报逐条"},
    "title": "华瓷股份（001216）：九成收入靠色釉陶瓷",
    "stock_code": "001216", "stock_name": "华瓷股份",
    "turns": [
        {"speaker": "bull", "line": "主营是色釉陶瓷，上半年5.76亿营收里占了九成，毛利率33.52%。"},
        {"speaker": "bear", "line": "九成收入压在一类产品上，存货和应收都压在账上，现金能不能回来要盯。"},
        {"speaker": "bull", "line": "所以真正要盯的是下一份财报的外销收入占比。"},
        {"speaker": "bear", "line": "股市有风险，投资需谨慎。", "short": True},
    ],
    "sides": {"bull": "单一品类做到九成，说明产品力扎实", "bear": "客户集中就是命门"},
    "hook": "下一份财报的外销收入占比",
    "question": "你更信渠道还是产能？",
    "spotlight": {"label": "经营现金流", "value": "-2.4亿→+8000万"},
    "disclaimers": [],
}

target = Path(__file__).resolve().parents[1] / "output" / "_preview_riskline"
if target.exists():
    shutil.rmtree(target)
target.mkdir(parents=True)

builder = HyperFramesBuilder({"video": {"output_dir": str(target), "canvas": "horizontal"}})
project = builder.build_project({"code": "001216", "quote": {"name": "华瓷股份"}}, script, {})
print(project.composition_path)
