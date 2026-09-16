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

import queue
import shutil
import subprocess
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

# Douyin can gate the final publish click behind an SMS challenge.  sau detects
# the popup, clicks 「获取验证码」 and then polls ``verify_code.txt`` (or stdin)
# inside an endless ``while True`` loop, so an unattended run hangs until the
# outer upload timeout kills it — 15 minutes of nothing, with no clue in the
# log.  Watching sau's own stream lets us fail in minutes with a real remedy.
SMS_CHALLENGE_MARKERS: tuple[str, ...] = ("检测到短信验证码弹窗", "已点击「获取验证码」", "等待验证码输入")
SMS_RESOLVED_MARKERS: tuple[str, ...] = ("已获取验证码", "验证码已填入", "验证码处理完成", "发布成功")

# Per-platform upload constraints and CLI capability.  ``runtime_flags`` marks
# whether ``sau <platform> upload-video`` accepts --headless/--headed (bilibili
# delegates to biliup and rejects them).  ``title``/``tags``/``desc`` are the
# publish limits we clamp metadata to before invoking the CLI.  ``covers`` lists
# the ``(flag, preset)`` pairs the platform accepts, where preset names one of
# cover.COVER_PRESETS — douyin/tencent take a landscape cover as well as the
# 3:4 portrait one, kuaishou/xiaohongshu take a single image, and bilibili's
# cover is landscape-first.
PLATFORM_SPECS: dict[str, dict[str, Any]] = {
    "douyin":      {"label": "抖音",  "title": 30, "tags": 4,  "desc": 150, "runtime_flags": True,  "tid": None,
                    "covers": (("--thumbnail", "portrait"), ("--thumbnail-landscape", "landscape"))},
    "bilibili":    {"label": "B站",   "title": 80, "tags": 10, "desc": 2000, "runtime_flags": False, "tid": BILIBILI_FINANCE_TID,
                    "covers": (("--thumbnail", "wide"),)},
    "kuaishou":    {"label": "快手",  "title": 30, "tags": 6,  "desc": 150, "runtime_flags": True,  "tid": None,
                    "covers": (("--thumbnail", "portrait"),)},
    "xiaohongshu": {"label": "小红书", "title": 20, "tags": 10, "desc": 1000, "runtime_flags": True,  "tid": None,
                    "covers": (("--thumbnail", "portrait"),)},
    "tencent":     {"label": "视频号", "title": 30, "tags": 6,  "desc": 120, "runtime_flags": True,  "tid": None,
                    "covers": (("--thumbnail-portrait", "portrait"), ("--thumbnail-landscape", "landscape"))},
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


class SmsChallengeGuard:
    """Fail fast when sau is stuck waiting for a Douyin SMS verification code.

    ``feed`` is called once per line of sau's output (and once per idle tick
    with an empty string), so the deadline is enforced even when the child
    process goes completely silent.

    ``verify_code_wait_seconds <= 0`` disables the guard entirely.
    """

    def __init__(self, code_file: Path, wait_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.code_file = Path(code_file)
        self.wait_seconds = float(wait_seconds)
        self._clock = clock
        self.deadline: float | None = None

    @property
    def armed(self) -> bool:
        return self.deadline is not None

    def feed(self, line: str) -> str | None:
        """Observe one output line; return an abort reason once the wait lapses."""
        if any(marker in line for marker in SMS_RESOLVED_MARKERS):
            self.deadline = None
            return None
        if any(marker in line for marker in SMS_CHALLENGE_MARKERS):
            if self.deadline is None:
                self.deadline = self._clock() + self.wait_seconds
            return None
        if self.deadline is None:
            return None
        # A human (or a watcher) can still rescue the run by dropping the code
        # into the file sau polls; that buys another window.
        if self.code_file.is_file():
            self.deadline = self._clock() + self.wait_seconds
            return None
        if self._clock() >= self.deadline:
            return (f"抖音要求短信验证码，等待 {self.wait_seconds:g}s 仍未收到；"
                    f"把手机收到的验证码写入 {self.code_file} 后重试")
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
                 sau_bin: str | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
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
        self._sleep = sleep
        self.metadata_gen = PublishMetadataGenerator(self.config)
        self._runner = runner or self._run_subprocess
        self._sau_bin = sau_bin

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

        for platform in pending:
            spec = PLATFORM_SPECS.get(platform)
            label = (spec or DEFAULT_SPEC)["label"] or platform
            if not spec:
                outcomes[platform] = PlatformResult(platform=platform, ok=False, detail="unsupported platform")
                continue
            on_event and on_event(platform, label, "上传中")
            outcome = self._publish_safely(platform, videos.get(platform, video), metadata, schedule, cover_files)
            on_event and on_event(platform, label, "完成" if outcome.ok else "失败")
            outcomes[platform] = outcome

        for attempt in range(1, self.retries + 1):
            retry_targets = [p for p in pending if p in PLATFORM_SPECS
                             and p in outcomes and not outcomes[p].ok and outcomes[p].retryable]
            if not retry_targets:
                break
            labels = "、".join(PLATFORM_LABELS.get(p, p) for p in retry_targets)
            on_event and on_event("*", "重试", f"第 {attempt} 次重试 {labels}（{self.retry_delay:g}s 后）")
            self._sleep(self.retry_delay)
            for platform in retry_targets:
                label = PLATFORM_LABELS.get(platform, platform)
                on_event and on_event(platform, label, "重试中")
                outcome = self._publish_safely(platform, videos.get(platform, video), metadata, schedule, cover_files)
                outcome.attempts = outcomes[platform].attempts + 1
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
    # Internals
    # ------------------------------------------------------------------
    def _publish_safely(self, platform: str, video: Path, metadata: PublishMetadata,
                        schedule: str | None, covers: Mapping[str, Path]) -> PlatformResult:
        """Publish one platform, absorbing *any* failure into its own result.

        ``_publish_one`` already handles the failures we anticipate; this is the
        backstop that keeps an unforeseen bug (a metadata edge case, a missing
        file) from taking down the other four platforms in the same run.
        """
        try:
            return self._publish_one(platform, video, metadata, schedule, covers)
        except Exception as exc:  # pragma: no cover - defensive
            return PlatformResult(platform=platform, ok=False,
                                  detail=f"{type(exc).__name__}: {exc}")

    def _publish_one(self, platform: str, video: Path, metadata: PublishMetadata,
                     schedule: str | None, covers: Mapping[str, Path] | None = None) -> PlatformResult:
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
        if spec.get("runtime_flags"):
            # bilibili delegates to the biliup binary, whose CLI rejects these.
            command.append("--headless" if self.headless else "--headed")
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
        return PlatformResult(platform=platform, ok=ok, command=command, detail=detail[-800:])

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
        return SmsChallengeGuard(base / "verify_code.txt", self.verify_code_wait)


def publish_video(video_path: str | Path, script: Mapping[str, Any], stock_name: str, stock_code: str,
                  config: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Convenience entry point used by the pipeline."""
    return SauPublisher(config).publish(video_path, script, stock_name, stock_code, **kwargs)
