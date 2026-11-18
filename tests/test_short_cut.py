"""快评版剪辑（short_cut，2026-11-15 P1-2）的契约测试。

快评版是同一期的「完播版」而不是第二条内容线：台词一字不改、逐轮 wav 复用，
只裁轮次子集并把时间轴重新归零。测试锁三件事：

1. select_turns 的取舍——保开场钩子、保收束+风险尾、中段按情绪/数字打分进预算；
2. build_short_cut 的时间轴——选中段按轮序重归零、audio_path 原样复用；
3. pipeline._render_short_form 的降级语义——只对超预算正片动刀，失败回落正片。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from rich.progress import Progress

import stocktalk.pipeline as pipeline_module
from stocktalk.pipeline import Pipeline
from stocktalk.modules.short_cut import build_short_cut, select_turns

# 每轮恰好 20 字，预算算术好核对：max_seconds=25 → 120 字预算。
PAD = "字" * 18


def _line(i: int) -> str:
    return f"{i:02d}{PAD}"


def _turn(i: int, *, emotion: str = "平述", beat: str = "argument") -> dict[str, Any]:
    return {"speaker": "bull" if i % 2 == 0 else "bear", "line": _line(i),
            "emotion": emotion, "beat": beat}


def _script(n: int = 9, emotions: list[str] | None = None) -> dict[str, Any]:
    emotions = emotions or ["平述"] * n
    return {"title": "测试（000001）：快评版", "series": "现金流排雷",
            "turns": [_turn(i, emotion=emotions[i]) for i in range(n)]}


def _audio(n: int = 9, duration: float = 2.0) -> dict[str, Any]:
    segments = [{"character": "bull" if i % 2 == 0 else "bear", "line": _line(i),
                 "start_time": i * duration, "end_time": (i + 1) * duration,
                 "duration": duration, "audio_path": f"/tmp/seg-{i}.wav"}
                for i in range(n)]
    return {"segments": segments, "total_duration": n * duration}


# ---------------------------------------------------------------------------
# select_turns
# ---------------------------------------------------------------------------


def test_short_script_is_kept_whole() -> None:
    """正片本来就在预算内时一刀不裁——快评版只对「比预算长」的正片动刀。"""
    turns = [_turn(i) for i in range(6)]
    assert select_turns(turns, max_seconds=60) == [0, 1, 2, 3, 4, 5]


def test_tiny_script_is_kept_whole_even_when_over_budget() -> None:
    """轮数低于下限（开场+交锋+收尾）剪出来不成片，宁可超预算也全保留。"""
    turns = [_turn(i) for i in range(3)]
    assert select_turns(turns, max_seconds=1) == [0, 1, 2]


def test_cut_keeps_the_hook_and_the_tail() -> None:
    """下标 0（开场钩子）与末 2 轮（收束一来一回）无条件保留。

    风险提示轮默认已关闭（2026-11-17），片尾只剩收束；若开回来，那轮会因
    台词含「股市有风险」被单独识别保留（见 test_risk_turn_is_kept_when_present）。
    """
    turns = [_turn(i) for i in range(9)]          # 180 字 > 120 字预算
    keep = select_turns(turns, max_seconds=25)
    assert 0 in keep
    assert {7, 8} <= set(keep)                    # keep_tail=2 的片尾收束
    assert len(keep) == 6                        # 预算再容得下中段轮


def test_risk_turn_is_kept_when_present() -> None:
    """口播风险轮若被打开（台词含「股市有风险」），快评版必须保住它。"""
    turns = [_turn(i) for i in range(8)]
    turns[-2] = {**turns[-2], "line": "丑话说在前头，股市有风险，投资需谨慎。",
                 "beat": "closing"}
    keep = select_turns(turns, max_seconds=25)
    assert 6 in keep                                 # 风险轮（倒二）被动态识别保留
    assert 7 in keep                                 # 收束轮


def test_hot_middle_turns_win_the_remaining_budget() -> None:
    """中段轮按情绪烈度（反驳/质疑/惊叹/顿悟）优先，平述轮先被砍。"""
    emotions = ["平述"] * 9
    emotions[3] = "反驳"                          # 唯一的交锋真发生了的轮次
    turns = [_turn(i, emotion=emotions[i]) for i in range(9)]
    keep = select_turns(turns, max_seconds=20)    # 96 字预算：开场+反驳轮+收束2轮
    assert 3 in keep
    assert not ({1, 2, 4} & set(keep))            # 平述中段轮被砍


# ---------------------------------------------------------------------------
# build_short_cut
# ---------------------------------------------------------------------------


def test_timeline_is_rezeroed_and_audio_reused() -> None:
    """选中段按轮序重归零计时，逐轮 wav 原样复用（不拼接、不重合成）。"""
    script = _script(7)
    cut = build_short_cut(script, _audio(7), max_seconds=25, pause=0.12)
    assert cut["kept"] == 6 and cut["dropped"] == 1   # 中段留 4 轮、片尾保收束 2 轮
    segments = cut["timeline"]["segments"]
    assert [s["line"] for s in segments] == [_line(i) for i in (0, 1, 2, 3, 5, 6)]
    assert [s["audio_path"] for s in segments] == [f"/tmp/seg-{i}.wav" for i in (0, 1, 2, 3, 5, 6)]
    assert [s["start_time"] for s in segments] == pytest.approx(
        [0.0, 2.12, 4.24, 6.36, 8.48, 10.6])
    assert cut["timeline"]["total_duration"] == pytest.approx(12.6)  # 6×2.0 + 5×0.12


def test_cut_keeps_script_metadata() -> None:
    """轮次子集不动元数据：标题/合集照常走正片的封面与发布链路。"""
    cut = build_short_cut(_script(7), _audio(7), max_seconds=25)
    assert cut["script"]["title"] == "测试（000001）：快评版"
    assert cut["script"]["series"] == "现金流排雷"


def test_within_budget_cut_changes_nothing() -> None:
    """预算内全保留时 dropped == 0——渲染层据此跳过另出片（抖音直接发正片）。"""
    script = _script(4)
    cut = build_short_cut(script, _audio(4), max_seconds=60)
    assert cut["dropped"] == 0 and cut["kept"] == 4
    assert len(cut["timeline"]["segments"]) == 4


# ---------------------------------------------------------------------------
# pipeline._render_short_form
# ---------------------------------------------------------------------------


class FakeBuilder:
    made: list["FakeBuilder"] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        FakeBuilder.made.append(self)

    def build(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    def render_mp4(self, variant: Any, output_path: Any) -> Path:
        Path(output_path).write_bytes(b"mp4")
        return Path(output_path)


def _pipeline(tmp_path: Path, *, short_form: dict[str, Any] | None) -> Pipeline:
    publish: dict[str, Any] = {
        "platforms": ["douyin"],
        "platform_canvas": {"douyin": "horizontal"},
    }
    if short_form is not None:
        publish["short_form"] = short_form
    return Pipeline({
        "output": {"dir": str(tmp_path / "out")},
        "video": {"canvas": "horizontal"},
        "publish": publish,
    })


def test_short_form_renders_the_douyin_cut(tmp_path: Path, monkeypatch: Any) -> None:
    """超预算正片另出一条 60-90s 快评版，画幅跟随 platform_canvas.douyin。"""
    pipeline = _pipeline(tmp_path, short_form={"enabled": True, "platform": "douyin", "max_seconds": 25})
    FakeBuilder.made = []
    monkeypatch.setattr(pipeline_module, "HyperFramesBuilder", FakeBuilder)
    with Progress() as progress:
        task = progress.add_task("render", total=1)
        cuts = pipeline._render_short_form(_STOCK, _script(9), _audio(9), tmp_path, "tag", progress, task)
    assert set(cuts) == {"douyin"}
    assert cuts["douyin"].endswith("tag.douyin-short.mp4")
    assert Path(cuts["douyin"]).is_file()
    # 短版项目目录独立于正片，画幅继承 douyin 的 platform_canvas。
    assert FakeBuilder.made[0].config["video"]["canvas"] == "horizontal"
    assert FakeBuilder.made[0].config["video"]["project_dir"].endswith("douyin-short")


def test_short_form_skips_when_the_script_fits(tmp_path: Path, monkeypatch: Any) -> None:
    """正片已在预算内 = 无可删轮次：不另出片，抖音直接发正片（不重复渲染）。"""
    pipeline = _pipeline(tmp_path, short_form={"enabled": True, "platform": "douyin", "max_seconds": 25})
    FakeBuilder.made = []
    monkeypatch.setattr(pipeline_module, "HyperFramesBuilder", FakeBuilder)
    with Progress() as progress:
        task = progress.add_task("render", total=1)
        cuts = pipeline._render_short_form(_STOCK, _script(4), _audio(4), tmp_path, "tag", progress, task)
    assert cuts == {}
    assert FakeBuilder.made == []


def test_short_form_is_off_unless_configured(tmp_path: Path, monkeypatch: Any) -> None:
    """没配 publish.short_form（含全部测试用的裸 dict 配置）＝关闭，零副作用。"""
    pipeline = _pipeline(tmp_path, short_form=None)
    FakeBuilder.made = []
    monkeypatch.setattr(pipeline_module, "HyperFramesBuilder", FakeBuilder)
    with Progress() as progress:
        task = progress.add_task("render", total=1)
        cuts = pipeline._render_short_form(_STOCK, _script(9), _audio(9), tmp_path, "tag", progress, task)
    assert cuts == {}
    assert FakeBuilder.made == []


def test_short_form_render_failure_degrades_to_the_master(tmp_path: Path, monkeypatch: Any) -> None:
    """快评版渲染挂了只丢短版，抖音回落正片——绝不连累主片与其它平台。"""
    pipeline = _pipeline(tmp_path, short_form={"enabled": True, "platform": "douyin", "max_seconds": 25})
    FakeBuilder.made = []

    class Boom(FakeBuilder):
        def render_mp4(self, variant: Any, output_path: Any) -> Path:
            raise RuntimeError("render crashed")

    monkeypatch.setattr(pipeline_module, "HyperFramesBuilder", Boom)
    with Progress() as progress:
        task = progress.add_task("render", total=1)
        cuts = pipeline._render_short_form(_STOCK, _script(9), _audio(9), tmp_path, "tag", progress, task)
    assert cuts == {}


def test_run_puts_the_short_cut_on_douyin(tmp_path: Path, monkeypatch: Any) -> None:
    """run() 的接线：短版映射覆盖画幅版，抖音发快评版、其余平台发正片。"""
    pipeline = _pipeline(tmp_path, short_form={"enabled": True, "platform": "douyin", "max_seconds": 25})
    FakeBuilder.made = []
    monkeypatch.setattr(pipeline_module, "HyperFramesBuilder", FakeBuilder)
    script = _script(9)
    pipeline.stock_client.get_stock_data = MagicMock(return_value=_STOCK)
    pipeline.dialogue_gen.generate = MagicMock(return_value=script)
    pipeline.compliance.review = MagicMock(return_value=script)
    pipeline.tts.synthesize = MagicMock(return_value=_audio(9))
    pipeline.builder.build = MagicMock(return_value={})
    pipeline.builder.render_mp4 = MagicMock(return_value=tmp_path / "out" / "video.mp4")
    pipeline._render_covers = MagicMock(return_value={})

    result = pipeline.run("000001")

    assert set(result["platform_videos"]) == {"douyin"}
    assert result["platform_videos"]["douyin"].endswith(".douyin-short.mp4")
    assert result["video_path"].endswith("video.mp4")          # 主片不受影响


_STOCK: dict[str, Any] = {
    "code": "000001",
    "quote": {"code": "000001", "name": "平安银行", "price": 12.5, "change_pct": 1.2},
    "technical": {"summary": {}, "history": [], "count": 0},
    "financials": {}, "f10": {}, "kline": [], "unavailable": {},
}
