"""封面图生成：用 headless Chrome 把 Jinja 模板截成各平台要求的封面 PNG。

发布到抖音/快手/小红书/视频号的视频需要 3:4 竖版封面，B站需要横版封面。
这里复用视频的配色与 A股 红涨绿跌 约定，用同一份模板渲染任意画幅。

Chrome 通过一次性的 ``--screenshot`` 模式驱动，并且**不能**依赖它的退出码：
本机 Chrome 截完图后常被 updater/crashpad 辅助进程拖着不退出（实测会一直挂着），
所以这里轮询产物文件、稳定后立即杀掉进程组。同理 ``--no-sandbox`` 是必需的——
带沙箱时 Chrome 在受限环境里会直接 ``Failed to initialize sandbox`` 后退出。

封面是发布链路上的**可选项**：Chrome 缺失或截图失败一律降级为「无封面发布」，
绝不因为一张封面图中断已经渲染好的视频发布。
"""

from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from stocktalk.modules.compliance import ComplianceAgent

# name -> (width, height).  3:4 竖版给抖音/快手/小红书/视频号，4:3 与 16:9 横版
# 给 B站（以及抖音/视频号的横版封面位）。
COVER_PRESETS: dict[str, tuple[int, int]] = {
    "portrait": (1080, 1440),
    "landscape": (1080, 810),
    "wide": (1440, 810),
}

# Chrome 常见安装位置；macOS 排前面，其次 Linux/Windows，最后 PATH。
CHROME_CANDIDATES: tuple[str, ...] = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
)

# Chart panel title per canvas family.
CHART_TITLES = {"portrait": "近30日价格走势", "landscape": "近30日价格走势", "wide": "近30日价格走势"}

Screenshotter = Callable[[str, Path, Path, tuple[int, int], float], bool]


class CoverError(RuntimeError):
    """A cover could not be rendered; callers degrade to publishing without one."""


def _png_size(path: Path) -> tuple[int, int] | None:
    """Read a PNG's dimensions straight from the IHDR chunk (no image library)."""
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    width, height = int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    return (width, height) if width and height else None


class CoverGenerator:
    """Render cover images for one video via headless Chrome."""

    def __init__(self, config: Mapping[str, Any] | None = None,
                 screenshotter: Screenshotter | None = None,
                 chrome: str | None = None) -> None:
        self.config = dict(config or {})
        cover = self.config.get("cover", {}) if isinstance(self.config.get("cover"), Mapping) else {}
        self.enabled = bool(cover.get("enabled", True))
        self.configured_chrome = str(cover.get("chrome_path") or "").strip()
        self.timeout = float(cover.get("timeout_seconds", 90))
        self.virtual_time_budget = int(cover.get("virtual_time_budget_ms", 4000))
        self._asset_dir = Path(__file__).resolve().parents[1] / "templates" / "cover"
        self._env = Environment(loader=FileSystemLoader(str(self._asset_dir)),
                                autoescape=select_autoescape(["html", "j2"]))
        self._screenshotter = screenshotter or self._chrome_screenshot
        self._chrome = chrome
        self.compliance = ComplianceAgent(self.config)

    # ------------------------------------------------------------------
    # Environment
    # ------------------------------------------------------------------
    def chrome_path(self) -> str | None:
        """Locate a usable Chrome/Chromium binary, or None when there is none.

        An explicitly configured ``cover.chrome_path`` is authoritative: if it
        does not exist we report "no Chrome" rather than silently falling back
        to a different binary than the operator asked for.
        """
        if self._chrome:
            return self._chrome
        if self.configured_chrome:
            candidate = Path(self.configured_chrome).expanduser()
            self._chrome = str(candidate) if candidate.is_file() else None
            return self._chrome
        for candidate in CHROME_CANDIDATES:
            if Path(candidate).is_file():
                self._chrome = candidate
                return candidate
        for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
            found = shutil.which(name)
            if found:
                self._chrome = found
                return found
        return None

    @property
    def available(self) -> bool:
        return self.enabled and self.chrome_path() is not None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def generate(self, stock_data: Mapping[str, Any], script: Mapping[str, Any],
                 stock_code: str, sizes: Sequence[str], out_dir: str | Path,
                 tag: str) -> dict[str, Path]:
        """Render every requested size; individual failures are skipped.

        Returns ``{size: png_path}``.  A size that cannot be rendered is simply
        absent — the caller publishes that platform without a cover.
        """
        if not self.enabled or not sizes:
            return {}
        chrome = self.chrome_path()
        if chrome is None:
            return {}
        directory = Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        rendered: dict[str, Path] = {}
        for size in dict.fromkeys(sizes):
            preset = COVER_PRESETS.get(size)
            if preset is None:
                continue
            try:
                rendered[size] = self._render_one(chrome, stock_data, script, stock_code,
                                                  size, preset, directory, tag)
            except Exception:  # A cover is a nice-to-have; never break the publish.
                continue
        return rendered

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _render_one(self, chrome: str, stock_data: Mapping[str, Any], script: Mapping[str, Any],
                    stock_code: str, size: str, preset: tuple[int, int],
                    directory: Path, tag: str) -> Path:
        width, height = preset
        png = directory / f"{tag}.cover-{size}.png"
        page = self._html(stock_data, script, stock_code, size, width, height)
        with tempfile.TemporaryDirectory(prefix="tangulunjin-cover-") as temp:
            root = Path(temp)
            html_path = root / "cover.html"
            html_path.write_text(page, encoding="utf-8")
            if png.exists():
                png.unlink()
            if not self._screenshotter(chrome, html_path, png, (width, height), self.timeout):
                raise CoverError(f"{size} 封面截图失败")
        measured = _png_size(png)
        if measured != preset:
            raise CoverError(f"{size} 封面尺寸异常: {measured}，期望 {preset}")
        return png

    def _html(self, stock_data: Mapping[str, Any], script: Mapping[str, Any], stock_code: str,
              size: str, width: int, height: int) -> str:
        quote = stock_data.get("quote") if isinstance(stock_data.get("quote"), Mapping) else {}
        technical = stock_data.get("technical") if isinstance(stock_data.get("technical"), Mapping) else {}
        stock_name = str(quote.get("name") or script.get("title") or stock_code).strip()
        tone = self._tone(quote)
        bars = self._bars(technical)
        clamps = [self._number(bar.get("close")) for bar in bars]
        clamps = [value for value in clamps if value is not None]
        sparkline, spark_end_y = self._sparkline(bars, tone)
        template = self._env.get_template("cover.html.j2")
        return template.render(
            name=stock_name or stock_code,
            code=stock_code,
            width=width,
            height=height,
            tone=tone,
            price_text=self._price_text(quote),
            change_text=self._change_text(quote),
            chart_title=CHART_TITLES.get(size, "近30日价格走势"),
            chart_min=f"{min(clamps):,.2f}" if clamps else "",
            chart_max=f"{max(clamps):,.2f}" if clamps else "",
            sparkline=sparkline,
            spark_end_y=spark_end_y,
            hook=self._hook(script, stock_name, stock_code),
            tags=self._tags(script),
        )

    @staticmethod
    def _sparkline(bars: Sequence[Mapping[str, Any]], tone: str) -> tuple[str, float | None]:
        """Draw the real close-price curve (no synthetic candles).

        Returns ``(svg, end_y_ratio)`` — the second value places the HTML
        end-point halo in the template (an HTML layer, so the SVG's
        ``preserveAspectRatio="none"`` stretch cannot turn it into an ellipse).

        ``preserveAspectRatio="none"`` stretches the curve to the panel, and
        ``vector-effect: non-scaling-stroke`` (set in CSS) keeps the stroke
        uniform — the template therefore needs no knowledge of the panel's
        exact pixel size.  Nothing inside the SVG is text or a circle, so
        stretching cannot distort a glyph or turn a dot into an ellipse.
        """
        closes = [CoverGenerator._number(bar.get("close")) for bar in bars]
        closes = [value for value in closes if value is not None]
        width, height = 1000.0, 420.0
        grid = "".join(
            f'<line class="grid" x1="0" y1="{height * frac:.1f}" x2="{width:.1f}" y2="{height * frac:.1f}"/>'
            for frac in (0.25, 0.5, 0.75)
        )
        if len(closes) < 2:
            return (f'<svg viewBox="0 0 {width:.0f} {height:.0f}" preserveAspectRatio="none" '
                    f'role="img">{grid}</svg>', None)
        floor, top = min(closes), max(closes)
        spread = max(top - floor, max(abs(top) * 0.012, 0.01))
        pad = height * 0.08
        step = width / (len(closes) - 1)
        points = [(index * step, pad + (top - value) / spread * (height - 2 * pad))
                  for index, value in enumerate(closes)]
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        area = f"M0,{height:.1f} L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points) + f" L{width:.1f},{height:.1f} Z"
        path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points)
        end_y_ratio = max(0.0, min(1.0, points[-1][1] / height)) * 100.0
        gradient = (
            f'<defs><linearGradient id="spark-fade" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="currentColor" stop-opacity=".34"/>'
            f'<stop offset="1" stop-color="currentColor" stop-opacity=".02"/>'
            f'</linearGradient></defs>'
        )
        svg = (
            f'<svg viewBox="0 0 {width:.0f} {height:.0f}" preserveAspectRatio="none" role="img">'
            f'{gradient}{grid}'
            f'<path class="spark-fill" fill="url(#spark-fade)" d="{area}"/>'
            f'<path class="spark-line" d="{path}"/>'
            f'</svg>'
        )
        return svg, round(end_y_ratio, 2)

    @staticmethod
    def _bars(technical: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """Flatten real daily closes out of the indicator history.

        最近 30 根：60 日在缩略图里压成一团噪声，且常与当日涨跌情绪相悖
        （60 日下行 + 当天大涨，红曲线画成一路向下）；30 日曲率清晰，
        也更贴近「今天发生了什么」的封面叙事。
        """
        history = technical.get("history") if isinstance(technical.get("history"), list) else []
        bars: list[Mapping[str, Any]] = []
        for item in [entry for entry in history if isinstance(entry, Mapping)][-30:]:
            price = item.get("price") if isinstance(item.get("price"), Mapping) else {}
            if CoverGenerator._number(price.get("current")) is None:
                continue
            bars.append({"close": price.get("current"), "date": str(item.get("timestamp") or "")})
        return bars

    def _hook(self, script: Mapping[str, Any], stock_name: str, stock_code: str) -> str:
        """One compliance-safe line of copy, without repeating the stock name."""
        title = str(script.get("title") or "").strip()
        # The cover already shows 「名称 代码」, so a title that opens with them
        # would read as a stutter; drop that prefix and keep the actual claim.
        stripped = title
        for lead in (f"{stock_name}（{stock_code}）", f"{stock_name}({stock_code})",
                     f"{stock_name} {stock_code}", f"{stock_name}{stock_code}", stock_name):
            if lead and stripped.startswith(lead):
                stripped = stripped[len(lead):]
                break
        hook = stripped.lstrip("：:·-—　 ").strip()
        if not hook:
            for turn in script.get("turns", ()):
                if isinstance(turn, Mapping) and str(turn.get("line", "")).strip():
                    hook = str(turn["line"]).strip()
                    break
        if not hook:
            hook = f"{stock_name}的生意本质与数据边界"
        return self._clip(self.compliance.sanitize(hook), 38)

    @staticmethod
    def _tags(script: Mapping[str, Any]) -> list[str]:
        """A short #话题 行 built from real script keywords.

        只取 2 个：feed 缩略图里第三个以后的胶囊读不清，纯噪声。
        """
        words: list[str] = []
        for turn in script.get("turns", ()):
            if not isinstance(turn, Mapping):
                continue
            for keyword in turn.get("visual", ()) or ():
                word = str(keyword).strip()
                if 2 <= len(word) <= 6 and word not in words:
                    words.append(word)
        for fallback in ("A股", "基本面", "财报解读"):
            if fallback not in words:
                words.append(fallback)
        return [f"#{word}" for word in words[:2]]

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _tone(quote: Mapping[str, Any]) -> str:
        change = CoverGenerator._number(quote.get("change_pct"))
        if change is None or change == 0:
            return "flat"
        return "up" if change > 0 else "down"

    @staticmethod
    def _price_text(quote: Mapping[str, Any]) -> str:
        price = CoverGenerator._number(quote.get("price"))
        return f"{price:,.2f}" if price is not None else "—"

    @staticmethod
    def _change_text(quote: Mapping[str, Any]) -> str:
        change = CoverGenerator._number(quote.get("change_pct"))
        return f"{change:+.2f}%" if change is not None else ""

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def _chrome_screenshot(self, chrome: str, html_path: Path, png: Path,
                           size: tuple[int, int], timeout: float) -> bool:
        """Screenshot ``html_path`` into ``png``; True when a stable file landed.

        Chrome is expected to hang after writing the image, so the process is
        killed as soon as the file stops growing — the exit status is ignored.
        """
        width, height = size
        with tempfile.TemporaryDirectory(prefix="tangulunjin-chrome-") as profile:
            command = [
                chrome, "--headless=new", "--no-sandbox", "--hide-scrollbars",
                "--force-device-scale-factor=1", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--mute-audio",
                f"--user-data-dir={profile}",
                f"--window-size={width},{height}",
                f"--virtual-time-budget={self.virtual_time_budget}",
                "--run-all-compositor-stages-before-draw",
                f"--screenshot={png}",
                html_path.as_uri(),
            ]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
            try:
                deadline = time.monotonic() + timeout
                previous = -1
                while time.monotonic() < deadline:
                    time.sleep(0.25)
                    size_now = png.stat().st_size if png.is_file() else 0
                    # Require the file to stop growing: a poll landing mid-write
                    # would otherwise hand back a truncated PNG.
                    if size_now > 1024 and size_now == previous:
                        return True
                    previous = size_now
                    if process.poll() is not None and size_now == 0:
                        return False
                return png.is_file() and png.stat().st_size > 1024
            finally:
                self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[Any]) -> None:
        """Kill the whole Chrome process group (helpers outlive the parent)."""
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()


def render_covers(stock_data: Mapping[str, Any], script: Mapping[str, Any], stock_code: str,
                  sizes: Sequence[str], out_dir: str | Path, tag: str,
                  config: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Path]:
    """Convenience entry point used by the pipeline."""
    return CoverGenerator(config, **kwargs).generate(stock_data, script, stock_code, sizes, out_dir, tag)
