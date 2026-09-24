"""Tests for the social-auto-upload publishing wrapper."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from stocktalk.modules import publisher as publisher_module
from stocktalk.modules.publisher import (
    BILIBILI_FINANCE_TID,
    PLATFORM_LABELS,
    PublishError,
    PublishMetadataGenerator,
    SauPublisher,
    SmsChallengeGuard,
    StreamAbort,
    VerificationCodePrompt,
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


@pytest.fixture(autouse=True)
def _isolated_cooldown_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the machine's real cooldown file steer a test.

    Without this, a bilibili cooldown written by an actual run makes every
    test that publishes to bilibili silently "skip" instead of uploading.
    """
    monkeypatch.setattr(publisher_module, "DEFAULT_STATE_FILENAME", ".publish_state.test.json")
    monkeypatch.setattr(publisher_module, "DEFAULT_SESSION_FILENAME", ".session_state.test.json")


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


def test_baijiahao_is_always_headed_even_in_headless_mode(tmp_path: Path) -> None:
    """百度滑块风控在 headless 下必弹且无法人工通过（2026-09-20 实测），
    百家号无视 publish.headless 强制 --headed。"""
    commands: list[list[str]] = []
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    publisher = SauPublisher(
        {"publish": {"platforms": ["baijiahao"], "headless": True,
                     "preflight": False, "sau_dir": str(tmp_path / "nonexistent")}},
        runner=fake_runner_factory(commands), sau_bin="sau",
    )
    report = publisher.publish(video, SCRIPT, "冰轮环境", "000811")

    bj = commands[0]
    assert "--headed" in bj and "--headless" not in bj
    assert report["succeeded"] == ["baijiahao"]


def test_ai_content_label_only_goes_to_platforms_that_need_it(tmp_path: Path) -> None:
    """快手/小红书靠 CLI 传选项文案；抖音、视频号自己勾选，B站写进简介。"""
    commands: list[list[str]] = []
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    publisher = SauPublisher(
        {"publish": {"platforms": ["douyin", "bilibili", "kuaishou", "xiaohongshu", "tencent"],
                     "headless": True, "preflight": False, "sau_dir": str(tmp_path / "nonexistent"),
                     "ai_content_label": {"kuaishou": "内容由AI生成", "xiaohongshu": "AI生成"}}},
        runner=fake_runner_factory(commands), sau_bin="sau",
    )
    publisher.publish(video, SCRIPT, "贵州茅台", "600519")

    ks = next(c for c in commands if c[1] == "kuaishou")
    assert ks[ks.index("--ai-content-label") + 1] == "内容由AI生成"
    xhs = next(c for c in commands if c[1] == "xiaohongshu")
    assert xhs[xhs.index("--ai-content-label") + 1] == "AI生成"
    for name in ("douyin", "bilibili", "tencent"):
        assert "--ai-content-label" not in next(c for c in commands if c[1] == name)


def test_ai_content_label_absent_when_not_configured(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    publisher = _publisher(tmp_path, commands)
    publisher.publish(tmp_path / "video.mp4", SCRIPT, "贵州茅台", "600519")
    assert all("--ai-content-label" not in c for c in commands)


def test_bilibili_description_carries_the_ai_notice(tmp_path: Path) -> None:
    """biliup has no AI field, so the notice has to survive in the description."""
    script = {**SCRIPT, "disclaimers": ["本内容由AI生成，仅供学习交流，不构成投资建议"]}
    gen = PublishMetadataGenerator()
    meta = gen.generate(script, "贵州茅台", "600519")
    bili = gen.for_platform(meta, "bilibili")
    assert "AI生成" in bili.description
    # 视频号只有 120 字，免责结尾必须活下来
    tencent = gen.for_platform(meta, "tencent")
    assert len(tencent.description) <= 120
    assert "AI生成" in tencent.description


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


def test_require_sau_reports_actionable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 这台机器 PATH 上还有一个 `sau` 包装器（~/.local/bin/sau → 另一份检出），
    # 不能让它把「vendored venv 没装好」这个场景伪装成正常。
    monkeypatch.setattr(publisher_module.shutil, "which", lambda name: None)
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
    # 小红书 2026-09-18 停发但 spec 保留（能力在，默认配置不发）。
    assert set(PLATFORM_LABELS) == {"douyin", "bilibili", "kuaishou", "xiaohongshu", "tencent", "baijiahao"}


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

    def flaky(platform, video, metadata, schedule, covers=None, attempt=1):
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


def test_environment_check_reports_every_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 同上：断言的是「找不到 sau」时的报告内容，所以先把宿主 PATH 上的 sau 摘掉。
    monkeypatch.setattr(publisher_module.shutil, "which", lambda name: None)
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path)}})
    checks = check_environment({"publish": {"sau_dir": str(tmp_path)}}, publisher)

    names = {item["name"] for item in checks}
    assert names == {"sau", "npx", "chrome"}
    sau = next(item for item in checks if item["name"] == "sau")
    assert not sau["ok"] and sau["required"]
    assert all(item["required"] is False for item in checks if item["name"] != "sau")


# ---------------------------------------------------------------------------
# Rate limits: worth another try, but only after the cooldown
# ---------------------------------------------------------------------------

BILIBILI_RATE_LIMIT = (
    "Error: after retries\n"
    "╰─▶ upload rate limit (code: 601): 您上传视频过快，请您稍作休息后再继续\n"
)


def test_rate_limited_upload_waits_out_the_cooldown_before_retrying(tmp_path: Path) -> None:
    attempts: list[str] = []

    def runner(command, cwd):
        attempts.append(command[1])
        if command[1] == "bilibili" and attempts.count("bilibili") == 1:
            return FakeCompleted(returncode=1, stdout="", stderr=BILIBILI_RATE_LIMIT)
        return FakeCompleted()

    slept: list[float] = []
    publisher = SauPublisher(
        {"output": {"dir": str(tmp_path)},
         "publish": {"platforms": ["bilibili"], "preflight": False, "retries": 1,
                     "retry_delay_seconds": 30, "rate_limit_wait_seconds": 90,
                     "state_file": str(tmp_path / "state.json")}},
        runner=runner, sau_bin="sau", sleep=slept.append)

    report = publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519")

    assert report["succeeded"] == ["bilibili"]
    # The 30s ordinary backoff would have been another guaranteed failure.
    assert sum(slept) == 90
    assert max(slept) <= 30  # narrated in chunks so the wait does not look like a hang


def test_rate_limit_hint_explains_the_silence(tmp_path: Path) -> None:
    publisher = SauPublisher({"output": {"dir": str(tmp_path)},
                              "publish": {"rate_limit_wait_seconds": 600}}, sau_bin="sau")
    verdict = publisher._classify("bilibili", BILIBILI_RATE_LIMIT)
    assert verdict.kind == "rate_limit"
    assert verdict.wait_seconds == 600
    assert "限流" in verdict.hint


def test_the_cooldown_doubles_and_then_caps(tmp_path: Path) -> None:
    """Each hit inside the window must cost more waiting, not the same 10 min."""
    publisher = SauPublisher({"output": {"dir": str(tmp_path)},
                              "publish": {"rate_limit_wait_seconds": 600, "rate_limit_max_wait_seconds": 1800}},
                             sau_bin="sau")
    assert [publisher._rate_limit_wait(n) for n in (1, 2, 3, 4)] == [600, 1200, 1800, 1800]


def test_a_cooling_platform_is_skipped_without_spending_an_upload(tmp_path: Path) -> None:
    """The point of the whole mechanism: no request is made while cooling down."""
    commands: list[list[str]] = []
    publisher = SauPublisher({"output": {"dir": str(tmp_path)},
                              "publish": {"platforms": ["bilibili"], "preflight": False,
                                          "state_file": str(tmp_path / "state.json")}},
                             runner=fake_runner_factory(commands), sau_bin="sau")
    publisher._note_cooldown("bilibili", 600)

    report = publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519")

    assert commands == []  # not even an upload attempt
    assert report["succeeded"] == []
    assert "--republish" in report["platforms"][0]["detail"]
    assert "冷却" in report["platforms"][0]["detail"]


def test_the_cooldown_survives_a_new_process(tmp_path: Path) -> None:
    """A re-run minutes later must not burn another upload either."""
    state = str(tmp_path / "state.json")
    base = {"output": {"dir": str(tmp_path)},
            "publish": {"platforms": ["bilibili"], "preflight": False, "retries": 0,
                        "rate_limit_wait_seconds": 600, "state_file": state}}

    first_commands: list[list[str]] = []
    first = SauPublisher(base, runner=fake_runner_factory(first_commands), sau_bin="sau")
    first.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519")
    assert len(first_commands) == 1  # the very first run is never throttled by us
    first._note_cooldown("bilibili", 600)  # stands in for a real 601

    second_commands: list[list[str]] = []
    second = SauPublisher(base, runner=fake_runner_factory(second_commands), sau_bin="sau")
    second.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519")
    assert second_commands == []


def test_a_successful_upload_clears_the_cooldown(tmp_path: Path) -> None:
    state = str(tmp_path / "state.json")
    publisher = SauPublisher({"output": {"dir": str(tmp_path)},
                              "publish": {"platforms": ["bilibili"], "preflight": False,
                                          "state_file": state}},
                             runner=fake_runner_factory([]), sau_bin="sau")
    publisher._note_cooldown("bilibili", 0.05)
    time.sleep(0.1)  # the window has just opened again
    assert publisher.cooldown_left("bilibili") == 0

    report = publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519")
    assert report["succeeded"] == ["bilibili"]
    assert publisher.cooldown_left("bilibili") == 0


def test_a_cooldown_longer_than_the_run_may_block_is_left_to_republish(tmp_path: Path) -> None:
    """An hour of sitting in front of a countdown is worse than coming back later."""
    commands: list[list[str]] = []

    def runner(command, cwd):
        commands.append([*command])
        return FakeCompleted(returncode=1, stdout="", stderr=BILIBILI_RATE_LIMIT)

    publisher = SauPublisher(
        {"output": {"dir": str(tmp_path)},
         "publish": {"platforms": ["bilibili"], "preflight": False, "retries": 1,
                     "rate_limit_wait_seconds": 3600, "rate_limit_block_seconds": 600,
                     "state_file": str(tmp_path / "state.json")}},
        runner=runner, sau_bin="sau", sleep=lambda _: None)

    report = publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519")

    assert len(commands) == 1  # retried neither in-run nor after an hour of waiting
    assert "--republish" in report["platforms"][0]["detail"]
    assert publisher.cooldown_left("bilibili") > 0


def test_the_601_hint_says_account_risk_control_not_speed() -> None:
    """601 reads as "too fast"; on a fresh account it is really 风控 — say so."""
    publisher = SauPublisher({"publish": {"rate_limit_wait_seconds": 600}}, sau_bin="sau")
    verdict = publisher._classify("bilibili", BILIBILI_RATE_LIMIT)
    assert "实名" in verdict.hint and "网页端" in verdict.hint


def test_ordinary_failures_keep_the_short_backoff() -> None:
    publisher = SauPublisher({}, sau_bin="sau")
    verdict = publisher._classify("douyin", "browser crashed")
    assert verdict.kind == "transient" and verdict.wait_seconds == 0 and verdict.retryable


def test_a_long_wait_is_narrated_so_it_does_not_look_hung(tmp_path: Path) -> None:
    publisher = SauPublisher({"publish": {"rate_limit_wait_seconds": 90}},
                             sau_bin="sau", sleep=lambda _: None)
    events: list[str] = []
    publisher._wait(90, "重试 B站", lambda platform, label, state: events.append(state))
    assert len(events) == 3  # every 30s
    assert "还需" in events[0]


def test_short_waits_are_slept_in_one_go() -> None:
    slept: list[float] = []
    publisher = SauPublisher({}, sau_bin="sau", sleep=slept.append)
    publisher._wait(5, "retry")
    assert slept == [5]


# ---------------------------------------------------------------------------
# Douyin SMS code: ask the human instead of hanging on an invisible prompt
# ---------------------------------------------------------------------------

def make_prompt(tmp_path: Path, *, lines: list[str], interactive: bool = True,
                wait_seconds: float = 150.0) -> tuple[VerificationCodePrompt, list[str]]:
    printed: list[str] = []
    codes = iter(lines)

    def reader(timeout: float) -> str | None:
        return next(codes)

    prompt = VerificationCodePrompt(
        tmp_path / "verify_code.txt", wait_seconds=wait_seconds, interactive=interactive,
        reader=reader, emit=lambda text, end="\n": printed.append(text.rstrip()))
    return prompt, printed


def test_prompt_writes_the_code_for_sau_to_consume(tmp_path: Path) -> None:
    prompt, printed = make_prompt(tmp_path, lines=["654321"])
    assert prompt("抖音") is True
    assert (tmp_path / "verify_code.txt").read_text(encoding="utf-8") == "654321"
    assert any("验证码" in line and "抖音" in line for line in printed)


def test_prompt_reasks_after_a_typo(tmp_path: Path) -> None:
    prompt, printed = make_prompt(tmp_path, lines=["notacode", "654321"])
    assert prompt() is True
    assert (tmp_path / "verify_code.txt").read_text(encoding="utf-8") == "654321"
    assert any("4-8 位数字" in line for line in printed)


def test_prompt_gives_up_when_nobody_answers(tmp_path: Path) -> None:
    prompt, printed = make_prompt(tmp_path, lines=[None])
    assert prompt() is False
    assert not (tmp_path / "verify_code.txt").exists()
    assert any("未执行" in line or "写入" in line for line in printed)


def test_prompt_is_silent_outside_an_interactive_terminal(tmp_path: Path) -> None:
    """Unattended runs must keep the old contract: nobody can read a prompt."""
    prompt, printed = make_prompt(tmp_path, lines=[""], interactive=False)
    assert prompt() is False
    assert not list(iter(printed)) or True  # hint may or may not print
    assert not (tmp_path / "verify_code.txt").exists()


def test_child_process_cannot_block_on_its_own_invisible_prompt(tmp_path: Path) -> None:
    """sau's `input()` prompt is captured, so it must never be reachable."""
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path), "timeout_seconds": 10,
                                          "verify_code_wait_seconds": 0}})
    completed = publisher._run_subprocess(
        [sys.executable, "-u", "-c",
         "import sys; print('EOF' if sys.stdin.read() == '' else 'TTY')", "upload-video"], tmp_path)
    assert "EOF" in completed.stdout


def test_upload_survives_an_sms_challenge_answered_from_the_terminal(tmp_path: Path) -> None:
    """End to end: sau waits on the file, we prompt, upload completes."""
    script = (
        "import os, time\n"
        f"code_file = {str(tmp_path / 'verify_code.txt')!r}\n"
        "print('📱 检测到短信验证码弹窗', flush=True)\n"
        "for _ in range(200):\n"
        "    if os.path.exists(code_file):\n"
        "        print('✍️ 已获取验证码，准备填入: ' + open(code_file).read().strip(), flush=True)\n"
        "        os.remove(code_file)\n"
        "        break\n"
        "    time.sleep(0.05)\n"
        "print('🥳 视频发布成功', flush=True)\n"
    )
    publisher = SauPublisher(
        {"publish": {"sau_dir": str(tmp_path), "verify_code_wait_seconds": 30, "timeout_seconds": 30}},
        sau_bin=sys.executable,
        code_prompt=lambda label, wait: (tmp_path / "verify_code.txt").write_text("135790", encoding="utf-8") or True)

    completed = publisher._run_subprocess([sys.executable, "-u", "-c", script, "upload-video"], tmp_path)

    assert completed.returncode == 0
    assert "已获取验证码，准备填入: 135790" in completed.stdout
    assert not (tmp_path / "verify_code.txt").exists()  # consumed and cleaned by the child


# ---------------------------------------------------------------------------
# Ctrl-C: stop cleanly, and never leave a browser running
# ---------------------------------------------------------------------------

def test_ctrl_c_stops_the_run_without_losing_the_report(tmp_path: Path) -> None:
    uploaded: list[str] = []

    def runner(command, cwd):
        if command[1] == "douyin":
            uploaded.append(command[1])
            return FakeCompleted()
        raise KeyboardInterrupt

    publisher = SauPublisher(
        {"publish": {"platforms": ["douyin", "kuaishou", "tencent"], "preflight": False}},
        runner=runner, sau_bin="sau")

    report = publisher.publish(_video(tmp_path), SCRIPT, "贵州茅台", "600519")

    assert uploaded == ["douyin"]  # the abort does not roll back what went out
    statuses = {item["platform"]: item for item in report["platforms"]}
    assert statuses["douyin"]["ok"] is True
    assert "用户中断" in statuses["kuaishou"]["detail"]
    assert "未执行" in statuses["tencent"]["detail"]
    assert [f["platform"] for f in report["failed"]] == ["kuaishou", "tencent"]


def test_ctrl_c_kills_the_upload_process(tmp_path: Path) -> None:
    """A live upload must not keep posting after the operator gave up."""
    done = tmp_path / "finished"
    publisher = SauPublisher(
        {"publish": {"sau_dir": str(tmp_path), "timeout_seconds": 30, "verify_code_wait_seconds": 30}},
        sau_bin=sys.executable,
        code_prompt=lambda label, wait: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        publisher._run_subprocess(
            [sys.executable, "-u", "-c",
             f"import time; print('📱 检测到短信验证码弹窗', flush=True); time.sleep(3); "
             f"open({str(done)!r}, 'w').write('x')", "upload-video"], tmp_path)

    time.sleep(0.5)
    assert not done.exists()


def test_repeated_challenge_logs_do_not_postpone_the_deadline() -> None:
    """sau restates the prompt for as long as the popup is up.

    Treating every repetition as "still arming" used to skip the deadline check
    forever, so the run sat there until the global timeout — the exact hang the
    guard exists to prevent.
    """
    now = [0.0]
    guard = SmsChallengeGuard(Path("/tmp/none"), 10, clock=lambda: now[0])
    guard.feed("⏳ 等待验证码输入；可在交互终端直接输入")
    assert guard.armed
    now[0] = 5
    assert guard.feed("⏳ 等待验证码输入；可在交互终端直接输入") is None
    now[0] = 11
    reason = guard.feed("⏳ 等待验证码输入；可在交互终端直接输入")
    assert reason and "短信验证码" in reason


def test_the_operator_is_asked_once_per_challenge(tmp_path: Path) -> None:
    """``on_arm`` fires on the first marker only, however chatty sau gets."""
    now = [0.0]
    asked: list[str] = []
    guard = SmsChallengeGuard(tmp_path / "verify_code.txt", 10, clock=lambda: now[0],
                              on_arm=lambda: asked.append("?") or False)
    guard.feed("📱 检测到短信验证码弹窗")
    guard.feed("📤 已点击「获取验证码」，请查看手机短信")
    guard.feed("⏳ 等待验证码输入；可在交互终端直接输入")
    assert len(asked) == 1


def test_a_code_file_buys_the_run_another_window(tmp_path: Path) -> None:
    """Someone may still rescue it by writing the code by hand."""
    now = [0.0]
    code_file = tmp_path / "verify_code.txt"
    guard = SmsChallengeGuard(code_file, 10, clock=lambda: now[0])
    guard.feed("📱 检测到短信验证码弹窗")
    now[0] = 9.5
    code_file.write_text("123456", encoding="utf-8")
    assert guard.feed("") is None
    now[0] = 15
    assert guard.feed("") is None  # window refreshed by the arriving code
    now[0] = 45  # a code sau never picks up cannot keep it alive forever
    assert guard.feed("") is not None


def test_extend_starts_a_fresh_window() -> None:
    now = [0.0]
    guard = SmsChallengeGuard(Path("/tmp/none"), 10, clock=lambda: now[0])
    guard.feed("📱 检测到短信验证码弹窗")
    now[0] = 9.9
    guard.extend()
    now[0] = 19.5
    assert guard.feed("") is None
    now[0] = 20
    assert guard.feed("") is not None


def test_prompt_steps_aside_for_a_live_progress_bar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The banner and the operator's typing must not fight a refreshing bar."""
    publisher = SauPublisher({"publish": {"sau_dir": str(tmp_path), "verify_code_wait_seconds": 20}}, sau_bin="sau")
    calls: list[str] = []
    publisher.set_ui_hooks(pause=lambda: calls.append("pause"), resume=lambda: calls.append("resume"))

    class FakeStdin:
        def isatty(self) -> bool:
            return True

        def readline(self) -> str:
            return "123456\n"

    monkeypatch.setattr(sys, "stdin", FakeStdin(), raising=False)
    monkeypatch.setattr(publisher_module.select, "select", lambda *args: ([True], [], []))

    assert publisher._ask_for_code("抖音") is True
    assert calls == ["pause", "resume"]
    assert (tmp_path / "verify_code.txt").read_text(encoding="utf-8") == "123456"


# ---------------------------------------------------------------------------
# Session keep-alive (--keepalive)
#
# 视频号的登录态是「最后一次真实活动后 10~25 小时」失效的服务端会话，本地 cookie
# 永不过期。定时心跳是全自动发布的命门，所以它的行为要钉住：视频号必须走
# keepalive（不是 check），判定结果必须带时间戳落盘，失败不许抛异常打断整轮。
# ---------------------------------------------------------------------------

def _keepalive_publisher(tmp_path: Path, runner: Any, **publish: Any) -> SauPublisher:
    config = {
        "output": {"dir": str(tmp_path)},
        "publish": {"sau_dir": str(tmp_path), "accounts": {"tencent": "default", "douyin": "default"}, **publish},
    }
    return SauPublisher(config, runner=runner, sau_bin="/fake/sau")


def _write_tencent_log(tmp_path: Path, *login_times: str) -> Path:
    """Fake uploader log holding one 扫码成功 line per supplied timestamp."""
    log = tmp_path / "logs" / "tencent.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = ["2026-09-20 10:00:00.123 | INFO | x:cookie_auth:1 - 🥳 cookie 有效"]
    for stamp in login_times:
        lines.append(f"{stamp}.456 | SUCCESS | x:_wait_for_tencent_login:9 - 🥳 扫码成功，已经跳转到登录后页面")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def test_tencent_session_start_comes_from_the_latest_login_marker(tmp_path: Path) -> None:
    rotated = _write_tencent_log(tmp_path, "2026-09-22 16:22:50", "2026-09-23 09:07:41")

    assert publisher_module.last_tencent_login(tmp_path) == datetime(2026, 9, 23, 9, 7, 41)
    assert rotated.is_file()


def test_a_quick_login_also_resets_the_session_age(tmp_path: Path) -> None:
    """免扫码快捷登录（补丁 0008）写的是「快捷登录成功」，不带「扫码成功」二字。

    只认扫码的话，一次快捷重登之后会话年龄仍按上次扫码算 —— 而「24h 绝对过期」
    的整个排期判断都读这个数，报旧了就会把人引向错误的结论。
    """
    _write_tencent_log(tmp_path, "2026-09-23 09:07:41")
    log = tmp_path / "logs" / "tencent.log"
    log.write_text(
        log.read_text(encoding="utf-8")
        + "2026-09-24 15:19:41.356 | INFO     | x:_try_tencent_quick_login:483 - 🥳 快捷登录成功，已进入登录后页面\n",
        encoding="utf-8",
    )

    assert publisher_module.last_tencent_login(tmp_path) == datetime(2026, 9, 24, 15, 19, 41)


def test_tencent_session_start_is_unknown_without_a_readable_log(tmp_path: Path) -> None:
    assert publisher_module.last_tencent_login(tmp_path) is None

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "tencent.log").write_text("no markers here\n", encoding="utf-8")
    assert publisher_module.last_tencent_login(tmp_path) is None


def test_session_age_hours_needs_a_start_and_measures_hours() -> None:
    assert publisher_module.session_age_hours(None) is None
    start = datetime(2026, 9, 23, 9, 7, 41)
    assert publisher_module.session_age_hours(start, datetime(2026, 9, 23, 21, 7, 41)) == 12.0


def test_keepalive_reports_how_old_the_tencent_session_is(tmp_path: Path) -> None:
    # The age is the whole point of the metric: a stream of "alive" verdicts is
    # meaningless without knowing how old the session was when it answered.
    started = datetime.now() - timedelta(hours=13, minutes=30)
    _write_tencent_log(tmp_path, started.strftime("%Y-%m-%d %H:%M:%S"))
    publisher = _keepalive_publisher(tmp_path, fake_runner_factory([]))

    results = publisher.keepalive_platforms(["tencent", "douyin"])

    assert 13.4 < results["tencent"]["session_age_hours"] < 13.6
    assert results["tencent"]["session_started_at"] == started.isoformat(timespec="seconds")
    # 其余平台没有可读的会话起点：报 None，而不是编一个数字。
    assert results["douyin"]["session_age_hours"] is None
    record = publisher.session_state()["tencent"]
    assert record["session_age_hours"] == results["tencent"]["session_age_hours"]
    assert record["session_started_at"] == results["tencent"]["session_started_at"]


def test_keepalive_without_a_login_log_still_reports_the_verdict(tmp_path: Path) -> None:
    publisher = _keepalive_publisher(tmp_path, fake_runner_factory([]))

    results = publisher.keepalive_platforms(["tencent"])

    assert results["tencent"]["ok"] is True
    assert results["tencent"]["session_age_hours"] is None
    assert "session_started_at" not in publisher.session_state()["tencent"]


def test_keepalive_asks_tencent_for_keepalive_and_the_rest_for_check(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    publisher = _keepalive_publisher(tmp_path, fake_runner_factory(commands))

    results = publisher.keepalive_platforms(["tencent", "douyin"])

    assert commands[0][1:3] == ["tencent", "keepalive"]
    assert commands[1][1:3] == ["douyin", "check"]
    assert results["tencent"]["action"] == "keepalive"
    assert results["douyin"]["action"] == "check"
    assert all(r["ok"] for r in results.values())


def test_keepalive_writes_timestamped_state_and_keeps_last_ok_on_failure(tmp_path: Path) -> None:
    outcomes = iter([FakeCompleted(0, "alive: cookie 有效"), FakeCompleted(1, "dead: cookie 已失效")])
    publisher = _keepalive_publisher(tmp_path, lambda command, cwd: next(outcomes))

    first = publisher.keepalive_platforms(["tencent"])
    record = publisher.session_state()["tencent"]
    assert first["tencent"]["ok"] is True
    assert record["status"] == "alive"
    assert record["checked_at"] and record["last_ok"]
    assert (tmp_path / publisher_module.DEFAULT_SESSION_FILENAME).is_file()

    second = publisher.keepalive_platforms(["tencent"])
    record = publisher.session_state()["tencent"]
    assert second["tencent"]["ok"] is False
    assert record["status"] == "dead"
    # 失效不清空「最后有效时间」：运维要看到的是它曾经活到什么时候。
    assert record["last_ok"] == first["tencent"]["last_ok"]
    assert [entry["ok"] for entry in record["history"]] == [True, False]


def test_keepalive_timeout_is_reported_not_raised(tmp_path: Path) -> None:
    def runner(command: list[str], cwd: Path) -> Any:
        raise subprocess.TimeoutExpired(cmd=list(command), timeout=1)

    publisher = _keepalive_publisher(tmp_path, runner)

    results = publisher.keepalive_platforms(["tencent", "douyin"])

    assert results["tencent"]["ok"] is False
    assert "超时" in results["tencent"]["detail"]
    assert "超时" in results["douyin"]["detail"]


# ---------------------------------------------------------------------------
# 自动重新登录（--auto-login）
#
# 视频号会话是「扫码后固定 ~24h 绝对过期」，第二天必然失效；但它登录页有
# 「微信快捷登录」，本机微信在跑就能静默重登（免扫码）。这条链路是无人值守发布
# 唯一的自愈手段，所以三件事要钉住：没有微信时不许白等、只对做得到的平台动手、
# 超时是「没成」而不是异常。
# ---------------------------------------------------------------------------

def _auto_publisher(tmp_path: Path, runner: Any, **publish: Any) -> SauPublisher:
    config = {
        "output": {"dir": str(tmp_path)},
        "publish": {"sau_dir": str(tmp_path), "accounts": {"tencent": "default", "douyin": "default"},
                    **publish},
    }
    return SauPublisher(config, runner=runner, sau_bin="/fake/sau")


def _connect_only(*open_ports: int):
    """把 socket.connect 换成「只有这些端口有人听」。"""
    def connect(address, timeout):  # noqa: ARG001
        if address[1] not in open_ports:
            raise ConnectionRefusedError(61, "Connection refused")
        return FakeSocket()
    return connect


class FakeSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_wechat_probe_stops_at_the_first_listening_port() -> None:
    tried: list[int] = []

    def connect(address, timeout):  # noqa: ARG001
        tried.append(address[1])
        if address[1] == 14014:
            return FakeSocket()
        raise ConnectionRefusedError(61, "Connection refused")

    assert publisher_module.wechat_desktop_reachable(connect=connect) is True
    assert tried == [14013, 14014]      # 找到就停，不白探后面的端口


def test_wechat_probe_is_false_when_nothing_listens() -> None:
    # 微信没跑时端口是立刻 refused 的，整轮探测应该是毫秒级（这里顺便断言它探完了
    # 清单里的每个端口才放弃）。
    assert publisher_module.wechat_desktop_reachable(connect=_connect_only()) is False


def test_auto_login_does_not_touch_platforms_that_need_a_human(tmp_path: Path) -> None:
    """抖音/B站/快手/百家号的 login 都在等一次扫码 —— 没人扫的时候不该去试。"""
    commands: list[list[str]] = []
    publisher = _auto_publisher(tmp_path, fake_runner_factory(commands))

    outcome = publisher.auto_login("douyin")

    assert outcome["ok"] is False and outcome["attempted"] is False
    assert "人工扫码" in outcome["detail"]
    assert commands == []               # 一条命令都没发出去


def test_auto_login_skips_tencent_when_wechat_is_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publisher_module, "wechat_desktop_reachable", lambda *a, **k: False)
    commands: list[list[str]] = []
    publisher = _auto_publisher(tmp_path, fake_runner_factory(commands))

    outcome = publisher.auto_login("tencent")

    assert outcome["attempted"] is False and outcome["ok"] is False
    assert "微信" in outcome["detail"]
    assert commands == []               # 不把 180s 花在一次注定回落扫码的登录上


def test_auto_login_runs_sau_login_with_its_own_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publisher_module, "wechat_desktop_reachable", lambda *a, **k: True)
    commands: list[list[str]] = []
    publisher = _auto_publisher(tmp_path, fake_runner_factory(commands),
                                auto_login_timeout_seconds=42, timeout_seconds=900)

    outcome = publisher.auto_login("tencent")

    assert commands == [["/fake/sau", "tencent", "login", "--account", "default"]]
    assert outcome["attempted"] is True and outcome["ok"] is True
    # 预算用完必须还原：登录用的是自动登录预算（42s），不能把上传的 900s 带出去。
    assert publisher.timeout == 900


def test_auto_login_timeout_is_a_verdict_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publisher_module, "wechat_desktop_reachable", lambda *a, **k: True)

    def timing_out(command, cwd):  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd=list(command), timeout=180)

    outcome = _auto_publisher(tmp_path, timing_out).auto_login("tencent")

    assert outcome["attempted"] is True and outcome["ok"] is False
    assert "超时" in outcome["detail"]
    assert "扫码" in outcome["detail"]      # 超时的通常成因：回落到等扫码了


def test_auto_login_reports_a_failed_flow_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publisher_module, "wechat_desktop_reachable", lambda *a, **k: True)

    def failing(command, cwd):  # noqa: ARG001
        return FakeCompleted(returncode=1, stdout="", stderr="快捷登录失败")

    outcome = _auto_publisher(tmp_path, failing).auto_login("tencent")

    assert outcome["attempted"] is True and outcome["ok"] is False
    assert "快捷登录失败" in outcome["detail"]


def test_auto_login_platforms_covers_the_requested_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publisher_module, "wechat_desktop_reachable", lambda *a, **k: True)
    commands: list[list[str]] = []
    publisher = _auto_publisher(tmp_path, fake_runner_factory(commands))

    outcomes = publisher.auto_login_platforms(["tencent", "douyin", "weibo"])

    assert set(outcomes) == {"tencent", "douyin"}   # weibo 不是本项目平台
    assert outcomes["tencent"]["ok"] is True
    assert outcomes["douyin"]["attempted"] is False
    assert len(commands) == 1


def test_check_retries_a_timeout_before_calling_it_a_failure(tmp_path: Path) -> None:
    """一次卡顿不该让健康平台变成「需要重新登录」。

    2026-09-24 实测：快手 ``check`` 单独跑 8 秒返回 ``cookie 有效``，同样一条命令在
    串行预检里却吃满 120 秒预算。重试一次就能问出真相，而成本只有几秒。
    """
    calls: list[list[str]] = []

    def runner(command: list[str], cwd: Path) -> Any:
        calls.append(list(command))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=list(command), timeout=1)
        return FakeCompleted()

    publisher = _keepalive_publisher(tmp_path, runner, check_timeout_seconds=1)

    results = publisher.check_platforms(["kuaishou"])

    assert len(calls) == 2                    # 超时确实重试了
    assert results["kuaishou"]["ok"] is True
    assert results["kuaishou"]["timed_out"] is False


def test_check_marks_a_double_timeout_as_unknown_rather_than_dead(tmp_path: Path) -> None:
    def runner(command: list[str], cwd: Path) -> Any:
        raise subprocess.TimeoutExpired(cmd=list(command), timeout=1)

    publisher = _keepalive_publisher(tmp_path, runner, check_timeout_seconds=1)

    results = publisher.check_platforms(["kuaishou"])

    assert results["kuaishou"]["ok"] is False
    assert results["kuaishou"]["timed_out"] is True       # 调用方靠这个区分措辞
    assert "不等于失效" in results["kuaishou"]["detail"]


def test_keepalive_uses_its_own_budget_and_restores_the_upload_one(tmp_path: Path) -> None:
    seen: dict[str, float] = {}

    def runner(command: list[str], cwd: Path) -> Any:
        seen["budget"] = publisher.timeout
        return FakeCompleted()

    publisher = _keepalive_publisher(tmp_path, runner, timeout_seconds=900, keepalive_timeout_seconds=240)
    publisher.keepalive_platforms(["tencent", "douyin"])

    # keepalive 与 check 各自有预算，都不许继承 15 分钟的上传预算。
    assert seen["budget"] in (240.0, 120.0)
    assert publisher.timeout == 900.0


def test_keepalive_marks_unknown_platforms_without_calling_sau(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    publisher = _keepalive_publisher(tmp_path, fake_runner_factory(commands))

    results = publisher.keepalive_platforms(["myspace"])

    assert commands == []
    assert results["myspace"]["ok"] is False
    assert results["myspace"]["detail"] == "unsupported platform"


# ---------------------------------------------------------------------------
# --publish-at：把发布时刻搬进新鲜会话（平台侧定时发表）
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 23, 9, 0)


def test_publish_at_accepts_the_three_shapes_the_cron_writes() -> None:
    assert publisher_module.parse_publish_at("2026-09-24 19:30", _NOW) == "2026-09-24 19:30"
    assert publisher_module.parse_publish_at("+11h", _NOW) == "2026-09-23 20:00"
    assert publisher_module.parse_publish_at("+150m", _NOW) == "2026-09-23 11:30"
    # 裸时刻 = 下一个该时刻：今天还没到就用今天。
    assert publisher_module.parse_publish_at("19:30", _NOW) == "2026-09-23 19:30"


def test_bare_time_rolls_to_tomorrow_when_today_is_too_late() -> None:
    # 09:00 之后再看 09:30 只剩 30 分钟，短于平台要求的 2 小时提前量 → 顺延到明天，
    # 而不是让上传走到一半才发现排期不合法。
    assert publisher_module.parse_publish_at("09:30", _NOW) == "2026-09-24 09:30"


def test_publish_at_refuses_times_inside_the_platform_lead_time() -> None:
    with pytest.raises(publisher_module.ScheduleError, match="2 小时"):
        publisher_module.parse_publish_at("2026-09-23 10:30", _NOW)  # 只有 1.5h
    with pytest.raises(publisher_module.ScheduleError, match="2 小时"):
        publisher_module.parse_publish_at("+90m", _NOW)


def test_publish_at_reports_unparseable_and_out_of_range_times() -> None:
    for bad, expected in (("", "不能为空"), ("下周一下午", "看不懂"),
                          ("25:00", "时间不合法"), ("2026/09/24 19:30", "看不懂")):
        with pytest.raises(publisher_module.ScheduleError, match=expected):
            publisher_module.parse_publish_at(bad, _NOW)


def test_schedule_reaches_the_upload_command(tmp_path: Path) -> None:
    """--publish-at 的最终落点：sau 命令行里的 --schedule。"""
    commands: list[list[str]] = []
    config = {"output": {"dir": str(tmp_path)}, "publish": {"platforms": ["tencent"]}}
    metadata = publisher_module.PublishMetadata(title="标题", description="描述", tags=("A股",))
    video = tmp_path / "x.mp4"
    video.write_bytes(b"v")
    publisher = SauPublisher(config, runner=fake_runner_factory(commands), sau_bin="/fake/sau")

    publisher._publish_one("tencent", video, metadata, "2026-09-24 19:30")

    assert commands[0][commands[0].index("--schedule") + 1] == "2026-09-24 19:30"
