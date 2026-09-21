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
