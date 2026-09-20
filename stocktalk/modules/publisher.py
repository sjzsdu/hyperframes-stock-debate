"""Multi-platform video publishing via the social-auto-upload CLI (``sau``).

The publisher is a thin, auditable wrapper: it builds per-platform metadata
(title/description/tags) from the approved script, applies compliance
sanitisation, then shells out to ``sau <platform> upload-video`` exactly the
way a human operator would.  Every command is recorded in the result so runs
stay debuggable, and one platform failing never blocks the others.

Platforms are supported through social-auto-upload's browser automation
(https://github.com/dreammis/social-auto-upload).  Each platform needs a
one-time interactive login that persists a cookie file:

    sau douyin login --account main

The publisher only ever invokes ``upload-video``, ``check``; logins are
deliberately left to the operator.  Bilibili is the odd one out: it drives the
external ``biliup`` binary (downloaded on first use) instead of a browser, so
its CLI has no ``--headless`` flag and its ``--desc``/``--tid`` are required.
"""

from __future__ import annotations

import json
import queue
import re
import select
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class PublishError(RuntimeError):
    """Raised when the sau CLI itself cannot be located or invoked."""


class StreamAbort(RuntimeError):
    """Raised when a streaming upload is killed for a known, actionable reason."""


# Bilibili category for 财经/商业 content (tid 207 = 财经).
BILIBILI_FINANCE_TID = 207

# Where per-platform rate-limit cooldowns are remembered between runs.  It sits
# next to the renders on purpose: the output directory is already disposable
# and gitignored, and a cooldown is a property of "this machine right now",
# not of the code.
DEFAULT_STATE_FILENAME = ".publish_state.json"

# Douyin can gate the final publish click behind an SMS challenge.  sau detects
# the popup, clicks 「获取验证码」 and then polls ``verify_code.txt`` (or stdin)
# inside an endless ``while True`` loop, so an unattended run hangs until the
# outer upload timeout kills it — 15 minutes of nothing, with no clue in the
# log.  Watching sau's own stream lets us fail in minutes with a real remedy.
SMS_CHALLENGE_MARKERS: tuple[str, ...] = ("检测到短信验证码弹窗", "已点击「获取验证码」", "等待验证码输入")
SMS_RESOLVED_MARKERS: tuple[str, ...] = ("已获取验证码", "验证码已填入", "验证码处理完成", "发布成功")

# Bilibili uploads through biliup, which refuses bursts with
# ``upload rate limit (code: 601): 您上传视频过快``.  The cooldown is minutes
# long, so honouring the ordinary 30s retry backoff only burns the retry budget
# and surprises nobody: it needs a wait of its own, then another try.
RATE_LIMIT_MARKERS: tuple[str, ...] = ("upload rate limit", "code: 601", "上传视频过快", "频率限制")
# A 601 reads like "you're going too fast", but on an account that has never
# published it is really 风控: new / low-level / unverified accounts get it on
# their very first upload, and waiting does not clear it.  Only a manual web or
# app upload (which may ask for a captcha) lifts it, so say that out loud
# instead of sending people to wait another ten minutes.
RATE_LIMIT_REMEDIES: dict[str, str] = {
    "bilibili": "B站 601 多半是账号风控而非真的「传太快」（Lv0/新号/未实名尤其容易中）："
                "先在 bilibili 网页端或 APP 手动投一个视频，弹验证码就过掉；"
                "并确认已绑定手机 + 通过实名认证（account.bilibili.com），之后再补发",
}

# A verification code is 4-8 digits on every platform we know; anything else is
# a typo worth re-asking rather than sending upstream.
CODE_PATTERN = re.compile(r"\d{4,8}")

# Per-platform upload constraints and CLI capability.  ``runtime_flags`` marks
# whether ``sau <platform> upload-video`` accepts --headless/--headed (bilibili
# delegates to biliup and rejects them).  ``title``/``tags``/``desc`` are the
# publish limits we clamp metadata to before invoking the CLI.  ``covers`` lists
# the ``(flag, preset)`` pairs the platform accepts, where preset names one of
# cover.COVER_PRESETS — douyin/tencent take a landscape cover as well as the
# 3:4 portrait one, kuaishou/xiaohongshu take a single image, and bilibili's
# cover is landscape-first.
#
# ``ai_statement_cli`` marks the platforms whose uploader needs the exact
# AI-content option text handed to it (the wording lives in a site dropdown and
# changes between redesigns).  The others declare it themselves: douyin picks
# 「内容由AI生成」 in its 自主声明 dialog, tencent picks 「含AI生成内容」 in its
# 视频标注 dropdown.  bilibili has no such field in the biliup CLI at all —
# there the AI notice rides along in the description instead.
PLATFORM_SPECS: dict[str, dict[str, Any]] = {
    "douyin":      {"label": "抖音",  "title": 30, "tags": 4,  "desc": 150, "runtime_flags": True,  "tid": None,
                    "ai_statement_cli": False,
                    "covers": (("--thumbnail", "portrait"), ("--thumbnail-landscape", "landscape"))},
    "bilibili":    {"label": "B站",   "title": 80, "tags": 10, "desc": 2000, "runtime_flags": False, "tid": BILIBILI_FINANCE_TID,
                    "ai_statement_cli": False,
                    "covers": (("--thumbnail", "wide"),)},
    "kuaishou":    {"label": "快手",  "title": 30, "tags": 6,  "desc": 150, "runtime_flags": True,  "tid": None,
                    "ai_statement_cli": True,
                    "covers": (("--thumbnail", "portrait"),)},
    "xiaohongshu": {"label": "小红书", "title": 20, "tags": 10, "desc": 1000, "runtime_flags": True,  "tid": None,
                    "ai_statement_cli": True,
                    "covers": (("--thumbnail", "portrait"),)},
    "tencent":     {"label": "视频号", "title": 30, "tags": 6,  "desc": 120, "runtime_flags": True,  "tid": None,
                    "ai_statement_cli": False,
                    "covers": (("--thumbnail-portrait", "portrait"), ("--thumbnail-landscape", "landscape"))},
    # 百家号上传器强制要求横版封面（缺失直接 ValueError），标题上限 30 字。
    # headed=True：百度滑块风控在 headless 下必弹且无法人工通过（2026-09-20
    # 两次发布死于「百度安全验证」），所以无视 publish.headless 强制有头，
    # 弹滑块时等用户在浏览器窗口里拖一下。
    "baijiahao":   {"label": "百家号", "title": 30, "tags": 5,  "desc": 200, "runtime_flags": True,  "tid": None,
                    "headed": True,
                    "ai_statement_cli": False,
                    "covers": (("--thumbnail", "wide"),)},
}

# Backwards-compatible views over PLATFORM_SPECS.
PLATFORM_LABELS: dict[str, str] = {name: spec["label"] for name, spec in PLATFORM_SPECS.items()}
TITLE_LIMITS: dict[str, int] = {name: spec["title"] for name, spec in PLATFORM_SPECS.items()}

DEFAULT_TAGS: tuple[str, ...] = ("A股", "股票", "财经", "价值投资", "上市公司分析")

DEFAULT_SPEC: dict[str, Any] = {"label": "", "title": 30, "tags": 4, "desc": 150, "runtime_flags": True,
                                "tid": None, "covers": ()}


def cover_sizes_for(platforms: Sequence[str]) -> list[str]:
    """Which cover presets the given platforms actually consume.

    Rendering is the expensive part, so only the sizes at least one platform
    asks for are produced (a 抖音-only run never renders the B站 横向 cover).
    """
    sizes: list[str] = []
    for platform in platforms:
        for _, preset in PLATFORM_SPECS.get(platform, DEFAULT_SPEC).get("covers", ()):
            if preset not in sizes:
                sizes.append(preset)
    return sizes


# The local toolchain each stage depends on.  Only ``required`` entries decide
# the exit code of the preflight command: a missing npx (render) or Chrome
# (cover) degrades gracefully, a missing sau CLI does not publish anything.
ENVIRONMENT_CHECKS: tuple[tuple[str, str, bool], ...] = (
    ("sau", "sau CLI（发布）", True),
    ("npx", "Node/npx（MP4 渲染）", False),
    ("chrome", "Chrome（封面图，缺失则无封面发布）", False),
)


def check_environment(config: Mapping[str, Any] | None = None, publisher: SauPublisher | None = None) -> list[dict[str, Any]]:
    """Probe the toolchain the render/publish path shells out to.

    Never raises: an unreachable tool is reported, not thrown, so the caller can
    print one complete table instead of dying on the first problem.
    """
    resolved = publisher or SauPublisher(config)
    found: dict[str, tuple[bool, str]] = {}
    try:
        found["sau"] = (True, resolved._require_sau())
    except PublishError as exc:
        found["sau"] = (False, str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        found["sau"] = (False, str(exc))
    npx = shutil.which("npx")
    found["npx"] = (bool(npx), npx or "未找到 npx；MP4 渲染需要 Node.js")
    try:
        from stocktalk.modules.cover import CoverGenerator

        chrome = CoverGenerator(resolved.config).chrome_path()
        found["chrome"] = (bool(chrome), chrome or "未找到 Chrome/Chromium；封面图将跳过")
    except Exception as exc:  # pragma: no cover - defensive
        found["chrome"] = (False, str(exc))
    checks: list[dict[str, Any]] = []
    for name, label, required in ENVIRONMENT_CHECKS:
        ok, detail = found.get(name, (False, "未检查"))
        checks.append({"name": name, "label": label, "ok": ok, "detail": detail, "required": required})
    return checks


@dataclass(frozen=True)
class PublishMetadata:
    """Ready-to-publish metadata for one video."""

    title: str
    description: str
    tags: tuple[str, ...]
    disclaimer: str = ""

    @property
    def full_description(self) -> str:
        return f"{self.description}\n\n{self.disclaimer}".strip() if self.disclaimer else self.description


@dataclass
class PlatformResult:
    """Outcome of one platform upload attempt."""

    platform: str
    ok: bool
    command: list[str] = field(default_factory=list)
    detail: str = ""
    attempts: int = 1
    # False for failures that a retry cannot fix (a stale cookie, an SMS
    # challenge): retrying those only burns another wait window.
    retryable: bool = True
    # Minimum silence required before this platform is worth another try
    # (a rate limit needs minutes, a crashed browser needs seconds).
    retry_after: float = 0.0


@dataclass(frozen=True)
class FailureVerdict:
    """What one failed upload deserves: another try, after how long, and why."""

    retryable: bool
    wait_seconds: float = 0.0
    hint: str = ""
    kind: str = "transient"


class SmsChallengeGuard:
    """Fail fast when sau is stuck waiting for a Douyin SMS verification code.

    ``feed`` is called once per line of sau's output (and once per idle tick
    with an empty string), so the deadline is enforced even when the child
    process goes completely silent.

    ``on_arm`` is invoked exactly once per challenge, right when the popup is
    recognised — the hook is where an interactive run asks the operator for the
    code.  Returning True means a code was delivered, and the window restarts.

    ``verify_code_wait_seconds <= 0`` disables the guard entirely.
    """

    def __init__(self, code_file: Path, wait_seconds: float,
                 clock: Callable[[], float] = time.monotonic,
                 on_arm: Callable[[], bool] | None = None,
                 max_windows: int = 4) -> None:
        self.code_file = Path(code_file)
        self.wait_seconds = float(wait_seconds)
        self._clock = clock
        self._on_arm = on_arm
        # A code file that sau never consumes would otherwise refresh forever,
        # which is the hang this class exists to prevent, so the total is capped:
        # four windows is plenty of time to read a text message and type it in.
        self.max_windows = max(1, int(max_windows))
        self.started_at: float | None = None
        self.deadline: float | None = None

    @property
    def armed(self) -> bool:
        return self.deadline is not None

    @property
    def exhausted(self) -> bool:
        """Has this challenge consumed its whole budget, refreshes included?"""
        return self.started_at is not None and self._clock() - self.started_at >= self._budget

    @property
    def _budget(self) -> float:
        return self.wait_seconds * self.max_windows

    def extend(self) -> None:
        """Grant another window, unless the challenge has used up its budget."""
        if self.deadline is not None and not self.exhausted:
            self.deadline = self._clock() + self.wait_seconds

    def feed(self, line: str) -> str | None:
        """Observe one output line; return an abort reason once the wait lapses."""
        if any(marker in line for marker in SMS_RESOLVED_MARKERS):
            self.deadline = None
            return None
        if self.deadline is None and any(marker in line for marker in SMS_CHALLENGE_MARKERS):
            # Only the *first* marker arms the guard.  sau repeats variants of
            # the same log line for as long as the popup is up, and letting each
            # repetition return early would postpone the deadline check
            # indefinitely — which is the hang this class exists to prevent.
            # Repetitions of the marker are also common; the arming above only
            # reacts to the first, so these fall through to the checks below.
            self.deadline = self._clock() + self.wait_seconds
            self.started_at = self._clock()
            if self._on_arm is not None and self._on_arm():
                self.extend()
            return None
        if self.deadline is None:
            return None
        # A human (or a watcher) can still rescue the run by dropping the code
        # into the file sau polls; that buys another window.
        if self.code_file.is_file() and not self.exhausted:
            self.extend()
            return None
        if self.exhausted or self._clock() >= self.deadline:
            waited = self._clock() - (self.started_at or self._clock())
            return (f"抖音要求短信验证码，等待 {waited:.0f}s 仍未收到；"
                    f"把手机收到的验证码写入 {self.code_file} 后重试")
        return None


def _default_prompt_emit(text: str, end: str = "\n") -> None:
    print(text, end=end, flush=True)


class VerificationCodePrompt:
    """Ask the operator for the 抖音 SMS code and hand it to sau.

    sau does prompt for the code itself, but its stdout is captured here, so the
    operator never sees ``请输入抖音短信验证码`` and the run looks hung until the
    timeout.  The child therefore gets a null stdin (which makes sau take its
    file-driven path) and we do the asking: print a banner nobody can miss, read
    the code straight from the terminal, write it to the file sau polls.

    Returns True when a code was delivered.  False means there was nobody to ask
    (cron/CI, piped stdin) or they declined — the caller then falls back to
    waiting for someone to write the file by hand, exactly as before.
    """

    def __init__(self, code_file: Path, *, wait_seconds: float = 150.0,
                 enabled: bool = True, interactive: bool | None = None,
                 stdin: Any | None = None,
                 reader: Callable[[float], str | None] | None = None,
                 emit: Callable[..., None] = _default_prompt_emit,
                 pause_ui: Callable[[], None] | None = None,
                 resume_ui: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.code_file = Path(code_file)
        self.wait_seconds = float(wait_seconds)
        self.enabled = enabled
        self._stdin = stdin if stdin is not None else sys.stdin
        self._interactive = bool(getattr(self._stdin, "isatty", lambda: False)()) if interactive is None else interactive
        self._reader = reader
        self._emit = emit
        self._pause_ui = pause_ui
        self._resume_ui = resume_ui
        self._clock = clock

    @property
    def available(self) -> bool:
        return self.enabled and self._interactive and self.wait_seconds > 0

    def __call__(self, label: str = "抖音") -> bool:
        if not self.available:
            if self.enabled and self.wait_seconds > 0:
                self._emit(f"  {label}要求短信验证码，但当前不是交互终端："
                           f"请把手机收到的验证码写入 {self.code_file}（{self.wait_seconds:g}s 内）")
            return False
        # Leave the last few seconds to the file-watch fallback below, so the
        # operator always gets one clear closing message either way.
        budget = max(5.0, self.wait_seconds - 5.0)
        deadline = self._clock() + budget
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                self._emit(f"  未在 {budget:g}s 内输入，仍会继续等待手工写入 {self.code_file}")
                return False
            code = self._ask(label, remaining)
            if code is None:
                self._emit(f"  等待输入超时；也可以把验证码手工写入 {self.code_file}")
                return False
            code = code.strip()
            if not code:
                self._emit("  已跳过，继续等待验证码文件")
                return False
            if not CODE_PATTERN.fullmatch(code):
                self._emit("  验证码一般是 4-8 位数字，请重新输入")
                continue
            self.code_file.parent.mkdir(parents=True, exist_ok=True)
            self.code_file.write_text(code, encoding="utf-8")
            self._emit(f"  已写入 {self.code_file.name}，正在提交…")
            return True

    def _ask(self, label: str, remaining: float) -> str | None:
        """Print the banner and read one line within ``remaining`` seconds."""
        if self._pause_ui is not None:
            self._pause_ui()  # a refreshing progress bar would fight the prompt
        try:
            self._emit("")
            self._emit(f"  ⚠️  {label}这次发布要过一道短信验证码风控")
            self._emit(f"      手机应该刚收到验证码，粘到这里回车就继续（{remaining:g}s 内不输入算失败）")
            self._emit("      验证码: ", end="")
            try:
                return self._read(remaining)
            finally:
                self._emit("")
        finally:
            if self._resume_ui is not None:
                self._resume_ui()

    def _read(self, timeout: float) -> str | None:
        """One line from stdin, or None when nothing arrived in time."""
        if self._reader is not None:
            return self._reader(timeout)
        try:
            ready, _, _ = select.select([self._stdin], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not ready:
            return None
        try:
            # An empty string is EOF: nobody is on the other end to answer.
            return self._stdin.readline() or None
        except (OSError, ValueError):
            return None


class PublishMetadataGenerator:
    """Derive per-platform publish metadata from an approved script."""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        publish = self.config.get("publish", {}) or {}
        self.extra_tags = tuple(str(t) for t in publish.get("extra_tags", ()) if str(t).strip())

    def generate(self, script: Mapping[str, Any], stock_name: str, stock_code: str) -> PublishMetadata:
        title = self._title(script, stock_name, stock_code)
        description = self._description(script, stock_name)
        tags = self._tags(script)
        disclaimer = " ".join(str(d) for d in script.get("disclaimers", ()) if str(d).strip())
        return PublishMetadata(title=title, description=description, tags=tags, disclaimer=disclaimer)

    def for_platform(self, metadata: PublishMetadata, platform: str) -> PublishMetadata:
        """Clamp metadata to one platform's limits (title length, tag count, description)."""
        spec = PLATFORM_SPECS.get(platform, DEFAULT_SPEC)
        title = metadata.title if len(metadata.title) <= spec["title"] else metadata.title[: spec["title"] - 1] + "…"
        description = self._fit_description(metadata.full_description, spec["desc"])
        return PublishMetadata(title=title, description=description, tags=metadata.tags[: spec["tags"]], disclaimer="")

    @staticmethod
    def _fit_description(text: str, limit: int) -> str:
        """Trim the description, keeping the disclaimer tail when it fits."""
        text = text.strip()
        if len(text) <= limit:
            return text
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) >= 2:
            tail = lines[-1]
            room = limit - len(tail) - 2
            if room > 20:
                return "\n".join(lines[:-1])[:room].rstrip() + "…\n" + tail
        return text[: limit - 1] + "…"

    def _title(self, script: Mapping[str, Any], stock_name: str, stock_code: str) -> str:
        raw = str(script.get("title") or "").strip()
        base = raw if raw else f"{stock_name}（{stock_code}）值得聊一聊"
        return base

    def _description(self, script: Mapping[str, Any], stock_name: str) -> str:
        """First bull + bear lines as a hook summary, compliance-safe."""
        lines: list[str] = []
        for turn in script.get("turns", ()):
            if not isinstance(turn, Mapping):
                continue
            line = str(turn.get("line", "")).strip()
            if line and line not in lines:
                lines.append(line)
            if len(lines) == 2:
                break
        hook = "；".join(lines) if lines else f"一起聊聊{stock_name}的生意本质。"
        return f"{hook}\n观点仅代表个人学习交流，欢迎评论区聊聊你的看法。"

    def _tags(self, script: Mapping[str, Any]) -> tuple[str, ...]:
        tags: list[str] = []
        for turn in script.get("turns", ()):
            if not isinstance(turn, Mapping):
                continue
            for keyword in turn.get("visual", ()) or ():
                word = str(keyword).strip()
                if 2 <= len(word) <= 8 and word not in tags:
                    tags.append(word)
        for tag in (*tags, *self.extra_tags, *DEFAULT_TAGS):
            if tag and tag not in tags:
                tags.append(tag)
        return tuple(tags[:8])


class SauPublisher:
    """Publish MP4 videos to Chinese platforms through the ``sau`` CLI."""

    def __init__(self, config: Mapping[str, Any] | None = None, runner: Any | None = None,
                 sau_bin: str | None = None, sleep: Callable[[float], None] = time.sleep,
                 code_prompt: Callable[[str, float], bool] | None = None) -> None:
        self.config = dict(config or {})
        publish = self.config.get("publish", {}) or {}
        self.sau_dir = Path(publish.get("sau_dir", "third_party/social-auto-upload"))
        self.workdir = self.sau_dir if self.sau_dir.is_dir() else Path.cwd()
        self.accounts: dict[str, str] = dict(publish.get("accounts", {}) or {})
        self.headless = bool(publish.get("headless", True))
        self.timeout = float(publish.get("timeout_seconds", 900))
        self.check_timeout = float(publish.get("check_timeout_seconds", 120))
        self.preflight = bool(publish.get("preflight", True))
        # Unattended runs get transient failures (network blips, one browser
        # timing out) for free; retrying only the failed platforms in the same
        # run beats a manual --publish-only pass over five platforms.
        self.retries = max(0, int(publish.get("retries", 0)))
        self.retry_delay = float(publish.get("retry_delay_seconds", 30))
        self.verify_code_wait = float(publish.get("verify_code_wait_seconds", 150))
        # A rate limit is asking us to slow down, and it says so for minutes.
        # It is also account-wide: every attempt made during the window resets
        # the clock, so the wait doubles per attempt and is remembered across
        # runs — otherwise a cron job burns one upload per run, forever.
        self.rate_limit_wait = float(publish.get("rate_limit_wait_seconds", 600))
        self.rate_limit_max_wait = float(publish.get("rate_limit_max_wait_seconds", 3600))
        # How long one run may sit and wait out a cooldown before it gives up
        # and leaves the platform to `--republish`.
        self.rate_limit_block = float(publish.get("rate_limit_block_seconds", 600))
        output_dir = Path((self.config.get("output", {}) or {}).get("dir", "output"))
        state_file = str(publish.get("state_file") or DEFAULT_STATE_FILENAME)
        self.state_path = Path(state_file) if Path(state_file).is_absolute() else output_dir / state_file
        # Ask for the SMS code in the terminal when there is one; cron/CI falls
        # back to dropping it into verify_code.txt by hand.
        self.interactive_verify_code = bool(publish.get("interactive_verify_code", True))
        # AI-generated content must be labelled on every platform: it is a
        # monetisation prerequisite now, not a nicety.  Most platforms take a
        # checkbox the uploader clicks; kuaishou/xiaohongshu need the option
        # text passed in because站点改版会换文案.  Accepts either one string or
        # a per-platform mapping.
        self.ai_content_label = publish.get("ai_content_label")
        self._sleep = sleep
        self.metadata_gen = PublishMetadataGenerator(self.config)
        self._runner = runner or self._run_subprocess
        self._sau_bin = sau_bin
        # Injected by tests and by anything with a nicer UI than `input()`.
        self._code_prompt = code_prompt
        self._pause_ui: Callable[[], None] | None = None
        self._resume_ui: Callable[[], None] | None = None

    def set_ui_hooks(self, pause: Callable[[], None] | None = None,
                     resume: Callable[[], None] | None = None) -> None:
        """Let a live progress display step aside while we prompt for a code."""
        self._pause_ui = pause
        self._resume_ui = resume

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def publish(self, video_path: str | Path, script: Mapping[str, Any], stock_name: str,
                stock_code: str, platforms: Sequence[str] | None = None,
                schedule: str | None = None,
                platform_videos: Mapping[str, str | Path] | None = None,
                covers: Mapping[str, str | Path] | None = None,
                on_event: Callable[[str, str, str], None] | None = None) -> dict[str, Any]:
        """Upload one MP4 to each configured platform; returns a per-platform report.

        ``platform_videos`` lets a platform receive its own cut (e.g. a
        horizontal render for Bilibili) instead of the default file, and
        ``covers`` maps a cover preset name (portrait/landscape/wide) to its
        PNG.  Either can be missing — every platform still gets published, just
        with that platform's default cut or without cover art.
        """
        video = Path(video_path)
        if not video.is_file():
            raise PublishError(f"video not found: {video}")
        self._require_sau()
        platform_list = list(platforms) if platforms else list(self.config.get("publish", {}).get("platforms", ()))
        if not platform_list:
            raise PublishError("no publish platforms configured (publish.platforms)")
        metadata = self.metadata_gen.generate(script, stock_name, stock_code)
        videos = {name: Path(path) for name, path in dict(platform_videos or {}).items()}
        for name, path in videos.items():
            if not path.is_file():
                raise PublishError(f"video not found for {name}: {path}")
        # A cover that is not there is not an error: `--publish-only` and
        # `--republish` may run long after the covers were rendered.
        cover_files = {preset: Path(path) for preset, path in dict(covers or {}).items() if Path(path).is_file()}

        # Pre-flight: a 8-minute render should not be wasted on a stale cookie.
        # Failing platforms are pulled out of the run and reported as skipped.
        # Preflight failures are *not* retried: the remedy is a fresh login, not
        # another browser launch.
        pending = list(platform_list)
        outcomes: dict[str, PlatformResult] = {}
        if self.preflight:
            on_event and on_event("*", "全部平台", "预检登录态")
            try:
                checks = self.check_platforms([p for p in pending if p in PLATFORM_SPECS])
            except Exception as exc:  # A broken probe must not block every platform.
                on_event and on_event("*", "预检", f"预检异常，跳过预检继续发布: {exc}")
            else:
                for platform, check in checks.items():
                    if check["ok"]:
                        continue
                    outcomes[platform] = PlatformResult(
                        platform=platform, ok=False, retryable=False,
                        detail=f"发布前预检未通过（很可能需要重新登录 sau {platform} login）: {check['detail']}")
                    pending.remove(platform)

        interrupted = False
        for index, platform in enumerate(pending):
            spec = PLATFORM_SPECS.get(platform)
            label = (spec or DEFAULT_SPEC)["label"] or platform
            if not spec:
                outcomes[platform] = PlatformResult(platform=platform, ok=False, detail="unsupported platform")
                continue
            cooling = self.cooldown_left(platform)
            if cooling > 0:
                # Retrying now would only restart the window: skip without
                # spending an upload, and say when to come back.
                on_event and on_event(platform, label, f"限流冷却中（还需 {self._format_wait(cooling)}）")
                advice = RATE_LIMIT_REMEDIES.get(platform, "")
                outcomes[platform] = PlatformResult(
                    platform=platform, ok=False, retryable=False,
                    detail=f"{label}限流冷却中，还需 {self._format_wait(cooling)}。"
                           f"冷却期内不会发起上传（发也只会被拒并重置窗口）；"
                           + (f"{advice}；" if advice else "")
                           + "到点后运行 `tangulunjin <股票代码> --republish` 补发。")
                continue
            on_event and on_event(platform, label, "上传中")
            try:
                outcome = self._publish_safely(platform, videos.get(platform, video), metadata,
                                               schedule, cover_files, attempt=1)
            except KeyboardInterrupt:
                # Ctrl-C once aborts the run, but the platforms already uploaded
                # still deserve a report — otherwise the recording of what went
                # out is lost with the traceback.
                interrupted = True
                outcome = PlatformResult(platform=platform, ok=False, retryable=False, detail="被用户中断（Ctrl-C）")
            if outcome.ok:
                self._clear_cooldown(platform)
            elif outcome.retry_after:
                self._note_cooldown(platform, outcome.retry_after)
            on_event and on_event(platform, label, "完成" if outcome.ok else "失败")
            outcomes[platform] = outcome
            if interrupted:
                for skipped in pending[index + 1:]:
                    outcomes[skipped] = PlatformResult(platform=skipped, ok=False, retryable=False,
                                                       detail="未执行（同一轮前面已被用户中断）")
                break

        if not interrupted:
            for attempt in range(1, self.retries + 1):
                retry_targets: list[str] = []
                for p in pending:
                    if p not in PLATFORM_SPECS or p not in outcomes:
                        continue
                    result = outcomes[p]
                    if result.ok or not result.retryable:
                        continue
                    # A cooldown longer than this run may sit through is left to
                    # --republish: blocking the terminal for an hour is worse
                    # than coming back later.
                    if result.retry_after > self.rate_limit_block:
                        outcomes[p] = PlatformResult(
                            platform=p, ok=False, retryable=False, attempts=result.attempts,
                            detail=f"{result.detail}\n需冷却 {self._format_wait(result.retry_after)}，"
                                   f"本轮不再等待；到点后运行 `tangulunjin <股票代码> --republish` 补发")
                        continue
                    retry_targets.append(p)
                if not retry_targets:
                    break
                labels = "、".join(PLATFORM_LABELS.get(p, p) for p in retry_targets)
                # Honour the longest cooldown in the batch: a rate-limited B站
                # shares the wait with anything else retrying alongside it.
                wait = max([self.retry_delay, *[outcomes[p].retry_after for p in retry_targets]])
                on_event and on_event("*", "重试", f"第 {attempt} 次重试 {labels}（{self._format_wait(wait)}后）")
                try:
                    self._wait(wait, f"重试 {labels}", on_event)
                except KeyboardInterrupt:
                    interrupted = True
                    for platform in retry_targets:
                        outcomes[platform] = PlatformResult(platform=platform, ok=False, retryable=False,
                                                            detail="重试等待中用户中断")
                    break
                for platform in retry_targets:
                    label = PLATFORM_LABELS.get(platform, platform)
                    on_event and on_event(platform, label, "重试中")
                    outcome = self._publish_safely(platform, videos.get(platform, video), metadata,
                                                   schedule, cover_files, attempt=attempt + 1)
                    outcome.attempts = outcomes[platform].attempts + 1
                    if outcome.ok:
                        self._clear_cooldown(platform)
                    elif outcome.retry_after:
                        self._note_cooldown(platform, outcome.retry_after)
                    outcomes[platform] = outcome
                    on_event and on_event(platform, label, "完成" if outcome.ok else "失败")

        results = [outcomes[p] for p in platform_list if p in outcomes]
        return {
            "video": str(video),
            "covers": {preset: str(path) for preset, path in cover_files.items()},
            "metadata": {"title": metadata.title, "description": metadata.description,
                         "tags": list(metadata.tags), "disclaimer": metadata.disclaimer},
            "platforms": [{"platform": r.platform, "label": PLATFORM_LABELS.get(r.platform, r.platform),
                           "ok": r.ok, "detail": r.detail, "command": r.command,
                           "attempts": r.attempts} for r in results],
            "succeeded": [r.platform for r in results if r.ok],
            "failed": [{"platform": r.platform, "detail": r.detail} for r in results if not r.ok],
        }

    def check_platforms(self, platforms: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Verify each platform's cookie via ``sau <platform> check``.

        Returns ``{platform: {"ok": bool, "detail": str}}``; never raises.
        """
        results: dict[str, dict[str, Any]] = {}
        for platform in platforms:
            if platform not in PLATFORM_SPECS:
                results[platform] = {"ok": False, "detail": "unsupported platform"}
                continue
            command = [self._require_sau(), platform, "check", "--account", self.accounts.get(platform, "default")]
            # The shared runner reads self.timeout at call time; checks must not
            # inherit the 15-minute upload budget.
            saved, self.timeout = self.timeout, self.check_timeout
            try:
                completed = self._runner(command, self.workdir)
            except subprocess.TimeoutExpired:
                results[platform] = {"ok": False, "detail": f"check timed out after {self.check_timeout:g}s"}
            except (OSError, subprocess.SubprocessError) as exc:
                results[platform] = {"ok": False, "detail": str(exc)}
            else:
                ok = completed.returncode == 0
                detail = ((completed.stdout or "") + (completed.stderr or "")).strip()
                results[platform] = {"ok": ok, "detail": detail[-300:] or ("ok" if ok else "check failed")}
            finally:
                self.timeout = saved
        return results

    # ------------------------------------------------------------------
    # Failure triage, waiting, prompting
    # ------------------------------------------------------------------
    def _wait(self, seconds: float, reason: str, on_event: Callable[[str, str, str], None] | None = None) -> None:
        """Sleep, narrating anything long enough to look like a hang.

        A ten-minute rate-limit cooldown with no output is indistinguishable
        from a dead run, so the remaining time is pushed through ``on_event``
        (which is what the progress bar displays).
        """
        if seconds <= 0:
            return
        if seconds <= 60:
            self._sleep(seconds)
            return
        remaining = seconds
        while remaining > 0:
            chunk = min(30.0, remaining)
            note = f"{reason}：还需 {self._format_wait(remaining)}"
            if on_event is not None:
                on_event("*", "等待", note)
            else:  # no progress display to talk to (--republish and friends)
                print(f"  {note}", flush=True)
            self._sleep(chunk)
            remaining -= chunk

    @staticmethod
    def _format_wait(seconds: float) -> str:
        seconds = max(0.0, float(seconds))
        if seconds < 60:
            return f"{seconds:g}s"
        minutes, rest = divmod(int(seconds), 60)
        return f"{minutes} 分 {rest:02d}s" if rest else f"{minutes} 分钟"

    def _classify(self, platform: str, detail: str, attempt: int = 1) -> FailureVerdict:
        """Decide whether another try can help, and how long to wait first.

        A 601 is account-wide, and every attempt inside the window restarts it,
        so the wait grows per attempt instead of repeating the same ten minutes.
        """
        text = (detail or "").lower()
        if any(marker.lower() in text for marker in RATE_LIMIT_MARKERS):
            label = PLATFORM_LABELS.get(platform, platform)
            wait = self._rate_limit_wait(attempt)
            remedy = RATE_LIMIT_REMEDIES.get(platform, "")
            if wait <= 0:
                advice = f"{label}限流：本轮不再重试"
                if remedy:
                    advice += f"。{remedy}"
                return FailureVerdict(retryable=False, wait_seconds=0.0, kind="rate_limit",
                                      hint=f"{advice}；稍后用 `tangulunjin <股票代码> --republish` 补发")
            advice = f"{label}限流：冷却 {self._format_wait(wait)}后再试（冷却期内不会发起上传）"
            if remedy:
                advice += f"; {remedy}"
            return FailureVerdict(retryable=True, wait_seconds=wait, kind="rate_limit", hint=advice)
        # Everything else is assumed transient (a browser crashed, the network
        # blipped) — retrying quickly is still the best guess.
        return FailureVerdict(retryable=True, wait_seconds=0.0)

    def _rate_limit_wait(self, attempt: int) -> float:
        """Cooldown for the Nth rate-limit hit: 10 → 20 → 40 minutes, capped."""
        if self.rate_limit_wait <= 0:
            return 0.0
        return min(self.rate_limit_wait * (2 ** max(0, attempt - 1)), self.rate_limit_max_wait)

    # ------------------------------------------------------------------
    # Rate-limit cooldown, remembered between runs
    # ------------------------------------------------------------------
    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, state: Mapping[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:  # A cooldown we cannot record just costs one extra try.
            pass

    def cooldown_left(self, platform: str) -> float:
        """Seconds until this platform's window reopens; 0 means free to try."""
        until = (self._read_state().get("cooldowns") or {}).get(platform)
        try:
            return max(0.0, float(until) - time.time())
        except (TypeError, ValueError):
            return 0.0

    def _note_cooldown(self, platform: str, seconds: float) -> None:
        if seconds <= 0:
            return
        state = self._read_state()
        cooldowns = dict(state.get("cooldowns") or {})
        # Never shorten a window that is already open.
        cooldowns[platform] = max(float(cooldowns.get(platform, 0)), time.time() + seconds)
        state["cooldowns"] = cooldowns
        self._write_state(state)

    def _clear_cooldown(self, platform: str) -> None:
        state = self._read_state()
        cooldowns = dict(state.get("cooldowns") or {})
        if cooldowns.pop(platform, None) is not None:
            state["cooldowns"] = cooldowns
            self._write_state(state)

    def _ask_for_code(self, label: str) -> bool:
        """Get a verification code from whoever is watching this terminal."""
        if not self.interactive_verify_code or self.verify_code_wait <= 0:
            return False
        if self._code_prompt is not None:
            return bool(self._code_prompt(label, self.verify_code_wait))
        base = self.sau_dir if self.sau_dir.is_dir() else self.workdir
        try:
            return VerificationCodePrompt(
                base / "verify_code.txt", wait_seconds=self.verify_code_wait,
                pause_ui=self._pause_ui, resume_ui=self._resume_ui)(label or "抖音")
        except Exception:  # A prompt that cannot be shown must not break an upload.
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _publish_safely(self, platform: str, video: Path, metadata: PublishMetadata,
                        schedule: str | None, covers: Mapping[str, Path],
                        attempt: int = 1) -> PlatformResult:
        """Publish one platform, absorbing *any* failure into its own result.

        ``_publish_one`` already handles the failures we anticipate; this is the
        backstop that keeps an unforeseen bug (a metadata edge case, a missing
        file) from taking down the other four platforms in the same run.
        """
        try:
            return self._publish_one(platform, video, metadata, schedule, covers, attempt=attempt)
        except Exception as exc:  # pragma: no cover - defensive
            return PlatformResult(platform=platform, ok=False,
                                  detail=f"{type(exc).__name__}: {exc}")

    def _publish_one(self, platform: str, video: Path, metadata: PublishMetadata,
                     schedule: str | None, covers: Mapping[str, Path] | None = None,
                     attempt: int = 1) -> PlatformResult:
        spec = PLATFORM_SPECS[platform]
        account = self.accounts.get(platform, "default")
        scoped = self.metadata_gen.for_platform(metadata, platform)
        # Absolute path is required: the subprocess runs with cwd set to the
        # sau project, and sau validates --file existence before parsing.
        command = [self._sau_bin or "sau", platform, "upload-video",
                   "--account", account, "--file", str(Path(video).resolve()),
                   "--title", scoped.title, "--desc", scoped.description]
        if scoped.tags:
            command += ["--tags", ",".join(scoped.tags)]
        if schedule:
            command += ["--schedule", schedule]
        if spec.get("tid"):
            command += ["--tid", str(spec["tid"])]
        for flag, preset in spec.get("covers", ()):
            cover = (covers or {}).get(preset)
            if cover is not None and Path(cover).is_file():
                command += [flag, str(Path(cover).resolve())]
        label = self._ai_label_for(platform)
        if label and spec.get("ai_statement_cli"):
            command += ["--ai-content-label", label]
        if spec.get("runtime_flags"):
            # bilibili delegates to the biliup binary, whose CLI rejects these.
            # baijiahao overrides to headed regardless of publish.headless.
            headed = bool(spec.get("headed")) or not self.headless
            command.append("--headed" if headed else "--headless")
        try:
            completed = self._runner(command, self.workdir)
        except StreamAbort as exc:
            return PlatformResult(platform=platform, ok=False, command=command, detail=str(exc),
                                  retryable=False)
        except subprocess.TimeoutExpired:
            return PlatformResult(platform=platform, ok=False, command=command,
                                  detail=f"upload timed out after {self.timeout:g}s")
        except (OSError, subprocess.SubprocessError) as exc:
            return PlatformResult(platform=platform, ok=False, command=command, detail=str(exc))
        ok = completed.returncode == 0
        detail = (completed.stderr or completed.stdout or ("ok" if ok else "unknown failure")).strip()
        if ok:
            return PlatformResult(platform=platform, ok=True, command=command, detail=detail[-800:])
        verdict = self._classify(platform, detail, attempt=attempt)
        shown = detail[-800:] + (f"\n{verdict.hint}" if verdict.hint else "")
        return PlatformResult(platform=platform, ok=False, command=command, detail=shown,
                              retryable=verdict.retryable, retry_after=verdict.wait_seconds)

    def _ai_label_for(self, platform: str) -> str:
        """Resolve the AI-content option text for one platform.

        ``publish.ai_content_label`` is either a single string (used for every
        platform that supports the flag) or a ``{platform: text}`` mapping for
        when 快手 and 小红书 word it differently.
        """
        raw = self.ai_content_label
        if isinstance(raw, Mapping):
            return str(raw.get(platform) or "").strip()
        return str(raw or "").strip()

    def _require_sau(self) -> str:
        """Resolve the sau entrypoint inside the vendored project's venv."""
        if self._sau_bin:
            return self._sau_bin
        venv_sau = self.sau_dir / ".venv" / "bin" / "sau"
        if venv_sau.is_file():
            # Absolute path is required: the subprocess runs with cwd set to the
            # sau project, so a relative executable path would resolve there.
            self._sau_bin = str(venv_sau.resolve())
            return self._sau_bin
        found = shutil.which("sau")
        if found:
            self._sau_bin = found
            return found
        raise PublishError(
            "sau CLI not found — run `uv sync` inside third_party/social-auto-upload "
            "or set publish.sau_dir to the social-auto-upload checkout"
        )

    def _run_subprocess(self, command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        """Run ``sau`` while streaming its output.

        Streaming buys two things a plain ``subprocess.run`` cannot: the upload
        is killed as soon as Douyin's SMS challenge proves unresolvable, and the
        timeout is still enforced when the child goes silent for minutes.
        stdout and stderr are merged because sau's loguru sink is stderr while
        progress noise lands on stdout — callers only ever surface the tail.
        """
        guard = self._make_guard(command)
        process = subprocess.Popen(
            list(command), cwd=cwd, text=True, encoding="utf-8", errors="replace",
            # A null stdin keeps sau from calling its own `input()` for the 抖音
            # SMS code: that prompt lands on the captured stream, where nobody
            # sees it, and the child blocks forever waiting for a reply.  With
            # no tty it takes the verify_code.txt path instead, which we can
            # feed from our own prompt.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
        )
        assert process.stdout is not None
        lines: list[str] = []
        stream: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            try:
                for line in process.stdout:  # type: ignore[union-attr]
                    stream.put(line)
            finally:
                stream.put(None)

        threading.Thread(target=pump, daemon=True).start()

        deadline = time.monotonic() + self.timeout
        abort_reason: str | None = None
        try:
            while True:
                try:
                    line = stream.get(timeout=1.0)
                except queue.Empty:
                    line = ""  # idle tick: still evaluate the guard and the timeout
                else:
                    if line is None:
                        break
                    lines.append(line)
                if guard is not None and abort_reason is None:
                    abort_reason = guard.feed(line)
                    if abort_reason:
                        break
                if time.monotonic() > deadline:
                    process.kill()
                    process.wait()
                    raise subprocess.TimeoutExpired(cmd=list(command), timeout=self.timeout,
                                                    output="".join(lines))
        except BaseException:
            # Ctrl-C (or anything else) must not leave a headless browser
            # running in the background with a half-finished upload.
            process.kill()
            process.wait()
            raise

        if abort_reason:
            process.kill()
        returncode = process.wait()
        output = "".join(lines)
        if abort_reason:
            raise StreamAbort(abort_reason)
        return subprocess.CompletedProcess(list(command), returncode, output, "")

    def _make_guard(self, command: Sequence[str]) -> SmsChallengeGuard | None:
        """Attach the SMS guard to video uploads only (never to ``check``)."""
        if self.verify_code_wait <= 0 or "upload-video" not in command:
            return None
        base = self.sau_dir if self.sau_dir.is_dir() else self.workdir
        platform = command[1] if len(command) > 1 else ""
        label = PLATFORM_LABELS.get(platform, platform) or "抖音"
        return SmsChallengeGuard(base / "verify_code.txt", self.verify_code_wait,
                                 on_arm=lambda: self._ask_for_code(label))


def publish_video(video_path: str | Path, script: Mapping[str, Any], stock_name: str, stock_code: str,
                  config: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Convenience entry point used by the pipeline."""
    return SauPublisher(config).publish(video_path, script, stock_name, stock_code, **kwargs)
