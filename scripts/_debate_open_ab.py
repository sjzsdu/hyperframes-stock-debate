#!/usr/bin/env python3
"""对辩开场/态度 A/B 对照工具（WAN-29，长期工具，不进 CI）。

用途：在**两个代码版本**上跑同一批股票、同样的采样参数，比较三项可复核的指标：

1. 多头首轮 = 反应/动作，还是生意陈述（用户要的"看到好消息就想上车"）；
2. 空头首轮 = 先问一个具体问题再拆，还是直接开讲；
3. 裸报数次数（一句里报了数字、却没有任何判断/动作/质疑的句子）。

为什么留在仓库里：这不是一次性脚手架——三项指标的口径和 payload 都写在这里，
换 prompt、换骨架、换模型之后用同一条命令就能复跑，比"再看一遍成片感觉如何"可
比。它**依赖外部 LLM 凭据**（`bl`），所以不接 CI；`scripts/_*.py` 这个前缀沿用
本仓既有的一次性脚本习惯，两者的区别写在这里。

用法：

    PYTHONPATH=<旧树> python3 scripts/_debate_open_ab.py --label before --json
    PYTHONPATH=<工作树> python3 scripts/_debate_open_ab.py --label after --json

    # 单只股票 / 单个采样（便于并行）
    PYTHONPATH=<树> python3 scripts/_debate_open_ab.py --stock maotai --sample 1 --json

判定的口径（写死在下面的词表里，改口径就改这里，不要在报告里另立标准）：

* 多头首轮 hit = 出现动作词（上车/坐不住/忍不住/不等了…）或反应词（愣/没想到/
  这数一出来…）；miss = 只做了生意陈述（出现占比、主营构成、业务结构这类词，
  且没有动作/反应词）。
* 空头首轮 hit = 带问号，或出现「你先说/先说清/我问一句」这类先问后拆的开法。
* 裸报数 = 按句切分后，一句里有数字、且不含判断词（意味着/说明/因为/所以/可是/
  但/凭什么/站不住/我认/反而/这就是/等于），也不是提问句。

payload 是**手写的样例数据**（不是实时行情），三只票分别代表：数据好看、数据难看、
业绩好但现金流差。这样"同一份数据的另一面"天然存在，才测得出碰撞。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from stocktalk.modules.dialogue_generator import DialogueGenerator

CONFIG: dict[str, Any] = {
    "dialogue": {
        "provider": "bailian-cli",
        "model": "qwen3.6-plus",
        # 固定骨架：对照的是"首拍怎么开场"，不能让骨架随机换。
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

# 三只票的样例数据：好看 / 难看 / 好利润坏现金流。
STOCKS: dict[str, dict[str, Any]] = {
    "lens": {
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
        "news": {"items": [{"date": "2025-10-20", "type": "研报",
                            "summary": "某券商上调公司盈利预测，理由是新机备货量大增。"}]},
    },
    "maotai": {
        "code": "600519",
        "quote": {"name": "贵州茅台", "price": 1486.0, "change_pct": 1.12},
        "stockinfo": {"总市值(亿元)": 18670},
        "financials": {
            "营业收入(亿元)": {"2025Q3": 1231.0, "2024Q3": 1083.1, "同比": "+13.7%"},
            "净利润(亿元)": {"2025Q3": 632.4, "2024Q3": 553.9, "同比": "+14.2%"},
            "毛利率(%)": {"2025Q3": 91.6, "2024Q3": 91.1},
            "经营现金流(亿元)": {"2025Q3": 648.2, "2024Q3": 571.0},
            "应收账款(亿元)": {"2025Q3": 0.9, "2024Q3": 0.7},
            "存货(亿元)": {"2025Q3": 486.0, "2024Q3": 431.5},
            "非经常性损益(亿元)": {"2025Q3": 1.2},
            "ROE(%)": {"2025Q3": 31.4, "2024Q3": 33.2},
        },
        "f10": {
            "所属行业": "白酒",
            "主营构成": {"明细": [
                {"名称": "茅台酒", "收入占比": "84%", "毛利率": "94.1%"},
                {"名称": "系列酒", "收入占比": "16%", "毛利率": "79.4%"},
            ]},
            "行业地位": {"同行家数": 19, "行业排名": "第1"},
            "经营评述": "公司称报告期内产品结构持续优化，直销渠道占比提升。",
        },
        "board": {"industry": "食品饮料", "concepts": ["白酒概念", "消费龙头"]},
        "news": {"items": [{"date": "2025-10-18", "type": "公告",
                            "summary": "公司公告前三季度经营数据，收入利润双位数增长。"}]},
    },
    "ofilm": {
        "code": "002456",
        "quote": {"name": "欧菲光", "price": 8.12, "change_pct": -4.6},
        "stockinfo": {"总市值(亿元)": 264},
        "financials": {
            "营业收入(亿元)": {"2025Q3": 168.4, "2024Q3": 172.9, "同比": "-2.6%"},
            "净利润(亿元)": {"2025Q3": 0.42, "2024Q3": -3.8},
            "毛利率(%)": {"2025Q3": 8.1, "2024Q3": 6.4},
            "经营现金流(亿元)": {"2025Q3": -6.8, "2024Q3": -2.1},
            "应收账款(亿元)": {"2025Q3": 74.6, "2024Q3": 63.2, "同比": "+18.0%"},
            "存货(亿元)": {"2025Q3": 58.9, "2024Q3": 47.3},
            "非经常性损益(亿元)": {"2025Q3": 1.9},
            "ROE(%)": {"2025Q3": 0.7, "2024Q3": -8.9},
        },
        "f10": {
            "所属行业": "光学光电子",
            "主营构成": {"明细": [
                {"名称": "摄像头模组", "收入占比": "66%", "毛利率": "6.8%"},
                {"名称": "光学镜头", "收入占比": "21%", "毛利率": "13.2%"},
                {"名称": "触控显示", "收入占比": "13%", "毛利率": "4.1%"},
            ]},
            "行业地位": {"同行家数": 57, "行业排名": "第9"},
            "经营评述": "公司称报告期内聚焦光学主业、压缩低毛利业务，毛利率同比改善。",
        },
        "board": {"industry": "电子元件", "concepts": ["华为概念", "摄像头"]},
        "news": {"items": [{"date": "2025-10-21", "type": "快讯",
                            "summary": "媒体报道公司进入某旗舰机型摄像头模组供应链。"}]},
    },
}

ACTION_WORDS = ("上车", "我先上", "先买", "买一点", "坐不住", "忍不住", "不等了", "手痒",
                "直接干", "拍板", "先干了", "押上", "锁仓", "建仓", "建个底仓", "底仓")
# 注意「我认 / 我先认」不算动作：那是"认可这个判断"，改前样本里多头就是这么写的，
# 把它算成动作会把 before 也判成命中——口径必须能区分"认同"与"下手"。
REACTION_WORDS = ("愣", "没想到", "这数一出来", "看到这个", "吓一跳", "一惊", "刚看到",
                  "一出来我就", "这个数一")
DUMP_WORDS = ("占比", "主营构成", "业务结构", "主要产品", "各占", "产品销售",
              "分产品", "收入构成")
JUDGE_WORDS = ("意味着", "说明", "因为", "所以", "可是", "但", "凭什么", "站不住",
               "我认", "反而", "这就是", "等于", "问题", "解释不了", "不能这么算")
ASK_OPENERS = ("你先说", "先说清", "先讲清", "我问一句", "你先解释", "你倒说说")
NUM_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?%?|[一二两三四五六七八九十百千万亿]{2,}")


def _turns(script: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in (script.get("turns") or []) if isinstance(t, dict)]


def _is_bare_number_sentence(sentence: str) -> bool:
    text = sentence.strip()
    if not text or not NUM_RE.search(text):
        return False
    if text.endswith(("？", "?")):
        return False
    return not any(word in text for word in JUDGE_WORDS)


def evaluate(script: dict[str, Any]) -> dict[str, Any]:
    turns = _turns(script)
    bull_first = next((t for t in turns if t.get("speaker") == "bull"), {})
    bear_first = next((t for t in turns if t.get("speaker") == "bear"), {})
    bull_line = str(bull_first.get("line") or "")
    bear_line = str(bear_first.get("line") or "")

    acted = any(word in bull_line for word in ACTION_WORDS)
    reacted = any(word in bull_line for word in REACTION_WORDS)
    dumped = any(word in bull_line for word in DUMP_WORDS)
    bare = sum(1 for t in turns
               for sentence in re.split(r"(?<=[。！？])", str(t.get("line") or ""))
               if _is_bare_number_sentence(sentence))
    return {
        "bull_first": bull_line,
        "bull_hit": bool(acted or reacted) and not (dumped and not (acted or reacted)),
        "bull_acted": acted,
        "bull_reacted": reacted,
        "bull_dumped": dumped,
        "bear_first": bear_line,
        "bear_hit": ("？" in bear_line or "?" in bear_line
                     or any(word in bear_line for word in ASK_OPENERS)),
        "bare_numbers": bare,
        "turns": len(turns),
        "sharpness": script.get("sharpness", {}).get("score"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="对辩开场 A/B 对照（真跑 LLM）")
    parser.add_argument("--label", default="sample", help="before / after")
    parser.add_argument("--stock", default="all", help="all 或 lens/maotai/ofilm")
    parser.add_argument("--sample", type=int, default=1, help="第几次采样")
    parser.add_argument("--json", action="store_true", help="每行输出一个 JSON 对象")
    args = parser.parse_args()

    keys = list(STOCKS) if args.stock == "all" else [args.stock]
    for key in keys:
        if key not in STOCKS:
            parser.error(f"unknown stock: {key}")
        payload = STOCKS[key]
        script = DialogueGenerator(CONFIG).generate(payload)
        report = evaluate(script)
        report.update({"label": args.label, "stock": key, "name": payload["quote"]["name"],
                       "title": script.get("title")})
        if args.json:
            print(json.dumps(report, ensure_ascii=False))
        else:
            print(f"[{args.label}/{key}#{args.sample}] {report['title']}")
            print(f"  多头首轮 hit={report['bull_hit']}（动作={report['bull_acted']} "
                  f"反应={report['bull_reacted']} 陈述={report['bull_dumped']}）：{report['bull_first']}")
            print(f"  空头首轮 hit={report['bear_hit']}：{report['bear_first']}")
            print(f"  裸报数={report['bare_numbers']} 轮数={report['turns']} 锐度={report['sharpness']}")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
