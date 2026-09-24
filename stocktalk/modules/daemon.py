"""守护进程 —— 常驻的发布调度器，替掉 cron。

它做四件事，每一件都是为了让「无人值守」真的成立：

1. **到点就跑**：排期逻辑全在 :mod:`stocktalk.modules.scheduler`，这里只管问一句
   「现在该不该发」，该发就把整条链路作为子进程跑起来。
2. **闲着不占东西**：不轮询。它问排期引擎「下一次该睁眼是几时」，然后一觉睡过去 ——
   闲时可能是九个小时后。睡觉期间零 CPU、零磁盘，状态也只在真的变了才落盘
   （见 :meth:`Scheduler.save`）。所以它在后台基本看不见。
3. **错过就认**：睡眠或关机导致窗口过去了，就不发了 —— 不做事后补发，理由见
   :mod:`stocktalk.modules.scheduler`（迟到的时刻比不发更假）。关机后由 launchd 的
   ``RunAtLoad`` 拉回来。
4. **把意外吃掉**：子进程非零退出不中断守护进程，按 ``retry_minutes`` 退避重试，
   累计 ``max_attempts`` 次才判死；登录失效由 ``--continue-anyway`` 在子进程内部降级成
   「发到还活着的平台」，而不是整条放弃。留痕同样重要：每次运行的完整输出追加到日志，
   退出码和选中的票写进状态文件，``--daemon-status`` 一眼能看。

5. **报结果**：日志和状态文件都躺在 ``output/`` 里 —— 没人会主动去看一个「应该没事」
   的目录。所以每次跑完（成功、部分成功、失败、错过窗口、被异常拉起）都调一次
   :mod:`stocktalk.modules.notify`，把结果推到你真正会看的地方。通知是**旁路**：
   发不出去只写一行日志，绝不影响发布本身。

跑的是**子进程**而不是 in-process 调用：一次全链路要十几分钟，中途崩掉不该带走守护
进程；而且这样「守护进程跑的那条命令」和「你手敲的那条命令」是同一个，行为不会分叉。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from xml.sax.saxutils import escape as xml_escape

from stocktalk.modules.notify import (
    FAILURE,
    KIND_LABELS,
    KIND_MARKS,
    MISSED,
    PARTIAL,
    RESTART,
    SUCCESS,
    RunReport,
    build_notifier,
    window_label,
)
from stocktalk.modules.scheduler import (
    ScheduleConfigError,
    Scheduler,
    parse_windows,
)

LAUNCHD_LABEL = "com.tangulunjin.daemon"
RUN_TIMEOUT_SECONDS = 7200
TAIL_LINES = 12
# 一次 tick 至少间隔这么久，防止「下一次睁眼」算到过去时进入忙等。
MIN_SLEEP_SECONDS = 1.0
# 没有任何未来计划时（窗口被清空、时钟被改），隔多久回头看一眼。
IDLE_SLEEP_SECONDS = 900.0

# 「选题（tongstock 热门榜）：名称(代码)」——cli.py 里这行格式是固定的，用来把
# 「今天发了哪只」记进状态，这样 --daemon-status 能直接告诉你结果而不是一个退出码。
_TOPIC_PATTERN = re.compile(r"选题（tongstock 热门榜）：(.+?)\((\d{6})\)")

# cli.py 里失败原因那几行也是固定的格式，子进程一旦非零退出，从全部输出里捞最后一条
# 命中。**最后一条**：中间那行「发布预检：X 登录态失效」在 --continue-anyway 下不是
# 结束原因，后面真正让它退出的是别的。
_REASON_PATTERNS = (
    re.compile(r"生成失败：(.+)"),
    re.compile(r"选题失败：(.+)"),
    re.compile(r"发布失败：(.+)"),
    re.compile(r"补发失败：(.+)"),
    re.compile(r"发布预检：(.+?登录态失效)"),
)


class DaemonError(RuntimeError):
    """守护进程没法跑起来（配置错了、launchd 不在等）。"""


@dataclass
class RunOutcome:
    exit_code: int
    topic: str = ""
    tail: str = ""
    timed_out: bool = False
    reason: str = ""   # 子进程自己打出来的失败原因（没走到发布时唯一的线索）


@dataclass
class TickResult:
    action: str  # ran | idle | skipped
    detail: str = ""


class Daemon:
    """一次守护进程的生命周期：配置 → 循环 → 每次跑一条完整链路。"""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        root: Path,
        now_fn: Callable[[], datetime] = datetime.now,
        sleep_fn: Callable[[float], None] = time.sleep,
        runner: Callable[[Sequence[str], Path, Path, float], RunOutcome] | None = None,
    ) -> None:
        self.config = config
        self.root = Path(root).resolve()
        settings = dict(config.get("daemon") or {})
        self.settings = settings
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn
        self._runner = runner or _run_subprocess

        output_dir = Path(config.get("output", {}).get("dir", "output"))
        if not output_dir.is_absolute():
            output_dir = self.root / output_dir
        self.output_dir = output_dir

        self.max_sleep_seconds = max(MIN_SLEEP_SECONDS, float(settings.get("max_sleep_seconds", 900)))
        self.run_timeout = float(settings.get("run_timeout_seconds", RUN_TIMEOUT_SECONDS))
        self.heartbeat_hours = float(settings.get("heartbeat_hours", 6))
        self.continue_on_login_failure = bool(settings.get("continue_on_login_failure", True))
        # 掉线的平台先自己试着登回来。目前只有视频号做得到（登录页的「微信快捷登录」，
        # 本机微信在跑就免扫码），而且它只会在**确认失效**时才动手，所以打开没有代价；
        # 半夜三点没人能扫码，这条是守护进程唯一能自救的机会。
        self.auto_login = bool(settings.get("auto_login", True))
        self.extra_args = [str(item) for item in (settings.get("extra_args") or [])]
        self.log_path = self._under_output(settings.get("log_file", "daemon.log"))
        self.pid_path = self._under_output(".daemon.pid")
        # 反馈通道（本机通知 / 手机推送 / 邮件）。logger 用它记「哪条通道没发出去」——
        # 通知自己坏了也要留痕，否则又会退化成「安静地失败」。
        self.notifier = build_notifier(config, root=self.root, logger=self._log)

        try:
            windows = parse_windows(settings.get("windows") or ())
        except ScheduleConfigError:
            raise
        self.scheduler = Scheduler(
            self._under_output(settings.get("state_file", ".daemon_state.json")),
            windows=windows,
            retry_minutes=float(settings.get("retry_minutes", 45)),
            max_attempts=int(settings.get("max_attempts", 3)),
            # 只有写测试或复现「今天怎么挑了这个时刻」时才需要固定它。
            seed=settings.get("seed"),
        )

    # -- 配置 -------------------------------------------------------------

    def _under_output(self, name: Any) -> Path:
        path = Path(str(name))
        return path if path.is_absolute() else self.output_dir / path

    @property
    def enabled_windows(self) -> list[str]:
        return [window.label for window in self.scheduler.windows]

    def command(self) -> list[str]:
        """每次运行执行的命令。手敲同一条即可复现守护进程的行为。"""
        cmd = [sys.executable, "-m", "stocktalk", "--auto-pick", "--publish", "--no-interactive"]
        if self.auto_login:
            cmd.append("--auto-login")
        if self.continue_on_login_failure:
            cmd.append("--continue-anyway")
        cmd.extend(self.extra_args)
        return cmd

    # -- 日志 -------------------------------------------------------------

    def _log(self, message: str, *, level: str = "INFO") -> None:
        stamp = self.now_fn().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} | {level:<7} | {message}"
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass  # 日志写不进去不该让守护进程倒下
        if self._stdout_is_log():
            return  # launchd 已经把 stdout 接到这个文件上，print 就是写第二遍
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass  # stdout 可能是已经断掉的管道（父进程先走一步），日志文件才是准的

    def _stdout_is_log(self) -> bool:
        """stdout 是否**就是**这个日志文件本身。

        plist 的 ``StandardOutPath`` 指的就是 ``daemon.log``（这样守护进程崩掉时
        的 traceback 也能留在同一个地方）。但 ``_log`` 自己也会往那儿写，于是每一
        行都会出现两次 —— 比对 inode 就能知道 stdout 是不是同一个文件。
        """
        try:
            out = os.fstat(sys.stdout.fileno())
            log = os.stat(self.log_path)
        except (OSError, ValueError, AttributeError):
            return False
        return (out.st_dev, out.st_ino) == (log.st_dev, log.st_ino)

    # -- pid --------------------------------------------------------------

    def write_pid(self) -> None:
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def clear_pid(self) -> None:
        try:
            self.pid_path.unlink()
        except OSError:
            pass

    def running_pid(self) -> int | None:
        """守护进程是否活着（读 pid 文件 + 探一次信号 0）。"""
        try:
            pid = int(self.pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        if pid == os.getpid():
            return pid
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return pid

    # -- 主循环 -----------------------------------------------------------

    def run_forever(self, *, max_ticks: int | None = None) -> int:
        if not self.scheduler.windows:
            self._log("daemon.windows 是空的 —— 没有窗口就永远没有该发的时刻，退出。", level="ERROR")
            return 1
        # 上一次的 pid 文件还在、而那个进程已经没了 —— 说明上次不是干净退出（被 kill -9、
        # 断电、或者 launchd 拉起了一次崩溃）。**这个要报**：守护进程最糟的状态是安静，
        # 而「它死过一次」正是你可能完全没察觉的那种事。
        stale = self.pid_path.exists() and self.running_pid() is None
        self.write_pid()
        self._log(f"守护进程启动 pid={os.getpid()} 窗口={', '.join(self.enabled_windows)}")
        self._log(f"每次运行: {' '.join(self.command())}")
        self._log(f"待机策略: 不轮询，睡到下一个发布时刻（最长 {self.max_sleep_seconds / 60:g} 分钟）")
        if stale:
            self._log("上次的 pid 文件还在且进程已不在 —— 上次是异常退出，已接管。", level="WARN")
            self._notify(RunReport(
                kind=RESTART, at=self.now_fn().isoformat(timespec="seconds"),
                detail="上次是异常退出（pid 文件没清干净），已被拉起并接管排期",
                next_at=self._next_at_text(), log_path=str(self.log_path)))
        previous = _catch_sigterm()
        ticks = 0
        try:
            while True:
                self.tick()
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    return 0
                self.sleep_fn(self.sleep_seconds())
        except KeyboardInterrupt:
            self._log("收到中断，退出。")
            return 0
        finally:
            self.clear_pid()
            _restore_signal(previous)

    def sleep_seconds(self) -> float:
        """这一觉睡多久 —— 到下一个发布时刻的距离，封顶 ``max_sleep_seconds``。

        这是「不消耗资源」的落点：闲置时不是一分钟一次地问「到点了吗」，而是一觉睡到
        下一个该睁眼的时刻（可能隔一整夜）。封顶只是兜底 —— 系统时钟被改、未来几天都
        没有排期时，总还能醒过来看一眼。
        """
        now = self.now_fn()
        wake = self.scheduler.next_wake_at(now)
        if wake is None:
            return min(IDLE_SLEEP_SECONDS, self.max_sleep_seconds)
        return min(max((wake - now).total_seconds(), MIN_SLEEP_SECONDS), self.max_sleep_seconds)

    def tick(self) -> TickResult:
        now = self.now_fn()
        decision = self.scheduler.decide(now)
        self.scheduler.prune(now.date())
        # 只在状态真的变了才写盘：空闲的守护进程一整夜不碰一次磁盘。
        self.scheduler.save()

        # 错过档位不能一声不吭 —— 那正是「以为它在跑、其实早停了」的来源。
        dropped = self.scheduler.last_dropped
        for plan in dropped:
            self._log(f"{plan.day} {plan.window} 这一档没发：{plan.note}", level="WARN")
        if dropped:
            self._notify_missed(dropped, now)

        self._maybe_heartbeat(now)

        if decision.plan is None:
            return TickResult(action="idle", detail=decision.reason)

        plan = decision.plan
        self.scheduler.claim(plan, now)
        self._log(f"开始发布 {plan.window} —— 第 {plan.attempts} 次尝试")

        started = self.now_fn()
        outcome = self._runner(self.command(), self.root, self.log_path, self.run_timeout)
        finished = self.now_fn()
        elapsed = (finished - started).total_seconds() / 60
        report = self._build_report(plan, outcome, started, finished, elapsed)
        self.scheduler.finish(plan, finished, exit_code=outcome.exit_code,
                              note=outcome.tail, topic=outcome.topic or plan.topic,
                              result=report.summary())

        if outcome.exit_code == 0:
            self._log(f"发布成功，用时 {elapsed:.1f} 分钟"
                      + (f"，选题 {outcome.topic}" if outcome.topic else ""))
        else:
            retry_left = self.scheduler.max_attempts - plan.attempts
            hint = (f"{self.scheduler.retry_minutes:g} 分钟后重试（还剩 {retry_left} 次）"
                    if retry_left > 0 else "已达重试上限，这一档放弃")
            self._log(f"发布失败 exit={outcome.exit_code}"
                      + ("（超时被杀）" if outcome.timed_out else "")
                      + f"，用时 {elapsed:.1f} 分钟；{hint}", level="ERROR")
            if outcome.tail:
                self._log("  末尾输出: " + outcome.tail.replace("\n", " ⏎ ")[:400], level="ERROR")

        report.next_at = self._next_at_text()
        self._notify(report)
        return TickResult(action="ran", detail=plan.window)

    # -- 报告 -------------------------------------------------------------

    def _build_report(self, plan: Any, outcome: RunOutcome, started: datetime,
                      finished: datetime, elapsed: float) -> RunReport:
        """把「退出码 + 末尾输出 + sidecar」拼成一份人能读的结果。

        三层信息，可靠性递减：
        1. **sidecar** —— 子进程自己写的 ``output/<code>_<ts>.json``，里面有平台级
           结果、视频标题、成片路径。这是唯一能说清「哪几个平台成了」的来源。
        2. **子进程输出的失败原因** —— 没走到发布就挂了（选题拉不到榜、数据缺块、
           渲染崩溃）时，sidecar 不存在，只能听子进程自己说。
        3. **退出码** —— 前两者都没有时的兜底。
        """
        run = self._inspect_run(started)
        topic = run.get("topic") or outcome.topic or plan.topic
        succeeded = list(run.get("succeeded") or [])
        failed = list(run.get("failed") or [])

        if outcome.timed_out:
            kind = FAILURE
        elif outcome.exit_code == 0 and not failed:
            kind = SUCCESS
        elif succeeded:
            kind = PARTIAL     # 有平台发出去了就不算全败，措辞也要跟着变
        else:
            kind = FAILURE

        reason = ""
        if kind == FAILURE:
            if outcome.timed_out:
                reason = f"超过 {self.run_timeout / 60:g} 分钟没跑完，被终止"
            else:
                reason = outcome.reason or f"子进程退出码 {outcome.exit_code}（详见日志末尾）"

        will_retry = outcome.exit_code != 0 and plan.attempts < self.scheduler.max_attempts
        code = _code_of(topic)
        return RunReport(
            kind=kind,
            day=plan.day,
            window=plan.window,
            at=finished.isoformat(timespec="seconds"),
            topic=topic,
            title=str(run.get("title") or ""),
            video=str(run.get("video") or ""),
            succeeded=succeeded,
            failed=failed,
            reason=reason,
            elapsed_minutes=elapsed,
            attempt=plan.attempts,
            max_attempts=self.scheduler.max_attempts,
            retry_in_minutes=self.scheduler.retry_minutes if will_retry else None,
            will_retry=will_retry,
            log_path=str(self.log_path),
            republish=f"tangulunjin {code} --republish" if code else "",
        )

    def _inspect_run(self, started: datetime) -> dict[str, Any]:
        """找这次跑出来的 sidecar（``output/*.json``，晚于开跑时刻的最新一个）。

        用 mtime 而不是解析 stdout：一次运行只跑一只票（``--auto-pick``），所以
        「开跑之后新出现的 sidecar」不可能有歧义。找不到也不算错 —— 没走到渲染就
        挂了的时候本来就没有，调用方会退回用子进程输出说话。

        两个坑：
        * ``pathlib`` 的 ``glob`` **会**匹配隐藏文件（跟 ``glob`` 模块不一样），而
          ``.daemon_state.json`` 正好在开跑和结束时各被写一次，mtime 永远比 sidecar
          新 —— 不排掉它就每次都读到自己写的状态文件，然后一路静默地报「没有平台信息」。
        * 认文件靠内容（有 ``stock_code``）而不是靠文件名规则：名字格式将来会变，
          而「sidecar 里必然有股票代码」是这个数据结构的定义。
        """
        cutoff = started.timestamp() - 60  # 容忍一点文件系统时间精度/时钟抖动
        candidates: list[tuple[float, Path]] = []
        try:
            paths = list(self.output_dir.glob("*.json"))
        except OSError:
            return {}
        for path in paths:
            if path.name.startswith("."):
                continue
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        for mtime, path in sorted(candidates, reverse=True):
            if mtime < cutoff:
                break
            data = _load_json(path)
            if not data.get("stock_code"):
                continue
            publish = data.get("publish") or {}
            entries = [item for item in (publish.get("platforms") or []) if isinstance(item, dict)]
            succeeded = [str(item.get("label") or item.get("platform") or "")
                         for item in entries if item.get("ok")]
            failed = [{"label": str(item.get("label") or item.get("platform") or ""),
                       "platform": str(item.get("platform") or ""),
                       "detail": str(item.get("detail") or "")} for item in entries if not item.get("ok")]
            video = str(data.get("video_path") or "")
            return {
                "topic": (f"{data.get('stock_name')}({data.get('stock_code')})"
                          if data.get("stock_name") else ""),
                "title": str((publish.get("metadata") or {}).get("title")
                             or (data.get("approved_script") or {}).get("title") or ""),
                "video": video if video and Path(video).is_file() else str(path.with_suffix(".mp4")),
                "succeeded": succeeded,
                "failed": failed,
            }
        return {}

    def _notify_missed(self, dropped: Sequence[Any], now: datetime) -> None:
        """错过窗口也要报。

        但**聚合成一条**：机器关了一整夜再打开时，可能一次结算掉好几档，发五条
        「漏了」只会让人把通知静音 —— 那等于把这条通道也废掉。
        """
        last = dropped[-1]
        if len(dropped) == 1:
            detail = str(last.note or "窗口已过")
            window = last.window
            headline = ""
        else:
            # 合起来报的时候没有单一档位可挂：「共 3 档没发」才是要说的那句话。
            labels = "、".join(f"{_short_day(plan.day)} {window_label(plan.window)}" for plan in dropped)
            detail = f"共 {len(dropped)} 档没发（{labels}）：窗口已过，按规则不补发"
            window = ""
            headline = f"{KIND_MARKS[MISSED]} {len(dropped)} 档漏了"
        self._notify(RunReport(kind=MISSED, day=last.day, window=window,
                               at=now.isoformat(timespec="seconds"), detail=detail,
                               headline_text=headline,
                               next_at=self._next_at_text(), log_path=str(self.log_path)))

    def _next_at_text(self) -> str:
        wake = self.scheduler.next_wake_at(self.now_fn())
        return f"{wake:%m-%d %H:%M}" if wake else ""

    def _notify(self, report: RunReport) -> None:
        """发反馈。通道出错只写日志 —— 通知是旁路，坏了也不许影响排期。"""
        for result in self.notifier.send(report):
            if not result["ok"]:
                continue  # Notifier 自己已经通过 logger 记过失败原因
            self._log(f"反馈已送出（{result['kind']}）：{report.headline()}")

    def _maybe_heartbeat(self, now: datetime) -> None:
        """每隔几小时说一句「我还活着，下一次是 X」。

        守护进程最糟的状态不是报错，而是安静：你会分不清它是「今天没事干」还是
        「昨晚上就死了」。所以宁可定期用一行日志证明它还在看表。
        """
        last = _maybe_dt(self.scheduler.state.get("last_heartbeat"))
        if last is not None and (now - last).total_seconds() < self.heartbeat_hours * 3600:
            return
        wake = self.scheduler.next_wake_at(now)
        tail = f"，下一次 {wake:%m-%d %H:%M}" if wake else "，未来几天没有排期"
        self._log(f"心跳：守护进程在看表{tail}")
        self.scheduler.note_heartbeat(now)
        self.scheduler.save()

    # -- 状态 -------------------------------------------------------------

    def status_lines(self) -> list[str]:
        now = self.now_fn()
        # 先把今天/明天的计划落定并结算，再报告。理由有二：一是「今天几点发」正是看
        # status 的目的，空着等于没回答；二是计划一旦固化就不会再变，于是
        # 「先看一眼、再去装服务」看到的时刻和真正跑起来的时刻是同一个。
        self.scheduler.refresh(now)
        self.scheduler.save()
        lines: list[str] = []
        pid = self.running_pid()
        lines.append(f"守护进程: {'运行中 pid=' + str(pid) if pid else '未运行'}")
        lines.append(f"发布窗口: {', '.join(self.enabled_windows) or '（未配置）'}")
        lines.append(f"重试: 失败后 {self.scheduler.retry_minutes:g} 分钟重试，"
                     f"最多 {self.scheduler.max_attempts} 次")
        wake = self.scheduler.next_wake_at(now)
        lines.append(f"下一次: {wake:%m-%d %H:%M}" if wake else "下一次: 未来几天没有排期")
        lines.append(f"待机: 不轮询，只在上面那个时刻睁眼；错过窗口不补发")
        lines.append(f"反馈: {self._notify_channels_text()}")
        lines.append(f"日志: {self.log_path}")
        last = self._last_result_text()
        if last:
            lines.append(f"最近: {last}")
        lines.append("")
        lines.extend(self.scheduler.status_lines(now))
        return lines

    def _notify_channels_text(self) -> str:
        ready = [name for name, ok, _ in self.notifier.channels_ready() if ok]
        skipped = [name for name, ok, _ in self.notifier.channels_ready() if not ok]
        if not self.notifier.enabled:
            return "关闭（notify.enabled 是 false）"
        text = "、".join(ready) or "无可用通道"
        if skipped:
            text += f"（{'、'.join(skipped)} 未配置）"
        return text

    def _last_result_text(self) -> str:
        """上一档的结果 —— 不用翻日志就能回答「上一次到底发出去没有」。"""
        last = self.scheduler.state.get("last_result")
        if not isinstance(last, dict) or not last.get("kind"):
            return ""
        mark = KIND_MARKS.get(str(last["kind"]), "🔔")
        head = f"{mark} {last.get('slot') or ''} {KIND_LABELS.get(str(last['kind']), last['kind'])}".strip()
        if last.get("topic"):
            head += f" · {last['topic']}"
        bits = [head]
        if last.get("succeeded"):
            bits.append("成功 " + "、".join(str(item) for item in last["succeeded"]))
        if last.get("failed"):
            bits.append("失败 " + "、".join(str(item) for item in last["failed"]))
        if last.get("reason"):
            bits.append(str(last["reason"]))
        return "  ".join(bits)


# ---------------------------------------------------------------------------
# 子进程执行
# ---------------------------------------------------------------------------

def _run_subprocess(command: Sequence[str], root: Path, log_path: Path,
                    timeout: float) -> RunOutcome:
    """跑一条完整链路，输出实时落日志，返回退出码 + 末尾输出。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    collected: list[str] = []
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n{'=' * 70}\n$ {' '.join(command)}\n(cwd={root})\n{'-' * 70}\n")
        log.flush()
        process = subprocess.Popen(
            list(command), cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, stdin=subprocess.DEVNULL,
        )

        def pump() -> None:
            # 单独一条线程读管道：主线程要留给超时看门狗。管道满了会死锁，所以
            # 必须边读边写，不能等 wait() 之后再读。
            assert process.stdout is not None
            for line in process.stdout:
                collected.append(line.rstrip("\n"))
                del collected[:-400]
                log.write(line)
                log.flush()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
            exit_code = -9
        reader.join(timeout=5)
        log.write(f"{'-' * 70}\nexit={exit_code}{' (timeout)' if timed_out else ''}\n")
        log.flush()

    tail = "\n".join(collected[-TAIL_LINES:])
    topic = ""
    reason = ""
    for line in collected:
        match = _TOPIC_PATTERN.search(line)
        if match:
            topic = f"{match.group(1)}({match.group(2)})"
        for pattern in _REASON_PATTERNS:
            found = pattern.search(line)
            if found:
                reason = found.group(1).strip()
                break
    return RunOutcome(exit_code=exit_code, topic=topic, tail=tail, timed_out=timed_out,
                      reason=reason)


# ---------------------------------------------------------------------------
# launchd（macOS）：让守护进程开机自启、崩了自动拉起
# ---------------------------------------------------------------------------

def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _launchd_path(daemon: Daemon) -> str:
    """launchd 能看到的 PATH。

    刻意**不继承**当前 shell 的 PATH：装服务的那一刻可能在任意环境里（编辑器内置
    终端、沙箱、别的工具的子进程），把那份 PATH 抄进 plist，服务以后就一直用错的
    路径。改成一份固定的基础清单 —— launchd 找不到 node/npx 的话渲染会直接失败，
    这个清单要覆盖 Homebrew（Apple Silicon 的 /opt/homebrew 与 Intel 的
    /usr/local）、uv/tongstock 所在的 ~/.local/bin，以及当前 venv。想再加自己的
    目录，配 ``daemon.path``。
    """
    candidates = [
        Path.home() / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path(sys.executable).parent,
    ]
    candidates.extend(Path(str(item)).expanduser() for item in (daemon.settings.get("path") or []))
    candidates.extend([Path("/usr/bin"), Path("/bin"), Path("/usr/sbin"), Path("/sbin")])

    ordered: list[str] = []
    for path in candidates:
        text = str(path)
        if text not in ordered:
            ordered.append(text)
    return ":".join(ordered)


def plist_body(daemon: Daemon) -> str:
    """生成 LaunchAgent 描述文件。

    ``KeepAlive`` + ``RunAtLoad`` 解决两件事：开机/登录后自己回来（不用手动起），
    以及进程意外退出后自动重启。系统睡眠时 launchd 会把进程一起挂起，唤醒后继续跑
    —— 正好和排期引擎的「醒来重新结算」咬合。
    """
    log = daemon.log_path
    env_path = _launchd_path(daemon)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{xml_escape(LAUNCHD_LABEL)}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{xml_escape(sys.executable)}</string>
    <string>-m</string>
    <string>stocktalk</string>
    <string>--daemon</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{xml_escape(str(daemon.root))}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>{xml_escape(env_path)}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>{xml_escape(str(log))}</string>
  <key>StandardErrorPath</key>
  <string>{xml_escape(str(log))}</string>
</dict>
</plist>
"""


@dataclass
class InstallOutcome:
    """装服务的结果。``loaded`` 是**查出来的**，不是 launchctl 报的。"""

    steps: list[str]
    loaded: bool


def install_launchd(daemon: Daemon) -> InstallOutcome:
    """写入 plist 并加载。返回给用户看的步骤说明 + 到底加载上没有。

    这里踩过两个坑，都会让「重装一次守护进程」变成「守护进程静默消失」：

    1. ``unload -w`` 会把这个 label 在 overrides 数据库里标成 ``disabled``，
       紧接着的 ``load -w`` 撞上旧进程还没退干净时直接报
       ``Input/output error`` —— 而 **legacy ``load`` 连非零退出码都不给**，
       光看返回值会以为一切正常。
    2. 从没有 GUI 会话权限的 shell（编辑器内置终端、沙箱、SSH）里，
       ``load``/``bootstrap`` 对**任何** plist 都返回 EIO。这种情况下服务根本没注册，
       但输出还是会说「已加载」。

    所以这里不等返回值，自己拿 :func:`launchd_state` 查一遍；没查到时把
    「请在系统自带的终端里跑这行」一起打出来 —— 安静的失败比报错难查得多。
    """
    if sys.platform != "darwin":
        raise DaemonError("launchd 是 macOS 的东西；其他系统请用 systemd --user 或直接前台跑 --daemon")
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist_body(daemon), encoding="utf-8")
    steps = [f"已写入 {path}"]
    domain = f"gui/{os.getuid()}"
    # 先卸再装：重复安装时 launchd 不会自己换掉旧定义。
    subprocess.run(["launchctl", "unload", "-w", str(path)],
                   capture_output=True, text=True, check=False)
    _wait_until_stopped(daemon)
    # unload -w 留下的 disabled 标记要显式清掉（见上面的坑 1）。
    subprocess.run(["launchctl", "enable", f"{domain}/{LAUNCHD_LABEL}"],
                   capture_output=True, text=True, check=False)
    subprocess.run(["launchctl", "load", "-w", str(path)],
                   capture_output=True, text=True, check=False)

    state = launchd_state()
    if "未加载" in state or "not found" in state:
        return InstallOutcome(steps=[
            *steps,
            "⚠ launchctl 没报错，但这个服务查不到 —— 它没有被加载。",
            "  常见原因：当前 shell 所在会话没有权限操作 launchd（编辑器内置终端、沙箱、SSH）。",
            "  在 macOS 自带的「终端.app」里跑下面这行，一步就好：",
            f"    launchctl load -w {path}",
            f"  核对: launchctl print {domain}/{LAUNCHD_LABEL}",
            "  在修好之前，守护进程不会自己跑 —— 也就不会有发布，更不会有反馈。",
        ], loaded=False)
    return InstallOutcome(steps=[
        *steps,
        f"launchctl 已加载（{state}）—— 开机自启、退出自动拉起",
        f"日志: {daemon.log_path}",
        "卸载: tangulunjin --daemon-uninstall",
        "跑完的结果会推到: " + daemon._notify_channels_text(),
    ], loaded=True)


def _wait_until_stopped(daemon: Daemon, *, timeout: float = 15.0, interval: float = 0.5) -> bool:
    """等上一个守护进程真的退出 —— launchd 的卸载是异步的。

    不等就会踩上面那个 EIO：``unload`` 刚返回、旧进程还在收尾，``load`` 就上来了。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if daemon.running_pid() is None:
            return True
        time.sleep(interval)
    return False


def uninstall_launchd() -> list[str]:
    path = plist_path()
    if not path.exists():
        return [f"没有装过（{path} 不存在）"]
    subprocess.run(["launchctl", "unload", "-w", str(path)],
                   capture_output=True, text=True, check=False)
    path.unlink(missing_ok=True)
    return [f"已卸载并删除 {path}"]


def launchd_state() -> str:
    """launchctl 里这个服务当前的状态，安装/排错时看。

    先问 ``launchctl print gui/<uid>/<label>``，而不是先看 ``launchctl list``：
    后者在有些上下文里会返回**空**——实测在本机的一个 shell 里就是 0 行，连系统
    服务都列不出来（沙箱 / 非交互会话都会这样）。只看 list 的话，正在跑的服务会
    被报成「未加载」，而「它到底跑没跑」恰恰是看这行的人唯一想知道的事。
    """
    printed = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
                             capture_output=True, text=True, check=False)
    if printed.returncode == 0:
        state = re.search(r"^\s*state = (\S+)", printed.stdout, re.MULTILINE)
        pid = re.search(r"^\s*pid = (\d+)", printed.stdout, re.MULTILINE)
        bits = [f"state={state.group(1)}" if state else "已加载"]
        if pid:
            bits.append(f"pid={pid.group(1)}")
        return "launchd: " + " ".join(bits)

    listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
    for line in listed.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == LAUNCHD_LABEL:
            return f"launchd: pid={parts[0]} status={parts[1]}"
    return "launchd: 未加载"


def build_daemon(config: Mapping[str, Any], *, root: Path | None = None, **kwargs: Any) -> Daemon:
    """从配置造一个 :class:`Daemon`；窗口写错时给出人话。"""
    root = Path(root) if root else Path.cwd()
    try:
        return Daemon(config, root=root, **kwargs)
    except ScheduleConfigError as exc:
        raise DaemonError(f"daemon.windows 配置有问题：{exc}") from exc


def _catch_sigterm() -> Any:
    """把 SIGTERM 接到与 Ctrl-C 同一条退出路径上，返回原 handler 以便还原。

    launchd 停服务、``launchctl unload``、以及用户的 ``pkill`` 发的都是 SIGTERM；
    Python 的默认行为是**立即终止**，绕开 ``finally``，于是 pid 文件留在磁盘上 ——
    之后 ``--daemon-status`` 可能读到它并误报「运行中」。同一个信号让 Ctrl-C 和
    launchd 走同一条干净退出的路。
    """
    def raise_interrupt(signum: int, frame: Any) -> None:  # noqa: ARG001
        raise KeyboardInterrupt

    try:
        return signal.signal(signal.SIGTERM, raise_interrupt)
    except (ValueError, OSError):
        return None  # 不在主线程（或平台不支持）—— 退出清理只是少一层保险


def _restore_signal(previous: Any) -> None:
    if previous is None:
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except (ValueError, OSError):
        pass


def _maybe_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _short_day(day: str) -> str:
    """``2026-09-24`` → ``9-24``（通知里不需要年份）。"""
    parts = str(day or "").split("-")
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return f"{int(parts[1])}-{int(parts[2])}"
    return str(day or "")


def _code_of(topic: str) -> str:
    """``"振华重工(600320)"`` → ``"600320"``（拼补发命令要用）。"""
    match = re.search(r"\((\d{6})\)", str(topic or ""))
    return match.group(1) if match else ""


def _load_json(path: Path) -> dict[str, Any]:
    """读一个 JSON 对象；坏了/不是对象就当没有（绝不抛）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
