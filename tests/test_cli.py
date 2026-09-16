"""Tests for the StockTalk CLI: batch targets, platform override, exit codes."""

from __future__ import annotations

import argparse
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
