"""剧本骨架注册表：可用性判定、剪枝、选择的确定性、短回应。

这些断言的目的是钉住"每期不一样"是靠**数据**决定的，而不是靠随机数或跨运行状态：
数据撑不起来的骨架不许上场，硬凑一个缺了关键开场的骨架比重复更糟。
"""

from __future__ import annotations

import pytest

from stocktalk.modules.arcs import (ARCS, MAINLINE, has_path, missing_needs,
                                    prune_beats, resolve_speakers, satisfies, select_arc)

# 只够讲行情：连主营构成都没有。任何依赖公司档案的骨架都不该上场。
THIN = {"code": "000001", "quote": {"name": "平安银行", "price": 10.5}}

FULL = {
    "code": "001216",
    "quote": {"name": "华瓷股份", "price": 24.0},
    "f10": {
        "公司名称": "湖南华瓷股份有限公司",
        "主营业务": "日用陶瓷",
        "所属行业": "家居用品",
        "主营构成": {"报告期": "2026-06-30", "明细": [
            {"项目": "色釉陶瓷(产品)", "收入": "5.2亿", "收入占比(%)": "90.56", "毛利率(%)": "33.52"}]},
        "经营评述": "公司推进越南基地建设。",
        "行业地位": {"研究行业": "家居用品", "同行家数": 73, "市场表现": {"排名": 12}},
        "题材标签": {"概念": ["一带一路", "跨境电商"]},
    },
    "financials": {"报告期": "2026-06-30", "营业收入(亿元)": 5.76, "净利润(亿元)": 1.03,
                   "经营现金流(亿元)": 0.88, "存货(亿元)": 2.76, "应收账款(亿元)": 1.67},
    "stockinfo": {"总市值(亿元)": 96.4, "换手率(%)": 8.1},
    "news": {"items": [{"title": "华瓷股份发布异动公告", "publish_time": "2026-09-21"}]},
    "technical": {"summary": {"signal": "bullish"}, "count": 60},
    "board": {"industry": ["家居用品"], "concepts": ["一带一路"]},
}


def test_has_path_reads_flattened_payload_and_treats_zero_as_present() -> None:
    assert has_path(FULL, "f10.主营构成.明细")
    assert has_path(FULL, "board.concepts")
    assert not has_path(FULL, "f10.管理层")
    assert not has_path(THIN, "f10")
    # 0 是信息（比如"毛利率 0%"），不算缺数据；空串/空表才算缺。
    assert has_path({"a": {"b": 0}}, "a.b")
    assert not has_path({"a": {"b": ""}}, "a.b")
    assert not has_path({"a": {"b": []}}, "a.b")


def test_needs_are_and_across_entries_or_within_one() -> None:
    assert satisfies(FULL, ("f10.主营构成.明细",))
    assert satisfies(FULL, ("f10.管理层|f10.主营构成.明细",))      # 任一满足即可
    assert not satisfies(FULL, ("f10.管理层",))                    # 缺
    assert not satisfies(FULL, ("f10.主营构成.明细", "f10.管理层"))  # 一条缺就整条不成立


def test_pruning_drops_the_beats_this_stock_cannot_support() -> None:
    """有就加，没有数据就不要——剪掉撑不起来的节拍，而不是让它硬凑。"""
    full = prune_beats(ARCS["annual"], FULL)
    thin = prune_beats(ARCS["annual"], THIN)
    assert [beat.key for beat in full] == ["revenue", "margin", "cash", "occupied", "shadow", "closing"]
    # 只有行情时，年报逐条只剩那个不依赖数据的隐患节拍（closing 无数据依赖，保留）
    assert [beat.key for beat in thin] == ["shadow", "closing"]
    assert "f10.主营构成.明细" in missing_needs(ARCS["annual"], THIN)


def test_thin_data_falls_back_to_the_mainline() -> None:
    choice = select_arc(THIN, code="000001", day="2026-09-22")
    assert choice.arc is MAINLINE
    assert choice.eligible == ()
    assert [beat.key for beat in choice.beats] == [beat.key for beat in MAINLINE.beats]
    # 为什么没用数据驱动骨架，要能事后解释（缺哪份数据）
    assert {arc_id for arc_id, _ in choice.skipped} == {"crack", "customer", "annual", "peers"}


def test_data_backed_arc_never_loses_its_required_beats() -> None:
    """够格的骨架必须是"依赖全中"的。

    裂痕式的追问/极短回应/留下分歧三个节拍不依赖数据，所以只看"剪枝后还剩几拍"
    的话，一份连主营构成都没有的 payload 也会被判定适合裂痕式——然后丢掉它最关键的
    "反常识数字开场"。这条断言就是挡住那种硬凑。
    """
    choice = select_arc(FULL, code="001216", day="2026-09-22")
    assert choice.arc.id in {"crack", "customer", "annual", "peers"}
    assert missing_needs(choice.arc, FULL) == ()
    assert [beat.key for beat in choice.beats] == [beat.key for beat in choice.arc.beats]


def test_selection_is_stable_per_code_and_spreads_across_codes() -> None:
    """同一只票同一天结果可复现；不同票之间要分散——这才是"每期不一样"的来源。"""
    first = select_arc(FULL, code="001216", day="2026-09-22")
    again = select_arc(FULL, code="001216", day="2026-09-22")
    assert first.arc.id == again.arc.id

    picked = {select_arc(FULL, code=code, day="2026-09-22").arc.id
              for code in ("001216", "300476", "300082", "600519", "000001", "002074")}
    assert len(picked) >= 2, f"多只票只落到一种骨架：{picked}"


def test_forced_arc_is_honoured_and_still_pruned() -> None:
    choice = select_arc(FULL, configured="annual", code="1")
    assert choice.forced and choice.arc.id == "annual"
    assert [beat.key for beat in choice.beats][0] == "revenue"

    # 强制指定但数据不够：照旧按数据剪枝，缺哪拍是可见的（arc.report().pruned）
    forced_thin = select_arc(THIN, configured="crack", code="1")
    assert forced_thin.forced
    assert [beat.key for beat in forced_thin.beats] == ["challenge", "concede", "fork", "closing"]
    assert "odd" in forced_thin.report()["pruned"]


def test_unknown_arc_name_falls_back_instead_of_raising() -> None:
    """配置里写错骨架名不该让整条流水线挂掉，退回按数据选。"""
    choice = select_arc(FULL, configured="no-such-arc", code="001216", day="2026-09-22")
    assert choice.arc.id in ARCS
    assert not choice.forced


def test_every_arc_declares_an_opener_and_sane_min_beats() -> None:
    """每条数据驱动骨架都要有开场约束（反套话）且节拍够多。"""
    for arc in ARCS.values():
        assert arc.beats, arc.id
        assert len(arc.beats) >= arc.min_beats, arc.id
        assert len({beat.key for beat in arc.beats}) == len(arc.beats), f"{arc.id} 有重复节拍 key"
    for arc_id in ("crack", "customer", "annual", "peers"):
        assert ARCS[arc_id].opener, f"{arc_id} 没有开场约束，模型会回到套话开场"
    assert not MAINLINE.opener  # 回退主线不额外约束开场


def test_crack_has_a_short_interjection_beat() -> None:
    """用户要求：对话该长则长、该短则短，有时候只是认可一句。"""
    shorts = [beat for beat in ARCS["crack"].beats if beat.short]
    assert shorts, "裂痕式缺少短回应节拍"
    assert shorts[0].max_chars and shorts[0].max_chars <= 30


def test_resolve_speakers_always_keeps_the_default_pair() -> None:
    """配置只覆盖其中一个角色时，另一个不能被判为非法（会静默丢台词）。"""
    assert resolve_speakers({}) == ("bull", "bear")
    assert resolve_speakers({"bull": {"name": "新手"}}) == ("bull", "bear")
    assert resolve_speakers({"bull": {}, "bear": {}, "expert": {}}) == ("bull", "bear", "expert")


def test_report_is_serialisable_for_the_review_page() -> None:
    report = select_arc(FULL, code="001216", day="2026-09-22").report()
    assert set(report) == {"id", "name", "forced", "beats", "pruned", "eligible", "skipped"}
    assert isinstance(report["beats"], list) and report["beats"]
    assert isinstance(report["skipped"], dict)


if __name__ == "__main__":
    pytest.main([__file__])


def test_every_arc_ends_with_a_dialogue_closing_beat() -> None:
    """2026-09-26 评审：9 轮对话以 bull 单方独白收尾，观众会觉得"然后呢？"。

    每条骨架的最后一拍必须是 closing 短回应——由对方接话收束，不许独白结束。
    """
    for arc in ARCS.values():
        last = arc.beats[-1]
        assert last.key == "closing", f"{arc.id} 以 {last.key} 收尾，没有对话收束"
        assert last.short and last.max_chars, f"{arc.id} 的 closing 必须是短回应"
