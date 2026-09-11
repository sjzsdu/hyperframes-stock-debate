"""Build an editable, data-driven HyperFrames stock discussion composition."""
from __future__ import annotations

import html
import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape


class HyperFramesBuildError(RuntimeError):
    """Raised when a project cannot be built or rendered."""


Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]

# Discussion topics, detected from the dialogue text itself. The first matching
# entry wins, so specific topics precede generic ones.
TOPICS: dict[str, tuple[str, tuple[str, ...]]] = {
    "money": ("量价资金", ("量能", "资金", "换手", "成交", "主力", "筹码", "入场", "流出", "流入")),
    "risk": ("风险与回撤", ("风险", "不确定", "警惕", "下行", "回撤", "波动", "分歧", "抛压", "概率", "仓位", "边界", "不良")),
    "valuation": ("估值水平", ("估值", "pe", "pb", "市盈", "市净", "贵不贵", "溢价", "泡沫")),
    "sentiment": ("市场情绪", ("情绪", "心理", "信心", "恐慌", "预期", "上头", "失望", "乐观", "悲观", "故事", "叙事", "共识")),
    "financial": ("财务与业绩", ("营收", "利润", "收入", "毛利", "净利", "roe", "财报", "业绩", "现金流", "费用", "负债", "盈利", "成本")),
    "industry": ("行业与业务", ("行业", "赛道", "格局", "竞争", "对手", "同业", "竞品", "份额", "市占", "天花板", "空间", "周期", "产业链", "渠道", "经销商", "政策", "消费", "需求", "供给", "产能", "壁垒", "护城河", "品牌", "公司", "业务", "商业", "管理层", "产品", "提价", "库存")),
    "technical": ("技术走势", ("k线", "均线", "技术", "macd", "指标", "走势", "趋势", "多头排列", "空头排列", "死叉", "金叉", "信号", "强度", "rsi", "kdj", "布林", "压力位", "支撑位", "突破", "回调", "振幅")),
    "outlook": ("后市关注", ("接下来", "未来", "后续", "观察", "验证", "催化剂", "展望", "预期差", "跟踪")),
}


@dataclass(frozen=True)
class HyperFramesProject:
    directory: Path
    composition_path: Path
    duration: float


class HyperFramesBuilder:
    """Create a 1920×1080 discussion composition from separate Jinja/CSS/JS assets."""

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
                seg["audio_path"] = self._project_audio_path(Path(ap), directory)
        duration = self._duration(segments, tts_timeline or {})
        self._copy_assets(directory)
        composition = directory / "index.html"; composition.write_text(self._html(stock_data, segments, duration), encoding="utf-8")
        (directory / "index.motion.json").write_text(json.dumps({"duration": duration, "assertions": [
            {"kind": "appearsBy", "selector": "#headline", "bySec": .8},
            {"kind": "staysInFrame", "selector": "#market-stage"},
            {"kind": "staysInFrame", "selector": "#bull-host"},
            {"kind": "staysInFrame", "selector": "#bear-host"},
            {"kind": "appearsBy", "selector": "#speech-1", "bySec": 1.2},
        ]}, ensure_ascii=False, indent=2), encoding="utf-8")
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

    @staticmethod
    def _project_audio_path(audio: Path, directory: Path) -> str:
        """Return a project-relative audio path, copying the file in when needed."""
        resolved = audio.resolve()
        try:
            return str(resolved.relative_to(directory.resolve()))
        except ValueError:
            target = directory / "audio" / audio.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if resolved != target.resolve():
                shutil.copyfile(resolved, target)
            return str(target.relative_to(directory))

    @property
    def _output_dir(self) -> Path: return Path(self.video_config.get("output_dir", "./output"))
    @property
    def _asset_dir(self) -> Path: return Path(__file__).resolve().parents[1] / "templates" / "hyperframes"
    @staticmethod
    def _run_subprocess(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]: return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=900, check=False)

    def _segments(self, script: Mapping[str, Any], timeline: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Build subtitle segments; classify each discussion topic from the line text."""
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
                    topic, topic_title = self._visual_topic(line)
                    segments.append({"line": line, "character": str(item.get("character", "bull")), "start": max(0.0, start), "duration": duration, "audio_path": item.get("audio_path"), "topic": topic, "topic_title": topic_title})
        if segments:
            segments = sorted(segments, key=lambda item: item["start"])
            return self._decorate_segments(segments)

        cursor = 0.0
        turns = script.get("turns", [])
        if isinstance(turns, list):
            for turn in turns:
                if not isinstance(turn, Mapping):
                    continue
                character, line = str(turn.get("speaker", "")), str(turn.get("line", "")).strip()
                if character in {"bull", "bear"} and line:
                    topic, topic_title = self._visual_topic(line)
                    duration = max(2.5, min(30.0, len(line) * 0.23))
                    segments.append({"line": line, "character": character, "start": cursor, "duration": duration, "audio_path": None, "topic": topic, "topic_title": topic_title})
                    cursor += duration + 0.25
        return self._decorate_segments(segments)

    def _decorate_segments(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add presentation windows and editable mix metadata to dialogue turns."""
        fx_chain = json.dumps({"version": 1, "nodes": [
            {"type": "highpass", "id": "n1", "label": "Remove Rumble", "params": {"frequency": 78, "q": .707, "poles": "2"}},
            {"type": "peaking", "id": "n2", "label": "Add Clarity", "params": {"frequency": 3000, "gain": 1.6, "q": 1}},
            {"type": "compressor", "id": "n3", "label": "Even Out Loudness", "params": {"threshold": -22, "ratio": 3, "attack": 18, "release": 220, "knee": 2.8, "makeup": 1.5, "mix": 1}},
            {"type": "limiter", "id": "n4", "label": "Peak Ceiling", "params": {"limit": -1, "attack": 5, "release": 70, "level_out": 0}},
        ]}, ensure_ascii=False, separators=(",", ":"))
        for index, segment in enumerate(segments):
            start = float(segment["start"])
            duration = float(segment["duration"])
            # Visual cards overlap the conversational breath so hand-offs dissolve
            # rather than cutting to an empty stage. Audio itself never overlaps.
            visual_start = max(0.0, start - (.08 if index else 0.0))
            visual_end = start + duration + (.1 if index < len(segments) - 1 else 0.0)
            fade = min(.035, duration / 4)
            automation = {"version": 1, "lanes": [{"target": "volume", "points": [
                {"t": 0, "v": 0}, {"t": fade, "v": 1},
                {"t": max(fade, duration - fade), "v": 1}, {"t": duration, "v": 0},
            ]}]}
            segment.update({
                "character_name": str((self.characters.get(segment.get("character"), {}) or {}).get("name") or segment.get("character")),
                "visual_start": round(visual_start, 3),
                "visual_duration": round(max(.1, visual_end - visual_start), 3),
                "fx_chain": fx_chain,
                "audio_automation": json.dumps(automation, separators=(",", ":")),
            })
        return segments

    @staticmethod
    def _visual_topic(text: str) -> tuple[str, str]:
        normalized = text.lower().replace(" ", "")
        for key, (title, terms) in TOPICS.items():
            if any(term in normalized for term in terms):
                return key, title
        return "industry", "公司与行业"

    def _duration(self, segments: list[Mapping[str, Any]], timeline: Mapping[str, Any]) -> float:
        end = max((float(x["start"]) + float(x["duration"]) for x in segments), default=0.)
        return round(max(1., self._float(timeline.get("total_duration"), 0.), end, self._float(self.video_config.get("duration"), 0.)), 3)

    def _copy_assets(self, directory: Path) -> None:
        for name in ("stock-debate.css", "stock-debate.js", "gsap.min.js"):
            shutil.copyfile(self._asset_dir / name, directory / name)

    def _html(self, stock: Mapping[str, Any], segments: list[Mapping[str, Any]], duration: float) -> str:
        quote = stock.get("quote", {}) if isinstance(stock.get("quote"), Mapping) else {}
        financials = stock.get("financials", {}) if isinstance(stock.get("financials"), Mapping) else {}
        f10 = stock.get("f10", {}) if isinstance(stock.get("f10"), Mapping) else {}
        technical = stock.get("technical", {}) if isinstance(stock.get("technical"), Mapping) else {}
        history = stock.get("kline") or stock.get("price_history") or []
        if not isinstance(history, list):
            history = []
        visuals = self._visuals(quote, financials, history, f10, technical)
        rendered_segments = [dict(segment, visual=visuals.get(str(segment.get("topic")), visuals["industry"])) for segment in segments]
        env = Environment(loader=FileSystemLoader(self._asset_dir), autoescape=select_autoescape(("html", "xml")))
        return env.get_template("index.html.j2").render(
            name=self._text(quote.get("name") or stock.get("name") or stock.get("code") or "股票"),
            code=self._text(stock.get("code") or quote.get("code") or ""),
            price_line=self._quote_line(quote),
            duration=duration,
            theme=str(self.video_config.get("theme", "dark")),
            financials=self._financials(quote, financials, technical),
            chart=self._candles(history),
            segments=rendered_segments,
            bull_name=str((self.characters.get("bull", {}) or {}).get("name") or "股市新手"),
            bear_name=str((self.characters.get("bear", {}) or {}).get("name") or "股市老登"),
        )

    @staticmethod
    def _quote_line(quote: Mapping[str, Any]) -> str:
        price = HyperFramesBuilder._float(quote.get("price"), float("nan"))
        change = HyperFramesBuilder._float(quote.get("change_pct"), float("nan"))
        parts: list[str] = []
        if math.isfinite(price):
            parts.append(f"最新价 {price:,.2f}")
        if math.isfinite(change):
            parts.append(f"涨跌幅 {change:+.2f}%")
        return "　".join(parts)

    def _financials(self, quote: Mapping[str, Any], financials: Mapping[str, Any], technical: Mapping[str, Any]) -> list[dict[str, str]]:
        # Six rows maximum keeps the card clear of the topic card below it.
        labels: list[tuple[str, tuple[str, ...]]] = [
            ("今日开盘", ("open",)), ("日内最高", ("high",)), ("日内最低", ("low",)),
            ("成交额", ("amount", "turnover")), ("成交量", ("volume", "vol")),
            ("毛利率", ("gross_margin", "毛利率")), ("ROE", ("roe", "净资产收益率")),
        ]
        rows: list[dict[str, str]] = []
        price = self._float(quote.get("price"), float("nan"))
        change = self._float(quote.get("change_pct"), float("nan"))
        if math.isfinite(price):
            rows.append({"label": "最新价", "value": f"{price:,.2f}"})
        if math.isfinite(change):
            rows.append({"label": "涨跌幅", "value": f"{change:+.2f}%"})
        for label, keys in labels:
            raw = next((quote[key] if key in quote else financials[key] for key in keys if quote.get(key) is not None or financials.get(key) is not None), None)
            value = self._format_metric(label, raw)
            if value != "—":
                rows.append({"label": label, "value": value})
        summary = technical.get("summary") if isinstance(technical.get("summary"), Mapping) else {}
        signal = self._text(summary.get("signal") or summary.get("trend") or None)
        if signal != "—":
            rows.append({"label": "趋势信号", "value": signal})
        return rows[:6]

    def _format_metric(self, label: str, value: Any) -> str:
        if value in (None, ""):
            return "—"
        number = self._float(value, float("nan"))
        if not math.isfinite(number):
            return self._text(value)
        if label in ("今日开盘", "日内最高", "日内最低"):
            return f"{number:,.2f}"
        if label == "成交量":
            return f"{number / 1e4:,.2f}万手" if number >= 1e4 else f"{number:,.0f}手"
        if label == "成交额":
            return f"{number / 1e4:,.2f}亿" if number >= 1e4 else f"{number:,.2f}万"
        return f"{number:g}"

    def _candles(self, bars: Any, highlight_last: bool = False) -> str:
        data = [x for x in bars if isinstance(x, Mapping)][-60:] if isinstance(bars, list) else []
        if len(data) < 2:
            return self._empty_svg(760, 410, "暂无价格走势数据")
        lows = [self._float(x.get("low"), self._float(x.get("close"), 0)) for x in data]
        highs = [self._float(x.get("high"), self._float(x.get("close"), 0)) for x in data]
        floor, top = min(lows), max(highs)
        spread = max(top - floor, max(abs(top) * .03, .01))
        y = lambda v: 340 - ((v - floor) / spread) * 300
        step = 700 / len(data)
        out = ['<path class="axis draw-line" d="M30 20H730M30 180H730M30 340H730"/>']
        closes: list[str] = []
        dates = [str(x.get("date") or x.get("timestamp") or "") for x in data]
        max_volume = max((self._float(x.get("volume"), 0) for x in data), default=0) or 0
        has_volume = max_volume > 0
        highlight_from = len(data) - max(4, len(data) // 4)
        if highlight_last:
            out.append(f'<rect class="discussion-range" x="{30 + highlight_from * step:.1f}" y="20" width="{(len(data) - highlight_from) * step:.1f}" height="320"/>')
        for i, x in enumerate(data):
            op, cl = self._float(x.get("open"), 0), self._float(x.get("close"), 0)
            hi, lo = self._float(x.get("high"), max(op, cl)), self._float(x.get("low"), min(op, cl))
            px = 30 + (i + .5) * step
            color = "up" if cl >= op else "down"
            w = min(16, step * .56)
            closes.append(f"{px:.1f},{y(cl):.1f}")
            body = f'<rect x="{px - w / 2:.1f}" y="{min(y(op), y(cl)):.1f}" width="{w:.1f}" height="{max(3, abs(y(op) - y(cl))):.1f}"/>'
            volume = (f'<rect class="volume" x="{px - w / 2:.1f}" y="{340 - max(2, self._float(x.get("volume"), 0) / max_volume * 45):.1f}" width="{w:.1f}" height="{max(2, self._float(x.get("volume"), 0) / max_volume * 45):.1f}"/>' if has_volume else "")
            out.append(f'<g class="candle {color}"><line x1="{px:.1f}" y1="{y(hi):.1f}" x2="{px:.1f}" y2="{y(lo):.1f}"/>{body}{volume}</g>')
        for index in {0, len(dates) // 2, len(dates) - 1}:
            if dates[index]:
                label = dates[index][5:] if len(dates[index]) >= 10 else dates[index]
                out.append(f'<text class="svg-axis" x="{30 + (index + .5) * step:.1f}" y="366" text-anchor="middle">{html.escape(label)}</text>')
        return '<svg viewBox="0 0 760 410" role="img" aria-label="价格走势、均线和成交量">' + ''.join(out) + f'<polyline class="ma draw-line" points="{" ".join(closes)}"/></svg>'

    def _visuals(self, quote: Mapping[str, Any], financials: Mapping[str, Any], bars: list[Any], f10: Mapping[str, Any], technical: Mapping[str, Any]) -> dict[str, str]:
        """SVG topic visuals for every discussion topic, derived from fetched data only."""
        return {
            "financial": self._financial_svg(financials, f10),
            "valuation": self._valuation_svg(quote, financials, f10),
            "industry": self._industry_svg(f10),
            "money": self._money_svg(quote, bars),
            "technical": self._candles(bars, highlight_last=True),
            "risk": self._risk_svg(bars),
            "sentiment": self._sentiment_svg(quote, bars),
            "outlook": self._outlook_svg(technical),
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
    def _empty_svg(width: int, height: int, label: str) -> str:
        return (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label)}">'
                f'<text class="svg-muted" x="{width // 2}" y="{height // 2}" text-anchor="middle">{html.escape(label)}</text></svg>')

    def _financial_svg(self, financials: Mapping[str, Any], f10: Mapping[str, Any]) -> str:
        series = financials.get("history") or financials.get("quarters") or financials.get("periods")
        points = [x for x in series if isinstance(x, Mapping)][-4:] if isinstance(series, list) else []
        values = [self._lookup(x, keys=("revenue", "营业收入", "net_profit", "净利润")) for x in points]
        values = [x for x in values if x is not None]
        if len(values) >= 2:
            maximum = max(abs(x) for x in values) or 1
            growth = (values[-1] / values[0] - 1) * 100 if values[0] else 0
            bars = ''.join(
                f'<rect class="bar" x="{42 + i * 68}" y="{94 - abs(value) / maximum * 64:.1f}" width="38" height="{abs(value) / maximum * 64:.1f}"/>'
                f'<text class="svg-muted" x="{61 + i * 68}" y="110" text-anchor="middle">{i + 1}期</text>'
                for i, value in enumerate(values))
            return (f'<svg viewBox="0 0 330 116" role="img" aria-label="最近财务期柱状图"><line class="chart-baseline" x1="25" y1="95" x2="310" y2="95"/>{bars}'
                    f'<text class="svg-value" x="305" y="24" text-anchor="end">期均变化 {growth:+.1f}%</text></svg>')
        current = self._lookup(financials, keys=("revenue", "营业收入", "net_profit", "净利润"))
        if current is not None:
            return (f'<svg viewBox="0 0 330 116" role="img" aria-label="最新财务指标"><text class="svg-value" x="20" y="46">最新报告值 {current:,.2f}</text>'
                    f'<text class="svg-muted" x="20" y="74">以最新财报披露口径为准</text></svg>')
        text = self._f10_text(f10, ("财务分析",))
        if text:
            return self._f10_snippet_svg(text[:52], "财务分析要点")
        return self._empty_svg(330, 116, "暂无财务数据")

    def _valuation_svg(self, quote: Mapping[str, Any], financials: Mapping[str, Any], f10: Mapping[str, Any]) -> str:
        pe = self._lookup(quote, financials, f10, keys=("pe", "PE", "pe_ttm", "市盈率"))
        pb = self._lookup(quote, financials, f10, keys=("pb", "PB", "市净率"))
        if pe is None and pb is None:
            return self._empty_svg(330, 116, "暂无 PE/PB 数据")
        value, label = (pe, "PE") if pe is not None else (pb, "PB")
        assert value is not None
        industry = self._lookup(quote, financials, f10, keys=("industry_pe", "行业PE", "industry_pb", "行业PB"))
        maximum = max(value, industry or 0, 1) * 1.25
        angle = -135 + min(1, max(0, value / maximum)) * 270
        marker = (f'<line class="gauge-industry" x1="165" y1="67" x2="{165 + 48 * math.cos(math.radians(-135 + min(1, (industry or 0) / maximum) * 270)):.1f}" '
                  f'y2="{67 + 48 * math.sin(math.radians(-135 + min(1, (industry or 0) / maximum) * 270)):.1f}"/>' if industry is not None else "")
        note = f'行业均值 {industry:.2f}' if industry is not None else "对比行业均值"
        return (f'<svg viewBox="0 0 330 116" role="img" aria-label="{label} 估值仪表盘"><path class="gauge-track" d="M112 102 A62 62 0 1 1 218 102"/>'
                f'<path class="gauge-fill" d="M112 102 A62 62 0 0 1 {165 + 62 * math.cos(math.radians(angle)):.1f} {102 + 62 * math.sin(math.radians(angle)):.1f}"/>{marker}'
                f'<text class="svg-value" x="165" y="71" text-anchor="middle">{label} {value:.2f}</text>'
                f'<text class="svg-muted" x="165" y="94" text-anchor="middle">{note}</text></svg>')

    def _industry_svg(self, f10: Mapping[str, Any]) -> str:
        """Company business profile card from F10 directory/sections when available."""
        directory = f10.get("directory") if isinstance(f10.get("directory"), Mapping) else {}
        fields: list[str] = []
        for label, keys in (("行业", ("行业", "所属行业", "industry")), ("主营", ("主营", "主营业务", "main_business", "产品")), ("公司", ("公司名称", "name", "公司"))):
            for key in keys:
                value = directory.get(key)
                if value not in (None, ""):
                    fields.append(f"{label}：{self._text(value)}")
                    break
        text = self._f10_text(f10, ("公司概况", "经营分析"))
        if text:
            fields.append(text[:52])
        if fields:
            lines = ''.join(f'<text class="svg-muted" x="20" y="{40 + i * 24}">{html.escape(line[:26])}</text>' for i, line in enumerate(fields[:3]))
            return f'<svg viewBox="0 0 330 116" role="img" aria-label="公司业务资料">{lines}</svg>'
        return self._empty_svg(330, 116, "暂无公司业务资料")

    def _money_svg(self, quote: Mapping[str, Any], bars: list[Any]) -> str:
        volumes = [self._float(x.get("volume"), float("nan")) for x in bars if isinstance(x, Mapping)] if isinstance(bars, list) else []
        volumes = [x for x in volumes if math.isfinite(x)][-20:]
        amount = self._lookup(quote, keys=("amount", "turnover"))
        if not volumes and amount is None:
            return self._empty_svg(330, 116, "暂无量价资金数据")
        out: list[str] = ['<line class="chart-baseline" x1="20" y1="95" x2="310" y2="95"/>']
        if volumes:
            peak = max(volumes) or 1
            step = 290 / len(volumes)
            closes = [self._float(x.get("close"), 0) for x in bars if isinstance(x, Mapping)][-len(volumes):]
            opens = [self._float(x.get("open"), 0) for x in bars if isinstance(x, Mapping)][-len(volumes):]
            for i, volume in enumerate(volumes):
                up = closes[i] >= opens[i] if i < len(closes) and i < len(opens) else True
                height = max(2, volume / peak * 62)
                out.append(f'<rect class="bar {"" if up else "bar-down"}" x="{20 + i * step:.1f}" y="{95 - height:.1f}" width="{step * .6:.1f}" height="{height:.1f}"/>')
        if amount is not None:
            text = f"{amount / 1e4:,.2f}亿" if amount >= 1e4 else f"{amount:,.2f}万"
            out.append(f'<text class="svg-value" x="305" y="24" text-anchor="end">成交额 {text}</text>')
        return f'<svg viewBox="0 0 330 116" role="img" aria-label="成交量与成交额">{"".join(out)}</svg>'

    def _risk_svg(self, bars: list[Any]) -> str:
        closes = [self._float(x.get("close"), float("nan")) for x in bars if isinstance(x, Mapping)] if isinstance(bars, list) else []
        closes = [x for x in closes if math.isfinite(x)]
        if len(closes) < 2:
            return self._empty_svg(330, 116, "暂无回撤走势数据")
        peak, drawdowns = closes[0], []
        for close in closes[-30:]:
            peak = max(peak, close); drawdowns.append((close / peak - 1) * 100)
        lowest = min(drawdowns); scale = max(abs(lowest), .1)
        points = ' '.join(f'{18 + i * 294 / max(1, len(drawdowns) - 1):.1f},{25 + abs(value) / scale * 68:.1f}' for i, value in enumerate(drawdowns))
        return (f'<svg viewBox="0 0 330 116" role="img" aria-label="历史回撤曲线"><path class="risk-area" d="M18 25 L{points} L312 93 L18 93 Z"/>'
                f'<polyline class="risk-line" points="{points}"/>'
                f'<text class="svg-risk" x="312" y="18" text-anchor="end">最大回撤 {lowest:.2f}%</text></svg>')

    def _sentiment_svg(self, quote: Mapping[str, Any], bars: list[Any]) -> str:
        change = self._lookup(quote, keys=("change_pct", "涨跌幅", "change"))
        volumes = [self._float(x.get("volume"), float("nan")) for x in bars if isinstance(x, Mapping)] if isinstance(bars, list) else []
        volumes = [x for x in volumes if math.isfinite(x)]
        if change is None and not volumes:
            return self._empty_svg(330, 116, "暂无市场情绪数据")
        magnitude = min(100, abs(change or 0) * 10)
        color = "sentiment-positive" if (change or 0) >= 0 else "sentiment-negative"
        volume_text = f'成交量 {volumes[-1]:,.0f}手' if volumes else ""
        bar = (f'<rect class="sentiment-track" x="20" y="38" width="290" height="18" rx="9"/>'
               f'<rect class="{color}" x="20" y="38" width="{magnitude / 100 * 290:.1f}" height="18" rx="9"/>') if change is not None else ""
        label = f'<text class="svg-value" x="20" y="29">涨跌幅 {change:.2f}%</text>' if change is not None else ""
        note = f'<text class="svg-muted" x="20" y="82">{volume_text}</text>' if volume_text else ""
        return f'<svg viewBox="0 0 330 116" role="img" aria-label="市场情绪指标">{bar}{label}{note}</svg>'

    def _outlook_svg(self, technical: Mapping[str, Any]) -> str:
        summary = technical.get("summary") if isinstance(technical.get("summary"), Mapping) else {}
        history = technical.get("history") if isinstance(technical.get("history"), list) else []
        latest = next((x for x in reversed(history) if isinstance(x, Mapping) and isinstance(x.get("price"), Mapping)), {})
        change = self._float((latest.get("price") or {}).get("change_pct") if latest else float("nan"), float("nan"))
        signal = self._text(summary.get("signal") or None)
        trend = self._text(summary.get("trend") or None)
        strength = summary.get("strength")
        if signal == "—" and trend == "—" and not math.isfinite(change):
            return self._empty_svg(330, 116, "暂无关注要点数据")
        parts = [f"信号 {signal}"] if signal != "—" else []
        if trend != "—":
            parts.append(f"趋势 {trend}")
        if strength is not None:
            parts.append(f"强度 {self._text(strength)}")
        change_text = f"近期涨跌 {change:+.2f}%" if math.isfinite(change) else ""
        return (f'<svg viewBox="0 0 330 116" role="img" aria-label="关注要点"><text class="svg-value" x="20" y="46">{" · ".join(parts)}</text>'
                f'<text class="svg-muted" x="20" y="74">{html.escape(change_text)}</text></svg>')

    @staticmethod
    def _f10_text(f10: Mapping[str, Any], blocks: Sequence[str]) -> str:
        sections = f10.get("sections") if isinstance(f10.get("sections"), Mapping) else {}
        for block in blocks:
            value = sections.get(block)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, Mapping):
                text = " ".join(str(v) for v in value.values() if v).strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _float(value: Any, default: float) -> float:
        try:
            value = float(value)
            return value if math.isfinite(value) else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _text(value: Any) -> str:
        return html.escape(str(value), quote=True) if value not in (None, "") else "—"
