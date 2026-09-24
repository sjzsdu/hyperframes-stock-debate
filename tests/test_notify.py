"""反馈层测试：正文渲染、通道解析、各家 webhook 载荷，以及「绝不抛异常」这条底线。

这里钉住的都是**产品决定**，不是实现细节：成功也要报、通知里要有补发命令、
ANSI 颜色码不许漏到手机上、没填 url 的通道不能算「发失败」。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from stocktalk.modules import notify as notify_mod
from stocktalk.modules.notify import (
    FAILURE,
    MISSED,
    PARTIAL,
    RESTART,
    SUCCESS,
    TEST,
    Notifier,
    RunReport,
    build_notifier,
    window_label,
)


def _report(kind: str = SUCCESS, **kwargs) -> RunReport:
    base = dict(kind=kind, day="2026-09-24", window="18:30-21:00", at="2026-09-24T20:34:00")
    base.update(kwargs)
    return RunReport(**base)


# ---------------------------------------------------------------------------
# 窗口标签 / 渲染
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window,expected", [
    ("07:30-09:30", "早档"),
    ("12:00-13:00", "午档"),
    ("15:00-16:00", "下午档"),
    ("18:30-21:00", "晚档"),
    ("22:30-23:30", "夜档"),
    ("02:00-03:00", "凌晨档"),
])
def test_window_label_turns_a_window_into_a_word(window: str, expected: str) -> None:
    assert window_label(window) == expected


def test_window_label_survives_garbage() -> None:
    """窗口是从配置里读的字符串，拼不出来也得给个能读的词，不能抛。"""
    assert window_label("") == "这一档"
    assert window_label("晚上") == "这一档"


def test_a_fully_successful_run_says_what_went_out() -> None:
    text = _report(
        topic="振华重工(600320)",
        title="振华重工（600320）：靠整机出海赚辛苦钱的钢铁巨轮",
        succeeded=["抖音", "快手", "视频号"], elapsed_minutes=12.4, attempt=1, max_attempts=3,
    ).text()

    assert text.splitlines()[0] == "✅ 9-24 晚档发布完成 · 振华重工(600320)"
    assert "振华重工（600320）：靠整机出海赚辛苦钱的钢铁巨轮" in text
    assert "成功: 抖音、快手、视频号" in text
    assert "失败" not in text


def test_a_partial_run_names_the_failures_and_the_republish_command() -> None:
    text = _report(
        PARTIAL, day="2026-09-24", window="07:30-09:30", topic="拓普集团(601689)",
        succeeded=["抖音", "快手", "视频号"],
        failed=[{"label": "B站", "detail": "601 风控冷却中，还需 23 小时"},
                {"label": "百家号", "detail": "凭证过期"}],
        republish="tangulunjin 601689 --republish",
    ).text()

    assert text.splitlines()[0] == "⚠️ 9-24 早档只发出去 3/5 · 拓普集团(601689)"
    assert "失败: B站 —— 601 风控冷却中，还需 23 小时" in text
    assert "补发: tangulunjin 601689 --republish" in text


def test_a_failed_run_carries_the_reason_and_the_retry_plan() -> None:
    text = _report(
        FAILURE, day="2026-09-24", window="07:30-09:30",
        reason="选题失败 —— tongstock 拉不到热门榜",
        attempt=1, max_attempts=3, retry_in_minutes=45, will_retry=True,
    ).text()

    assert text.splitlines()[0] == "❌ 9-24 早档没发出去"
    assert "原因: 选题失败 —— tongstock 拉不到热门榜" in text
    assert "下一步: 45 分钟后自动重试" in text


def test_running_out_of_attempts_reads_as_giving_up_not_retrying() -> None:
    text = _report(FAILURE, day="2026-09-24", window="07:30-09:30",
                   reason="渲染崩溃", attempt=3, max_attempts=3).text()

    assert "已达重试上限（3 次）" in text
    assert "自动重试" not in text


def test_a_missed_slot_says_so_and_gives_the_next_one() -> None:
    text = _report(MISSED, day="2026-09-24", window="07:30-09:30",
                   detail="错过 07:30-09:30，窗口已过", next_at="09-24 20:12").text()

    assert text.splitlines()[0] == "⏰ 9-24 早档漏了"
    assert "错过 07:30-09:30，窗口已过" in text
    assert "下次: 09-24 20:12" in text


def test_restart_and_test_reports_do_not_pretend_to_be_a_slot() -> None:
    """重启/自检跟「哪一档」无关，标题里硬凑一个时刻只会让人看不懂。"""
    assert _report(RESTART, detail="上次异常退出").headline() == "🔁 守护进程重启"
    assert _report(TEST, detail="通道自检").headline() == "🔔 通道自检"


def test_ansi_colour_codes_never_reach_the_notification() -> None:
    """sau 的上传结果是带颜色码的（``\\x1b[38;2;61;208;141m``），手机上看就是乱码。"""
    text = _report(
        PARTIAL, failed=[{"label": "抖音", "detail": "2026-09-24 \x1b[38;2;1;2;3m上传失败\x1b[0m 重试"}],
    ).text()

    assert "\x1b" not in text
    assert "上传失败" in text


def test_summary_keeps_only_what_status_and_history_need() -> None:
    summary = _report(SUCCESS, topic="振华重工(600320)", succeeded=["抖音"],
                      failed=[{"label": "B站", "detail": "x" * 500}]).summary()

    assert summary["slot"] == "9-24 晚档"
    assert summary["failed"] == ["B站"]      # 失败原因太长，不塞进状态文件
    assert "detail" not in summary


# ---------------------------------------------------------------------------
# 通道解析 / 该不该发
# ---------------------------------------------------------------------------


def test_no_notify_section_means_switched_off() -> None:
    """直接构造 Daemon 的调用方（含测试）不该被通知打扰；真实运行走 load_config。"""
    notifier = Notifier({})

    assert notifier.enabled is False
    assert notifier.should_send(_report()) is False
    assert notifier.send(_report()) == []


def test_channels_accept_bare_names_and_dicts() -> None:
    notifier = build_notifier({"notify": {"enabled": True, "channels": [
        "macos", {"kind": "webhook", "type": "feishu", "url": "https://x"},
    ]}})

    assert [item["kind"] for item in notifier.channels] == ["macos", "webhook"]
    assert notifier.channels[1]["type"] == "feishu"


def test_unconfigured_channels_are_skipped_not_reported_as_failures(monkeypatch) -> None:
    """默认配置把所有通道都列了一遍。空 url 是「还没启用」，不是「发失败」——
    混在一起会让日志每次运行都刷一堆假告警，真的坏了反而看不出来。"""
    calls: list[str] = []
    monkeypatch.setattr(notify_mod, "_notify_macos",
                        lambda title, text, sound: calls.append(title) or (True, "ok"))
    notifier = build_notifier({"notify": {"enabled": True, "channels": [
        "macos", {"kind": "webhook", "url": ""}, {"kind": "smtp", "host": ""},
    ]}})

    results = notifier.send(_report())

    assert calls, "本机通知应该照发"
    assert [item.get("skipped") for item in results] == [None, True, True]
    assert [name for name, _ok, _detail in notifier.channels_ready()] == ["macos", "webhook(wecom)", "smtp"]


def test_each_kind_has_its_own_switch() -> None:
    notifier = build_notifier({"notify": {"enabled": True, "on_success": False,
                                         "channels": ["macos"]}})

    assert notifier.should_send(_report(SUCCESS)) is False
    assert notifier.should_send(_report(FAILURE)) is True


def test_a_channel_that_explodes_does_not_take_the_run_down(monkeypatch) -> None:
    """通知是旁路：它坏了最多是收不到消息，绝不许把一次成功的发布变成失败。"""
    def boom(title, text, sound):
        raise RuntimeError("osascript 没了")

    monkeypatch.setattr(notify_mod, "_notify_macos", boom)
    notifier = build_notifier({"notify": {"enabled": True, "channels": ["macos"]}})

    results = notifier.send(_report())

    assert results == [{"kind": "macos", "ok": False, "detail": "RuntimeError: osascript 没了"}]


# ---------------------------------------------------------------------------
# webhook
# ---------------------------------------------------------------------------


def _capture_webhook(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    def fake(url, payload, timeout, *, method="POST"):
        sent.append({"url": url, "payload": payload, "method": method})
        return True, "ok"

    monkeypatch.setattr(notify_mod, "_http_post", fake)
    return sent


@pytest.mark.parametrize("kind,key", [
    ("wecom", "markdown"), ("dingtalk", "markdown"), ("feishu", "content"), ("json", None),
])
def test_every_robot_flavour_gets_the_body_its_own_way(monkeypatch, kind: str, key: str) -> None:
    sent = _capture_webhook(monkeypatch)
    notifier = build_notifier({"notify": {"enabled": True, "channels": [
        {"kind": "webhook", "type": kind, "url": "https://example.com/hook"},
    ]}})

    notifier.send(_report(SUCCESS, topic="振华重工(600320)", succeeded=["抖音"]))

    payload = sent[0]["payload"]
    assert sent[0]["method"] == "POST"
    assert "振华重工(600320)" in json.dumps(payload, ensure_ascii=False)
    if key:
        assert key in payload


def test_bark_uses_a_get_with_the_text_in_the_path(monkeypatch) -> None:
    sent = _capture_webhook(monkeypatch)
    notifier = build_notifier({"notify": {"enabled": True, "channels": [
        {"kind": "webhook", "type": "bark", "url": "https://api.day.app/KEY"},
    ]}})

    notifier.send(_report(FAILURE, reason="选题失败"))

    assert sent[0]["method"] == "GET"
    assert sent[0]["url"].startswith("https://api.day.app/KEY/")
    assert sent[0]["payload"] is None


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def test_an_http_200_with_a_nonzero_errcode_is_a_failure(monkeypatch) -> None:
    """企业微信/webhook 失效时返回 200 + errcode。只看状态码会喜滋滋地报「送达」。"""
    monkeypatch.setattr(notify_mod.urllib.request, "urlopen",
                        lambda *a, **k: _Response(b'{"errcode":93000,"errmsg":"invalid webhook url"}'))

    ok, detail = notify_mod._http_post("https://example.com/hook", {"a": 1}, 5)

    assert ok is False
    assert "93000" in detail and "invalid webhook url" in detail


def test_a_plain_200_reads_as_ok(monkeypatch) -> None:
    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", lambda *a, **k: _Response(b"ok"))

    assert notify_mod._http_post("https://example.com/hook", {"a": 1}, 5) == (True, "ok")


def test_a_dead_endpoint_is_reported_not_raised(monkeypatch) -> None:
    def refuse(*_a, **_k):
        raise notify_mod.urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", refuse)

    ok, detail = notify_mod._http_post("https://nope.invalid/hook", {"a": 1}, 5)

    assert ok is False
    assert "URLError" in detail


# ---------------------------------------------------------------------------
# macOS 通知
# ---------------------------------------------------------------------------


def test_macos_notification_builds_a_quoted_applescript_command(monkeypatch) -> None:
    monkeypatch.setattr(notify_mod.shutil, "which", lambda name: "/usr/bin/osascript")
    captured: list[list[str]] = []

    def fake_run(command, **kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(notify_mod.subprocess, "run", fake_run)

    ok, _ = notify_mod._notify_macos('标题带"引号"', "正文 \\ 反斜杠", "Glass")

    assert ok is True
    script = captured[0][-1]
    assert script.startswith("display notification")
    assert '\\"引号\\"' in script          # 双引号必须转义，否则 AppleScript 直接语法错
    assert "\\\\" in script                # 反斜杠同理
    assert 'sound name "Glass"' in script


def test_macos_notification_without_sound_stays_silent(monkeypatch) -> None:
    monkeypatch.setattr(notify_mod.shutil, "which", lambda name: "/usr/bin/osascript")
    captured: list[list[str]] = []
    monkeypatch.setattr(notify_mod.subprocess, "run",
                        lambda command, **k: captured.append(command) or subprocess.CompletedProcess(command, 0, "", ""))

    notify_mod._notify_macos("t", "b", "")

    assert "sound name" not in captured[0][-1]


def test_osascript_failure_is_reported_with_its_stderr(monkeypatch) -> None:
    monkeypatch.setattr(notify_mod.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(notify_mod.subprocess, "run",
                        lambda command, **k: subprocess.CompletedProcess(command, 1, "", "not authorized"))

    ok, detail = notify_mod._notify_macos("t", "b", "")

    assert ok is False and detail == "not authorized"


def test_no_osascript_means_not_macos(monkeypatch) -> None:
    monkeypatch.setattr(notify_mod.shutil, "which", lambda name: None)

    ok, detail = notify_mod._notify_macos("t", "b", "")

    assert ok is False and "osascript" in detail


def test_success_and_failure_use_different_sounds(monkeypatch) -> None:
    sounds: list[str] = []
    monkeypatch.setattr(notify_mod, "_notify_macos",
                        lambda title, text, sound: sounds.append(sound) or (True, "ok"))
    notifier = build_notifier({"notify": {"enabled": True, "channels": ["macos"],
                                         "sound": {"success": "Glass", "failure": "Basso"}}})

    notifier.send(_report(SUCCESS))
    notifier.send(_report(FAILURE))

    assert sounds == ["Glass", "Basso"]


# ---------------------------------------------------------------------------
# 邮件
# ---------------------------------------------------------------------------


def test_smtp_password_comes_from_the_environment_only(monkeypatch) -> None:
    """配置文件是会被提交、会被贴出来的东西，密码不能住在里面。"""
    monkeypatch.delenv("TANGULUNJIN_SMTP_PASSWORD", raising=False)

    ok, detail = notify_mod._smtp_send(
        {"host": "smtp.qq.com", "user": "me@qq.com", "to": "me@qq.com"}, "s", "b")

    assert ok is False
    assert "TANGULUNJIN_SMTP_PASSWORD" in detail


def test_smtp_sends_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("TANGULUNJIN_SMTP_PASSWORD", "secret")
    sent: list[tuple] = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def login(self, user, password):
            sent.append(("login", user, password))

        def send_message(self, message):
            sent.append(("send", message["Subject"], message["To"]))

    monkeypatch.setattr(notify_mod.smtplib, "SMTP_SSL", FakeSMTP)

    ok, detail = notify_mod._smtp_send(
        {"host": "smtp.qq.com", "user": "me@qq.com", "to": ["a@x.com", "b@x.com"]}, "标题", "正文")

    assert ok is True
    assert ("connect", "smtp.qq.com", 465) in sent
    assert ("login", "me@qq.com", "secret") in sent
    assert ("send", "标题", "a@x.com, b@x.com") in sent
    assert "a@x.com" in detail


def test_smtp_needs_host_and_recipient() -> None:
    assert notify_mod._smtp_send({}, "s", "b") == (False, "缺 host/to")


# ---------------------------------------------------------------------------
# 逃生舱
# ---------------------------------------------------------------------------


def test_command_channel_pipes_the_report_as_json(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(notify_mod.subprocess, "run",
                        lambda command, **k: captured.append({"command": command, **k})
                        or subprocess.CompletedProcess(command, 0, "", ""))

    ok, _ = notify_mod._run_command("ntfy publish mytopic", _report(SUCCESS), 5)

    assert ok is True
    payload = json.loads(captured[0]["input"])
    assert payload["kind"] == SUCCESS
    assert payload["succeeded"] == []


def test_command_channel_can_get_a_report_file_instead_of_stdin(monkeypatch, tmp_path: Path) -> None:
    captured: list[dict] = []

    def fake_run(command, **k):
        # 临时文件在 _run_command 的 finally 里就删了，所以要在这里把它读出来。
        path = Path(command.split("--file ", 1)[1].strip("'"))
        captured.append({"command": command, "body": json.loads(path.read_text(encoding="utf-8"))})
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(notify_mod.subprocess, "run", fake_run)

    notify_mod._run_command("push --file {report}", _report(SUCCESS), 5)

    assert "{report}" not in captured[0]["command"]
    assert captured[0]["body"]["kind"] == SUCCESS


def test_command_channel_reports_a_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr(notify_mod.subprocess, "run",
                        lambda command, **k: subprocess.CompletedProcess(command, 3, "", "boom"))

    ok, detail = notify_mod._run_command("false", _report(SUCCESS), 5)

    assert ok is False and detail == "boom"


def test_command_channel_needs_a_command() -> None:
    assert notify_mod._run_command("   ", _report(SUCCESS), 5) == (False, "未配置命令")


def test_an_unknown_channel_is_reported_not_ignored() -> None:
    notifier = build_notifier({"notify": {"enabled": True, "channels": ["telepathy"]}})

    assert notifier.channels_ready() == [("telepathy", False,
                                          "不认识的通道 kind=telepathy（可选: macos / webhook / smtp / command）")]
