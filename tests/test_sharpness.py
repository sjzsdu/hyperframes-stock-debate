"""锐度判定清单的契约测试（2026-10-27 WAN-27）。

清单是把"像在报流水账"拆成可核对的红绿灯，所以测试要守两件事：

1. **每一项都能被单独踩红**——一条永远为真的判定等于没有判定；
2. **实测样本上确实会踩红**——下面那份 "流水账" 是改动前那期真实文案的形状
   （长首句报数、两边都没推翻对方、结尾推给下一份财报），它必须挂掉同一批项。
"""

from __future__ import annotations

from typing import Any

import pytest

from stocktalk.modules import sharpness

SHARP: dict[str, Any] = {
    "sides": {"bull": "直销放量足以对冲周期", "bear": "价盘松动会先反噬利润"},
    "spotlight": {"label": "存货", "value": "512亿≈半年收入"},
    "turns": [
        {"speaker": "bull", "emotion": "惊叹",
         "line": "自己卖的比给经销商卖，一件多赚一成二——凭什么？"},
        {"speaker": "bear", "emotion": "反驳",
         "line": "不对，高毛利是拿经销商垫的钱换来的，渠道推力已经弱了三成。"},
        {"speaker": "bull", "emotion": "坚守",
         "line": "存货五百一十二亿压着不动，说明它是主动囤基酒，不是卖不掉。"},
        {"speaker": "bear", "emotion": "质疑",
         "line": "问题是动销一缓这五百亿就变成周转天数，代价压在自己账上。"},
        {"speaker": "bull", "emotion": "顿悟",
         "line": "所以真正的分歧只有一个：价盘一个月内跌两成就全露馅。"},
    ],
    "quality": {"cliche_hits": [], "repeated_phrases": []},
}

FLAT: dict[str, Any] = {
    # 两边立场几乎是同一句话的两面 → 没有真对立面
    "sides": {"bull": "公司经营稳健", "bear": "公司经营需要关注"},
    "turns": [
        {"speaker": "bull", "emotion": "平述",
         "line": "宴席上的那杯白色液体，半年能卖七百一十四亿，中报里核心单品独占六百个亿，占比百分之八十四。"},
        {"speaker": "bear", "emotion": "平述",
         "line": "毛利率很高，公司把直销渠道比例拉高，利润池确实厚了，但经销商差价空间被压缩。"},
        {"speaker": "bull", "emotion": "平述",
         "line": "经营现金流少于利润，说明当期发货多，整体来看经营状况还是稳健的。"},
        {"speaker": "bear", "emotion": "平述",
         "line": "存货体量较大，动销一缓周转压力就会出来，这些都需要持续观察。"},
        {"speaker": "bull", "emotion": "平述",
         "line": "等下一份财报出来，我们就知道增速能不能撑住了。"},
    ],
    "quality": {"cliche_hits": ["这门生意"], "repeated_phrases": []},
}


def _failed(script: dict[str, Any]) -> list[str]:
    return sharpness.evaluate(script)["failed"]


def test_the_sharp_sample_passes_every_check() -> None:
    report = sharpness.evaluate(SHARP)
    assert report["failed"] == [], report["checks"]
    assert report["score"] == 1.0


def test_the_flat_sample_fails_the_checks_it_should() -> None:
    """改动前那种"流水账"形状必须踩红——否则这套标准验不出任何东西。"""
    failed = set(_failed(FLAT))
    assert {"hook", "opposing", "anomaly", "no_rerun", "sharp_close"} <= failed, failed


@pytest.mark.parametrize("check_id", [item for item in
                                      ("hook", "front_load", "opposing", "refutable",
                                       "anomaly", "no_rerun", "sharp_close")])
def test_every_check_can_fail_on_its_own(check_id: str) -> None:
    """每一项都要能单独踩红：一条永远为真的判定等于没有判定。"""
    script = {key: (dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value)
              for key, value in SHARP.items()}
    turns = [dict(turn) for turn in SHARP["turns"]]
    if check_id == "hook":
        turns[0]["line"] = "先说一句长的：这家公司去年卖了很多很多酒，收入很好，利润也不错，渠道也很稳。"
    elif check_id == "front_load":
        turns[0]["line"] = "毛利率九成一个点，先看这个数。"
    elif check_id == "opposing":
        script["sides"] = {"bull": "看好", "bear": "看好"}
        for turn in turns:
            turn["emotion"] = "平述"
            turn["line"] = turn["line"].replace("不对，", "").replace("问题是", "")
    elif check_id == "refutable":
        for turn in turns:
            turn["line"] = "渠道很重要，护城河很深，公司做得不错。"
    elif check_id == "anomaly":
        script.pop("spotlight", None)
        for turn in turns:
            turn["line"] = "渠道占比四成四，利润也还可以，这一轮先看渠道。"
    elif check_id == "no_rerun":
        script["quality"] = {"cliche_hits": ["这门生意"], "repeated_phrases": ["这门生意怎么转"]}
    elif check_id == "sharp_close":
        turns[-1]["line"] = "等下一份财报出来，我们就知道增速能不能撑住了。"
    script["turns"] = turns
    assert check_id in _failed(script)


def test_every_check_carries_evidence_a_human_can_check() -> None:
    """判定必须能引用原文：审查页上"未过"旁边要看得见是哪一句。"""
    report = sharpness.evaluate(FLAT)
    for item in report["checks"]:
        assert item["label"] and item["detail"]
        if not item["ok"]:
            assert "id" in item and "detail" in item
    assert any(item.get("evidence") for item in report["checks"])


def test_summary_names_the_failed_checks() -> None:
    line = sharpness.summary(sharpness.evaluate(FLAT))
    assert "未过" in line and "hook" in line
