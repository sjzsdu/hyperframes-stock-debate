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
        segments = self._segments(script or {}, tts_timeline or {})
        for seg in segments:
            ap = seg.get("audio_path")
            if ap:
                try: seg["audio_path"] = str(Path(ap).relative_to(directory))
                except ValueError: pass
        duration = self._duration(segments, tts_timeline or {})
        self._copy_assets(directory)
        composition = directory / "index.html"; composition.write_text(self._html(stock_data, segments, duration), encoding="utf-8")
        (directory / "index.motion.json").write_text(json.dumps({"duration": duration, "assertions": [{"kind": "appearsBy", "selector": "#headline", "bySec": .8}, {"kind": "staysInFrame", "selector": "#finance-card"}, {"kind": "appearsBy", "selector": "#summary-card", "bySec": max(.1, duration - 1)}]}, ensure_ascii=False, indent=2), encoding="utf-8")
        return HyperFramesProject(directory, composition, duration)
    build = build_project

    def render_mp4(self, project: HyperFramesProject | str | Path, output_path: str | Path | None = None) -> Path:
        """Run HyperFrames CLI and return a non-empty MP4 path."""
        directory = project.directory if isinstance(project, HyperFramesProject) else Path(project)
        directory = directory.resolve()
        destination = Path(output_path or self.video_config.get("output_path", self._output_dir / "stocktalk.mp4")).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if shutil.which("npx") is None:
            raise HyperFramesBuildError("Node.js/npx is required to run HyperFrames CLI")
        quality = str(self.video_config.get("quality", "high"))
        command = ("npx", "hyperframes", "render", str(directory), "--output", str(destination), "--quality", quality)
        try:
            completed = self._runner(command, directory)
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
    def _output_dir(self) -> Path: return Path(self.video_config.get("output_dir", "./output"))
    @property
    def _asset_dir(self) -> Path: return Path(__file__).resolve().parents[1] / "templates" / "hyperframes"
    @staticmethod
    def _run_subprocess(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]: return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=900, check=False)

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
                    prompt = str(item.get("visual_prompt", ""))
                    topic, topic_title = self._visual_topic(line + " " + prompt) if prompt else ("financial", "数据焦点")
                    segments.append({"line": line, "character": str(item.get("character", "bull")), "start": max(0.0, start), "duration": duration, "audio_path": item.get("audio_path"), "visual_prompt": prompt or f"字幕突出：{line}", "topic": topic, "topic_title": topic_title})
        if segments:
            return sorted(segments, key=lambda item: item["start"])

        cursor = 0.0
        turns = script.get("turns", [])
        if isinstance(turns, list):
            for turn in turns:
                if not isinstance(turn, Mapping):
                    continue
                character, line = str(turn.get("speaker", "")), str(turn.get("line", "")).strip()
                if character in {"bull", "bear"} and line:
                    prompt = str(turn.get("visual_prompt", ""))
                    topic, topic_title = self._visual_topic(line + " " + prompt)
                    duration = max(2.5, min(30.0, len(line) * 0.23))
                    segments.append({"line": line, "character": character, "start": cursor, "duration": duration, "audio_path": None, "visual_prompt": prompt or f"字幕突出：{line}", "topic": topic, "topic_title": topic_title})
                    cursor += duration + 0.25
        return segments

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
        f10 = stock.get("f10", {}) if isinstance(stock.get("f10"), Mapping) else {}
        visuals = self._visuals(quote, financials, stock.get("kline", []), f10)
        rendered_segments = [dict(segment, visual=visuals.get(str(segment.get("topic")), visuals["financial"])) for segment in segments]
        env = Environment(loader=FileSystemLoader(self._asset_dir), autoescape=select_autoescape(("html", "xml")))
        return env.get_template("index.html.j2").render(name=self._text(quote.get("name") or stock.get("name") or stock.get("code") or "股票"), code=self._text(stock.get("code") or quote.get("code") or "股票"), price=self._number_text(quote.get("price")), change=self._number_text(quote.get("change_pct"), "%"), duration=duration, theme=str(self.video_config.get("theme", "dark")), roles=self._roles(), financials=self._financials(financials), chart=self._candles(stock.get("kline", [])), segments=rendered_segments)
    def _roles(self) -> list[dict[str, str]]:
        roles=[]
        for key, default, stance, bio, avatar in (("bull", "看多方", "看多", "寻找增长与催化", "▲"), ("bear", "看空方", "看空", "审视估值与风险", "▼")):
            data = self.characters.get(key, {}) if isinstance(self.characters, Mapping) else {}; data = data if isinstance(data, Mapping) else {}
            roles.append({"key":key,"name":self._text(data.get("name") or default),"stance":self._text(data.get("stance") or stance),"bio":self._text(data.get("persona") or bio),"avatar":self._text(data.get("avatar") or avatar)})
        return roles
    def _financials(self, data: Mapping[str, Any]) -> list[dict[str,str]]:
        return [{"label": label, "value": self._text(next((data[k] for k in keys if data.get(k) is not None), "—"))} for label, keys in (("营收",("revenue","营业收入")),("净利润",("net_profit","净利润")),("毛利率",("gross_margin","毛利率")),("ROE",("roe","净资产收益率")))]
    def _candles(self, bars: Any, highlight_last: bool = False) -> str:
        data=[x for x in bars if isinstance(x,Mapping)][-40:] if isinstance(bars,list) else []
        if not data: return '<svg viewBox="0 0 760 410"><text x="380" y="205" class="muted" text-anchor="middle">暂无 K 线数据</text></svg>'
        lows=[self._float(x.get("low"),self._float(x.get("close"),0)) for x in data]; highs=[self._float(x.get("high"),self._float(x.get("close"),0)) for x in data]; floor,top=min(lows),max(highs); spread=max(top-floor,max(abs(top)*.03,.01)); y=lambda v:378-((v-floor)/spread)*338; step=700/len(data); out=['<path class="axis draw-line" d="M30 40H730M30 210H730M30 378H730"/>']; closes=[]
        max_volume=max((self._float(x.get("volume"),0) for x in data),default=1) or 1
        highlight_from = len(data) - max(4, len(data) // 4)
        if highlight_last:
            out.append(f'<rect class="discussion-range" x="{30 + highlight_from * step:.1f}" y="40" width="{(len(data) - highlight_from) * step:.1f}" height="338"/>')
        for i,x in enumerate(data):
            op,cl=self._float(x.get("open"),0),self._float(x.get("close"),0); hi,lo=self._float(x.get("high"),max(op,cl)),self._float(x.get("low"),min(op,cl)); px=30+(i+.5)*step; color="up" if cl>=op else "down"; w=min(16,step*.56); closes.append(f"{px:.1f},{y(cl):.1f}"); volume_height=max(2,self._float(x.get("volume"),0)/max_volume*45); out.append(f'<g class="candle {color}"><line x1="{px:.1f}" y1="{y(hi):.1f}" x2="{px:.1f}" y2="{y(lo):.1f}"/><rect x="{px-w/2:.1f}" y="{min(y(op),y(cl)):.1f}" width="{w:.1f}" height="{max(3,abs(y(op)-y(cl))):.1f}"/><rect class="volume" x="{px-w/2:.1f}" y="{378-volume_height:.1f}" width="{w:.1f}" height="{volume_height:.1f}"/></g>')
        return '<svg viewBox="0 0 760 410" role="img" aria-label="K线、均线和成交量走势">'+''.join(out)+f'<polyline class="ma draw-line" points="{" ".join(closes)}"/></svg>'

    def _visuals(self, quote: Mapping[str, Any], financials: Mapping[str, Any], bars: Any, f10: Mapping[str, Any]) -> dict[str, str]:
        """Return SVG-only topic cards. Missing fields stay visibly unavailable, never fabricated."""
        return {
            "valuation": self._valuation_svg(quote, financials, f10),
            "financial": self._financial_svg(financials),
            "technical": self._candles(bars, highlight_last=True),
            "risk": self._risk_svg(bars),
            "comparison": self._comparison_svg(quote, financials, f10),
            "sentiment": self._sentiment_svg(quote, bars),
        }

    @staticmethod
    def _lookup(*sources: Mapping[str, Any], keys: Sequence[str]) -> float | None:
        for source in sources:
            for key in keys:
                if key in source:
                    value = HyperFramesBuilder._float(source.get(key), float("nan"))
                    if math.isfinite(value):
                        return value
        return None

    @staticmethod
    def _empty_svg(label: str) -> str:
        return f'<svg viewBox="0 0 330 116" role="img" aria-label="{html.escape(label)}"><text class="svg-muted" x="165" y="61" text-anchor="middle">{html.escape(label)}</text></svg>'

    def _valuation_svg(self, quote: Mapping[str, Any], financials: Mapping[str, Any], f10: Mapping[str, Any]) -> str:
        pe = self._lookup(quote, financials, f10, keys=("pe", "PE", "pe_ttm", "市盈率"))
        pb = self._lookup(quote, financials, f10, keys=("pb", "PB", "市净率"))
        if pe is None and pb is None:
            return self._empty_svg("暂无 PE/PB 估值数据")
        value, label = (pe, "PE") if pe is not None else (pb, "PB")
        assert value is not None
        industry = self._lookup(quote, financials, f10, keys=("industry_pe", "行业PE", "industry_pb", "行业PB"))
        # The gauge range is data-derived: compare the current measure against a provided industry mean.
        maximum = max(value, industry or 0, 1) * 1.25
        angle = -135 + min(1, max(0, value / maximum)) * 270
        marker = f'<line class="gauge-industry" x1="165" y1="67" x2="{165 + 48 * math.cos(math.radians(-135 + min(1, (industry or 0) / maximum) * 270)):.1f}" y2="{67 + 48 * math.sin(math.radians(-135 + min(1, (industry or 0) / maximum) * 270)):.1f}"/>' if industry is not None else ""
        return f'<svg viewBox="0 0 330 116" role="img" aria-label="{label} 估值仪表盘"><path class="gauge-track" d="M112 102 A62 62 0 1 1 218 102"/><path class="gauge-fill" d="M112 102 A62 62 0 0 1 {165 + 62 * math.cos(math.radians(angle)):.1f} {102 + 62 * math.sin(math.radians(angle)):.1f}"/>{marker}<text class="svg-value" x="165" y="71" text-anchor="middle">{label} {value:.2f}</text><text class="svg-muted" x="165" y="94" text-anchor="middle">行业均值 {industry:.2f}</text></svg>' if industry is not None else f'<svg viewBox="0 0 330 116" role="img" aria-label="{label} 估值仪表盘"><path class="gauge-track" d="M112 102 A62 62 0 1 1 218 102"/><path class="gauge-fill" d="M112 102 A62 62 0 0 1 {165 + 62 * math.cos(math.radians(angle)):.1f} {102 + 62 * math.sin(math.radians(angle)):.1f}"/><text class="svg-value" x="165" y="71" text-anchor="middle">{label} {value:.2f}</text><text class="svg-muted" x="165" y="94" text-anchor="middle">未提供行业均值</text></svg>'

    def _financial_svg(self, financials: Mapping[str, Any]) -> str:
        series = financials.get("history") or financials.get("quarters") or financials.get("periods")
        points = [x for x in series if isinstance(x, Mapping)][-4:] if isinstance(series, list) else []
        values = [self._lookup(x, keys=("revenue", "营业收入", "net_profit", "净利润")) for x in points]
        values = [x for x in values if x is not None]
        if not values:
            current = self._lookup(financials, keys=("revenue", "营业收入", "net_profit", "净利润"))
            if current is None:
                return self._empty_svg("暂无最近四期营收/利润数据")
            values = [current]
        maximum = max(abs(x) for x in values) or 1
        bars = ''.join(f'<rect class="bar" x="{42 + i * 68}" y="{94 - abs(value) / maximum * 64:.1f}" width="38" height="{abs(value) / maximum * 64:.1f}"/><text class="svg-muted" x="{61 + i * 68}" y="110" text-anchor="middle">{i + 1}期</text>' for i, value in enumerate(values))
        return f'<svg viewBox="0 0 330 116" role="img" aria-label="最近财务期柱状图"><line class="chart-baseline" x1="25" y1="95" x2="310" y2="95"/>{bars}<text class="svg-value" x="305" y="24" text-anchor="end">最近 {len(values)} 期</text></svg>'

    def _risk_svg(self, bars: Any) -> str:
        closes = [self._float(x.get("close"), float("nan")) for x in bars if isinstance(x, Mapping)] if isinstance(bars, list) else []
        closes = [x for x in closes if math.isfinite(x)]
        if len(closes) < 2:
            return self._empty_svg("暂无回撤走势数据")
        peak, drawdowns = closes[0], []
        for close in closes[-30:]:
            peak = max(peak, close); drawdowns.append((close / peak - 1) * 100)
        lowest = min(drawdowns); scale = max(abs(lowest), .1)
        points = ' '.join(f'{18 + i * 294 / max(1, len(drawdowns)-1):.1f},{25 + abs(value) / scale * 68:.1f}' for i, value in enumerate(drawdowns))
        return f'<svg viewBox="0 0 330 116" role="img" aria-label="历史回撤曲线"><path class="risk-area" d="M18 25 L{points} L312 93 L18 93 Z"/><polyline class="risk-line" points="{points}"/><text class="svg-risk" x="312" y="18" text-anchor="end">最大回撤 {lowest:.2f}%</text></svg>'

    def _comparison_svg(self, quote: Mapping[str, Any], financials: Mapping[str, Any], f10: Mapping[str, Any]) -> str:
        raw = [self._lookup(quote, financials, f10, keys=keys) for keys in (("pe", "PE", "pe_ttm"), ("revenue_growth", "营收增长率"), ("roe", "ROE", "净资产收益率"), ("gross_margin", "毛利率"))]
        values = [x for x in raw if x is not None]
        if len(values) < 2:
            return self._empty_svg("暂无行业多维对比数据")
        maximum = max(abs(x) for x in values) or 1
        angles = [-90, 0, 90, 180]
        radii = [(abs(raw[i] or 0) / maximum * 42) for i in range(4)]
        points = ' '.join(f'{165 + radii[i] * math.cos(math.radians(angles[i])):.1f},{58 + radii[i] * math.sin(math.radians(angles[i])):.1f}' for i in range(4))
        axes = ''.join(f'<line class="radar-axis" x1="165" y1="58" x2="{165 + 48 * math.cos(math.radians(a)):.1f}" y2="{58 + 48 * math.sin(math.radians(a)):.1f}"/>' for a in angles)
        labels = ''.join(f'<text class="svg-muted" x="{165 + 64 * math.cos(math.radians(a)):.1f}" y="{62 + 64 * math.sin(math.radians(a)):.1f}" text-anchor="middle">{label}</text>' for a, label in zip(angles, ("估值", "成长", "盈利", "安全")))
        return f'<svg viewBox="0 0 330 116" role="img" aria-label="行业多维雷达图"><polygon class="radar-grid" points="165,10 213,58 165,106 117,58"/>{axes}<polygon class="radar-shape" points="{points}"/>{labels}</svg>'

    def _sentiment_svg(self, quote: Mapping[str, Any], bars: Any) -> str:
        change = self._lookup(quote, keys=("change_pct", "涨跌幅", "change"))
        volumes = [self._float(x.get("volume"), float("nan")) for x in bars if isinstance(x, Mapping)] if isinstance(bars, list) else []
        volumes = [x for x in volumes if math.isfinite(x)]
        if change is None and not volumes:
            return self._empty_svg("暂无市场情绪/资金数据")
        magnitude = min(100, abs(change or 0) * 10)
        color = "sentiment-positive" if (change or 0) >= 0 else "sentiment-negative"
        volume_text = f'成交量 {volumes[-1]:,.0f}' if volumes else "暂无成交量"
        return f'<svg viewBox="0 0 330 116" role="img" aria-label="市场情绪指标"><rect class="sentiment-track" x="20" y="38" width="290" height="18" rx="9"/><rect class="{color}" x="20" y="38" width="{magnitude / 100 * 290:.1f}" height="18" rx="9"/><text class="svg-value" x="20" y="29">涨跌幅 {change:.2f}%</text><text class="svg-muted" x="20" y="82">{volume_text}</text></svg>' if change is not None else self._empty_svg("暂无涨跌幅情绪数据")
    @staticmethod
    def _float(value: Any, default: float) -> float:
        try: value=float(value); return value if math.isfinite(value) else default
        except (TypeError,ValueError): return default
    @staticmethod
    def _text(value: Any) -> str: return html.escape(str(value),quote=True) if value not in (None,"") else "—"
    @classmethod
    def _number_text(cls, value: Any, suffix: str="") -> str:
        value=cls._float(value,float("nan")); return "—" if not math.isfinite(value) else f"{value:,.2f}{suffix}"
