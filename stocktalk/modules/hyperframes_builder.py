"""Build an editable, data-driven HyperFrames stock debate composition."""
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

from jinja2 import Environment, FileSystemLoader, select_autoescape

class HyperFramesBuildError(RuntimeError):
    """Raised when a project cannot be built or rendered."""

Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]
TOPICS = {
    "financial": ("财务趋势", ("营收", "利润", "收入", "毛利", "roe", "财报", "业绩")),
    "technical": ("技术走势", ("k线", "均线", "技术", "成交量", "macd", "指标", "走势")),
    "comparison": ("行业对比", ("行业", "对比", "同业", "排名", "竞品")),
    "valuation": ("估值温度", ("估值", "pe", "pb", "市盈", "市净")),
    "sentiment": ("市场情绪", ("情绪", "心理", "信心", "恐慌", "预期")),
    "risk": ("风险提示", ("风险", "不确定", "警惕", "压力", "下行", "回撤", "波动")),
}

@dataclass(frozen=True)
class HyperFramesProject:
    directory: Path
    composition_path: Path
    duration: float

class HyperFramesBuilder:
    """Create a 1920×1080 debate composition from separate Jinja/CSS/JS assets."""
    def __init__(self, config: Mapping[str, Any] | None = None, runner: Runner | None = None) -> None:
        self.config = dict(config or {})
        video = self.config.get("video", {})
        self.video_config = dict(video) if isinstance(video, Mapping) else {}
        self.characters = self.config.get("characters", {})
        self._runner = runner or self._run_subprocess

    def build_project(self, stock_data: Mapping[str, Any], script: Mapping[str, Any] | None, tts_timeline: Mapping[str, Any] | None) -> HyperFramesProject:
        directory = Path(self.video_config.get("project_dir", self._output_dir / "hyperframes")); directory.mkdir(parents=True, exist_ok=True)
        segments = self._segments(script or {}, tts_timeline or {}); duration = self._duration(segments, tts_timeline or {})
        self._copy_assets(directory)
        composition = directory / "index.html"; composition.write_text(self._html(stock_data, segments, duration), encoding="utf-8")
        (directory / "index.motion.json").write_text(json.dumps({"duration": duration, "assertions": [{"kind": "appearsBy", "selector": "#headline", "bySec": .8}, {"kind": "staysInFrame", "selector": "#finance-card"}, {"kind": "appearsBy", "selector": "#summary-card", "bySec": max(.1, duration - 1)}]}, ensure_ascii=False, indent=2), encoding="utf-8")
        return HyperFramesProject(directory, composition, duration)
    build = build_project

    def render_mp4(self, project: HyperFramesProject | str | Path, output_path: str | Path | None = None) -> Path:
        directory = project.directory if isinstance(project, HyperFramesProject) else Path(project)
        destination = Path(output_path or self.video_config.get("output_path", self._output_dir / "stocktalk.mp4")); destination.parent.mkdir(parents=True, exist_ok=True)
        if shutil.which("npx") is None: raise HyperFramesBuildError("Node.js/npx is required to run HyperFrames CLI")
        try: completed = self._runner(("npx", "hyperframes", "render", str(directory), "--output", str(destination), "--quality", "high"), directory)
        except (OSError, subprocess.SubprocessError) as exc: raise HyperFramesBuildError(f"HyperFrames render could not start: {exc}") from exc
        if completed.returncode != 0: raise HyperFramesBuildError(f"HyperFrames render failed: {(completed.stderr or completed.stdout or 'unknown renderer failure').strip()}")
        if not destination.is_file() or destination.stat().st_size == 0: raise HyperFramesBuildError("HyperFrames render completed without creating a usable MP4")
        return destination
    render = render_mp4

    @property
    def _output_dir(self) -> Path: return Path(self.video_config.get("output_dir", "./output"))
    @property
    def _asset_dir(self) -> Path: return Path(__file__).resolve().parents[1] / "templates" / "hyperframes"
    @staticmethod
    def _run_subprocess(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]: return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=900, check=False)

    def _segments(self, script: Mapping[str, Any], timeline: Mapping[str, Any]) -> list[dict[str, Any]]:
        prompts = {(role, str(entry.get("line", "")).strip()): str(entry.get("visual_prompt", "")) for round_ in script.get("rounds", []) if isinstance(round_, Mapping) for role in ("bull", "bear") if isinstance((entry := round_.get(role)), Mapping)} if isinstance(script.get("rounds"), list) else {}
        result = []
        for item in timeline.get("segments", []) if isinstance(timeline.get("segments"), list) else []:
            if isinstance(item, Mapping) and str(item.get("line", "")).strip() and self._float(item.get("duration"), 0) > 0:
                result.append(self._make_segment(str(item["line"]).strip(), str(item.get("character", "bull")), self._float(item.get("start_time"), 0), self._float(item["duration"], 0), item.get("audio_path"), str(item.get("visual_prompt") or prompts.get((str(item.get("character", "bull")), str(item["line"]).strip()), ""))))
        if result: return sorted(result, key=lambda x: x["start"])
        cursor = 0.
        for round_ in script.get("rounds", []) if isinstance(script.get("rounds"), list) else []:
            if isinstance(round_, Mapping):
                for role in ("bull", "bear"):
                    entry = round_.get(role); line = str(entry.get("line", "") if isinstance(entry, Mapping) else entry or "").strip()
                    if line:
                        length = max(2.5, min(9., len(line) * .23)); result.append(self._make_segment(line, role, cursor, length, None, str(entry.get("visual_prompt", "") if isinstance(entry, Mapping) else ""))); cursor += length + .25
        return result

    def _make_segment(self, line: str, character: str, start: float, duration: float, audio: Any, prompt: str) -> dict[str, Any]:
        topic, topic_title = self._visual_topic(line + " " + prompt)
        return {"line": line, "character": "bear" if character.lower() == "bear" else "bull", "start": max(0., start), "duration": duration, "audio_path": audio, "visual_prompt": prompt or f"字幕突出：{line}", "topic": topic, "topic_title": topic_title}
    @staticmethod
    def _visual_topic(text: str) -> tuple[str, str]:
        normalized = text.lower().replace(" ", "")
        return next(((key, title) for key, (title, terms) in TOPICS.items() if any(term in normalized for term in terms)), ("financial", "数据焦点"))
    def _duration(self, segments: list[Mapping[str, Any]], timeline: Mapping[str, Any]) -> float:
        end = max((float(x["start"]) + float(x["duration"]) for x in segments), default=0.)
        return round(max(1., self._float(timeline.get("total_duration"), 0.), end, self._float(self.video_config.get("duration"), 0.)), 3)
    def _copy_assets(self, directory: Path) -> None:
        for name in ("stock-debate.css", "stock-debate.js"): shutil.copyfile(self._asset_dir / name, directory / name)

    def _html(self, stock: Mapping[str, Any], segments: list[Mapping[str, Any]], duration: float) -> str:
        quote = stock.get("quote", {}) if isinstance(stock.get("quote"), Mapping) else {}; financials = stock.get("financials", {}) if isinstance(stock.get("financials"), Mapping) else {}
        env = Environment(loader=FileSystemLoader(self._asset_dir), autoescape=select_autoescape(("html", "xml")))
        return env.get_template("index.html.j2").render(name=self._text(quote.get("name") or stock.get("name") or stock.get("code") or "股票"), code=self._text(stock.get("code") or quote.get("code") or "股票"), price=self._number_text(quote.get("price")), change=self._number_text(quote.get("change_pct"), "%"), duration=duration, theme=str(self.video_config.get("theme", "dark")), roles=self._roles(), financials=self._financials(financials), chart=self._candles(stock.get("kline", [])), segments=segments)
    def _roles(self) -> list[dict[str, str]]:
        roles=[]
        for key, default, stance, bio, avatar in (("bull", "看多方", "看多", "寻找增长与催化", "▲"), ("bear", "看空方", "看空", "审视估值与风险", "▼")):
            data = self.characters.get(key, {}) if isinstance(self.characters, Mapping) else {}; data = data if isinstance(data, Mapping) else {}
            roles.append({"key":key,"name":self._text(data.get("name") or default),"stance":self._text(data.get("stance") or stance),"bio":self._text(data.get("persona") or bio),"avatar":self._text(data.get("avatar") or avatar)})
        return roles
    def _financials(self, data: Mapping[str, Any]) -> list[dict[str,str]]:
        return [{"label": label, "value": self._text(next((data[k] for k in keys if data.get(k) is not None), "—"))} for label, keys in (("营收",("revenue","营业收入")),("净利润",("net_profit","净利润")),("毛利率",("gross_margin","毛利率")),("ROE",("roe","净资产收益率")))]
    def _candles(self, bars: Any) -> str:
        data=[x for x in bars if isinstance(x,Mapping)][-40:] if isinstance(bars,list) else []
        if not data: return '<svg viewBox="0 0 760 410"><text x="380" y="205" class="muted" text-anchor="middle">暂无 K 线数据</text></svg>'
        lows=[self._float(x.get("low"),self._float(x.get("close"),0)) for x in data]; highs=[self._float(x.get("high"),self._float(x.get("close"),0)) for x in data]; floor,top=min(lows),max(highs); spread=max(top-floor,max(abs(top)*.03,.01)); y=lambda v:378-((v-floor)/spread)*338; step=700/len(data); out=['<path class="axis draw-line" d="M30 40H730M30 210H730M30 378H730"/>']; closes=[]
        max_volume=max((self._float(x.get("volume"),0) for x in data),default=1) or 1
        for i,x in enumerate(data):
            op,cl=self._float(x.get("open"),0),self._float(x.get("close"),0); hi,lo=self._float(x.get("high"),max(op,cl)),self._float(x.get("low"),min(op,cl)); px=30+(i+.5)*step; color="up" if cl>=op else "down"; w=min(16,step*.56); closes.append(f"{px:.1f},{y(cl):.1f}"); volume_height=max(2,self._float(x.get("volume"),0)/max_volume*45); out.append(f'<g class="candle {color}"><line x1="{px:.1f}" y1="{y(hi):.1f}" x2="{px:.1f}" y2="{y(lo):.1f}"/><rect x="{px-w/2:.1f}" y="{min(y(op),y(cl)):.1f}" width="{w:.1f}" height="{max(3,abs(y(op)-y(cl))):.1f}"/><rect class="volume" x="{px-w/2:.1f}" y="{378-volume_height:.1f}" width="{w:.1f}" height="{volume_height:.1f}"/></g>')
        return '<svg viewBox="0 0 760 410" role="img" aria-label="K线、均线和成交量走势">'+''.join(out)+f'<polyline class="ma draw-line" points="{" ".join(closes)}"/></svg>'
    @staticmethod
    def _float(value: Any, default: float) -> float:
        try: value=float(value); return value if math.isfinite(value) else default
        except (TypeError,ValueError): return default
    @staticmethod
    def _text(value: Any) -> str: return html.escape(str(value),quote=True) if value not in (None,"") else "—"
    @classmethod
    def _number_text(cls, value: Any, suffix: str="") -> str:
        value=cls._float(value,float("nan")); return "—" if not math.isfinite(value) else f"{value:,.2f}{suffix}"
