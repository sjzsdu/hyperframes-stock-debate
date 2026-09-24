"""结果反馈 —— 每次运行结束后，把「发了什么、成没成」送到你眼前。

为什么需要它
------------

无人值守的自动化最糟的失败不是报错，而是**安静**：片子没发出去，你却在三天后
才发现。守护进程的日志、状态文件都已经在记，但它们都躺在 ``output/`` 里 ——
没人会主动去看一个「应该没事」的目录。所以每次跑完，都要主动敲一下人。

三条设计线
----------

1. **成功也要报。** 只在失败时说话会制造另一种焦虑：收到通知＝出事，没收到＝
   不知道是没事还是死了。成功一条轻量通知（本机一个横幅），失败一条醒目通知，
   两边都到才算「我知道它干了什么」。
2. **通道可换，内容不变。** 要送到哪去（macOS 通知中心／企业微信机器人／
   邮件／任意命令）和你到底想知道什么（发了哪只票、哪几个平台成了、为什么没成、
   怎么补发）是两件事。:class:`RunReport` 负责后者并只渲染一次，通道负责搬运。
   加一个通道只是加一个 ``kind``。
3. **反馈永远不许弄坏发布。** 网络不通、webhook 失效、邮件密码错 —— 都只写一行
   日志。通知是旁路，不是链路的一环。

**本模块从不抛异常。** 所有通道的失败都被收成 ``(ok, detail)`` 返回给调用方，
由调用方记日志。理由：如果通知失败能让一次成功的发布变成非零退出，那这个功能
的净收益是负的。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import smtplib
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# ---------------------------------------------------------------------------
# 结果的种类
# ---------------------------------------------------------------------------

SUCCESS = "success"    # 配的平台全发成功
PARTIAL = "partial"    # 发出去一部分（另一部分失败/掉线/冷却）
FAILURE = "failure"    # 一个平台都没发出去，或者根本没走到发布
MISSED = "missed"      # 窗口过了，按规则不补发
RESTART = "restart"    # 守护进程被拉起（上次是异常退出）
TEST = "test"          # --notify-test 的自检

KIND_MARKS = {
    SUCCESS: "✅", PARTIAL: "⚠️", FAILURE: "❌",
    MISSED: "⏰", RESTART: "🔁", TEST: "🔔",
}
KIND_LABELS = {
    SUCCESS: "发布完成", PARTIAL: "部分成功", FAILURE: "发布失败",
    MISSED: "错过档位", RESTART: "守护进程重启", TEST: "通道自检",
}
# 通知要吵到什么程度：成功一条横幅就够，失败值得响一下。
KIND_SOUNDS = {SUCCESS: "success", PARTIAL: "failure", FAILURE: "failure"}

DEFAULT_TIMEOUT_SECONDS = 15.0

# sau 的上传结果里带着一长串 ANSI 颜色码（``\x1b[38;2;61;208;141m``）。终端里好看，
# 塞进手机通知就是一堆乱码 —— 渲染正文之前先剥掉。
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def window_label(window: str) -> str:
    """``"18:30-21:00"`` → ``"晚档"``。

    通知的标题最多只扫一眼，「2026-09-24 18:30-21:00 这一档」来不及读，
    而「9-24 晚档」正好能塞进一行标题。
    """
    text = str(window or "").split("-", 1)[0].strip()
    try:
        hour = int(text.split(":", 1)[0])
    except (ValueError, IndexError):
        return "这一档"
    if hour < 5:
        return "凌晨档"
    if hour < 11:
        return "早档"
    if hour < 14:
        return "午档"
    if hour < 18:
        return "下午档"
    if hour < 22:
        return "晚档"
    return "夜档"


def _short_date(day: str) -> str:
    """``2026-09-24`` → ``9-24``（通知标题里不需要年份）。"""
    parts = str(day or "").split("-")
    if len(parts) == 3:
        return f"{int(parts[1])}-{int(parts[2])}" if parts[1].isdigit() and parts[2].isdigit() else day
    return day


@dataclass
class RunReport:
    """一次运行结果的**结构化**版本 —— 渲染与传输都从这里派生。

    只装「你想在手机上看到的东西」：发了哪只票、哪几个平台成了、为什么没成、
    下一步怎么办。日志、退出码、完整输出留在 ``output/daemon.log``，不往手机上搬。
    """

    kind: str
    day: str = ""
    window: str = ""
    at: str = ""                        # 这条结果产生的时刻（ISO），状态文件里要用
    topic: str = ""                     # "振华重工(600320)"
    title: str = ""                     # 视频标题
    video: str = ""
    succeeded: list[str] = field(default_factory=list)   # 平台中文名
    failed: list[dict[str, str]] = field(default_factory=list)  # {label, detail}
    reason: str = ""                    # 失败原因（人话）
    elapsed_minutes: float | None = None
    attempt: int = 0
    max_attempts: int = 0
    retry_in_minutes: float | None = None
    will_retry: bool = False
    next_at: str = ""                   # "09-24 20:12"
    log_path: str = ""
    republish: str = ""                 # "tangulunjin 600320 --republish"
    detail: str = ""                    # MISSED / RESTART / TEST 的自由文本
    headline_text: str = ""             # 少数情况下标题得自己写（合并报「漏了」时）

    # -- 渲染 -------------------------------------------------------------

    @property
    def mark(self) -> str:
        return KIND_MARKS.get(self.kind, "🔔")

    @property
    def slot(self) -> str:
        """``"9-24 早档"``。没给窗口时只留日期 —— 合并报「漏了」的时候没有单一窗口。"""
        bits = [_short_date(self.day)]
        if self.window:
            bits.append(window_label(self.window))
        return " ".join(bit for bit in bits if bit)

    def headline(self) -> str:
        """第一行，同时是通知标题 / 邮件主题。"""
        if self.headline_text:
            return self.headline_text
        mark = self.mark
        if self.kind == SUCCESS:
            tail = "发布完成"
        elif self.kind == PARTIAL:
            total = len(self.succeeded) + len(self.failed)
            tail = f"只发出去 {len(self.succeeded)}/{total}"
        elif self.kind == FAILURE:
            tail = "没发出去"
        elif self.kind == MISSED:
            tail = "漏了"
        else:
            # 重启 / 自检跟「哪一档」无关，硬凑一个时刻反而看不懂。
            return f"{mark} {KIND_LABELS.get(self.kind, self.kind)}"
        head = f"{mark} {self.slot}{tail}" if self.slot else f"{mark} {tail}"
        return f"{head} · {self.topic}" if self.topic else head

    def lines(self) -> list[str]:
        """通知正文。顺序＝读的时候想知道答案的顺序。"""
        out: list[str] = [self.headline()]
        if self.kind == MISSED:
            out.append(self.detail or "窗口已过，按规则不补发")
            if self.next_at:
                out.append(f"下次: {self.next_at}")
            return out
        if self.kind in (RESTART, TEST):
            if self.detail:
                out.append(self.detail)
            if self.next_at:
                out.append(f"下次: {self.next_at}")
            return out

        if self.title:
            out.append(self.title)
        if self.succeeded:
            out.append("成功: " + "、".join(self.succeeded))
        for item in self.failed:
            detail = _one_line(item.get("detail", ""), 90)
            out.append(f"失败: {item.get('label', item.get('platform', '?'))}"
                       + (f" —— {detail}" if detail else ""))
        if self.reason:
            out.append(f"原因: {_one_line(self.reason, 120)}")
        if self.elapsed_minutes is not None:
            out.append(f"用时: {self.elapsed_minutes:.1f} 分钟"
                       + (f"（第 {self.attempt}/{self.max_attempts} 次尝试）"
                          if self.attempt and self.max_attempts else ""))
        if self.will_retry and self.retry_in_minutes:
            out.append(f"下一步: {self.retry_in_minutes:g} 分钟后自动重试")
        elif self.kind == FAILURE and self.attempt and self.attempt >= self.max_attempts:
            out.append(f"下一步: 已达重试上限（{self.max_attempts} 次），这一档放弃")
        if self.republish:
            out.append(f"补发: {self.republish}")
        return out

    def text(self) -> str:
        return "\n".join(self.lines())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "day": self.day, "window": self.window, "at": self.at,
            "topic": self.topic, "title": self.title, "video": self.video,
            "succeeded": list(self.succeeded), "failed": list(self.failed),
            "reason": self.reason, "elapsed_minutes": self.elapsed_minutes,
            "attempt": self.attempt, "max_attempts": self.max_attempts,
            "retry_in_minutes": self.retry_in_minutes, "will_retry": self.will_retry,
            "next_at": self.next_at, "log_path": self.log_path,
            "republish": self.republish, "detail": self.detail,
        }

    def summary(self) -> dict[str, Any]:
        """塞进状态文件的那一份 —— ``--daemon-status`` 和事后排错都看它。"""
        return {
            "at": self.at, "kind": self.kind, "slot": self.slot, "topic": self.topic,
            "title": self.title, "video": self.video,
            "succeeded": list(self.succeeded),
            "failed": [item.get("label") or item.get("platform", "") for item in self.failed],
            "reason": self.reason,
        }


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(ANSI_ESCAPE.sub("", str(text or "")).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------

def _http_post(url: str, payload: Any, timeout: float, *,
               method: str = "POST") -> tuple[bool, str]:
    """POST（或 GET）一个 URL，顺带把「HTTP 200 但业务失败」也认出来。

    企业微信/钉钉/飞书在 webhook 失效时返回的是 **200 + errcode**，只看状态码会
    喜滋滋地报告「发成功了」。所以响应能解析成 JSON 且带 errcode/code 时，
    非零一律算失败。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json; charset=utf-8"} if body else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx
        return False, f"HTTP {exc.code} {_one_line(exc.reason or '', 60)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {_one_line(str(exc), 80)}"

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return True, "ok"
    if isinstance(data, dict):
        code = data.get("errcode", data.get("code", data.get("StatusCode", 0)))
        if isinstance(code, int) and code != 0:
            return False, f"errcode={code} {_one_line(data.get('errmsg') or data.get('msg') or '', 60)}"
    return True, "ok"


def _webhook_payload(kind: str, report: RunReport, *, title: str, text: str) -> Any:
    """把同一份正文摆成各家机器人认的字段名。

    企业微信/钉钉/飞书/裸 JSON 只差字段名，正文都是同一份 markdown —— 所以「内容」
    这一层不用为每个平台重写一遍。
    """
    if kind == "wecom":      # 企业微信群机器人
        return {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{text}"}}
    if kind == "dingtalk":   # 钉钉自定义机器人
        return {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n\n{text}"}}
    if kind == "feishu":     # 飞书自定义机器人
        return {"msg_type": "text", "content": {"text": f"{title}\n{text}"}}
    return {"title": title, "text": text, "kind": report.kind, "report": report.to_dict()}


def _notify_macos(title: str, text: str, sound: str) -> tuple[bool, str]:
    """走一次 ``osascript`` 弹通知中心横幅。

    比的是「人在电脑前」这一档：零配置、零凭据、立刻到。缺点同样明确 —— 人不在
    电脑前就只剩通知中心的历史记录，所以它不能是唯一通道。

    注意：launchd 起的进程没有 bundle，系统会把通知算在「脚本编辑器」名下。第一次
    没收到的话，去「系统设置 → 通知 → 脚本编辑器」把它打开（README 有写）。
    """
    if not shutil.which("osascript"):
        return False, "osascript 不在 PATH 上（非 macOS？）"
    script = f'display notification {_applescript(text)} with title {_applescript(title)}'
    if sound:
        script += f' sound name {_applescript(sound)}'
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=DEFAULT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {_one_line(str(exc), 80)}"
    if done.returncode != 0:
        return False, _one_line(done.stderr or f"osascript 退出码 {done.returncode}", 100)
    return True, "ok"


def _applescript(text: str) -> str:
    """包成 AppleScript 字符串字面量。反斜杠和双引号必须转义，换行可以留着。"""
    escaped = str(text).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _smtp_send(settings: Mapping[str, Any], subject: str, body: str) -> tuple[bool, str]:
    host = str(settings.get("host") or "")
    recipients = [item for item in (settings.get("to") if isinstance(settings.get("to"), (list, tuple))
                                    else [settings.get("to")]) if item]
    if not host or not recipients:
        return False, "缺 host/to"
    user = str(settings.get("user") or "")
    env_name = str(settings.get("password_env") or "TANGULUNJIN_SMTP_PASSWORD")
    # 密码只从环境变量读：配置文件是会被提交、会被贴出来的东西。
    password = os.environ.get(env_name, "")
    if user and not password:
        return False, f"环境变量 {env_name} 没有值（SMTP 密码）"

    sender = str(settings.get("sender") or user or recipients[0])
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(str(item) for item in recipients)
    message["Subject"] = subject
    message.set_content(body)

    port = int(settings.get("port", 465))
    timeout = float(settings.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    try:
        if port == 587:
            with smtplib.SMTP(host, port, timeout=timeout) as server:
                server.starttls()
                if user:
                    server.login(user, password)
                server.send_message(message)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=timeout) as server:
                if user:
                    server.login(user, password)
                server.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        return False, f"{type(exc).__name__}: {_one_line(str(exc), 80)}"
    return True, f"已发往 {', '.join(str(item) for item in recipients)}"


def _run_command(command: str, report: RunReport, timeout: float,
                 cwd: Path | None = None) -> tuple[bool, str]:
    """逃生舱：任何能收一条命令的推送工具都能接进来。

    命令里写 ``{report}`` 就替换成一份 JSON 的路径；没写就把 JSON 喂给 stdin。
    这样 ``ntfy`` / ``bark`` / ``pushover`` / 自己写的脚本都只差一行配置。

    ``cwd`` 是项目根：用户写的相对路径（比如把 JSON 存到 ``output/`` 下）才符合直觉。
    """
    if not command.strip():
        return False, "未配置命令"
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    temp_path: str | None = None
    try:
        if "{report}" in command:
            handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            handle.write(payload)
            handle.close()
            temp_path = handle.name
            command = command.replace("{report}", shlex.quote(temp_path))
        done = subprocess.run(command, shell=True, input=None if temp_path else payload,
                              capture_output=True, text=True, timeout=timeout, check=False,
                              cwd=str(cwd) if cwd else None)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {_one_line(str(exc), 80)}"
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    if done.returncode != 0:
        return False, _one_line(done.stderr or f"退出码 {done.returncode}", 100)
    return True, "ok"


# ---------------------------------------------------------------------------
# 门面
# ---------------------------------------------------------------------------

class Notifier:
    """按配置把 :class:`RunReport` 送到各个通道。

    配置（``notify`` 段，见 ``config/default.yaml``）里 ``channels`` 是一个列表，
    每项形如 ``{kind: macos}`` 或 ``{kind: webhook, type: wecom, url: ...}``；
    只写字符串 ``macos`` 也认（等价于 ``{kind: macos}``）。没配 ``url``/``host``
    的通道会被当作「还没启用」静默跳过 —— 这样默认配置里可以一次性列全所有通道，
    用户填哪个哪个就活。
    """

    def __init__(self, config: Mapping[str, Any] | None = None, *, root: Path | None = None,
                 logger: Callable[[str, str], None] | None = None) -> None:
        settings = dict((config or {}).get("notify") or {})
        self.settings = settings
        # 配置里**没有** notify 段就是关的。真实运行走 load_config（永远带默认段），
        # 这条例外保护的是直接构造 Daemon 的调用方 —— 包括测试 —— 不会被通知打扰。
        self.enabled = bool(settings.get("enabled", False))
        self.channels = _parse_channels(settings.get("channels") or [])
        self.timeout = float(settings.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        self.root = Path(root) if root else Path.cwd()
        self.sounds = dict(settings.get("sound") or {})
        self._logger = logger

    # -- 该不该发 ---------------------------------------------------------

    def should_send(self, report: RunReport) -> bool:
        if not self.enabled or not self.channels:
            return False
        flag = {
            SUCCESS: "on_success", PARTIAL: "on_partial", FAILURE: "on_failure",
            MISSED: "on_missed", RESTART: "on_restart", TEST: "on_test",
        }.get(report.kind)
        if flag is None:
            return True
        return bool(self.settings.get(flag, True))

    # -- 发 ---------------------------------------------------------------

    def send(self, report: RunReport) -> list[dict[str, Any]]:
        """逐个通道发同一份内容。返回值只用于日志/自检，**从不抛异常**。"""
        if not self.should_send(report):
            return []
        title = report.headline()
        text = "\n".join(report.lines()[1:]) or title
        results: list[dict[str, Any]] = []
        for channel in self.channels:
            ready, why = _channel_ready(channel)
            if not ready:
                # 默认配置里把所有通道都列了一遍，没填 url/host 的就是「还没启用」，
                # 不是「发失败」。混在一起会让日志每次运行都刷一遍假的告警。
                results.append({"kind": channel["kind"], "ok": False, "detail": why, "skipped": True})
                continue
            outcome = self._dispatch(channel, report, title, text)
            results.append({"kind": channel["kind"], "ok": outcome[0], "detail": outcome[1]})
            self._note(channel["kind"], outcome)
        return results

    def _dispatch(self, channel: Mapping[str, Any], report: RunReport,
                  title: str, text: str) -> tuple[bool, str]:
        kind = channel["kind"]
        try:
            if kind == "macos":
                sound = str(self.sounds.get(KIND_SOUNDS.get(report.kind, ""), "")
                            or channel.get("sound") or "")
                return _notify_macos(title, text, sound)
            if kind == "webhook":
                return self._send_webhook(channel, report, title, text)
            if kind == "smtp":
                return _smtp_send({**channel, "timeout_seconds": self.timeout}, title,
                                  f"{report.headline()}\n\n{text}\n")
            if kind == "command":
                return _run_command(str(channel.get("command") or ""), report, self.timeout,
                                    cwd=self.root)
            return False, f"不认识的通道 kind={kind}（可选: macos / webhook / smtp / command）"
        except Exception as exc:  # noqa: BLE001 —— 通知失败绝不许影响发布
            return False, f"{type(exc).__name__}: {_one_line(str(exc), 80)}"

    def _send_webhook(self, channel: Mapping[str, Any], report: RunReport,
                      title: str, text: str) -> tuple[bool, str]:
        url = str(channel.get("url") or "")
        if not url:
            return False, "未填 url（跳过）"
        kind = str(channel.get("type") or "wecom")
        payload = _webhook_payload(kind, report, title=title, text=text)
        if kind == "bark":
            # Bark 是 GET + 路径参数，不是 JSON POST。
            base = url.rstrip("/")
            return _http_post(f"{base}/{urllib.parse.quote(title)}/{urllib.parse.quote(text)}",
                              None, self.timeout, method="GET")
        return _http_post(url, payload, self.timeout, method="POST")

    def _note(self, kind: str, outcome: tuple[bool, str]) -> None:
        if self._logger is None:
            return
        if outcome[0]:
            if outcome[1] and outcome[1] != "ok":
                self._logger(f"反馈已送出（{kind}）：{outcome[1]}", "INFO")
        else:
            self._logger(f"反馈通道 {kind} 没发出去：{outcome[1]}", "WARN")

    # -- 自检 -------------------------------------------------------------

    def channels_ready(self) -> list[tuple[str, bool, str]]:
        """每个通道「能不能发」：没填 url/host 的通道在这里显形。

        ``--notify-test`` 用它，免得「我以为配了，其实 url 是空的」这种事拖到
        真正出错的那天才发现。
        """
        out: list[tuple[str, bool, str]] = []
        for channel in self.channels:
            ok, detail = _channel_ready(channel)
            name = channel["kind"] if channel["kind"] != "webhook" else f"webhook({channel.get('type') or 'wecom'})"
            out.append((name, ok, detail))
        return out


def _parse_channels(raw: Sequence[Any]) -> list[dict[str, Any]]:
    """``["macos", {kind: webhook, url: ...}]`` → 归一化的字典列表。"""
    channels: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            channels.append({"kind": item.strip()})
        elif isinstance(item, Mapping):
            kind = str(item.get("kind") or "").strip()
            if kind:
                channels.append({str(key): value for key, value in item.items()})
    return channels


def _channel_ready(channel: Mapping[str, Any]) -> tuple[bool, str]:
    """这个通道填全了没有。没填全＝还没启用，不是失败。"""
    kind = channel["kind"]
    if kind == "webhook":
        ok = bool(channel.get("url"))
        return ok, "已配置" if ok else "url 为空 —— 还没启用"
    if kind == "smtp":
        ok = bool(channel.get("host")) and bool(channel.get("to"))
        return ok, "已配置" if ok else "host/to 为空 —— 还没启用"
    if kind == "command":
        ok = bool(str(channel.get("command") or "").strip())
        return ok, "已配置" if ok else "command 为空 —— 还没启用"
    if kind == "macos":
        return True, "已配置"
    return False, f"不认识的通道 kind={kind}（可选: macos / webhook / smtp / command）"


def build_notifier(config: Mapping[str, Any] | None, *, root: Path | None = None,
                   logger: Callable[[str, str], None] | None = None) -> Notifier:
    return Notifier(config, root=root, logger=logger)
