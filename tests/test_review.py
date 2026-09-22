"""审查页（``<tag>.review.html``）与成片清单的测试。

生成流程跑完后人要过目的三样东西——画面、封面、发布文案——都在这张页面
上；这些测试钉住「一张页面能看全」这个契约，以及画幅/封面与平台的对应关系。
"""

from __future__ import annotations

from pathlib import Path

from stocktalk.modules.review import cover_usage, video_inventory, write_review_page

SCRIPT = {
    "title": "冰轮环境（000811）：从卖设备到温控方案的生意跃迁",
    "turns": [
        {"speaker": "bull", "line": "温控方案意味着更稳的收入", "visual": ["温控方案"]},
        {"speaker": "bear", "line": "但估值并不便宜", "visual": ["估值"]},
    ],
    "disclaimers": ["本内容由AI生成，仅供参考，不构成投资建议"],
    "compliance_issues": ["[bull] 包含禁用词: 必涨"],
}


def test_video_inventory_names_every_canvas_and_its_platforms() -> None:
    entries = video_inventory(
        "output/x.mp4", {"kuaishou": "output/x.vertical.mp4"},
        {"douyin": "horizontal", "kuaishou": "vertical"}, "horizontal",
    )

    assert [entry["file"] for entry in entries] == ["x.mp4", "x.vertical.mp4"]
    assert entries[0]["label"] == "桌面版（横屏）"
    assert "抖音" in entries[0]["platforms"] and "快手" not in entries[0]["platforms"]
    assert entries[1]["label"] == "手机版（竖屏）"
    assert "快手" in entries[1]["platforms"]


def test_cover_usage_maps_every_preset_to_its_platforms() -> None:
    usage = cover_usage(["douyin", "bilibili", "baijiahao"])

    assert usage["portrait"] == ["抖音"]
    assert usage["wide"] == ["B站", "百家号"]


def test_review_page_gathers_video_covers_copy_and_compliance(tmp_path: Path) -> None:
    path = write_review_page(
        tmp_path, "x", stock_name="冰轮环境", stock_code="000811", script=SCRIPT,
        video_path=tmp_path / "x.mp4",
        platform_videos={"kuaishou": str(tmp_path / "x.vertical.mp4")},
        covers={"portrait": str(tmp_path / "x.cover-portrait.png"),
                "wide": str(tmp_path / "x.cover-wide.png")},
        platform_canvas={"douyin": "horizontal", "kuaishou": "vertical"},
        platforms=["douyin", "kuaishou", "bilibili"], main_canvas="horizontal",
        title="冰轮环境（000811）：从卖设备到温控方案的生意跃迁",
        description="视频简介\n本内容由AI生成", tags=["A股", "财报解读"], duration=96.4,
    )
    html = path.read_text(encoding="utf-8")

    assert path.name == "x.review.html"
    # 两种画幅的成片、两张封面都必须在同一页里
    assert "x.mp4" in html and "x.vertical.mp4" in html
    assert "x.cover-portrait.png" in html and "x.cover-wide.png" in html
    assert "竖版封面" in html and "1440×810" in html
    # 等高 + 按比例分宽布局：flex-grow 与 aspect-ratio 都来自素材宽高比
    assert 'style="flex: 1.7778 1 0;"' in html and 'style="flex: 0.5625 1 0;"' in html
    assert 'style="aspect-ratio: 1.7778;"' in html and 'style="aspect-ratio: 0.5625;"' in html
    # 文案与时长：审查时不必再回去翻 JSON
    assert "冰轮环境" in html and "从卖设备到温控方案的生意跃迁" in html
    assert "96 秒" in html
    # 合规自检与风险提示要一起呈现，审查时才知道哪些话被改写过
    assert "包含禁用词" in html
    assert "不构成投资建议" in html


def test_review_page_renders_without_covers_or_metadata(tmp_path: Path) -> None:
    """封面渲染失败（无 Chrome）时审查页仍要出——成片是主角。"""
    path = write_review_page(
        tmp_path, "y", stock_name="平安银行", stock_code="000001", script={},
        video_path=tmp_path / "y.mp4", covers={}, platforms=[],
    )
    html = path.read_text(encoding="utf-8")

    assert "y.mp4" in html
    assert "封面图" not in html


def test_review_page_shows_the_arc_interaction_copy_and_rewrites(tmp_path: Path) -> None:
    """骨架、片尾互动文案、逐字改写对照都要在同一页上。

    这三样决定"这期为什么长这样"：骨架说明结构选择，互动文案说明片尾会打什么
    上屏，改写对照说明合规层动了哪几句。审查时不该再回去翻 JSON。
    """
    script = {
        **SCRIPT,
        "arc": {"id": "annual", "name": "年报逐条", "beats": ["revenue", "margin", "shadow"],
                "pruned": ["cash", "occupied"], "eligible": ["annual", "crack"], "forced": False},
        "arc_notes": ["[第3轮] 骨架指定由 bear 说这段（margin），模型派给了 bull"],
        "hook": "下一份财报的外销收入占比",
        "question": "你更信渠道还是产能？",
        "sides": {"bull": "乐观一边：产品力扎实", "bear": "谨慎一边：客户集中就是命门"},
        "compliance_rewrites": [
            {"where": "片尾·bull立场", "before": "看多一方：产品力扎实", "after": "乐观一边：产品力扎实"}],
    }
    path = write_review_page(tmp_path, "z", stock_name="华瓷股份", stock_code="001216",
                             script=script, video_path=tmp_path / "z.mp4", platforms=[])
    html = path.read_text(encoding="utf-8")

    assert "本期结构与互动" in html
    assert "年报逐条" in html
    # 被剪掉的节拍要说明"这只票没有对应数据"，否则看不出结构为什么变短
    assert "revenue → margin → shadow" in html
    assert "cash" in html and "occupied" in html and "没有对应数据" in html
    # 候选骨架：为什么这期不是裂痕式
    assert "annual" in html and "crack" in html
    # 模型没照骨架走的地方要留痕
    assert "模型派给了 bull" in html
    # 片尾会打在画面上的四样东西
    assert "下一份财报的外销收入占比" in html
    assert "你更信渠道还是产能？" in html
    assert "客户集中就是命门" in html
    # 改写对照：改了什么、改成什么（语义有没有被改跑要人能判断）
    assert "改写对照" in html
    assert "看多一方：产品力扎实" in html and "乐观一边：产品力扎实" in html
