"""谈股论金 CLI 测试：批量目标、平台覆盖、退出码。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from stocktalk import cli
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
    with _patch_pipeline(fake), patch.object(cli.sys, "stdin", None, create=True):
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
    with _patch_pipeline(fake):
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
    def __init__(self, results: dict[str, dict[str, Any]]) -> None:
        self.results = results

    def check_platforms(self, platforms: list[str]) -> dict[str, dict[str, Any]]:
        return {p: self.results[p] for p in platforms}


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


# ---------------------------------------------------------------------------
# --republish  补发
# ---------------------------------------------------------------------------

class FakeRepublishPipeline:
    def __init__(self) -> None:
        self.publisher = type("P", (), {"publish": lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("not set"))})()
        self.calls: list[dict[str, Any]] = []

    def wire(self, report: dict[str, Any]) -> None:
        def publish(video, script, **kwargs):
            self.calls.append({"video": video, "script": script, **kwargs})
            return report
        self.publisher = type("P", (), {"publish": staticmethod(publish)})()


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
