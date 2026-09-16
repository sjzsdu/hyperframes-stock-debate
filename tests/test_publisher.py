"""Tests for the social-auto-upload publishing wrapper."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from stocktalk.modules.publisher import (
    BILIBILI_FINANCE_TID,
    PLATFORM_LABELS,
    PublishError,
    PublishMetadataGenerator,
    SauPublisher,
    SmsChallengeGuard,
    StreamAbort,
    check_environment,
    cover_sizes_for,
)


SCRIPT: dict[str, Any] = {
    "title": "贵州茅台（600519）：现金流里的护城河",
    "turns": [
        {"speaker": "bull", "line": "现金流和定价权都在", "beat": "生意本质", "visual": ["生意本质", "现金流"]},
        {"speaker": "bear", "line": "估值位置需要看清楚", "beat": "风险追问", "visual": ["估值"]},
    ],
    "disclaimers": ["本内容仅供学习交流，不构成投资建议"],
}


@dataclass
class FakeCompleted:
    returncode: int = 0
    stdout: str = "uploaded"
    stderr: str = ""


def fake_runner_factory(commands: list[list[str]]):
    def runner(command, cwd):
        commands.append([*command])
        return FakeCompleted()
    return runner


# ---------------------------------------------------------------------------
# Metadata generation
# ---------------------------------------------------------------------------

def test_metadata_uses_script_title_and_hook_lines() -> None:
    meta = PublishMetadataGenerator().generate(SCRIPT, "贵州茅台", "600519")
    assert meta.title == "贵州茅台（600519）：现金流里的护城河"
    assert "现金流和定价权都在" in meta.description
    assert "估值位置需要看清楚" in meta.description
    assert "不构成投资建议" in meta.full_description


def test_metadata_tags_merge_keywords_defaults_and_extra() -> None:
    gen = PublishMetadataGenerator({"publish": {"extra_tags": ["白酒"]}})
    meta = gen.generate(SCRIPT, "贵州茅台", "600519")
    assert "生意本质" in meta.tags and "现金流" in meta.tags  # from visual keywords
    assert "白酒" in meta.tags  # user extra
    assert "A股" in meta.tags  # defaults
    assert len(meta.tags) <= 8


def test_metadata_is_clamped_per_platform() -> None:
    gen = PublishMetadataGenerator()
    meta = gen.generate(SCRIPT, "贵州茅台", "600519")
    xhs = gen.for_platform(meta, "xiaohongshu")
    assert len(xhs.title) <= 20
    dy = gen.for_platform(meta, "douyin")
    assert len(dy.title) <= 30
    assert len(dy.tags) <= 4


def test_title_falls_back_when_script_has_none() -> None:
    meta = PublishMetadataGenerator().generate({"turns": []}, "平安银行", "000001")
    assert "平安银行" in meta.title and "000001" in meta.title


# ---------------------------------------------------------------------------
# Publisher command construction
# ---------------------------------------------------------------------------

def _publisher(tmp_path: Path, commands: list[list[str]]) -> SauPublisher:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    publisher = SauPublisher(
        {"publish": {"platforms": ["douyin", "bilibili", "kuaishou", "xiaohongshu", "tencent"],
                     "accounts": {"douyin": "acc-dy", "bilibili": "acc-bili"},
                     "headless": True, "preflight": False, "sau_dir": str(tmp_path / "nonexistent")}},
        runner=fake_runner_factory(commands),
        sau_bin="sau",
    )
    return publisher


def test_publish_builds_sau_commands_for_all_platforms(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    publisher = _publisher(tmp_path, commands)
    report = publisher.publish(tmp_path / "video.mp4", SCRIPT, "贵州茅台", "600519")

    assert [p["platform"] for p in report["platforms"]] == ["douyin", "bilibili", "kuaishou", "xiaohongshu", "tencent"]
    assert report["succeeded"] == ["douyin", "bilibili", "kuaishou", "xiaohongshu", "tencent"]
    assert len(commands) == 5

    douyin = commands[0]
    assert douyin[:3] == ["sau", "douyin", "upload-video"]
    assert "--account" in douyin and "acc-dy" in douyin
    assert "--file" in douyin and str(tmp_path / "video.mp4") in douyin
    assert "--headless" in douyin

    bili = next(c for c in commands if c[1] == "bilibili")
    assert "acc-bili" in bili
    assert bili[bili.index("--tid") + 1] == str(BILIBILI_FINANCE_TID)


def test_publish_clamps_title_for_xiaohongshu(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    publisher = _publisher(tmp_path, commands)
    long_script = {**SCRIPT, "title": "这个标题非常长" * 10}
    publisher.publish(tmp_path / "video.mp4", long_script, "贵州茅台", "600519")
    xhs = next(c for c in commands if c[1] == "xiaohongshu")
    title = xhs[xhs.index("--title") + 1]
    assert len(title) <= 20


def test_publish_forwards_schedule(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    publisher = _publisher(tmp_path, commands)
    publisher.publish(tmp_path / "video.mp4", SCRIPT, "贵州茅台", "600519", schedule="2026-09-16 19:30")
    assert "--schedule" in commands[0]
    assert commands[0][commands[0].index("--schedule") + 1] == "2026-09-16 19:30"


def test_one_platform_failure_does_not_block_others(tmp_path: Path) -> None:
    def flaky_runner(command, cwd):
        if command[1] == "kuaishou":
            return FakeCompleted(returncode=1, stdout="", stderr="cookie expired")
        return FakeCompleted()

    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    publisher = SauPublisher(
        {"publish": {"platforms": ["douyin", "kuaishou", "bilibili"], "preflight": False}},
        runner=flaky_runner, sau_bin="sau",
    )
    report = publisher.publish(video, SCRIPT, "贵州茅台", "600519")
    assert report["succeeded"] == ["douyin", "bilibili"]
    assert report["failed"] == [{"platform": "kuaishou", "detail": "cookie expired"}]


def test_publish_requires_video_file(tmp_path: Path) -> None:
    publisher = SauPublisher({}, runner=fake_runner_factory([]), sau_bin="sau")
    with pytest.raises(PublishError, match="video not found"):
        publisher.publish(tmp_path / "missing.mp4", SCRIPT, "贵州茅台", "600519")


def test_publish_requires_configured_platforms(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    publisher = SauPublisher({"publish": {"platforms": []}}, runner=fake_runner_factory([]), sau_bin="sau")
    with pytest.raises(PublishError, match="no publish platforms"):
        publisher.publish(video, SCRIPT, "贵州茅台", "600519")


def test_unsupported_platform_is_reported_not_fatal(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    publisher = SauPublisher({"publish": {"platforms": ["douyin", "weibo"], "preflight": False}}, runner=fake_runner_factory([]), sau_bin="sau")
    report = publisher.publish(video, SCRIPT, "贵州茅台", "600519")
    assert report["succeeded"] == ["douyin"]
    assert report["failed"][0]["platform"] == "weibo"


def test_require_sau_reports_actionable_error(tmp_path: Path) -> None:
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path / "none")}}, runner=fake_runner_factory([]))
    with pytest.raises(PublishError, match="uv sync"):
        publisher._require_sau()


def test_timeout_is_reported_per_platform(tmp_path: Path) -> None:
    def slow_runner(command, cwd):
        raise subprocess.TimeoutExpired(cmd=command, timeout=900)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    publisher = SauPublisher({"publish": {"platforms": ["douyin"], "preflight": False}}, runner=slow_runner, sau_bin="sau")
    report = publisher.publish(video, SCRIPT, "贵州茅台", "600519")
    assert not report["succeeded"]
    assert "timed out" in report["failed"][0]["detail"]


def test_preflight_skips_platforms_with_stale_cookies(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def runner(command, cwd):
        commands.append([*command])
        if command[1:3] == ["kuaishou", "check"]:
            return FakeCompleted(returncode=1, stdout="cookie expired")
        return FakeCompleted()

    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    publisher = SauPublisher(
        {"publish": {"platforms": ["douyin", "kuaishou"], "accounts": {"kuaishou": "ks-acc"}}},
        runner=runner, sau_bin="sau",
    )
    report = publisher.publish(video, SCRIPT, "贵州茅台", "600519")

    assert report["succeeded"] == ["douyin"]
    failed = report["failed"]
    assert len(failed) == 1 and failed[0]["platform"] == "kuaishou"
    assert "预检" in failed[0]["detail"]
    # The stale platform is checked but never uploaded to.
    uploads = [c for c in commands if "upload-video" in c]
    assert [c[1] for c in uploads] == ["douyin"]
    checks = [c for c in commands if "check" in c]
    assert ["sau", "kuaishou", "check", "--account", "ks-acc"] in checks
    assert all(c[1] != "bilibili" for c in checks)  # not configured → not checked


def test_preflight_bilibili_command_has_no_headless_flag(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    publisher = SauPublisher(
        {"publish": {"platforms": ["bilibili"], "preflight": False}},
        runner=fake_runner_factory(commands), sau_bin="sau",
    )
    report = publisher.publish(video, SCRIPT, "贵州茅台", "600519")
    assert report["succeeded"] == ["bilibili"]
    bili = commands[0]
    assert "--headless" not in bili and "--headed" not in bili
    assert bili[bili.index("--tid") + 1] == str(BILIBILI_FINANCE_TID)


def test_all_supported_platforms_have_labels_and_limits() -> None:
    assert set(PLATFORM_LABELS) == {"douyin", "bilibili", "kuaishou", "xiaohongshu", "tencent"}


# ---------------------------------------------------------------------------
# In-run retry of failed platforms
# ---------------------------------------------------------------------------

def _video(tmp_path: Path) -> Path:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return video


def test_failed_platform_is_retried_within_the_same_run(tmp_path: Path) -> None:
    attempts = {"kuaishou": 0, "douyin": 0}

    def flaky(command, cwd):
        platform = command[1]
        attempts[platform] += 1
        if platform == "kuaishou" and attempts[platform] == 1:
            return FakeCompleted(returncode=1, stdout="", stderr="browser crashed")
        return FakeCompleted()

    slept: list[float] = []
    publisher = SauPublisher(
        {"publish": {"platforms": ["douyin", "kuaishou"], "preflight": False,
                     "retries": 1, "retry_delay_seconds": 5}},
        runner=flaky, sau_bin="sau", sleep=slept.append,
    )
    report = publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519")

    assert report["succeeded"] == ["douyin", "kuaishou"]
    assert report["failed"] == []
    assert attempts == {"douyin": 1, "kuaishou": 2}  # healthy platform not re-uploaded
    assert slept == [5.0]
    assert [p["attempts"] for p in report["platforms"]] == [1, 2]


def test_retries_are_capped(tmp_path: Path) -> None:
    calls = {"n": 0}

    def always_fails(command, cwd):
        calls["n"] += 1
        return FakeCompleted(returncode=1, stdout="", stderr="still broken")

    publisher = SauPublisher(
        {"publish": {"platforms": ["douyin"], "preflight": False, "retries": 2, "retry_delay_seconds": 0}},
        runner=always_fails, sau_bin="sau", sleep=lambda _: None,
    )
    report = publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519")

    assert calls["n"] == 3  # 1 initial + 2 retries
    assert report["failed"] == [{"platform": "douyin", "detail": "still broken"}]
    assert report["platforms"][0]["attempts"] == 3


def test_sms_abort_is_not_retried(tmp_path: Path) -> None:
    """The SMS window is expensive; retrying it can never succeed on its own."""
    calls: dict[str, int] = {}

    def runner(command, cwd):
        calls[command[1]] = calls.get(command[1], 0) + 1
        if command[1] == "douyin":
            raise StreamAbort("抖音要求短信验证码，等待 150s 仍未收到")
        return FakeCompleted()

    publisher = SauPublisher(
        {"publish": {"platforms": ["douyin", "kuaishou"], "preflight": False,
                     "retries": 2, "retry_delay_seconds": 0}},
        runner=runner, sau_bin="sau", sleep=lambda _: None,
    )
    report = publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519")

    assert calls == {"douyin": 1, "kuaishou": 1}
    assert report["succeeded"] == ["kuaishou"]
    assert report["platforms"][0]["attempts"] == 1
    assert "短信验证码" in report["failed"][0]["detail"]


def test_preflight_failure_is_not_retried(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def runner(command, cwd):
        commands.append([*command])
        if command[1:3] == ["kuaishou", "check"]:
            return FakeCompleted(returncode=1, stdout="cookie expired")
        return FakeCompleted()

    slept: list[float] = []
    publisher = SauPublisher(
        {"publish": {"platforms": ["douyin", "kuaishou"], "retries": 2, "retry_delay_seconds": 1}},
        runner=runner, sau_bin="sau", sleep=slept.append,
    )
    report = publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519")

    uploads = [c[1] for c in commands if "upload-video" in c]
    assert uploads == ["douyin"]
    assert slept == []  # a stale cookie is fixed by logging in, not by retrying
    assert report["failed"][0]["platform"] == "kuaishou"


# ---------------------------------------------------------------------------
# Douyin SMS challenge: fail fast instead of hanging until the timeout
# ---------------------------------------------------------------------------

def test_sms_guard_aborts_when_no_code_arrives(tmp_path: Path) -> None:
    now = [0.0]
    guard = SmsChallengeGuard(tmp_path / "verify_code.txt", 100, clock=lambda: now[0])

    assert guard.feed("2026-09-16 11:17:55 | WARNING: 📱 检测到短信验证码弹窗") is None
    assert guard.armed
    now[0] = 50
    assert guard.feed("") is None  # idle ticks keep the window open
    now[0] = 101
    reason = guard.feed("")
    assert reason and "短信验证码" in reason


def test_sms_guard_window_is_refreshed_while_a_code_file_exists(tmp_path: Path) -> None:
    code_file = tmp_path / "verify_code.txt"
    now = [0.0]
    guard = SmsChallengeGuard(code_file, 100, clock=lambda: now[0])
    guard.feed("⏳ 等待验证码输入；可在交互终端直接输入")

    code_file.write_text("123456", encoding="utf-8")
    now[0] = 150  # a code arriving after the deadline still rescues the run
    assert guard.feed("") is None
    now[0] = 240
    assert guard.feed("") is None

    code_file.unlink()  # sau consumed the code ("验证码文件已清理")
    now[0] = 300
    assert guard.feed("") is None
    now[0] = 345  # no new code → give up and report
    assert guard.feed("") is not None


def test_sms_guard_clears_after_the_code_is_accepted(tmp_path: Path) -> None:
    now = [0.0]
    guard = SmsChallengeGuard(tmp_path / "verify_code.txt", 10, clock=lambda: now[0])
    guard.feed("📱 检测到短信验证码弹窗")
    guard.feed("✍️ 已获取验证码，准备填入: 123456")
    assert not guard.armed

    now[0] = 999
    assert guard.feed("") is None  # a resolved challenge never aborts the run


def test_sms_guard_is_attached_to_uploads_only(tmp_path: Path) -> None:
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path)}})
    assert publisher._make_guard(["sau", "douyin", "check", "--account", "default"]) is None
    assert publisher._make_guard(["sau", "douyin", "upload-video", "--file", "x.mp4"]) is not None


def test_streaming_runner_aborts_a_stuck_sms_upload(tmp_path: Path) -> None:
    """End-to-end check of the streaming runner: marker seen → child killed."""
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path), "verify_code_wait_seconds": 1}})
    command = [sys.executable, "-u", "-c",
               "import sys, time; print('📱 检测到短信验证码弹窗', flush=True); time.sleep(30)",
               "upload-video"]

    with pytest.raises(StreamAbort, match="短信验证码"):
        publisher._run_subprocess(command, tmp_path)


def test_streaming_runner_merges_output_and_enforces_timeout(tmp_path: Path) -> None:
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path), "timeout_seconds": 1,
                                          "verify_code_wait_seconds": 0}})
    completed = publisher._run_subprocess(
        [sys.executable, "-u", "-c", "print('sau upload-video done')", "upload-video"], tmp_path,
    )
    assert completed.returncode == 0
    assert "sau upload-video done" in completed.stdout

    with pytest.raises(subprocess.TimeoutExpired):
        publisher._run_subprocess(
            [sys.executable, "-u", "-c", "import time; time.sleep(30)", "upload-video"], tmp_path,
        )


# ---------------------------------------------------------------------------
# Cover art plumbing
# ---------------------------------------------------------------------------

def test_cover_sizes_follow_configured_platforms() -> None:
    assert cover_sizes_for(["douyin"]) == ["portrait", "landscape"]
    assert cover_sizes_for(["bilibili"]) == ["wide"]
    assert cover_sizes_for(["kuaishou", "xiaohongshu"]) == ["portrait"]
    assert cover_sizes_for(["nope"]) == []


def _video(tmp_path: Path) -> Path:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0")
    return video


def _cover(tmp_path: Path, name: str) -> Path:
    cover = tmp_path / name
    cover.write_bytes(b"png")
    return cover


def test_publish_passes_each_platform_its_own_covers(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path), "preflight": False, "platforms": []}},
                             runner=fake_runner_factory(commands), sau_bin="sau")
    covers = {"portrait": _cover(tmp_path, "p.png"), "landscape": _cover(tmp_path, "l.png"),
              "wide": _cover(tmp_path, "w.png")}

    publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519",
                      platforms=["douyin", "bilibili", "tencent", "kuaishou"], covers=covers)

    by_platform = {cmd[1]: cmd for cmd in commands}  # cmd = [sau, <platform>, upload-video, ...]
    douyin = by_platform["douyin"]
    assert str(covers["portrait"].resolve()) in douyin
    assert str(covers["landscape"].resolve()) in douyin
    assert "--thumbnail-landscape" not in by_platform["bilibili"]
    assert str(covers["wide"].resolve()) in by_platform["bilibili"]
    assert "--thumbnail-portrait" in by_platform["tencent"]
    assert "--thumbnail-landscape" in by_platform["tencent"]
    assert str(covers["portrait"].resolve()) in by_platform["kuaishou"]


def test_publish_skips_a_missing_cover_file(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path), "preflight": False, "platforms": []}},
                             runner=fake_runner_factory(commands), sau_bin="sau")
    ghost = tmp_path / "missing.png"

    publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519",
                      platforms=["douyin"], covers={"portrait": ghost, "landscape": _cover(tmp_path, "l.png")})

    (command,) = commands
    assert "--thumbnail" not in command
    assert "--thumbnail-landscape" in command


def test_a_broken_platform_never_blocks_the_rest(tmp_path: Path) -> None:
    """The isolation backstop: an unexpected exception stays on its platform."""
    commands: list[list[str]] = []
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path), "preflight": False, "platforms": []}},
                             runner=fake_runner_factory(commands), sau_bin="sau")
    exploded: list[str] = []
    real_publish_one = publisher._publish_one

    def flaky(platform, video, metadata, schedule, covers=None):
        if platform == "douyin":
            exploded.append(platform)
            raise RuntimeError("boom")
        return real_publish_one(platform, video, metadata, schedule, covers)

    publisher._publish_one = flaky  # type: ignore[method-assign]
    report = publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519",
                               platforms=["douyin", "kuaishou"])

    assert exploded == ["douyin"]
    statuses = {item["platform"]: item["ok"] for item in report["platforms"]}
    assert statuses == {"douyin": False, "kuaishou": True}
    assert "boom" in next(item["detail"] for item in report["platforms"] if item["platform"] == "douyin")


def test_a_broken_preflight_does_not_block_the_rest(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path), "preflight": True, "platforms": []}},
                             runner=fake_runner_factory(commands), sau_bin="sau")
    publisher.check_platforms = lambda platforms: (_ for _ in ()).throw(RuntimeError("probe exploded"))  # type: ignore[method-assign]

    report = publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519", platforms=["kuaishou"])

    assert report["succeeded"] == ["kuaishou"]


def test_environment_check_reports_every_tool(tmp_path: Path) -> None:
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path)}})
    checks = check_environment({"publish": {"sau_dir": str(tmp_path)}}, publisher)

    names = {item["name"] for item in checks}
    assert names == {"sau", "npx", "chrome"}
    sau = next(item for item in checks if item["name"] == "sau")
    assert not sau["ok"] and sau["required"]
    assert all(item["required"] is False for item in checks if item["name"] != "sau")
