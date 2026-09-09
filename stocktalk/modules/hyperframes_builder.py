"""Build and render a deterministic HyperFrames stock-debate composition.

The builder intentionally accepts the normalized payloads produced by the data,
script, and TTS modules instead of coupling the video layer to their concrete
classes.  This keeps the pipeline testable and makes the generated project
editable in HyperFrames Studio.
"""

from __future__ import annotations

import html
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


class HyperFramesBuildError(RuntimeError):
    """Raised when a project cannot be built or rendered."""


Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class HyperFramesProject:
    """Paths and duration for a generated, standalone HyperFrames project."""

    directory: Path
    composition_path: Path
    duration: float


class HyperFramesBuilder:
    """Create a 1920×1080 dark-finance debate composition and render it to MP4."""

    def __init__(self, config: Mapping[str, Any] | None = None, runner: Runner | None = None) -> None:
        self.config = dict(config or {})
        video = self.config.get("video", {})
        self.video_config = dict(video) if isinstance(video, Mapping) else {}
        self.characters = self.config.get("characters", {})
        self._runner = runner or self._run_subprocess

    def build_project(
        self,
        stock_data: Mapping[str, Any],
        script: Mapping[str, Any] | None,
        tts_timeline: Mapping[str, Any] | None,
    ) -> HyperFramesProject:
        """Write ``index.html`` and a motion contract, returning the project.

        ``tts_timeline['segments']`` supplies real audio paths and timestamps.
        If audio is not available yet, script lines still get deterministic
        display windows, allowing a visual draft to be reviewed first.
        """
        project_dir = Path(self.video_config.get("project_dir", self._output_dir / "hyperframes"))
        project_dir.mkdir(parents=True, exist_ok=True)
        segments = self._segments(script or {}, tts_timeline or {})
        duration = self._duration(segments, tts_timeline or {})
        composition = project_dir / "index.html"
        composition.write_text(self._html(stock_data, segments, duration, project_dir), encoding="utf-8")
        (project_dir / "index.motion.json").write_text(
            json.dumps(
                {
                    "duration": duration,
                    "assertions": [
                        {"kind": "appearsBy", "selector": "#headline", "bySec": 0.8},
                        {"kind": "staysInFrame", "selector": "#finance-card"},
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return HyperFramesProject(project_dir, composition, duration)

    build = build_project

    def render_mp4(self, project: HyperFramesProject | str | Path, output_path: str | Path | None = None) -> Path:
        """Run HyperFrames CLI and return a non-empty MP4 path.

        Rendering is an explicit pipeline step; callers may build, inspect, and
        approve the generated HTML before invoking this method.
        """
        project_dir = project.directory if isinstance(project, HyperFramesProject) else Path(project)
        destination = Path(output_path or self.video_config.get("output_path", self._output_dir / "stocktalk.mp4"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if shutil.which("npx") is None:
            raise HyperFramesBuildError("Node.js/npx is required to run HyperFrames CLI")
        command = ("npx", "hyperframes", "render", str(project_dir), "--output", str(destination), "--quality", str(self.video_config.get("quality", "high")))
        try:
            completed = self._runner(command, project_dir)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HyperFramesBuildError(f"HyperFrames render could not start: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown renderer failure").strip()
            raise HyperFramesBuildError(f"HyperFrames render failed: {detail}")
        if not destination.is_file() or destination.stat().st_size == 0:
            raise HyperFramesBuildError("HyperFrames render completed without creating a usable MP4")
        return destination

    render = render_mp4

    @property
    def _output_dir(self) -> Path:
        return Path(self.video_config.get("output_dir", "./output"))

    @staticmethod
    def _run_subprocess(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=900, check=False)

    def _segments(self, script: Mapping[str, Any], timeline: Mapping[str, Any]) -> list[dict[str, Any]]:
        supplied = timeline.get("segments", [])
        segments: list[dict[str, Any]] = []
        if isinstance(supplied, list):
            for item in supplied:
                if not isinstance(item, Mapping):
                    continue
                line = str(item.get("line", "")).strip()
                duration = self._float(item.get("duration"), 0.0)
                start = self._float(item.get("start_time"), 0.0)
                if line and duration > 0:
                    segments.append({"line": line, "character": str(item.get("character", "bull")), "start": max(0.0, start), "duration": duration, "audio_path": item.get("audio_path")})
        if segments:
            return sorted(segments, key=lambda item: item["start"])

        cursor = 0.0
        for round_data in script.get("rounds", []) if isinstance(script.get("rounds", []), list) else []:
            if not isinstance(round_data, Mapping):
                continue
            for character in ("bull", "bear"):
                entry = round_data.get(character)
                line = str(entry.get("line", "") if isinstance(entry, Mapping) else entry or "").strip()
                if line:
                    duration = max(2.5, min(9.0, len(line) * 0.23))
                    segments.append({"line": line, "character": character, "start": cursor, "duration": duration, "audio_path": None})
                    cursor += duration + 0.25
        return segments

    def _duration(self, segments: list[Mapping[str, Any]], timeline: Mapping[str, Any]) -> float:
        supplied = self._float(timeline.get("total_duration"), 0.0)
        end = max((float(segment["start"]) + float(segment["duration"]) for segment in segments), default=0.0)
        configured = self._float(self.video_config.get("duration"), 0.0)
        return round(max(1.0, supplied, end, configured), 3)

    def _html(self, stock: Mapping[str, Any], segments: list[Mapping[str, Any]], duration: float, project_dir: Path) -> str:
        quote = stock.get("quote", {}) if isinstance(stock.get("quote"), Mapping) else {}
        financials = stock.get("financials", {}) if isinstance(stock.get("financials"), Mapping) else {}
        code = self._text(stock.get("code") or quote.get("code") or "股票")
        name = self._text(quote.get("name") or stock.get("name") or code)
        price = self._number_text(quote.get("price"))
        change = self._number_text(quote.get("change_pct"), suffix="%")
        financial_rows = self._financial_rows(financials)
        chart = self._candles(stock.get("kline", []))
        role_cards = self._role_cards(duration)
        speech_cards = "\n".join(self._speech_card(segment, index) for index, segment in enumerate(segments, 1))
        audio = "\n".join(self._audio_clip(segment, index, project_dir) for index, segment in enumerate(segments, 1) if segment.get("audio_path"))
        return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=1920, height=1080" />
  <title>{name} · 股会说</title>
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  <style>
    * {{ box-sizing: border-box; }} body {{ margin:0; background:#07111f; color:#f5f8ff; font-family:Arial,sans-serif; }}
    #root {{ position:relative; width:1920px; height:1080px; overflow:hidden; background:radial-gradient(circle at 15% 20%,#14345a 0,#07111f 43%,#030813 100%); }}
    .clip {{ position:absolute; inset:0; }} .grid {{ position:absolute; inset:0; opacity:.19; background-image:linear-gradient(#4f84b822 1px,transparent 1px),linear-gradient(90deg,#4f84b822 1px,transparent 1px); background-size:64px 64px; }}
    .ticker {{ position:absolute; top:66px; left:88px; }} .eyebrow {{ color:#78bbff; font-size:28px; letter-spacing:5px; }} h1 {{ margin:12px 0 0; font-size:68px; }} .quote {{ color:#7ee7be; font-size:32px; margin-top:12px; }}
    .panel {{ background:#0c1b30e8; border:1px solid #2b5179; border-radius:22px; box-shadow:0 16px 50px #0008; }}
    .roles {{ position:absolute; top:290px; left:88px; width:440px; height:245px; }} .role {{ inset:auto; width:440px; height:112px; padding:22px 26px; }} .role.bull {{ top:0; border-left:7px solid #62d6ac; }} .role.bear {{ top:130px; border-left:7px solid #ff8a9c; }} .role strong {{ font-size:30px; }} .role p {{ color:#b9c9db; font-size:20px; margin:7px 0 0; }}
    .finance {{ position:absolute; top:230px; right:88px; bottom:auto; left:auto; width:420px; height:470px; padding:26px; }} .finance h2,.chart h2 {{ margin:0 0 16px; color:#a8d1ff; font-size:28px; }} .metric {{ display:flex; justify-content:space-between; padding:13px 0; border-top:1px solid #244260; font-size:22px; }} .metric span:last-child {{ color:#fff; font-weight:700; }}
    .chart {{ position:absolute; left:570px; right:auto; top:230px; bottom:auto; width:800px; height:510px; padding:24px; }} .chart svg {{ width:100%; height:410px; }} .axis {{ stroke:#41617f; stroke-width:1; }}
    .speech {{ position:absolute; top:auto; left:370px; right:370px; bottom:96px; height:180px; padding:30px 42px; display:flex; align-items:center; }} .speaker {{ font-size:25px; color:#7ee7be; font-weight:bold; }} .speech.bear .speaker {{ color:#ff9baa; }} .speech p {{ margin:12px 0 0; font-size:34px; line-height:1.35; }}
    .disclaimer {{ position:absolute; bottom:32px; width:100%; text-align:center; color:#92a6be; font-size:20px; letter-spacing:1px; }}
  </style>
</head>
<body>
  <div id="root" data-composition-id="stock-debate" data-start="0" data-width="1920" data-height="1080" data-duration="{duration}" data-fps="30">
    <div id="background" class="clip" data-start="0" data-duration="{duration}" data-track-index="0"><div class="grid" data-layout-ignore></div></div>
    <section id="headline" class="clip" data-start="0" data-duration="{duration}" data-track-index="1"><div class="ticker"><div class="eyebrow">股会说 · 财报观点碰撞</div><h1>{name} <span style="color:#78bbff">{code}</span></h1><div class="quote">最新价 {price}　涨跌幅 {change}</div></div></section>
    {role_cards}
    <section id="finance-card" class="clip panel finance" data-start="0.4" data-duration="{max(0.1, duration - 0.4)}" data-track-index="3"><h2>财报数据摘要</h2>{financial_rows}</section>
    <section id="kline-chart" class="clip panel chart" data-start="0.7" data-duration="{max(0.1, duration - 0.7)}" data-track-index="4"><h2>K 线走势</h2>{chart}</section>
    {speech_cards}
    {audio}
    <div id="disclaimer" class="clip disclaimer" data-start="0" data-duration="{duration}" data-track-index="30">内容仅为虚拟人物观点交流，不构成任何投资建议；市场有风险，决策需谨慎。</div>
  </div>
  <script>
    const tl = gsap.timeline({{ paused: true }});
    tl.fromTo("#headline .ticker", {{ opacity: 0, y: -30 }}, {{ opacity: 1, y: 0, duration: .65, ease: "power2.out" }}, 0);
    tl.fromTo(".finance", {{ opacity: 0, x: 34 }}, {{ opacity: 1, x: 0, duration: .55, ease: "power2.out" }}, .25);
    tl.fromTo(".chart", {{ opacity: 0, y: 28 }}, {{ opacity: 1, y: 0, duration: .55, ease: "power2.out" }}, .4);
    window.__timelines["stock-debate"] = tl;
  </script>
</body>
</html>'''

    def _role_cards(self, duration: float) -> str:
        cards = []
        for character, label, mood in (("bull", "看多方", "乐观观察"), ("bear", "看空方", "理性审视")):
            info = self.characters.get(character, {}) if isinstance(self.characters, Mapping) else {}
            display = self._text(info.get("name") if isinstance(info, Mapping) else "") or label
            cards.append(f'<section id="role-{character}" class="clip panel role {character}" data-start="0.25" data-duration="{max(0.1, duration - 0.25)}" data-track-index="2"><strong>{display}</strong><p>{mood}</p></section>')
        return "<div class=\"roles\">" + "".join(cards) + "</div>"

    def _speech_card(self, segment: Mapping[str, Any], index: int) -> str:
        character = "bear" if str(segment["character"]).lower() == "bear" else "bull"
        info = self.characters.get(character, {}) if isinstance(self.characters, Mapping) else {}
        label = self._text(info.get("name") if isinstance(info, Mapping) else "") or ("看空方" if character == "bear" else "看多方")
        return f'<section id="speech-{index}" class="clip panel speech {character}" data-start="{segment["start"]}" data-duration="{segment["duration"]}" data-track-index="10"><div><div class="speaker">{label}</div><p>{self._text(segment["line"])}</p></div></section>'

    @staticmethod
    def _audio_clip(segment: Mapping[str, Any], index: int, project_dir: Path) -> str:
        source = Path(str(segment["audio_path"])).expanduser()
        try:
            relative = os.path.relpath(source, project_dir).replace(os.sep, "/")
        except ValueError:
            relative = str(source).replace(os.sep, "/")
        return f'<audio id="audio-{index}" src="{html.escape(relative, quote=True)}" data-start="{segment["start"]}" data-duration="{segment["duration"]}" data-track-index="{100 + index}"></audio>'

    def _financial_rows(self, financials: Mapping[str, Any]) -> str:
        aliases = (("营收", ("revenue", "营业收入")), ("净利润", ("net_profit", "净利润")), ("毛利率", ("gross_margin", "毛利率")), ("ROE", ("roe", "净资产收益率")))
        rows = []
        for label, keys in aliases:
            value = next((financials[key] for key in keys if financials.get(key) is not None), "—")
            rows.append(f'<div class="metric"><span>{label}</span><span>{self._text(value)}</span></div>')
        return "".join(rows)

    def _candles(self, bars: Any) -> str:
        values = [bar for bar in bars if isinstance(bar, Mapping)] if isinstance(bars, list) else []
        values = values[-40:]
        if not values:
            return '<svg viewBox="0 0 760 410" role="img" aria-label="暂无 K 线数据"><text x="380" y="205" fill="#8fa7c1" text-anchor="middle" font-size="26">暂无 K 线数据</text></svg>'
        lows = [self._float(bar.get("low"), self._float(bar.get("close"), 0)) for bar in values]
        highs = [self._float(bar.get("high"), self._float(bar.get("close"), 0)) for bar in values]
        floor, ceiling = min(lows), max(highs)
        spread = max(ceiling - floor, max(abs(ceiling) * .03, .01))
        def y(value: float) -> float: return 378 - ((value - floor) / spread) * 338
        step = 700 / max(len(values), 1)
        shapes = ['<line class="axis" x1="30" y1="40" x2="730" y2="40"/><line class="axis" x1="30" y1="210" x2="730" y2="210"/><line class="axis" x1="30" y1="378" x2="730" y2="378"/>']
        for index, bar in enumerate(values):
            open_, close = self._float(bar.get("open"), 0), self._float(bar.get("close"), 0)
            high, low = self._float(bar.get("high"), max(open_, close)), self._float(bar.get("low"), min(open_, close))
            x, color = 30 + (index + .5) * step, "#63d8a8" if close >= open_ else "#ff8193"
            top, height = min(y(open_), y(close)), max(3, abs(y(open_) - y(close)))
            width = min(16, step * .56)
            shapes.append(f'<line x1="{x:.1f}" y1="{y(high):.1f}" x2="{x:.1f}" y2="{y(low):.1f}" stroke="{color}" stroke-width="2"/><rect x="{x - width / 2:.1f}" y="{top:.1f}" width="{width:.1f}" height="{height:.1f}" fill="{color}"/>')
        return '<svg viewBox="0 0 760 410" role="img" aria-label="K 线图">' + "".join(shapes) + "</svg>"

    @staticmethod
    def _float(value: Any, default: float) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _text(value: Any) -> str:
        return html.escape(str(value), quote=True) if value not in (None, "") else "—"

    @classmethod
    def _number_text(cls, value: Any, suffix: str = "") -> str:
        number = cls._float(value, float("nan"))
        return "—" if not math.isfinite(number) else f"{number:,.2f}{suffix}"
