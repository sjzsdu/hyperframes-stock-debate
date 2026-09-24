"""谈股论金 CLI 测试：批量目标、平台覆盖、退出码。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from stocktalk import cli
from stocktalk.modules.topic_picker import HotTopic
from stocktalk.pipeline import PipelineError


def _result(code: str, publish: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"stock_code": code, "stock_name": "测试股", "tag": f"{code}_1", "script": {"turns": [{}]},
            "approved_script": {}, "audio": {}, "srt": None, "project_dir": "output/x",
            "video_path": "output/x.mp4", "rendered": True, "platform_videos": {},
            "preview": False, "publish": publish, "elapsed_seconds": 1.0}


def _publish_report(failed: list[str] | None = None) -> dict[str, Any]:
    failed = failed or []
    return {"video": "output/x.mp4", "metadata": {}, "succeeded": [], "failed": [{"platform": p, "detail": "boom"} for p in failed],
            "platforms": [{"platform": p, "label": "抖音", "ok": False, "detail": "boom", "command": [], "attempts": 2} for p in failed]}


class FakePipeline:
    def __init__(self, outcomes: dict[str, Any] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[str] = []
        self.config: dict[str, Any] = {}

    def run(self, code: str, stock_name: str | None = None, *, render: bool = True,
            preview: bool = False, publish: bool = False) -> dict[str, Any]:
        self.calls.append(code)
        outcome = self.outcomes.get(code, _result(code))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _args(**overrides: Any) -> argparse.Namespace:
    base = {"stock_codes": [], "name": None, "watchlist": None,
            "no_render": False, "preview": False, "publish": True}
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# Target collection
# ---------------------------------------------------------------------------

def test_collect_targets_reads_watchlist_and_dedupes(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("601689 拓普集团\n# 注释行\n\n600519\n601689\n", encoding="utf-8")
    parser = argparse.ArgumentParser()

    targets = cli._collect_targets(_args(stock_codes=["000001"], watchlist=str(watchlist)), parser)

    assert targets == [("000001", None), ("601689", "拓普集团"), ("600519", None)]


def test_collect_targets_applies_name_only_for_a_single_code() -> None:
    parser = argparse.ArgumentParser()
    assert cli._collect_targets(_args(stock_codes=["601689"], name="拓普集团"), parser) == [("601689", "拓普集团")]
    assert cli._collect_targets(_args(stock_codes=["601689", "600519"], name="拓普集团"), parser) == [("601689", None), ("600519", None)]


def test_collect_targets_requires_at_least_one_code() -> None:
    with pytest.raises(SystemExit):
        cli._collect_targets(_args(), argparse.ArgumentParser())


def test_collect_targets_rejects_missing_watchlist() -> None:
    with pytest.raises(SystemExit):
        cli._collect_targets(_args(watchlist="/tmp/definitely-not-here.txt"), argparse.ArgumentParser())


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------


def test_summary_lists_every_canvas_cut_and_its_platforms() -> None:
    """两种画幅都要出现在摘要里——只列主片会让人以为手机版没渲出来。"""
    result = _result("002246")
    result["canvas"] = "horizontal"
    result["platform_canvas"] = {"douyin": "horizontal", "bilibili": "horizontal",
                                 "kuaishou": "horizontal", "tencent": "horizontal",
                                 "xiaohongshu": "vertical"}
    result["platform_videos"] = {"xiaohongshu": "output/x.vertical.mp4"}

    lines = cli._video_lines(result)

    assert len(lines) == 2
    assert lines[0].startswith("桌面版（横屏）: output/x.mp4")
    assert "抖音" in lines[0] and "小红书" not in lines[0]
    assert lines[1].startswith("手机版（竖屏）: output/x.vertical.mp4")
    assert "小红书" in lines[1]


def test_summary_falls_back_to_filename_when_result_omits_canvas() -> None:
    """旧结果 JSON 没有 canvas 字段时，仍要把分画幅成片列出来。"""
    result = _result("002246")
    result["platform_videos"] = {"xiaohongshu": "output/x.vertical.mp4"}

    lines = cli._video_lines(result)

    assert [line.split(":")[0] for line in lines] == ["主片", "手机版（竖屏）"]


# ---------------------------------------------------------------------------
# Batch orchestration
# ---------------------------------------------------------------------------

def test_run_all_continues_after_a_failed_stock(capsys: pytest.CaptureFixture[str]) -> None:
    pipeline = FakePipeline({"600519": PipelineError("股票数据获取失败")})
    args = _args()
    targets = [("601689", None), ("600519", None), ("000001", None)]

    failures = cli._run_all(pipeline, targets, args)  # type: ignore[arg-type]

    assert pipeline.calls == ["601689", "600519", "000001"]
    assert [code for code, _ in failures] == ["600519"]
    assert "股票数据获取失败" in failures[0][1]


def test_run_all_reports_platform_failures(capsys: pytest.CaptureFixture[str]) -> None:
    pipeline = FakePipeline({"601689": _result("601689", _publish_report(["douyin"]))})

    failures = cli._run_all(pipeline, [("601689", None)], _args())  # type: ignore[arg-type]

    assert failures == [("601689", "发布失败：抖音")]


# ---------------------------------------------------------------------------
# main(): wiring, platform override, exit codes
# ---------------------------------------------------------------------------

def _patch_pipeline(fake: FakePipeline, config: dict[str, Any] | None = None):
    def factory(conf):
        fake.config = conf
        return fake
    return patch.multiple(cli, Pipeline=factory, load_config=lambda path=None: dict(config or {}))


def test_main_runs_every_code_and_exits_zero_on_success(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakePipeline()
    with _patch_pipeline(fake), patch.object(cli.sys, "stdin", None, create=True), \
         patch.object(cli, "SauPublisher", lambda config: _ok_publisher()):
        cli.main(["601689", "600519", "--publish"])

    assert fake.calls == ["601689", "600519"]
    assert "汇总 2 只 / 失败 0 只" in capsys.readouterr().out


def test_main_exits_nonzero_when_a_stock_fails(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakePipeline({"600519": PipelineError("LLM 超时")})
    with _patch_pipeline(fake):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["601689", "600519"])

    assert excinfo.value.code == 1
    assert "LLM 超时" in capsys.readouterr().out


def test_main_exits_nonzero_when_publishing_fails() -> None:
    fake = FakePipeline({"601689": _result("601689", _publish_report(["douyin", "kuaishou"]))})
    with _patch_pipeline(fake), patch.object(cli.sys, "stdin", None, create=True), \
         patch.object(cli, "SauPublisher", lambda config: _ok_publisher()):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["601689", "--publish"])

    assert excinfo.value.code == 1


def test_main_platform_override_reaches_the_publisher() -> None:
    fake = FakePipeline()
    with _patch_pipeline(fake):
        cli.main(["601689", "--platforms", "douyin,bilibili", "--no-render"])

    assert fake.config["publish"]["platforms"] == ["douyin", "bilibili"]


def test_main_rejects_an_unknown_platform() -> None:
    with _patch_pipeline(FakePipeline()):
        with pytest.raises(SystemExit):
            cli.main(["601689", "--platforms", "douyin,weibo"])


# ---------------------------------------------------------------------------
# --check  发布预检
# ---------------------------------------------------------------------------

class FakeCheckPublisher:
    def __init__(self, results: dict[str, dict[str, Any]] | None = None) -> None:
        self.results = results or {}
        self.probed: list[list[str]] = []

    def check_platforms(self, platforms: list[str]) -> dict[str, dict[str, Any]]:
        self.probed.append(list(platforms))
        return {p: self.results.get(p, {"ok": True, "detail": "ok"}) for p in platforms}


def _ok_publisher() -> FakeCheckPublisher:
    return FakeCheckPublisher({"p": {"ok": True, "detail": "ok"} for p in PLATFORM_ALL})


PLATFORM_ALL = ("douyin", "bilibili", "kuaishou", "tencent", "baijiahao")


def _env(ok: bool = True) -> list[dict[str, Any]]:
    return [{"name": "sau", "label": "sau CLI（发布）", "ok": ok, "detail": "/bin/sau", "required": True},
            {"name": "npx", "label": "Node/npx（MP4 渲染）", "ok": True, "detail": "/bin/npx", "required": False},
            {"name": "chrome", "label": "Chrome（封面图）", "ok": True, "detail": "/chrome", "required": False}]


def test_check_exits_zero_when_everything_passes(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeCheckPublisher({p: {"ok": True, "detail": "ok"} for p in ("douyin", "kuaishou")})
    with patch.multiple(cli, Pipeline=fake, load_config=lambda path=None: {"publish": {"platforms": ["douyin", "kuaishou"]}}), \
         patch.object(cli, "SauPublisher", lambda config: fake), \
         patch.object(cli, "check_environment", lambda config, publisher=None: _env()):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--check"])

    assert excinfo.value.code == 0
    assert "平台 2/2 可发布" in capsys.readouterr().out


def test_check_exits_nonzero_and_prints_the_login_remedy(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeCheckPublisher({"douyin": {"ok": True, "detail": "ok"},
                               "kuaishou": {"ok": False, "detail": "cookie expired\nplease login"}})
    with patch.multiple(cli, Pipeline=fake, load_config=lambda path=None: {"publish": {"platforms": ["douyin", "kuaishou"]}}), \
         patch.object(cli, "SauPublisher", lambda config: fake), \
         patch.object(cli, "check_environment", lambda config, publisher=None: _env()):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--check"])

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "平台 1/2 可发布" in out
    assert "sau kuaishou login --account default" in out


def test_check_fails_when_a_required_tool_is_missing(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeCheckPublisher({"douyin": {"ok": True, "detail": "ok"}, "kuaishou": {"ok": True, "detail": "ok"}})
    env = _env(ok=False)
    with patch.multiple(cli, Pipeline=fake, load_config=lambda path=None: {"publish": {"platforms": ["douyin", "kuaishou"]}}), \
         patch.object(cli, "SauPublisher", lambda config: fake), \
         patch.object(cli, "check_environment", lambda config, publisher=None: env):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--check"])

    assert excinfo.value.code == 1
    assert "必需工具缺失" in capsys.readouterr().out


def test_check_separates_a_timed_out_probe_from_a_dead_cookie(capsys: pytest.CaptureFixture[str]) -> None:
    """超时是「没问出来」，不是「失效」，提示必须不一样。

    2026-09-24 实测：快手 ``check`` 单独跑 8 秒就返回 ``cookie 有效``，却在一次串行
    预检里吃满 120 秒预算。当时 ``--check`` 照着「需要重新登录」把它列了出来 ——
    而照着那句去 ``login`` 会覆盖掉本来有效的 cookie，把一个能发的平台弄成真的
    需要重登。
    """
    fake = FakeCheckPublisher({"douyin": {"ok": True, "detail": "ok"},
                               "kuaishou": {"ok": False, "timed_out": True, "detail": "探活超时"}})
    with patch.multiple(cli, Pipeline=fake, load_config=lambda path=None: {"publish": {"platforms": ["douyin", "kuaishou"]}}), \
         patch.object(cli, "SauPublisher", lambda config: fake), \
         patch.object(cli, "check_environment", lambda config, publisher=None: _env()):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--check"])

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "sau kuaishou login" not in out      # 不许催人去重新登录
    assert "探活超时" in out
    assert "别急着重新登录" in out
    assert "sau kuaishou check --account default" in out  # 该做的是重试探活


# ---------------------------------------------------------------------------
# 发布前探活（--publish 开跑前的登录态门禁）
# ---------------------------------------------------------------------------

_GATE_CONFIG = {"publish": {"platforms": ["douyin", "tencent"], "accounts": {"tencent": "default"}}}


def test_publish_gate_blocks_and_prints_the_remedy_when_a_cookie_is_dead(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakePipeline()
    fake_pub = FakeCheckPublisher({"douyin": {"ok": True, "detail": "ok"},
                                   "tencent": {"ok": False, "detail": "cookie 已失效"}})
    with _patch_pipeline(fake, _GATE_CONFIG), \
         patch.object(cli, "SauPublisher", lambda config: fake_pub), \
         patch.object(cli.sys, "stdin", None, create=True):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["601689", "--publish"])

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "发布预检" in out and "视频号(tencent)" in out
    assert "uv run sau tencent login --account default" in out
    # 门禁在渲染前：不该烧掉一次生成
    assert fake.calls == []


def test_publish_gate_continues_with_healthy_platforms_on_yes(capsys: pytest.CaptureFixture[str]) -> None:
    fake_pub = FakeCheckPublisher({"douyin": {"ok": True, "detail": "ok"},
                                   "tencent": {"ok": False, "detail": "cookie 已失效"}})
    fake = FakePipeline()

    class Stdin:
        def isatty(self) -> bool:
            return True

    with _patch_pipeline(fake, _GATE_CONFIG), \
         patch.object(cli, "SauPublisher", lambda config: fake_pub), \
         patch.object(cli.sys, "stdin", Stdin(), create=True), \
         patch("builtins.input", return_value="y"):
        cli.main(["601689", "--publish"])

    assert fake.calls == ["601689"]
    assert fake_pub.probed == [["douyin", "tencent"]]


def test_publish_gate_passes_silently_when_all_alive(capsys: pytest.CaptureFixture[str]) -> None:
    fake_pub = _ok_publisher()
    with _patch_pipeline(FakePipeline(), _GATE_CONFIG), \
         patch.object(cli, "SauPublisher", lambda config: fake_pub), \
         patch.object(cli.sys, "stdin", None, create=True):
        cli.main(["601689", "--publish"])

    assert "登录态均有效" in capsys.readouterr().out


def test_no_preflight_skips_the_probe() -> None:
    def _boom(config):
        raise AssertionError("--no-preflight 不应实例化 SauPublisher")

    with _patch_pipeline(FakePipeline(), _GATE_CONFIG), \
         patch.object(cli, "SauPublisher", _boom), \
         patch.object(cli.sys, "stdin", None, create=True):
        cli.main(["601689", "--publish", "--no-preflight"])


# ---------------------------------------------------------------------------
# --republish  补发
# ---------------------------------------------------------------------------

class FakeRepublishPipeline:
    def __init__(self) -> None:
        self.publisher = type("P", (), {"publish": lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("not set"))})()
        self.calls: list[dict[str, Any]] = []
        self.render_calls: list[str] = []
        self.rendered: dict[str, str] = {}

    def wire(self, report: dict[str, Any]) -> None:
        def publish(video, script, **kwargs):
            self.calls.append({"video": video, "script": script, **kwargs})
            return report
        self.publisher = type("P", (), {"publish": staticmethod(publish)})()

    def render_covers(self, script, stock_code, tag, stock_name="") -> dict[str, str]:
        self.render_calls.append(stock_code)
        return dict(self.rendered)


def _write_run(output_dir: Path, tag: str, *, failed: list[str], covers: bool = True) -> tuple[Path, dict[str, str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    video = output_dir / f"{tag}.mp4"
    video.write_bytes(b"0")
    cover_files: dict[str, str] = {}
    if covers:
        for size in ("portrait", "landscape"):
            cover = output_dir / f"{tag}.cover-{size}.png"
            cover.write_bytes(b"png")
            cover_files[size] = str(cover)
    (output_dir / f"{tag}.json").write_text(json.dumps({
        "stock_code": tag.split("_")[0], "stock_name": "拓普集团", "video_path": str(video),
        "approved_script": {"title": "拓普集团（601689）：看点", "turns": [{"speaker": "bull", "line": "看点"}]},
        "platform_videos": {}, "covers": cover_files,
        "publish": {"failed": [{"platform": p, "detail": "boom"} for p in failed]},
    }, ensure_ascii=False), encoding="utf-8")
    return video, cover_files


def _republish_config(output_dir: Path) -> dict[str, Any]:
    return {"output": {"dir": str(output_dir)},
            "publish": {"platforms": ["douyin", "kuaishou"], "schedule": ""}}


def test_republish_reuploads_only_last_failures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output_dir = tmp_path / "output"
    video, covers = _write_run(output_dir, "601689_20260916_120000", failed=["douyin"])
    fake = FakeRepublishPipeline()
    fake.wire({"succeeded": ["douyin"], "failed": [], "platforms": []})
    with patch.multiple(cli, Pipeline=lambda config: fake, load_config=lambda path=None: _republish_config(output_dir)):
        cli.main(["601689", "--republish"])

    call = fake.calls[0]
    assert Path(call["video"]) == video
    assert call["platforms"] == ["douyin"]
    assert call["covers"] == covers
    assert call["stock_code"] == "601689"
    assert "无需" not in capsys.readouterr().out


def test_republish_platforms_flag_overrides_last_failures(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    _write_run(output_dir, "601689_20260916_120000", failed=[])
    fake = FakeRepublishPipeline()
    fake.wire({"succeeded": ["kuaishou"], "failed": [], "platforms": []})
    with patch.multiple(cli, Pipeline=lambda config: fake, load_config=lambda path=None: _republish_config(output_dir)):
        cli.main(["601689", "--republish", "--platforms", "kuaishou"])

    assert fake.calls[0]["platforms"] == ["kuaishou"]


def test_republish_skips_when_nothing_failed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output_dir = tmp_path / "output"
    _write_run(output_dir, "601689_20260916_120000", failed=[])
    fake = FakeRepublishPipeline()
    fake.wire({"succeeded": [], "failed": [], "platforms": []})
    with patch.multiple(cli, Pipeline=lambda config: fake, load_config=lambda path=None: _republish_config(output_dir)):
        cli.main(["601689", "--republish"])

    assert fake.calls == []
    assert "无需补发" in capsys.readouterr().out


def test_republish_picks_the_newest_run(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    _write_run(output_dir, "601689_20260915_090000", failed=["kuaishou"])
    newest, _ = _write_run(output_dir, "601689_20260916_120000", failed=["douyin"])
    fake = FakeRepublishPipeline()
    fake.wire({"succeeded": ["douyin"], "failed": [], "platforms": []})
    with patch.multiple(cli, Pipeline=lambda config: fake, load_config=lambda path=None: _republish_config(output_dir)):
        cli.main(["601689", "--republish"])

    assert Path(fake.calls[0]["video"]) == newest
    assert fake.calls[0]["platforms"] == ["douyin"]


def test_republish_exits_nonzero_when_the_video_is_gone(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    with patch.multiple(cli, Pipeline=lambda config: FakeRepublishPipeline(),
                        load_config=lambda path=None: _republish_config(output_dir)):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["601689", "--republish"])

    assert excinfo.value.code == 1


def test_republish_requires_exactly_one_code(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    _write_run(output_dir, "601689_20260916_120000", failed=["douyin"])
    with patch.multiple(cli, Pipeline=lambda config: FakeRepublishPipeline(),
                        load_config=lambda path=None: _republish_config(output_dir)):
        with pytest.raises(SystemExit):
            cli.main(["601689", "600519", "--republish"])


def test_republish_still_exits_nonzero_when_it_fails_again(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    _write_run(output_dir, "601689_20260916_120000", failed=["douyin"])
    fake = FakeRepublishPipeline()
    fake.wire({"succeeded": [], "failed": [{"platform": "douyin", "detail": "again"}],
               "platforms": [{"platform": "douyin", "label": "抖音", "ok": False, "detail": "again",
                              "command": [], "attempts": 1}]})
    with patch.multiple(cli, Pipeline=lambda config: fake, load_config=lambda path=None: _republish_config(output_dir)):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["601689", "--republish"])

    assert excinfo.value.code == 1


# ---------------------------------------------------------------------------
# --publish-only  只发已生成的成片
# ---------------------------------------------------------------------------

def _write_unpublished(output_dir: Path, tag: str = "000980_20260917_135737") -> Path:
    """A run that rendered but never published: no covers, no publish report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    video = output_dir / f"{tag}.mp4"
    video.write_bytes(b"0")
    (output_dir / f"{tag}.json").write_text(json.dumps({
        "stock_code": "000980", "stock_name": "众泰汽车", "video_path": str(video),
        "approved_script": {"title": "众泰汽车（000980）：产能与订单的博弈", "turns": [{"speaker": "bull", "line": "看点"}]},
        "platform_videos": {"xiaohongshu": str(output_dir / f"{tag}.vertical.mp4")},
        "covers": {}, "publish": None,
    }, ensure_ascii=False), encoding="utf-8")
    (output_dir / f"{tag}.vertical.mp4").write_bytes(b"0")
    return video


def test_publish_only_renders_covers_the_original_run_never_made(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    video = _write_unpublished(output_dir)
    cover = output_dir / "000980_20260917_135737.cover-portrait.png"
    fake = FakeRepublishPipeline()
    fake.rendered = {"portrait": str(cover)}
    fake.wire({"succeeded": ["douyin"], "failed": [], "platforms": []})
    with patch.multiple(cli, Pipeline=lambda config: fake, load_config=lambda path=None: _republish_config(output_dir)):
        cli.main(["--publish-only", str(video)])

    assert fake.render_calls == ["000980"]
    assert fake.calls[0]["covers"] == {"portrait": str(cover)}
    assert fake.calls[0]["stock_code"] == "000980"
    assert fake.calls[0]["platform_videos"]["xiaohongshu"].endswith(".vertical.mp4")


def test_publish_only_records_the_result_so_republish_can_resume(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    video = _write_unpublished(output_dir)
    cover = output_dir / "000980_20260917_135737.cover-portrait.png"
    cover.write_bytes(b"png")
    fake = FakeRepublishPipeline()
    fake.rendered = {"portrait": str(cover)}
    fake.wire({"succeeded": ["douyin"], "failed": [{"platform": "bilibili", "detail": "boom"}],
               "platforms": [{"platform": "bilibili", "label": "B站", "ok": False, "detail": "boom",
                              "command": [], "attempts": 1}]})
    with patch.multiple(cli, Pipeline=lambda config: fake, load_config=lambda path=None: _republish_config(output_dir)):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--publish-only", str(video)])

    assert excinfo.value.code == 1
    saved = json.loads((output_dir / "000980_20260917_135737.json").read_text(encoding="utf-8"))
    assert [item["platform"] for item in saved["publish"]["failed"]] == ["bilibili"]
    assert saved["covers"] == {"portrait": str(cover)}


def test_publish_only_keeps_existing_covers(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    video, covers = _write_run(output_dir, "000980_20260917_135737", failed=[])
    fake = FakeRepublishPipeline()
    fake.wire({"succeeded": ["douyin"], "failed": [], "platforms": []})
    with patch.multiple(cli, Pipeline=lambda config: fake, load_config=lambda path=None: _republish_config(output_dir)):
        cli.main(["--publish-only", str(video)])

    assert fake.render_calls == []
    assert fake.calls[0]["covers"] == covers


def test_no_interactive_is_applied_after_the_config_loads(tmp_path: Path) -> None:
    """--no-interactive must not blow up on a `config` that has not been read yet."""
    output_dir = tmp_path / "output"
    _write_run(output_dir, "601689_20260916_120000", failed=[])
    fake = FakePipeline()
    with patch.multiple(cli, Pipeline=lambda config: fake, load_config=lambda path=None: _republish_config(output_dir)):
        cli.main(["601689", "--no-interactive", "--no-render"])

    assert fake.calls == ["601689"]


# ---------------------------------------------------------------------------
# Session keep-alive
# ---------------------------------------------------------------------------

class _StubKeepalivePublisher:
    """Records which platforms were heartbeaten and reports a fixed verdict."""

    # 子类改这个就能设定哪些平台「失效」。
    DEAD: tuple[str, ...] = ()

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.dead = list(self.DEAD)
        self.session_path = Path("output/.session_state.json")
        self.workdir = Path("third_party/social-auto-upload")
        self.seen: list[list[str]] = []

    def keepalive_platforms(self, platforms: list[str]) -> dict[str, dict[str, Any]]:
        self.seen.append(list(platforms))
        out: dict[str, dict[str, Any]] = {}
        for platform in platforms:
            ok = platform not in self.dead
            out[platform] = {
                "ok": ok,
                "detail": "alive" if ok else "dead: cookie 已失效（被跳转到登录页）",
                "action": "keepalive" if platform == "tencent" else "check",
                "checked_at": "2026-09-23T14:31:08+08:00",
                "last_ok": "2026-09-23T14:31:08+08:00" if ok else "2026-09-23T09:07:54+08:00",
            }
        return out


class _DeadTencentStub(_StubKeepalivePublisher):
    DEAD = ("tencent",)


def test_keepalive_covers_every_configured_platform_and_reports_dead_ones(capsys: Any) -> None:
    config = {"publish": {"platforms": ["tencent", "douyin"], "accounts": {"tencent": "default"}}}
    with patch.object(cli, "SauPublisher", _DeadTencentStub):
        code = cli._keepalive(config)

    assert code == 1
    out = capsys.readouterr().out
    assert "视频号(tencent)" in out and "keepalive" in out
    assert "sau tencent login --account default" in out   # 失效时的补救要写在脸上
    assert "output/.session_state.json" in out


def test_keepalive_exits_zero_when_every_platform_is_alive(capsys: Any) -> None:
    config = {"publish": {"platforms": ["tencent"]}}
    with patch.object(cli, "SauPublisher", _StubKeepalivePublisher):
        assert cli._keepalive(config) == 0

    assert "✓ 视频号(tencent)" in capsys.readouterr().out


def test_keepalive_without_platforms_fails_loudly(capsys: Any) -> None:
    with patch.object(cli, "SauPublisher", _StubKeepalivePublisher):
        assert cli._keepalive({"publish": {"platforms": []}}) == 1

    assert "未配置任何平台" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Subject picking (--auto-pick)
# ---------------------------------------------------------------------------

RANKING = [HotTopic(rank=1, code="000910", name="大亚圣象", mentions=12, hot_score=25),
           HotTopic(rank=2, code="605058", name="澳弘电子", mentions=4, hot_score=75)]


class _StubPicker:
    """Feeds a fixed ranking and remembers what was recorded."""

    max_runs = 2
    picks: list[Any] = []
    recorded: list[tuple[str, bool]] = []

    def __init__(self, config: dict[str, Any]) -> None:
        self.rotation_path = Path("output/.topic_rotation.json")

    def topics(self, date: str | None = None, top: int = 10) -> list[HotTopic]:
        return list(RANKING)

    def select(self, topics: list[HotTopic]) -> HotTopic | None:
        return self.picks[0] if self.picks else topics[1]

    def record(self, topic: HotTopic, ok: bool) -> dict[str, Any]:
        type(self).recorded.append((topic.code, ok))
        return {"published": ok, "runs": 1}


def _pick_args(**overrides: Any) -> argparse.Namespace:
    base = {"auto_pick": True, "topic_top": 10, "stock_codes": [], "watchlist": None, "name": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_auto_pick_fills_the_subject_for_the_run(capsys: Any) -> None:
    args = _pick_args()
    parser = argparse.ArgumentParser()
    with patch.object(cli, "TopicPicker", _StubPicker):
        topic = cli._auto_pick({"publish": {}}, args, parser)

    assert topic is not None and topic.code == "605058"
    assert args.stock_codes == ["605058"]      # 选题结果变成这次运行的唯一目标
    assert args.name == "澳弘电子"              # 名称直接带进 pipeline，省一次查询
    assert "热门榜" in capsys.readouterr().out


def test_auto_pick_is_inert_without_the_flag() -> None:
    args = _pick_args(auto_pick=False)
    assert cli._auto_pick({}, args, argparse.ArgumentParser()) is None
    assert args.stock_codes == []


def test_auto_pick_refuses_to_share_with_an_explicit_code() -> None:
    args = _pick_args(stock_codes=["601689"])
    with pytest.raises(SystemExit):
        cli._auto_pick({}, args, argparse.ArgumentParser())


def test_auto_pick_exits_when_the_ranking_is_spent(capsys: Any) -> None:
    args = _pick_args()
    with patch.object(cli, "TopicPicker", type("_Empty", (_StubPicker,), {"picks": [None]})):
        with pytest.raises(SystemExit) as excinfo:
            cli._auto_pick({"publish": {}}, args, argparse.ArgumentParser())

    assert excinfo.value.code == 1
    assert "都已发过" in capsys.readouterr().err


def test_record_pick_marks_success_and_failure(capsys: Any) -> None:
    _StubPicker.recorded = []
    with patch.object(cli, "TopicPicker", _StubPicker):
        cli._record_pick({}, RANKING[0], ok=True)
        cli._record_pick({}, RANKING[1], ok=False)

    assert _StubPicker.recorded == [("000910", True), ("605058", False)]
    out = capsys.readouterr().out
    assert "已发布" in out and "第 1 次" in out


# ---------------------------------------------------------------------------
# --publish-at：把发布时刻挪进「会话还活着的那一刻」
# ---------------------------------------------------------------------------

class _ConfigCapturingPipeline:
    """Records the config the CLI handed to the pipeline, then plays one run."""

    seen: dict[str, Any] = {}

    def __init__(self, config: dict[str, Any]) -> None:
        type(self).seen = config

    def run(self, code: str, stock_name: str | None = None, *, render: bool = True,
            preview: bool = False, publish: bool = False) -> dict[str, Any]:
        return _result(code)


def test_publish_at_is_resolved_and_written_into_the_config() -> None:
    _ConfigCapturingPipeline.seen = {}
    with patch.multiple(cli, Pipeline=_ConfigCapturingPipeline, load_config=lambda path=None: {"publish": {}}):
        cli.main(["601689", "--no-render", "--publish-at", "+5h"])

    # 三条发布路径（主流程 / --publish-only / --republish）都从 config 读 schedule，
    # 所以写回 config 才是唯一不会漏的接法。
    assert cli.parse_publish_at(_ConfigCapturingPipeline.seen["publish"]["schedule"]) \
        == _ConfigCapturingPipeline.seen["publish"]["schedule"]


def test_publish_at_rejects_a_time_inside_the_two_hour_lead(capsys: Any) -> None:
    with patch.multiple(cli, Pipeline=FakePipeline, load_config=lambda path=None: {"publish": {}}):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["601689", "--no-render", "--publish-at", "+30m"])

    assert excinfo.value.code == 2
    assert "2 小时" in capsys.readouterr().err


def test_publish_at_ignores_config_when_absent() -> None:
    _ConfigCapturingPipeline.seen = {}
    with patch.multiple(cli, Pipeline=_ConfigCapturingPipeline,
                        load_config=lambda path=None: {"publish": {"schedule": "2026-09-24 09:30"}}):
        cli.main(["601689", "--no-render"])

    # 没给 --publish-at 就保留配置里的值（显式配置优先于默认空）。
    assert _ConfigCapturingPipeline.seen["publish"]["schedule"] == "2026-09-24 09:30"


# ---------------------------------------------------------------------------
# 降级发布（--continue-anyway）与守护进程
# ---------------------------------------------------------------------------


def test_publish_gate_continues_when_continue_anyway_is_given(capsys: pytest.CaptureFixture[str]) -> None:
    """无人值守那一档：有平台掉线也照发其余平台，而不是整条放弃。

    2026-09-24 早班的实际损失就是这么来的 —— 视频号掉线导致「一个平台都没发」，
    而当时另外四个平台都是好的。
    """
    fake = FakePipeline()
    fake_pub = FakeCheckPublisher({"douyin": {"ok": True, "detail": "ok"},
                                   "tencent": {"ok": False, "detail": "cookie 已失效"}})
    with _patch_pipeline(fake, _GATE_CONFIG), \
         patch.object(cli, "SauPublisher", lambda config: fake_pub), \
         patch.object(cli.sys, "stdin", None, create=True):
        cli.main(["601689", "--publish", "--continue-anyway"])

    out = capsys.readouterr().out
    assert "--continue-anyway" in out
    assert "1/2" in out  # 明确报出只发了几个平台，别让人以为 5 个都发了
    assert "uv run sau tencent login --account default" in out
    assert fake.calls == ["601689"]  # 渲染照跑，没有整条退出


def test_publish_gate_does_not_block_or_prompt_on_a_timed_out_probe(capsys: pytest.CaptureFixture[str]) -> None:
    """探活超时不该拦下整条命令 —— 那是用「不知道」冒充「不能发」。

    这里**没有** ``--continue-anyway``：如果超时被当成失效，就会打印登录提示并
    在非交互环境直接退出（或交互地追问一句）。两个后果都不对：发布本身照试才是
    正确的默认，真发不出去会在发布阶段记进 sidecar，代价是一次上传而非一次渲染。
    """
    fake = FakePipeline()
    fake_pub = FakeCheckPublisher({"douyin": {"ok": True, "detail": "ok"},
                                   "tencent": {"ok": False, "timed_out": True, "detail": "探活超时"}})
    with _patch_pipeline(fake, _GATE_CONFIG), \
         patch.object(cli, "SauPublisher", lambda config: fake_pub), \
         patch.object(cli.sys, "stdin", None, create=True):
        cli.main(["601689", "--publish"])

    out = capsys.readouterr().out
    assert fake.calls == ["601689"]              # 没有 SystemExit，渲染照跑
    assert "探活超时" in out and "照发" in out
    assert "uv run sau tencent login" not in out  # 没催人重新登录


# ---------------------------------------------------------------------------
# 自动重新登录（--auto-login / --auto）
#
# 早班那次「视频号失效 → 整条不发」的下一步：让命令自己把登录补上。只有视频号
# 做得到（微信快捷登录，免扫码）；关键是登录之后必须**复核**，以及退不回时
# 仍然走原来那套（不静默把失效平台当健康平台发出去）。
# ---------------------------------------------------------------------------

class FakeAutoLoginPublisher:
    """预检 + 自动重新登录的可编程替身：第一次探活读 ``gate``，登录后复核读 ``after``。"""

    def __init__(self, gate: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]] | None = None,
                 *, login_ok: bool = True, attempted: bool = True, detail: str = "登录流程已跑完") -> None:
        self.gate = gate
        self.after = after if after is not None else {}
        self.login_ok = login_ok
        self.attempted = attempted
        self.detail = detail
        self.login_calls: list[list[str]] = []
        self.probed: list[list[str]] = []

    def check_platforms(self, platforms: list[str]) -> dict[str, dict[str, Any]]:
        self.probed.append(list(platforms))
        source = self.after if self.login_calls else self.gate
        return {p: dict(source.get(p, {"ok": True, "detail": "ok"})) for p in platforms}

    def auto_login_platforms(self, platforms: list[str]) -> dict[str, dict[str, Any]]:
        self.login_calls.append(list(platforms))
        return {p: {"platform": p, "ok": self.login_ok, "attempted": self.attempted,
                    "detail": self.detail} for p in platforms}


def test_auto_login_recovers_the_platform_and_publishing_continues(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakePipeline()
    fake_pub = FakeAutoLoginPublisher({"douyin": {"ok": True, "detail": "ok"},
                                       "tencent": {"ok": False, "detail": "cookie 已失效（页面跳转到登录页）"}},
                                      after={"tencent": {"ok": True, "detail": "valid"}})
    with _patch_pipeline(fake, _GATE_CONFIG), \
         patch.object(cli, "SauPublisher", lambda config: fake_pub), \
         patch.object(cli.sys, "stdin", None, create=True):
        cli.main(["601689", "--publish", "--auto"])

    out = capsys.readouterr().out
    assert fake_pub.login_calls == [["tencent"]]        # 只对做得到的平台动手
    assert "自动重新登录" in out and "2/2" in out
    assert fake.calls == ["601689"]                    # 渲染照跑，没有退出


def test_auto_login_flag_is_opt_in(capsys: pytest.CaptureFixture[str]) -> None:
    """没有 --auto-login 时行为不变：还是打印补救并退出（失败那次的老路径）。"""
    fake_pub = FakeAutoLoginPublisher({"douyin": {"ok": True, "detail": "ok"},
                                       "tencent": {"ok": False, "detail": "cookie 已失效"}})
    fake = FakePipeline()
    with _patch_pipeline(fake, _GATE_CONFIG), \
         patch.object(cli, "SauPublisher", lambda config: fake_pub), \
         patch.object(cli.sys, "stdin", None, create=True):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["601689", "--publish"])

    assert excinfo.value.code == 1
    assert fake_pub.login_calls == []
    assert fake.calls == []


def test_auto_login_still_blocks_when_the_recheck_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """登录动作成功 ≠ 会话可用：复核没过就必须照旧拦下，不能当它已经好了。"""
    fake_pub = FakeAutoLoginPublisher({"douyin": {"ok": True, "detail": "ok"},
                                       "tencent": {"ok": False, "detail": "cookie 已失效"}},
                                      after={"tencent": {"ok": False, "detail": "仍是登录页"}})
    fake = FakePipeline()
    with _patch_pipeline(fake, _GATE_CONFIG), \
         patch.object(cli, "SauPublisher", lambda config: fake_pub), \
         patch.object(cli.sys, "stdin", None, create=True):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["601689", "--publish", "--auto"])

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "自动重新登录" in out and "仍未恢复" in out
    assert "uv run sau tencent login --account default" in out
    assert fake.calls == []


def test_auto_login_explains_when_wechat_is_not_running(capsys: pytest.CaptureFixture[str]) -> None:
    """微信没跑 → 没尝试，不是「试了失败」；措辞要如实，并照旧给出人工补救。"""
    fake_pub = FakeAutoLoginPublisher({"douyin": {"ok": True, "detail": "ok"},
                                       "tencent": {"ok": False, "detail": "cookie 已失效"}},
                                      attempted=False, login_ok=False,
                                      detail="本机微信客户端未运行（快捷登录按钮不会出现）")
    fake = FakePipeline()
    with _patch_pipeline(fake, _GATE_CONFIG), \
         patch.object(cli, "SauPublisher", lambda config: fake_pub), \
         patch.object(cli.sys, "stdin", None, create=True):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["601689", "--publish", "--auto"])

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "本机微信客户端未运行" in out
    assert "uv run sau tencent login --account default" in out            # 该人工做的那步还在
    assert fake_pub.probed == [_GATE_CONFIG["publish"]["platforms"]]      # 没复核（没登录过）


def test_auto_login_reports_platforms_that_need_a_scan(capsys: pytest.CaptureFixture[str]) -> None:
    """抖音失效时不该被"自动登录"碰，但要明确告诉人它需要扫码。"""
    config = {"publish": {"platforms": ["douyin", "tencent"]}}
    fake_pub = FakeAutoLoginPublisher({"douyin": {"ok": False, "detail": "cookie 已失效"},
                                       "tencent": {"ok": False, "detail": "cookie 已失效"}},
                                      after={"tencent": {"ok": True, "detail": "valid"}})
    fake = FakePipeline()
    with _patch_pipeline(fake, config), \
         patch.object(cli, "SauPublisher", lambda config: fake_pub), \
         patch.object(cli.sys, "stdin", None, create=True):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["601689", "--publish", "--auto"])

    assert excinfo.value.code == 1        # 抖音仍失效 → 照旧拦下
    out = capsys.readouterr().out
    assert "需要人工扫码登录" in out and "抖音" in out
    assert fake_pub.login_calls == [["tencent"]]
    assert fake.calls == []


def test_daemon_status_prints_the_schedule(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    config = {"output": {"dir": str(tmp_path)},
              "daemon": {"windows": ["07:30-09:30", "18:30-21:00"]}}
    with patch.multiple(cli, load_config=lambda path=None: dict(config)), \
         patch.object(cli, "launchd_state", lambda: "launchd: 未加载"):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--daemon-status"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "07:30-09:30" in out and "18:30-21:00" in out
    assert "launchd: 未加载" in out


def test_daemon_reports_a_broken_window_instead_of_crashing(
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """窗口写错要在启动时讲人话，而不是等到了三点该发的时候才炸。"""
    config = {"output": {"dir": str(tmp_path)}, "daemon": {"windows": ["23:00-01:00"]}}
    with patch.multiple(cli, load_config=lambda path=None: dict(config)):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--daemon-status"])

    assert excinfo.value.code == 1
    assert "跨了午夜" in capsys.readouterr().err


def test_daemon_refuses_to_run_without_any_window(
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    config = {"output": {"dir": str(tmp_path)}, "daemon": {"windows": []}}
    with patch.multiple(cli, load_config=lambda path=None: dict(config)):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--daemon"])

    assert excinfo.value.code == 1
    assert "发布窗口" in capsys.readouterr().err
