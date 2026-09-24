"""守护进程测试：tick 决策、窗口边界、待机睡眠、重试退避、命令拼装、launchd 描述文件。

时间全部靠注入的时钟推进，测试不真等；子进程执行只在两个用例里跑真的（很短的
python -c），因为「边读边写、超时可杀」这条恰恰是管道逻辑最容易错的地方。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from stocktalk.modules import daemon as daemon_mod
from stocktalk.modules import notify as notify_mod
from stocktalk.modules.daemon import (
    LAUNCHD_LABEL,
    Daemon,
    RunOutcome,
    _catch_sigterm,
    _restore_signal,
    _run_subprocess,
    launchd_state,
    plist_body,
)
from stocktalk.modules.scheduler import Plan


class Clock:
    """可控时钟。守护进程只通过 now_fn 取时间，所以推进它就是推进世界。"""

    def __init__(self, at: str) -> None:
        self.at = datetime.fromisoformat(at)

    def __call__(self) -> datetime:
        return self.at

    def jump_to(self, moment: datetime) -> datetime:
        self.at = moment
        return self.at

    def advance(self, **kwargs: float) -> datetime:
        self.at += timedelta(**kwargs)
        return self.at


class Recorder:
    """假的子进程执行器：记下命令，返回预设结果。"""

    def __init__(self, exit_code: int = 0, topic: str = "", tail: str = "", reason: str = "") -> None:
        self.exit_code = exit_code
        self.topic = topic
        self.tail = tail
        self.reason = reason
        self.calls: list[list[str]] = []

    def __call__(self, command, root, log_path, timeout) -> RunOutcome:
        self.calls.append(list(command))
        return RunOutcome(exit_code=self.exit_code, topic=self.topic, tail=self.tail,
                          reason=self.reason)


# 固定种子：计划时刻由随机数决定，不钉死的话测试就会在「计划落在窗口尾部」的那天
# 无缘无故变红。
SEED = 20260924
# 随机时刻的可复现预期值（tests/test_scheduler.py 钉住了窗口取值算法，这里跟着走）。
MORNING_SLOT = datetime(2026, 9, 24, 7, 31)


def _config(tmp_path: Path, **overrides) -> dict:
    settings = {
        "windows": ["07:30-09:30"],
        "seed": SEED,
        "max_sleep_seconds": 900,
        "min_gap_minutes": 0,
        "retry_minutes": 45,
        "max_attempts": 3,
        "run_timeout_seconds": 60,
        "continue_on_login_failure": True,
    }
    settings.update(overrides)
    return {"output": {"dir": str(tmp_path)}, "daemon": settings}


def _daemon(tmp_path: Path, clock: Clock, recorder: Recorder, **overrides) -> Daemon:
    return Daemon(_config(tmp_path, **overrides), root=tmp_path, now_fn=clock,
                  sleep_fn=lambda _seconds: None, runner=recorder)


def _first_plan(daemon: Daemon) -> Plan:
    plans = daemon.scheduler.plans_for(date(2026, 9, 24))
    assert plans, "排期应该已经生成"
    return plans[0]


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------


def test_tick_runs_the_full_pipeline_when_the_slot_arrives(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T06:00:00")
    recorder = Recorder(topic="拓普集团(601689)")
    daemon = _daemon(tmp_path, clock, recorder)

    assert daemon.tick().action == "idle"  # 先落一次计划
    plan = _first_plan(daemon)
    assert plan.when == MORNING_SLOT   # 计划时刻可复现，下面的断言才有意义
    clock.jump_to(plan.when + timedelta(minutes=1))

    assert daemon.tick().action == "ran"
    assert recorder.calls == [[
        sys.executable, "-m", "stocktalk", "--auto-pick", "--publish",
        "--no-interactive", "--auto-login", "--continue-anyway",
    ]]
    assert _first_plan(daemon).status == "done"
    assert _first_plan(daemon).topic == "拓普集团(601689)"


def test_tick_stays_idle_before_the_slot(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T05:00:00")
    recorder = Recorder()
    daemon = _daemon(tmp_path, clock, recorder)

    assert daemon.tick().action == "idle"
    assert recorder.calls == []


def test_a_slot_the_machine_slept_through_is_written_off(tmp_path: Path) -> None:
    """合盖睡到 10:30 才醒，窗口 09:30 已过：那一档直接认，不补发。

    补一条几小时前的片子，那个时间点反而更假，还会把当天的节奏搅乱。
    """
    clock = Clock("2026-09-24T06:00:00")
    recorder = Recorder()
    daemon = _daemon(tmp_path, clock, recorder)
    daemon.tick()

    clock.jump_to(datetime(2026, 9, 24, 10, 30))
    assert daemon.tick().action == "idle"

    assert recorder.calls == []
    assert _first_plan(daemon).status == "missed"
    assert daemon.scheduler.state["stats"]["missed"] == 1


def test_a_failing_run_retries_then_gives_up(tmp_path: Path) -> None:
    """子进程失败不该带走守护进程：退避重试，到上限才判死。"""
    clock = Clock("2026-09-24T00:05:00")
    recorder = Recorder(exit_code=1, tail="渲染失败")
    # 全天窗口：重试要跨三次退避，窄窗口会把「重试」和「窗口过期」搅在一起。
    daemon = _daemon(tmp_path, clock, recorder, windows=["00:00-23:59"])
    daemon.tick()
    clock.jump_to(_first_plan(daemon).when + timedelta(minutes=1))

    assert daemon.tick().action == "ran"           # 第 1 次
    assert _first_plan(daemon).status == "pending"  # 退回待发
    assert daemon.tick().action == "idle"           # 退避窗口里不重试

    clock.advance(minutes=46)
    assert daemon.tick().action == "ran"            # 第 2 次
    clock.advance(minutes=46)
    assert daemon.tick().action == "ran"            # 第 3 次 → 上限

    assert _first_plan(daemon).status == "failed"
    assert len(recorder.calls) == 3
    assert daemon.scheduler.state["stats"]["failures"] == 1


def test_tick_survives_a_runner_that_reports_a_timeout(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T06:00:00")

    def hanging_runner(command, root, log_path, timeout) -> RunOutcome:
        return RunOutcome(exit_code=-9, timed_out=True, tail="")

    daemon = Daemon(_config(tmp_path), root=tmp_path, now_fn=clock,
                    sleep_fn=lambda _seconds: None, runner=hanging_runner)
    daemon.tick()
    clock.jump_to(_first_plan(daemon).when + timedelta(minutes=1))

    assert daemon.tick().action == "ran"
    assert _first_plan(daemon).status == "pending"  # 超时也算一次失败，可以重试
    assert _first_plan(daemon).exit_code == -9


# ---------------------------------------------------------------------------
# 待机：不轮询、不写盘
# ---------------------------------------------------------------------------


def test_sleep_stretches_to_the_next_slot_instead_of_polling(tmp_path: Path) -> None:
    """空闲时不问「到点了吗」，而是算准了直接睡到下一个发布时刻。"""
    clock = Clock("2026-09-24T06:00:00")
    daemon = _daemon(tmp_path, clock, Recorder(), max_sleep_seconds=43200)
    daemon.tick()

    assert daemon.sleep_seconds() == (MORNING_SLOT - clock()).total_seconds()


def test_sleep_is_capped_so_a_suspended_timer_cannot_overshoot(tmp_path: Path) -> None:
    """macOS 睡眠期间单调时钟不走字：一觉睡太久，醒来时计时器还剩一大截，
    那一档就被无声地睡过去了。所以每一觉都要封顶。"""
    clock = Clock("2026-09-24T06:00:00")
    daemon = _daemon(tmp_path, clock, Recorder(), max_sleep_seconds=600)
    daemon.tick()

    assert daemon.sleep_seconds() == 600


def test_sleep_falls_back_to_an_idle_interval_when_nothing_is_planned(tmp_path: Path) -> None:
    """未来几天都没有排期（窗口被清空、时钟被改过）时不能睡死：定期回头看一眼。"""
    clock = Clock("2026-09-24T06:00:00")
    daemon = _daemon(tmp_path, clock, Recorder(), windows=[])

    assert daemon.sleep_seconds() == 900


def test_an_idle_daemon_does_not_touch_the_state_file(tmp_path: Path) -> None:
    """闲置时反复睁眼也不该动磁盘 —— 这正是「守护进程不消耗资源」的落点。"""
    clock = Clock("2026-09-24T06:00:00")
    daemon = _daemon(tmp_path, clock, Recorder())
    daemon.tick()                       # 首 tick 会落计划 + 写一次心跳
    state_file = tmp_path / ".daemon_state.json"
    stamp = state_file.stat().st_mtime_ns

    for _ in range(60):                 # 一分钟一次地看了一小时，什么也没发生
        daemon.tick()

    assert state_file.stat().st_mtime_ns == stamp


# ---------------------------------------------------------------------------
# 命令与状态
# ---------------------------------------------------------------------------


def test_command_drops_continue_anyway_when_it_is_switched_off(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T06:00:00")
    daemon = _daemon(tmp_path, clock, Recorder(), continue_on_login_failure=False)
    assert "--continue-anyway" not in daemon.command()


def test_command_drops_auto_login_when_it_is_switched_off(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T06:00:00")
    daemon = _daemon(tmp_path, clock, Recorder(), auto_login=False)
    assert "--auto-login" not in daemon.command()


def test_command_appends_extra_args(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T06:00:00")
    daemon = _daemon(tmp_path, clock, Recorder(), extra_args=["--publish-at", "+3h"])
    assert daemon.command()[-2:] == ["--publish-at", "+3h"]


def test_status_lines_describe_the_daemon(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T06:00:00")
    daemon = _daemon(tmp_path, clock, Recorder())
    daemon.tick()

    lines = daemon.status_lines()
    body = "\n".join(lines)

    assert "未运行" in body or "运行中" in body
    assert "07:30-09:30" in body
    assert "计划" in body
    assert "不补发" in body
    assert "下一次: 09-24 07:31" in body


def test_run_forever_stops_after_max_ticks(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T05:00:00")
    daemon = _daemon(tmp_path, clock, Recorder())
    assert daemon.run_forever(max_ticks=3) == 0


def test_run_forever_refuses_to_start_without_windows(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T05:00:00")
    daemon = _daemon(tmp_path, clock, Recorder(), windows=[])
    assert daemon.run_forever() == 1


def test_pid_file_is_written_and_cleared(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T05:00:00")
    daemon = _daemon(tmp_path, clock, Recorder())
    daemon.run_forever(max_ticks=1)
    # run_forever 的 finally 会清掉 pid，所以这里只该剩「没有活着的进程」。
    assert daemon.running_pid() is None


def test_sigterm_takes_the_same_clean_exit_path_as_ctrl_c() -> None:
    """launchd 停服务发 SIGTERM，默认行为会绕开 finally —— pid 文件就留在磁盘上，
    之后 --daemon-status 可能读到它并误报「运行中」。"""
    previous = _catch_sigterm()
    try:
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGTERM)
    finally:
        _restore_signal(previous)


# ---------------------------------------------------------------------------
# 真子进程（管道边读边写 + 超时可杀）
# ---------------------------------------------------------------------------


def test_run_subprocess_parses_the_topic_from_child_output(tmp_path: Path) -> None:
    command = [sys.executable, "-c",
               "print('选题（tongstock 热门榜）：拓普集团(601689)')"]
    outcome = _run_subprocess(command, tmp_path, tmp_path / "daemon.log", 30)

    assert outcome.exit_code == 0
    assert outcome.topic == "拓普集团(601689)"
    assert "拓普集团" in (tmp_path / "daemon.log").read_text(encoding="utf-8")


def test_run_subprocess_kills_a_hung_child(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    outcome = _run_subprocess(command, tmp_path, tmp_path / "daemon.log", 0.5)

    assert outcome.timed_out is True
    assert outcome.exit_code != 0


def test_run_subprocess_does_not_deadlock_on_a_lot_of_output(tmp_path: Path) -> None:
    """管道写满就死锁是这类代码的经典坑：必须边读边落盘。"""
    command = [sys.executable, "-c",
               "for i in range(4000): print('x' * 80)"]
    outcome = _run_subprocess(command, tmp_path, tmp_path / "daemon.log", 60)

    assert outcome.exit_code == 0
    assert len(outcome.tail.splitlines()) == 12


# ---------------------------------------------------------------------------
# launchd
# ---------------------------------------------------------------------------


def test_plist_body_points_launchd_at_the_daemon(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T06:00:00")
    daemon = _daemon(tmp_path, clock, Recorder())
    body = plist_body(daemon)

    assert LAUNCHD_LABEL in body
    assert "--daemon" in body
    assert "<key>RunAtLoad</key>" in body
    assert "<key>KeepAlive</key>" in body
    # launchd 的环境极干净，不补 PATH 就找不到 node/npx，渲染会直接失败。
    assert "<key>PATH</key>" in body
    assert str(tmp_path) in body


def test_plist_escapes_paths_that_contain_xml_characters(tmp_path: Path) -> None:
    awkward = tmp_path / "a&b"
    awkward.mkdir()
    clock = Clock("2026-09-24T06:00:00")
    daemon = Daemon(_config(awkward), root=awkward, now_fn=clock,
                    sleep_fn=lambda _seconds: None, runner=Recorder())
    assert "a&amp;b" in plist_body(daemon)


# ---------------------------------------------------------------------------
# 留痕：错过要报，活着也要报
# ---------------------------------------------------------------------------


def test_a_missed_slot_is_logged_instead_of_swallowed(tmp_path: Path) -> None:
    """安静地不发最危险 —— 你会分不清「今天没事干」和「它早就死了」。"""
    clock = Clock("2026-09-24T06:00:00")
    daemon = _daemon(tmp_path, clock, Recorder())
    daemon.tick()
    clock.jump_to(datetime(2026, 9, 24, 16, 0))
    daemon.tick()

    assert "这一档没发" in (tmp_path / "daemon.log").read_text(encoding="utf-8")


def test_heartbeat_proves_the_daemon_is_still_watching_the_clock(tmp_path: Path) -> None:
    clock = Clock("2026-09-24T00:30:00")
    daemon = _daemon(tmp_path, clock, Recorder(), heartbeat_hours=6)

    daemon.tick()                 # 首次就说一句
    clock.advance(hours=6)
    daemon.tick()                 # 跨过间隔，再说一句
    clock.advance(minutes=30)
    daemon.tick()                 # 还没到点，不吵

    log = (tmp_path / "daemon.log").read_text(encoding="utf-8")
    assert log.count("心跳：守护进程在看表") == 2


def test_logging_survives_a_broken_stdout(tmp_path: Path, monkeypatch) -> None:
    """父进程先走一步、stdout 管道断了的时候，日志仍要落文件、进程仍要能退干净。

    实测踩到过：pkill 之后没有「收到中断」那行日志，因为 print 先炸了。
    """
    class Broken:
        def write(self, *_args): raise BrokenPipeError
        def flush(self): raise BrokenPipeError

    daemon = _daemon(tmp_path, Clock("2026-09-24T06:00:00"), Recorder())
    monkeypatch.setattr(sys, "stdout", Broken())

    daemon._log("测试")  # noqa: SLF001 —— 就是要直接验这一条

    assert "测试" in (tmp_path / "daemon.log").read_text(encoding="utf-8")


def test_log_lines_are_not_written_twice_when_stdout_is_the_log(
        tmp_path: Path, monkeypatch) -> None:
    """launchd 的 StandardOutPath 指的就是 daemon.log，stdout 于是*就是*那个文件。

    `_log` 再 print 一遍，每行都会在日志里出现两次 —— 装完服务第一次读日志就撞到了。
    """
    daemon = _daemon(tmp_path, Clock("2026-09-24T06:00:00"), Recorder())
    log_path = tmp_path / "daemon.log"

    with log_path.open("a", encoding="utf-8") as handle:
        monkeypatch.setattr(sys, "stdout", handle)
        daemon._log("只该出现一次")  # noqa: SLF001 —— 就是要直接验这一条

    assert log_path.read_text(encoding="utf-8").count("只该出现一次") == 1


def test_logging_still_echoes_when_stdout_is_somewhere_else(
        tmp_path: Path, monkeypatch) -> None:
    """前台跑（或 `| tee`）时 stdout 是别的地方，就该照常打出来。"""
    daemon = _daemon(tmp_path, Clock("2026-09-24T06:00:00"), Recorder())
    console = tmp_path / "console.txt"

    with console.open("w", encoding="utf-8") as handle:
        monkeypatch.setattr(sys, "stdout", handle)
        daemon._log("两边都要有")  # noqa: SLF001

    assert "两边都要有" in (tmp_path / "daemon.log").read_text(encoding="utf-8")
    assert "两边都要有" in console.read_text(encoding="utf-8")


def test_plist_path_is_fixed_and_does_not_leak_the_installing_shell(
        tmp_path: Path, monkeypatch) -> None:
    """装服务时的 shell PATH 可能是编辑器/沙箱给的；抄进 plist 会让服务以后一直用错的路径。"""
    monkeypatch.setenv("PATH", "/nonexistent/sandbox/bin")
    daemon = _daemon(tmp_path, Clock("2026-09-24T06:00:00"), Recorder())

    body = plist_body(daemon)

    assert "sandbox" not in body
    assert "/usr/bin" in body
    assert str(Path.home() / ".local" / "bin") in body


def test_daemon_path_setting_appends_extra_directories(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, Clock("2026-09-24T06:00:00"), Recorder(),
                     path=["/opt/nvm/versions/node/v22/bin"])
    assert "/opt/nvm/versions/node/v22/bin" in plist_body(daemon)


# ---------------------------------------------------------------------------
# 服务到底有没有在跑（`launchctl list` 会骗人）
# ---------------------------------------------------------------------------

PRINT_RUNNING = """gui/501/com.tangulunjin.daemon = {
\tactive count = 1
\tpath = /Users/someone/Library/LaunchAgents/com.tangulunjin.daemon.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /tmp/x/.venv/bin/python
\tpid = 86081
}
"""


def _fake_launchctl(monkeypatch, *, print_result, list_stdout=""):
    """替掉 launchctl：print 走一段真输出/失败，list 走一段表格文本。"""
    def runner(command, **_kwargs):
        if command[1] == "print":
            code, out = print_result
        else:
            code, out = 0, list_stdout
        return subprocess.CompletedProcess(command, code, stdout=out, stderr="")

    monkeypatch.setattr(daemon_mod.subprocess, "run", runner)


def test_launchd_state_reads_the_running_service_from_print(monkeypatch) -> None:
    """`launchctl list` 在某些上下文返回空（实测本机某个 shell 就是 0 行），
    那时它会把「正在跑」报成「未加载」—— 正好答错唯一被关心的那个问题。"""
    _fake_launchctl(monkeypatch, print_result=(0, PRINT_RUNNING), list_stdout="")

    state = launchd_state()

    assert "running" in state
    assert "pid=86081" in state


def test_launchd_state_falls_back_to_list_when_print_is_unavailable(monkeypatch) -> None:
    """非 GUI 会话里 print 可能失败（SSH 等），这时才轮到 list。"""
    listed = "-\t0\tcom.apple.something\n85324\t0\t" + LAUNCHD_LABEL + "\n"
    _fake_launchctl(monkeypatch, print_result=(113, ""), list_stdout=listed)

    assert launchd_state() == "launchd: pid=85324 status=0"


def test_launchd_state_says_not_loaded_only_when_both_agree(monkeypatch) -> None:
    _fake_launchctl(monkeypatch, print_result=(113, ""), list_stdout="-\t0\tcom.apple.other\n")

    assert launchd_state() == "launchd: 未加载"


# ---------------------------------------------------------------------------
# 结果反馈
# ---------------------------------------------------------------------------

LABELS = {"douyin": "抖音", "bilibili": "B站", "kuaishou": "快手",
          "tencent": "视频号", "baijiahao": "百家号"}


def _capture_notifications(monkeypatch) -> list[dict]:
    """接住所有反馈，测试就不碰真的 osascript / 网络。"""
    sent: list[dict] = []

    def fake(title, text, sound):
        sent.append({"title": title, "text": text, "sound": sound})
        return True, "ok"

    monkeypatch.setattr(notify_mod, "_notify_macos", fake)
    return sent


def _write_sidecar(directory: Path, *, code: str = "600320", name: str = "振华重工",
                   good: tuple[str, ...] = ("douyin", "kuaishou"),
                   bad: tuple[tuple[str, str], ...] = (), moment: datetime | None = None) -> Path:
    """造一份子进程会写出来的 sidecar，并把它钉在指定的 mtime 上。

    mtime 必须显式设置：``_inspect_run`` 按「开跑之后出现的文件」找，而测试里的
    时钟是 2026-09-24，跟跑测试的真实时刻没有关系。
    """
    payload = {
        "stock_code": code,
        "stock_name": name,
        "approved_script": {"title": f"{name}（{code}）：标题"},
        "publish": {
            "metadata": {"title": f"{name}（{code}）：标题"},
            "platforms": [{"platform": p, "label": LABELS[p], "ok": True, "detail": ""} for p in good]
            + [{"platform": p, "label": LABELS[p], "ok": False, "detail": detail} for p, detail in bad],
        },
    }
    path = directory / f"{code}_20260924_080000.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    stamp = (moment or datetime(2026, 9, 24, 7, 32)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _notifying_daemon(tmp_path: Path, clock: Clock, recorder: Recorder, **flags) -> Daemon:
    config = _config(tmp_path)
    config["notify"] = {"enabled": True, "channels": ["macos"], **flags}
    return Daemon(config, root=tmp_path, now_fn=clock, sleep_fn=lambda _seconds: None, runner=recorder)


def _run_the_morning_slot(daemon: Daemon, clock: Clock) -> None:
    daemon.tick()                       # 先落一次计划
    clock.jump_to(_first_plan(daemon).when + timedelta(minutes=1))
    daemon.tick()


def test_a_successful_run_reports_topic_platforms_and_title(tmp_path: Path, monkeypatch) -> None:
    sent = _capture_notifications(monkeypatch)
    clock = Clock("2026-09-24T06:00:00")
    daemon = _notifying_daemon(tmp_path, clock, Recorder(exit_code=0))
    _write_sidecar(tmp_path)

    _run_the_morning_slot(daemon, clock)

    assert len(sent) == 1
    assert sent[0]["title"] == "✅ 9-24 早档发布完成 · 振华重工(600320)"
    assert "成功: 抖音、快手" in sent[0]["text"]
    assert "振华重工（600320）：标题" in sent[0]["text"]


def test_a_partial_run_reports_the_failed_platforms_and_how_to_republish(tmp_path: Path, monkeypatch) -> None:
    """「发到 3/5 个平台」在无人值守里是常态（--continue-anyway），
    通知必须说清哪几个没发、以及怎么补 —— 否则这条消息没有行动价值。"""
    sent = _capture_notifications(monkeypatch)
    clock = Clock("2026-09-24T06:00:00")
    daemon = _notifying_daemon(tmp_path, clock, Recorder(exit_code=0))
    _write_sidecar(tmp_path, good=("douyin", "kuaishou", "tencent"),
                   bad=(("bilibili", "\x1b[31m601 风控冷却中\x1b[0m"), ("baijiahao", "凭证过期")))

    _run_the_morning_slot(daemon, clock)

    text = sent[0]["text"]
    assert sent[0]["title"] == "⚠️ 9-24 早档只发出去 3/5 · 振华重工(600320)"
    assert "失败: B站 —— 601 风控冷却中" in text      # ANSI 颜色码已被剥掉
    assert "补发: tangulunjin 600320 --republish" in text


def test_a_run_that_never_reached_publishing_reports_the_child_reason(tmp_path: Path, monkeypatch) -> None:
    """选题拉不到榜、F10 缺块都会在渲染之前就退出，那时 sidecar 根本不存在 ——
    唯一的线索是子进程自己打出来的那一行。"""
    sent = _capture_notifications(monkeypatch)
    clock = Clock("2026-09-24T06:00:00")
    daemon = _notifying_daemon(tmp_path, clock, Recorder(exit_code=1, reason="选题失败 —— tongstock 拉不到热门榜"))

    _run_the_morning_slot(daemon, clock)

    assert sent[0]["title"] == "❌ 9-24 早档没发出去"
    assert "原因: 选题失败 —— tongstock 拉不到热门榜" in sent[0]["text"]
    assert "45 分钟后自动重试" in sent[0]["text"]


def test_a_timed_out_run_says_it_was_killed(tmp_path: Path, monkeypatch) -> None:
    sent = _capture_notifications(monkeypatch)
    clock = Clock("2026-09-24T06:00:00")
    daemon = _notifying_daemon(tmp_path, clock, Recorder(exit_code=-9))
    daemon._runner = lambda command, root, log_path, timeout: RunOutcome(
        exit_code=-9, tail="", timed_out=True)  # type: ignore[assignment]

    _run_the_morning_slot(daemon, clock)

    assert "超过" in sent[0]["text"] and "被终止" in sent[0]["text"]


def test_a_missed_slot_is_reported_and_several_missed_slots_collapse_into_one(
        tmp_path: Path, monkeypatch) -> None:
    """机器关两天再打开会一次结算掉好几档。一次发五条「漏了」只会被静音 ——
    那等于把这条通道也废掉。"""
    sent = _capture_notifications(monkeypatch)
    clock = Clock("2026-09-24T06:00:00")
    daemon = _notifying_daemon(tmp_path, clock, Recorder())
    daemon.tick()
    clock.jump_to(datetime(2026, 9, 26, 23, 0))

    daemon.tick()

    assert len(sent) == 1
    assert sent[0]["title"] == "⏰ 3 档漏了"
    assert "共 3 档没发" in sent[0]["text"]
    assert "不补发" in sent[0]["text"]


def test_an_unexpected_restart_is_reported(tmp_path: Path, monkeypatch) -> None:
    """pid 文件还在、进程却没了 = 上次不是干净退出。这个必须报：守护进程最糟的
    状态是安静，而「它死过一次」正是你完全可能没察觉的那种事。"""
    sent = _capture_notifications(monkeypatch)
    clock = Clock("2026-09-24T06:00:00")
    daemon = _notifying_daemon(tmp_path, clock, Recorder())
    daemon.pid_path.write_text("999999", encoding="utf-8")   # 早已不存在的进程

    daemon.run_forever(max_ticks=1)

    assert [item["title"] for item in sent] == ["🔁 守护进程重启"]


def test_a_clean_restart_says_nothing(tmp_path: Path, monkeypatch) -> None:
    sent = _capture_notifications(monkeypatch)
    daemon = _notifying_daemon(tmp_path, Clock("2026-09-24T06:00:00"), Recorder())

    daemon.run_forever(max_ticks=1)      # 第一次启动：pid 文件本来就不该在

    assert sent == []


def test_nothing_is_sent_when_notify_is_not_configured(tmp_path: Path, monkeypatch) -> None:
    """不带 notify 段的配置（测试、库调用）必须一次都不打扰人。"""
    sent = _capture_notifications(monkeypatch)
    clock = Clock("2026-09-24T06:00:00")
    daemon = _daemon(tmp_path, clock, Recorder(exit_code=0))
    _write_sidecar(tmp_path)

    _run_the_morning_slot(daemon, clock)

    assert sent == []


def test_status_shows_the_last_result_so_nobody_has_to_read_the_log(tmp_path: Path, monkeypatch) -> None:
    _capture_notifications(monkeypatch)
    clock = Clock("2026-09-24T06:00:00")
    daemon = _notifying_daemon(tmp_path, clock, Recorder(exit_code=0))
    _write_sidecar(tmp_path)

    _run_the_morning_slot(daemon, clock)
    lines = daemon.status_lines()

    assert any(line.startswith("最近: ✅ 9-24 早档 发布完成 · 振华重工(600320)") for line in lines)
    assert any(line.startswith("反馈: macos") for line in lines)


def test_the_last_result_survives_a_restart(tmp_path: Path, monkeypatch) -> None:
    """状态写在 .daemon_state.json 里，所以重启后的 --daemon-status 仍然答得出
    「上一次发出去没有」。"""
    _capture_notifications(monkeypatch)
    clock = Clock("2026-09-24T06:00:00")
    daemon = _notifying_daemon(tmp_path, clock, Recorder(exit_code=0))
    _write_sidecar(tmp_path)
    _run_the_morning_slot(daemon, clock)

    fresh = _notifying_daemon(tmp_path, clock, Recorder())

    assert any("振华重工(600320)" in line for line in fresh.status_lines())


def test_notify_test_command_reports_every_channel(tmp_path: Path, monkeypatch, capsys) -> None:
    """--notify-test 存在的意义：别等出事那天才发现「配了但收不到」。"""
    from stocktalk.cli import _notify_test

    sent = _capture_notifications(monkeypatch)
    code = _notify_test({"notify": {"enabled": True, "channels": [
        "macos", {"kind": "webhook", "url": ""},
    ]}})

    assert code == 0
    assert sent and sent[0]["title"] == "🔔 通道自检"
    output = capsys.readouterr().out
    assert "webhook: 跳过（url 为空 —— 还没启用）" in output


def test_notify_test_fails_loudly_when_feedback_is_switched_off(capsys) -> None:
    from stocktalk.cli import _notify_test

    assert _notify_test({"notify": {"enabled": False, "channels": ["macos"]}}) == 1
    assert "notify.enabled 是 false" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 装服务：不信任 launchctl 的返回值
# ---------------------------------------------------------------------------


def _fake_launchctl_calls(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(daemon_mod.subprocess, "run", runner)
    return calls


def test_install_verifies_the_service_instead_of_trusting_launchctl(tmp_path: Path, monkeypatch) -> None:
    """legacy ``launchctl load`` 在失败时**也返回 0**（实测从无 GUI 会话的 shell 里
    对任何 plist 都报 EIO 而退出码是 0）。只信返回值的话，输出会说「已加载」，
    而实际上服务根本没注册 —— 于是守护进程不会跑，也就没有发布、更没有反馈。"""
    monkeypatch.setattr(daemon_mod, "plist_path", lambda: tmp_path / "daemon.plist")
    monkeypatch.setattr(daemon_mod, "launchd_state", lambda: "launchd: 未加载")
    calls = _fake_launchctl_calls(monkeypatch)
    daemon = _daemon(tmp_path, Clock("2026-09-24T06:00:00"), Recorder())

    outcome = daemon_mod.install_launchd(daemon)

    assert outcome.loaded is False
    text = "\n".join(outcome.steps)
    assert "这个服务查不到" in text
    assert "launchctl load -w" in text          # 告诉人具体跑哪一行
    assert (tmp_path / "daemon.plist").is_file()
    assert ["launchctl", "unload", "-w", str(tmp_path / "daemon.plist")] in calls
    assert ["launchctl", "load", "-w", str(tmp_path / "daemon.plist")] in calls


def test_install_clears_the_disabled_flag_that_unload_leaves_behind(tmp_path: Path, monkeypatch) -> None:
    """``unload -w`` 会把 label 写进 overrides 数据库标成 disabled。不清掉它，
    下一次 load 就会静默失败 —— 也就是「重装一次守护进程＝守护进程消失」。"""
    monkeypatch.setattr(daemon_mod, "plist_path", lambda: tmp_path / "daemon.plist")
    monkeypatch.setattr(daemon_mod, "launchd_state", lambda: "launchd: state=running pid=1")
    calls = _fake_launchctl_calls(monkeypatch)
    daemon = _daemon(tmp_path, Clock("2026-09-24T06:00:00"), Recorder())

    outcome = daemon_mod.install_launchd(daemon)

    assert outcome.loaded is True
    assert ["launchctl", "enable", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"] in calls
    assert any("已加载" in step for step in outcome.steps)
    assert any("跑完的结果会推到" in step for step in outcome.steps)


def test_install_refuses_to_claim_success_in_the_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:
    """没加载上就退非零 —— 包一层脚本的人才能发现。"""
    from stocktalk.cli import main

    monkeypatch.setattr(daemon_mod, "plist_path", lambda: tmp_path / "daemon.plist")
    monkeypatch.setattr(daemon_mod, "launchd_state", lambda: "launchd: 未加载")
    _fake_launchctl_calls(monkeypatch)

    with pytest.raises(SystemExit) as exit_info:
        main(["--daemon-install", "--output-dir", str(tmp_path)])

    assert exit_info.value.code == 1
    assert "终端" in capsys.readouterr().out
