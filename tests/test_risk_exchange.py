"""片尾风险提示交换轮的契约测试（2026-10-27 WAN-27）。

这个模块的 docstring 一直写着"上线前有断言兜底（tests/test_risk_exchange.py）"，
但这个文件此前并不存在。现在补上，因为它是**用户点名那两句的直接产源**：
片尾这两轮由程序确定性追加，每期必念，模板写错就是整季复读。

守四件事：
1. 合规锚点一句都不能少（这是底线，句式怎么变都要有「股市有风险」「投资需谨慎」）；
2. 模板池干净：不出现套话，尤其不出现「这门生意」「把…聊透了」（回归哨兵）；
3. 特征模板占主导、同票同日可复现（原有的"不千篇一律"设计要求）；
4. 运行时闸门真的会拦：往池子里塞一条含套话的模板，`build` 不会选中它。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stocktalk.modules import copy_guard
from stocktalk.modules.arcs import satisfies
from stocktalk.modules.risk_exchange import (EXCHANGES, MAX_LINE_CHARS, RISK_A, RISK_B,
                                             RiskExchange, TRAITS, _format, build)

RICH = {
    "quote": {"name": "贵州茅台"},
    "f10": {"所属行业": "食品饮料-酿酒",
            "主营构成": {"明细": [{"项目": "茅台酒(产品)"}]}},
}
# 最坏情况：占位符填进能通过长度护栏的最长值。
FILL = {"industry": "食品饮料与酿酒", "item": "其他系列酒产品", "name": "贵州茅台"}


def _rendered() -> list[str]:
    sentences: list[str] = []
    for exchange in EXCHANGES:
        for text in (exchange.a, exchange.b):
            for key, _ in TRAITS:
                text = text.replace(f"{{{key}}}", FILL.get(key, ""))
            sentences.append(text)
    return sentences


def test_every_template_keeps_the_compliance_anchors() -> None:
    for exchange in EXCHANGES:
        assert RISK_A in exchange.a, exchange.id
        assert RISK_B in exchange.b, exchange.id


def test_templates_never_name_the_phrases_the_user_called_out() -> None:
    """回归哨兵：这两句曾经是每期必念的片尾轮，删掉它们要有测试守着。"""
    joined = "\n".join(_rendered())
    assert "这门生意" not in joined
    assert "聊透" not in joined


def test_template_pool_is_free_of_cliches_after_placeholders_are_filled() -> None:
    dirty = {sentence: [hit["phrase"] for hit in copy_guard.source_violations(sentence)]
             for sentence in _rendered()}
    assert not {k: v for k, v in dirty.items() if v}, dirty


def test_rendered_lines_stay_within_one_subtitle_row() -> None:
    for sentence in _rendered():
        assert len(sentence) <= MAX_LINE_CHARS, sentence


def test_build_is_reproducible_for_the_same_symbol_and_day() -> None:
    day = date(2026, 10, 27)
    assert build(RICH, code="600519", day=day) == build(RICH, code="600519", day=day)


def test_trait_templates_dominate_the_pool() -> None:
    """用户要求"按个股特征变化"：够格时特征模板应该占多数。"""
    trait = sum(1 for _ in range(120)
                if build(RICH, code=f"60051{_ % 10}",
                         day=(date(2026, 10, 1) + timedelta(days=_)).isoformat())["id"] in
                {"industry", "item", "name"})
    assert trait / 120 > 0.5


def test_thin_data_falls_back_to_generic_templates_instead_of_forcing_a_trait() -> None:
    """没有任何个股特征时不许硬凑行业/主营/股名——退回通用池。"""
    thin = {"quote": {}}
    picked = {build(thin, code=str(index), day="2026-10-27")["id"] for index in range(40)}
    assert picked <= {"generic-ledger", "generic-numbers", "generic-fork"}


def test_a_cliche_written_into_a_new_template_is_dropped_at_runtime() -> None:
    """运行时闸门：以后有人往池子里加一句套话，它不会被念出来。"""
    bad = RiskExchange("regression", "聊完全部生意，股市有风险。",
                       "投资需谨慎，先看懂再谈其他。")
    assert _format(bad, RICH) is None
    clean = RiskExchange("fine", "账摆在这儿，股市有风险。", "投资需谨慎，答案自己去核。")
    assert _format(clean, RICH) is not None


def test_overlong_template_is_dropped() -> None:
    long_one = RiskExchange("long", "这一句故意写得非常非常长超过了一行字幕的上限，股市有风险。",
                            "投资需谨慎。")
    assert _format(long_one, RICH) is None


@pytest.mark.parametrize("exchange", EXCHANGES, ids=lambda item: item.id)
def test_each_template_declares_data_this_payload_can_supply(exchange) -> None:
    """needs 写的点路径必须真能在这份数据上取到，否则这条候选永远上不了场。"""
    assert not exchange.needs or satisfies(RICH, exchange.needs)
