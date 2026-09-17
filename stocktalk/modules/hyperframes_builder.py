"""Build an editable, data-driven HyperFrames stock discussion composition."""
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

# One-line captions: the spoken line is split at punctuation so every caption
# fits a single row of the 968px caption strip at 46px (~19 CJK chars).  18
# leaves a safety margin for punctuation and latin runs.
CAPTION_MAX_CHARS = 18
CAPTION_BREAKS = "，。；！？、：,.!?;:"
# Punctuation that ends a sentence — never merge a chunk across one of these.
CAPTION_SENTENCE_ENDS = "。；！？.!?;"

# Insets (top, bottom) kept clear per canvas: every short-video app overlays
# the author row on top and the caption / action bar at the bottom, so content
# laid out to the very edge gets covered once it is live.  Values are pixels on
# the 1080×1920 vertical and 1920×1080 horizontal canvases.
DEFAULT_SAFE_AREA: dict[str, tuple[int, int]] = {"vertical": (240, 460), "horizontal": (60, 100)}
# Side inset.  小红书 keeps its controls on the bottom bar, but 抖音 stacks
# like / comment / share down the right edge, so a vertical cut meant for 抖音
# wants ~120 here.
DEFAULT_SAFE_SIDE: dict[str, int] = {"vertical": 56, "horizontal": 70}

# Discussion topics, detected from the dialogue text itself. The first matching
# entry wins, so specific topics precede generic ones.
TOPICS: dict[str, tuple[str, tuple[str, ...]]] = {
    "money": ("量价资金", ("量能", "资金", "换手", "成交", "主力", "筹码", "入场", "流出", "流入")),
    "risk": ("风险与回撤", ("风险", "不确定", "警惕", "下行", "回撤", "波动", "分歧", "抛压", "概率", "仓位", "边界", "不良")),
    "valuation": ("估值水平", ("估值", "pe", "pb", "市盈", "市净", "贵不贵", "溢价", "泡沫")),
    "sentiment": ("市场情绪", ("情绪", "心理", "信心", "恐慌", "预期", "上头", "失望", "乐观", "悲观", "故事", "叙事", "共识")),
    "financial": ("财务与业绩", ("营收", "利润", "收入", "毛利", "净利", "roe", "财报", "业绩", "现金流", "费用", "负债", "盈利", "成本")),
    "news": ("近期资讯", ("资讯", "新闻", "研报", "快讯", "公告", "报道", "消息面", "评级", "券商")),
    "industry": ("行业与业务", ("行业", "赛道", "格局", "竞争", "对手", "同业", "竞品", "份额", "市占", "天花板", "空间", "周期", "产业链", "渠道", "经销商", "政策", "消费", "需求", "供给", "产能", "壁垒", "护城河", "品牌", "公司", "业务", "商业", "管理层", "产品", "提价", "库存")),
    "technical": ("技术走势", ("k线", "均线", "技术", "macd", "指标", "走势", "趋势", "多头排列", "空头排列", "死叉", "金叉", "信号", "强度", "rsi", "kdj", "布林", "压力位", "支撑位", "突破", "回调", "振幅")),
    "outlook": ("后市关注", ("接下来", "未来", "后续", "观察", "验证", "催化剂", "展望", "预期差", "跟踪")),
}


@dataclass(frozen=True)
class HyperFramesProject:
    directory: Path
    composition_path: Path
    duration: float


def split_caption_lines(line: str, max_chars: int = CAPTION_MAX_CHARS) -> list[str]:
    """Split one spoken line into single-row caption chunks.

    Chunks break at punctuation where possible and hard-split an over-long
    clause, then greedily merge short neighbours — a caption that flashes past
    faster than it can be read is worse than a slightly denser one.
    """
    text = " ".join(str(line or "").split())
    if not text:
        return []
    atoms: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in CAPTION_BREAKS:
            atoms.append(buffer)
            buffer = ""
    if buffer:
        atoms.append(buffer)

    pieces: list[str] = []
    for atom in atoms:
        while len(atom) > max_chars:
            pieces.append(atom[:max_chars])
            atom = atom[max_chars:]
        if atom:
            pieces.append(atom)

    merged: list[str] = []
    for piece in pieces:
        # A hard split can leave a leading "，"; punctuation belongs at the end of
        # the chunk it follows.  One extra char still fits the 888px caption row.
        while piece and piece[0] in CAPTION_BREAKS and merged and len(merged[-1]) < max_chars + 1:
            merged[-1] += piece[0]
            piece = piece[1:]
        previous = merged[-1] if merged else ""
        if previous and previous[-1] not in CAPTION_SENTENCE_ENDS and len(previous) + len(piece) <= max_chars:
            merged[-1] = previous + piece
        elif piece:
            merged.append(piece)
    return merged


# Canvas presets: name -> (width, height).  Vertical is the default target
# because抖音/快手/小红书 play 9:16 full-screen — a 16:9 video there only fills
# the middle 56% of the phone screen.  Horizontal stays for B站/YouTube.
CANVAS_PRESETS: dict[str, tuple[int, int]] = {
    "horizontal": (1920, 1080),
    "vertical": (1080, 1920),
}


class HyperFramesBuilder:
    """Create a discussion composition from separate Jinja/CSS/JS assets.

    The canvas size is configurable via ``video.canvas`` ("horizontal" for
    1920×1080, "vertical" for 1080×1920); both carry the same pixel count, so
    render time is unaffected by the choice.
    """

    def __init__(self, config: Mapping[str, Any] | None = None, runner: Runner | None = None) -> None:
        self.config = dict(config or {})
        video = self.config.get("video", {})
        self.video_config = dict(video) if isinstance(video, Mapping) else {}
        self.characters = self.config.get("characters", {})
        self.layout = str(self.video_config.get("canvas", "horizontal")).strip().lower()
        if self.layout not in CANVAS_PRESETS:
            self.layout = "horizontal"
        self.canvas_width, self.canvas_height = CANVAS_PRESETS[self.layout]
        # off  — no on-screen captions; the spoken line is left to the platform
        # line — one-line captions, split at punctuation and cycled per turn
        # full — the whole spoken line as one large caption block
        self.subtitle_mode = self._subtitle_mode(self.video_config.get("subtitles"))
        self.subtitles = self.subtitle_mode != "off"
        # A one-line caption strip has to fit the stage width: the vertical
        # column is 968px at 46px/char, the horizontal one 1220px at 34px.
        self.caption_max_chars = 22 if self.layout == "horizontal" else 18
        # Insets the platform's own player UI will cover: a vertical feed puts
        # the author row on top and the caption + action bar at the bottom.
        self.safe_top, self.safe_bottom = self._safe_area(self.video_config.get("safe_area"))
        self.safe_side = self._safe_side(self.video_config.get("safe_area"))
        self._runner = runner or self._run_subprocess

    def _safe_side(self, raw: Any) -> int:
        """Resolve the left/right inset kept clear of the platform UI."""
        side = DEFAULT_SAFE_SIDE.get(self.layout, 0)
        if isinstance(raw, Mapping):
            per_canvas = raw.get(self.layout)
            source = per_canvas if isinstance(per_canvas, Mapping) else raw
            if isinstance(source, Mapping) and "side" in source:
                try:
                    side = int(source["side"])
                except (TypeError, ValueError):
                    pass
        return max(0, min(int(side), self.canvas_width // 6))

    def _safe_area(self, raw: Any) -> tuple[int, int]:
        """Resolve per-canvas insets (top, bottom) reserved for platform UI.

        Published vertical values follow the conservative end of the published
        safe-zone guides: ~240px top (author row + follow button) and ~460px
        bottom (title, body text, hashtags, action bar).  Landscape only needs
        to clear a progress bar and a title strip.
        """
        top, bottom = DEFAULT_SAFE_AREA.get(self.layout, (0, 0))
        if isinstance(raw, Mapping):
            per_canvas = raw.get(self.layout)
            source = per_canvas if isinstance(per_canvas, Mapping) else raw
            if isinstance(source, Mapping):
                for key, current in (("top", top), ("bottom", bottom)):
                    if key in source:
                        try:
                            value = int(source[key])
                        except (TypeError, ValueError):
                            continue
                        if key == "top":
                            top = value
                        else:
                            bottom = value
        canvas_h = self.canvas_height
        top = max(0, min(int(top), canvas_h // 4))
        bottom = max(0, min(int(bottom), canvas_h // 2))
        return top, bottom

    def _subtitle_mode(self, raw: Any) -> str:
        """Resolve ``video.subtitles`` into off/line/full (bool kept for compat).

        Unset means ``line``: one caption row at a time, swapped as the sentence
        is spoken.  Both canvases use it — a caption block that wraps to three
        rows would need a taller band and would push the stage around, which is
        exactly the reflow the one-line strip exists to avoid.
        """
        valid = ("off", "line", "full")
        if raw is None:
            return "line"
        if isinstance(raw, bool):
            return "line" if raw else "off"
        mode = str(raw).strip().lower()
        mode = {"true": "line", "false": "off", "on": "line", "off": "off",
                "one-line": "line", "single": "line", "1line": "line"}.get(mode, mode)
        return mode if mode in valid else "line"

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
            {"kind": "appearsBy", "selector": "#headline", "bySec": 2.6},
            {"kind": "staysInFrame", "selector": "#visual-frame"},
            {"kind": "staysInFrame", "selector": "#caption-zone"},
            {"kind": "appearsBy", "selector": ".visual-item", "bySec": 2.6},
        ]}, ensure_ascii=False, indent=2), encoding="utf-8")
        return HyperFramesProject(directory, composition, duration)
    build = build_project

    def render_mp4(self, project: HyperFramesProject | str | Path, output_path: str | Path | None = None) -> Path:
        """Run HyperFrames CLI and return a non-empty MP4 path."""
        directory = project.directory if isinstance(project, HyperFramesProject) else Path(project)
        directory = directory.resolve()
        destination = Path(output_path or self.video_config.get("output_path", self._output_dir / "tangulunjin.mp4")).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if shutil.which("npx") is None:
            raise HyperFramesBuildError("Node.js/npx is required to run HyperFrames CLI")
        quality = str(self.video_config.get("quality", "high"))
        timeout_seconds = float(self.video_config.get("render_timeout_seconds", 3600))
        # Long compositions (2-8 min at 30fps = 3600-14400 frames) must stream frames
        # to the encoder instead of buffering ~8MB/frame on disk (a 4-minute render
        # would otherwise demand ~60GB of free space and abort on the disk precheck).
        # 1) Raise the streaming-encode duration ceiling (default 240s).
        # 2) Stream parallel (multi-worker) capture too — HF_CAPTURE_PARALLEL_STREAM
        #    routes interleaved beginFrame capture from all workers into the encoder;
        #    without it, >1 worker silently falls back to the disk path.
        max_streaming = max(7200, int((timeout_seconds / 3) if timeout_seconds else 7200))
        parallel_stream = "false" if str(self.video_config.get("parallel_stream_capture", True)).lower() in {"0", "false", "no"} else "true"
        env = {
            **os.environ,
            "PRODUCER_STREAMING_ENCODE_MAX_DURATION_SECONDS": str(max_streaming),
            "HF_CAPTURE_PARALLEL_STREAM": parallel_stream,
            "HF_DE_PARALLEL_STREAM": parallel_stream,
        }
        # --yes keeps npx from blocking on its interactive install prompt
        # (stdout/stderr are captured here, so the prompt would never be seen).
        command = ("npx", "--yes", "hyperframes", "render", str(directory), "--output", str(destination), "--quality", quality)
        try:
            completed = self._runner(command, directory, env=env)
        except subprocess.TimeoutExpired as exc:
            raise HyperFramesBuildError(
                f"HyperFrames render timed out after {timeout_seconds:g}s — "
                "raise video.render_timeout_seconds in the config (long scripts need far more than 15 minutes)"
            ) from exc
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
    def _run_subprocess(self, command: Sequence[str], cwd: Path,
                        env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        timeout = float(self.video_config.get("render_timeout_seconds", 3600))
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              check=False, env=dict(env) if env else None)

    def _segments(self, script: Mapping[str, Any], timeline: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Build slide segments; classify each discussion topic from the line text."""
        script_turns = [t for t in (script.get("turns", []) if isinstance(script.get("turns"), list) else [])
                        if isinstance(t, Mapping)]
        turns_by_line: dict[str, Mapping[str, Any]] = {}
        for turn in script_turns:
            line = str(turn.get("line", "")).strip()
            if line and line not in turns_by_line:
                turns_by_line[line] = turn

        def meta_for(line: str, character: str) -> tuple[str, str, list[str]]:
            turn = turns_by_line.get(line)
            keywords = [str(x) for x in ((turn or {}).get("visual") or []) if str(x).strip()]
            if not keywords:
                keywords = self._keywords_for(line, character)
            topic_key, topic_title = self._visual_topic(line)
            beat = str((turn or {}).get("beat") or "").strip()
            return topic_key, beat[:24] if beat else topic_title, keywords

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
                    character = str(item.get("character", "bull"))
                    topic, topic_title, keywords = meta_for(line, character)
                    segments.append({"line": line, "character": character, "start": max(0.0, start), "duration": duration, "audio_path": item.get("audio_path"), "topic": topic, "topic_title": topic_title, "keywords": keywords})
        if segments:
            segments = sorted(segments, key=lambda item: item["start"])
            return self._decorate_segments(segments)

        cursor = 0.0
        for turn in script_turns:
            character, line = str(turn.get("speaker", "")), str(turn.get("line", "")).strip()
            if character in {"bull", "bear"} and line:
                topic, topic_title, keywords = meta_for(line, character)
                duration = max(2.5, min(30.0, len(line) * 0.23))
                segments.append({"line": line, "character": character, "start": cursor, "duration": duration, "audio_path": None, "topic": topic, "topic_title": topic_title, "keywords": keywords})
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
                "is_first": index == 0,
                # Caption offsets are relative to the turn's own audio start, so
                # the JS timeline can place every row on one absolute clock and a
                # row is on screen exactly while that part is spoken.
                "caption_lines": self._caption_timing(segment.get("line", ""), duration, start),
            })
        return segments

    def _caption_timing(self, line: Any, duration: float, origin: float) -> list[dict[str, Any]]:
        """Lay single-row caption chunks out across one turn's spoken duration."""
        text = " ".join(str(line or "").split())
        if not text:
            return []
        if self.subtitle_mode == "full":
            return [{"text": text, "offset": 0.0, "duration": round(max(.35, duration), 3)}]
        chunks = split_caption_lines(text, self.caption_max_chars)
        if not chunks:
            return []
        total = sum(len(chunk) for chunk in chunks)
        cursor = 0.0
        timed: list[dict[str, Any]] = []
        for chunk in chunks:
            share = duration * len(chunk) / total
            timed.append({"text": chunk,
                          "offset": round(max(0.0, cursor), 3),
                          "duration": round(max(.35, share), 3)})
            cursor += share
        return timed

    # ------------------------------------------------------------------
    # Stable stage: content slots instead of one full-screen slide per turn
    # ------------------------------------------------------------------
    # The stage (backdrop, headline, panel frame, caption box) is permanent.
    # Only these slots change, and each one is diffed against the previous turn:
    # an unchanged slot is merged into a longer window and therefore never
    # re-animates, while a changed one dissolves in place.  Nothing on screen
    # translates or scales, which is what removes the per-turn "jump".
    SLOT_NAMES = ("tint", "kicker", "keywords", "visual", "caption", "progress")
    # When the permanent stage (headline, panel frame, caption box) fades in.
    # Deliberately early: the opening title card only covers the panel area, so
    # the headline and the first caption are live from the very first word.
    STAGE_IN = 0.35

    @staticmethod
    def _merge_slot(entries: Sequence[tuple[float, float, str, dict[str, Any]]]) -> list[dict[str, Any]]:
        """Collapse adjacent slot entries that carry identical content.

        ``entries`` are ``(start, duration, key, payload)`` tuples in timeline
        order.  A run of turns that keeps the same speaker / topic / artwork
        becomes one element with a longer window, so the timeline has nothing to
        animate at the seam — the picture simply does not move.
        """
        runs: list[dict[str, Any]] = []
        for start, duration, key, payload in entries:
            if not key:
                continue
            begin = max(0.0, float(start))
            end = begin + max(0.25, float(duration))
            if runs and runs[-1]["key"] == key:
                runs[-1]["duration"] = round(end - runs[-1]["start"], 3)
                continue
            runs.append({"start": round(begin, 3),
                         "duration": round(end - begin, 3),
                         "key": key, **payload})
        return runs

    def _slots(self, segments: Sequence[Mapping[str, Any]], duration: float = 0.0) -> dict[str, list[dict[str, Any]]]:
        """Split the dialogue into independently-updating stage slots."""
        slots: dict[str, list[dict[str, Any]]] = {name: [] for name in self.SLOT_NAMES}
        first = dict(segments[0]) if segments else {}
        market_end = (float(first.get("visual_start") or 0.0) + float(first.get("visual_duration") or 1.0)
                      if first else max(1.0, float(duration) - 0.9))
        # The opening beat shows the real market stage (K-line + quote panel)
        # instead of a talking-point board, so the clip opens on actual data.
        market_start = min(self.STAGE_IN, max(0.2, market_end - 0.6))
        market_span = max(0.6, market_end - market_start)
        visual_entries: list[tuple[float, float, str, dict[str, Any]]] = [
            (market_start, market_span, "__market__", {"kind": "market"})]
        first_character = str(first.get("character") or "bull")
        tint_entries: list[tuple[float, float, str, dict[str, Any]]] = [
            (market_start, market_span, first_character, {"character": first_character})]
        kicker_entries: list[tuple[float, float, str, dict[str, Any]]] = []
        keyword_entries: list[tuple[float, float, str, dict[str, Any]]] = []
        for index, segment in enumerate(segments):
            start = float(segment.get("visual_start") or 0.0)
            # The last turn holds its slot content through the recap pad, so the
            # stage never sits bare between the final turn and the outro card.
            duration = max(0.25, float(segment.get("visual_duration") or 1.0))
            if index == len(segments) - 1:
                duration += 0.5
            character = str(segment.get("character") or "bull")
            topic = str(segment.get("topic") or "industry")
            title = str(segment.get("topic_title") or "公司与行业")
            if index:
                tint_entries.append((start, duration, character, {"character": character}))
            kicker_entries.append((start, duration, f"{topic}|{title}",
                                   {"text": title, "character": character, "topic": topic}))
            keywords = [str(k) for k in (segment.get("keywords") or []) if str(k).strip()]
            keyword_entries.append((start, duration, "||".join(keywords),
                                    {"keywords": keywords, "character": character}))
            if index:
                visual = str(segment.get("visual") or "")
                visual_entries.append((start, duration, visual, {"kind": "board", "svg": visual}))
            turn_start = float(segment.get("start") or 0.0)
            for caption in (segment.get("caption_lines") or []):
                text = str(caption.get("text") or "").strip()
                if not text:
                    continue
                slots["caption"].append({
                    "start": round(max(0.0, turn_start + float(caption.get("offset") or 0.0)), 3),
                    "duration": round(max(0.3, float(caption.get("duration") or 0.8)), 3),
                    "text": text, "character": character,
                })
            slots["progress"].append({"start": round(turn_start, 3),
                                      "duration": round(max(0.2, float(segment.get("duration") or 1.0)), 3)})
        slots["tint"] = self._merge_slot(tint_entries)
        slots["kicker"] = self._merge_slot(kicker_entries)
        slots["keywords"] = self._merge_slot(keyword_entries)
        slots["visual"] = self._merge_slot(visual_entries)
        return slots

    @staticmethod
    def _visual_topic(text: str) -> tuple[str, str]:
        normalized = text.lower().replace(" ", "")
        for key, (title, terms) in TOPICS.items():
            if any(term in normalized for term in terms):
                return key, title
        return "industry", "公司与行业"

    def _keywords_for(self, line: str, character: str) -> list[str]:
        """Extract up to 4 on-screen keywords from the spoken line itself."""
        display_name = str((self.characters.get(character, {}) or {}).get("name") or "")
        text = line.replace(display_name, "") if display_name else line
        for punctuation in ("，", "。", "！", "？", "；", "：", "……", "—"):
            text = text.replace(punctuation, "|")
        phrases = [chunk.strip() for chunk in text.split("|")]
        keywords: list[str] = []
        for phrase in phrases:
            phrase = phrase.strip("、的了呢吧吗啊啊～ ")
            if 2 <= len(phrase) <= 12 and phrase not in keywords:
                keywords.append(phrase)
            if len(keywords) == 4:
                break
        if not keywords and line.strip():
            keywords.append(line.strip()[:12])
        return keywords

    @staticmethod
    def _keyword(line: str, keywords: Sequence[str]) -> str:
        """Return the first keyword that actually appears in the spoken line."""
        for keyword in keywords:
            if keyword and keyword in line:
                return keyword
        return keywords[0] if keywords else ""

    def _duration(self, segments: list[Mapping[str, Any]], timeline: Mapping[str, Any]) -> float:
        end = max((float(x["start"]) + float(x["duration"]) for x in segments), default=0.)
        total = round(max(1., self._float(timeline.get("total_duration"), 0.), end, self._float(self.video_config.get("duration"), 0.)), 3)
        # Intro fade-in (0.45s) and outro recap (0.45s) pad the composition with
        # a natural opening and closing instead of hard cuts at both ends.
        return round(total + (0.9 if segments else 0.), 3)

    def _copy_assets(self, directory: Path) -> None:
        for name in ("stock-debate.css", "stock-debate.js", "gsap.min.js"):
            shutil.copyfile(self._asset_dir / name, directory / name)

    def _html(self, stock: Mapping[str, Any], segments: list[Mapping[str, Any]], duration: float) -> str:
        quote = stock.get("quote", {}) if isinstance(stock.get("quote"), Mapping) else {}
        financials = stock.get("financials", {}) if isinstance(stock.get("financials"), Mapping) else {}
        f10 = stock.get("f10", {}) if isinstance(stock.get("f10"), Mapping) else {}
        news = stock.get("news", {}) if isinstance(stock.get("news"), Mapping) else {}
        technical = stock.get("technical", {}) if isinstance(stock.get("technical"), Mapping) else {}
        history = stock.get("kline") or stock.get("price_history") or []
        if not isinstance(history, list):
            history = []
        if not history:
            history = self._real_close_bars(technical)
        visuals = self._visuals(quote, financials, history, f10, technical, news)
        rendered_segments = [dict(segment,
                                  visual=self._turn_board_svg(segment, quote, history, technical, news))
                             for segment in segments]
        slots = self._slots(rendered_segments, duration)
        stock_name = str(quote.get("name") or stock.get("name") or stock.get("code") or "股票")
        env = Environment(loader=FileSystemLoader(self._asset_dir), autoescape=select_autoescape(("html", "xml")))
        return env.get_template("index.html.j2").render(
            name=self._text(stock_name),
            code=self._text(stock.get("code") or quote.get("code") or ""),
            price_line=self._quote_line(quote),
            price_tone=self._price_tone(quote),
            outro_title=self._text(f"以上就是{stock_name}的生意观察"),
            outro_tip="行情会变，生意逻辑才是主线——下次再一起跟踪验证",
            outro_svg=visuals["outro"],
            duration=duration,
            theme=str(self.video_config.get("theme", "dark")),
            layout=self.layout,
            subtitles=self.subtitles,
            subtitle_mode=self.subtitle_mode,
            canvas_width=self.canvas_width,
            canvas_height=self.canvas_height,
            safe_top=self.safe_top,
            safe_bottom=self.safe_bottom,
            safe_side=self.safe_side,
            stage_in=self.STAGE_IN,
            financials=self._financials(quote, financials, technical),
            chart=self._candles(history),
            f10_text=self._f10_text(f10, ("公司概况", "经营分析")),
            segments=rendered_segments,
            slots=slots,
        )

    @staticmethod
    def _real_close_bars(technical: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Flatten real daily closes from the indicator history into bar mappings.

        These mappings contain only real provider data (date/close/change);
        OHLC fields are deliberately absent so downstream code can tell the
        difference between genuine K-line rows and a close-price curve.
        """
        history = technical.get("history") if isinstance(technical.get("history"), list) else []
        bars: list[dict[str, Any]] = []
        for item in [x for x in history if isinstance(x, Mapping)]:
            price = item.get("price") if isinstance(item.get("price"), Mapping) else {}
            close = HyperFramesBuilder._float(price.get("current"), float("nan"))
            if not math.isfinite(close):
                continue
            bar: dict[str, Any] = {"date": str(item.get("timestamp") or ""), "close": close}
            change = HyperFramesBuilder._float(price.get("change"), float("nan"))
            if math.isfinite(change):
                bar["change"] = change
            bars.append(bar)
        return bars

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

    @staticmethod
    def _price_tone(quote: Mapping[str, Any]) -> str:
        """A股配色方向：up=红涨，down=绿跌，flat/未知=中性色。"""
        change = HyperFramesBuilder._float(quote.get("change_pct"), float("nan"))
        if not math.isfinite(change) or change == 0:
            return "flat"
        return "up" if change > 0 else "down"

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
        tone = self._price_tone(quote)
        if tone == "flat":
            tone = ""
        if math.isfinite(price):
            rows.append({"label": "最新价", "value": f"{price:,.2f}", "tone": tone})
        if math.isfinite(change):
            rows.append({"label": "涨跌幅", "value": f"{change:+.2f}%", "tone": tone})
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
        """Draw the price chart strictly from real provider data.

        When genuine OHLC bars exist (kline endpoint) a candlestick chart is
        drawn; otherwise the real close-price line is drawn instead — never a
        synthetic candle shape.
        """
        data = [x for x in bars if isinstance(x, Mapping)][-60:] if isinstance(bars, list) else []
        if len(data) < 2:
            return self._empty_svg(760, 410, "价格走势")
        close_values = [self._float(x.get("close"), float("nan")) for x in data]
        if not all(math.isfinite(value) for value in close_values):
            return self._empty_svg(760, 410, "价格走势")
        # Candles only when every bar carries genuine OHLC; otherwise a close
        # line, so the chart never invents open/high/low or volume shapes.
        has_ohlc = all(
            math.isfinite(self._float(x.get("open"), float("nan")))
            and math.isfinite(self._float(x.get("high"), float("nan")))
            and math.isfinite(self._float(x.get("low"), float("nan")))
            for x in data
        )
        lows = [self._float(x.get("low"), value) for x, value in zip(data, close_values)]
        highs = [self._float(x.get("high"), value) for x, value in zip(data, close_values)]
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
            cl = close_values[i]
            px = 30 + (i + .5) * step
            closes.append(f"{px:.1f},{y(cl):.1f}")
            if not has_ohlc:
                continue
            op = self._float(x.get("open"), cl)
            hi = self._float(x.get("high"), cl)
            lo = self._float(x.get("low"), cl)
            color = "up" if cl >= op else "down"
            w = min(16, step * .56)
            body = f'<rect x="{px - w / 2:.1f}" y="{min(y(op), y(cl)):.1f}" width="{w:.1f}" height="{max(3, abs(y(op) - y(cl))):.1f}"/>'
            volume = (f'<rect class="volume" x="{px - w / 2:.1f}" y="{340 - max(2, self._float(x.get("volume"), 0) / max_volume * 45):.1f}" width="{w:.1f}" height="{max(2, self._float(x.get("volume"), 0) / max_volume * 45):.1f}"/>' if has_volume else "")
            out.append(f'<g class="candle {color}"><line x1="{px:.1f}" y1="{y(hi):.1f}" x2="{px:.1f}" y2="{y(lo):.1f}"/>{body}{volume}</g>')
        for index in {0, len(dates) // 2, len(dates) - 1}:
            if dates[index]:
                label = dates[index][5:] if len(dates[index]) >= 10 else dates[index]
                out.append(f'<text class="svg-axis" x="{30 + (index + .5) * step:.1f}" y="366" text-anchor="middle">{html.escape(label)}</text>')
        # A股配色：无 OHLC 时的收盘折线按区间涨跌着色（红涨绿跌）。
        direction = "up" if close_values[-1] >= close_values[0] else "down"
        line_class = "ma draw-line" if has_ohlc else f"close-line draw-line {direction}"
        return '<svg viewBox="0 0 760 410" role="img" aria-label="价格走势（真实行情数据）">' + ''.join(out) + f'<polyline class="{line_class}" points="{" ".join(closes)}"/></svg>'

    # ------------------------------------------------------------------
    # Per-turn talking-point board
    # ------------------------------------------------------------------
    def _turn_board_svg(self, segment: Mapping[str, Any], quote: Mapping[str, Any],
                        bars: list[Any], technical: Mapping[str, Any], news: Mapping[str, Any] | None) -> str:
        """A visual unique to the spoken turn: the real facts the line mentions
        (left) plus an annotated real mini K-line (right).

        Replaces the old one-SVG-per-topic card, which repeated the same graphic
        for every turn classified to the same topic.
        """
        line = str(segment.get("line") or "")
        topic = str(segment.get("topic") or "industry")
        topic_title = html.escape(str(segment.get("topic_title") or "公司与行业"))
        facts = self._turn_facts(line, quote, bars, technical)
        W, H = 1160, 500
        parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{topic_title}·真实数据">']
        # divider between fact zone and chart zone
        parts.append('<line x1="392" y1="24" x2="392" y2="476" stroke="rgba(101,151,196,.22)" stroke-width="1"/>')
        # header kicker
        parts.append('<circle cx="36" cy="42" r="5" fill="#ffd166"/>')
        parts.append(f'<text x="52" y="50" class="snap-label" style="font-size:20px;letter-spacing:2px">{topic_title}</text>')
        # fact blocks
        y = 104
        for fact in facts[:3]:
            parts.append(f'<text x="34" y="{y}" class="snap-label" style="font-size:19px">{html.escape(fact["label"])}</text>')
            value = html.escape(fact["value"])
            parts.append(f'<text x="34" y="{y + 56}" class="snap-value {fact.get("tone","")}" style="font-size:44px">{value}</text>')
            if fact.get("sub"):
                parts.append(f'<text x="34" y="{y + 88}" class="snap-label" style="font-size:16px">{html.escape(fact["sub"])}</text>')
            y += 132
        if not facts:
            parts.append('<text x="34" y="170" class="snap-label" style="font-size:20px">真实行情数据</text>')
        # right body
        keywords = [str(k).strip() for k in (segment.get("keywords") or []) if str(k).strip()]
        if topic == "news":
            parts.append(self._turn_news_body(news or {}))
        else:
            parts.append(self._mini_chart_body(bars, topic, quote, keywords))
        # Speech-tied focus caption: guarantees each turn's card is distinct and
        # visibly tied to exactly what is being said (keywords are LLM labels,
        # never fabricated numbers).
        if topic != "news" and keywords:
            focus = " · ".join(keywords[:2])[:30]
            parts.append(f'<text x="442" y="478" fill="#ffd166" font-size="18" font-weight="700">◆ 讨论点</text>')
            parts.append(f'<text x="536" y="478" fill="#dbe7f5" font-size="18">{html.escape(focus)}</text>')
        parts.append('<text x="1130" y="492" text-anchor="end" class="snap-label" style="font-size:14px">数据来源：通达信实时行情</text>')
        parts.append('</svg>')
        return ''.join(parts)

    def _turn_facts(self, line: str, quote: Mapping[str, Any], bars: list[Any],
                    technical: Mapping[str, Any]) -> list[dict[str, str]]:
        """Pick up to 3 real metrics whose names actually occur in the line."""
        text = line.lower().replace(" ", "")
        def has(*terms: str) -> bool:
            return any(t in text for t in terms)
        facts: list[dict[str, str]] = []

        def add(label: str, value: str, tone: str = "", sub: str = "") -> None:
            if value and value != "—" and all(f["label"] != label for f in facts):
                facts.append({"label": label, "value": value, "tone": tone, "sub": sub})

        price = self._float(quote.get("price"), float("nan"))
        change = self._float(quote.get("change_pct"), float("nan"))
        tone = "up" if math.isfinite(change) and change > 0 else "down" if math.isfinite(change) and change < 0 else ""
        closes = [self._float(b.get("close"), float("nan")) for b in bars if isinstance(b, Mapping)] if isinstance(bars, list) else []
        closes = [c for c in closes if math.isfinite(c)]

        if has("成交额", "成交", "放量", "缩量", "量能", "换手", "资金", "活跃"):
            amount = self._lookup(quote, keys=("amount", "turnover"))
            if amount is not None:
                add("成交额", self._format_metric("成交额", amount))
        if has("开盘"):
            v = self._lookup(quote, keys=("open",))
            if v is not None: add("今日开盘", f"{v:,.2f}")
        if has("最高", "高点", "压力位"):
            v = self._lookup(quote, keys=("high",))
            if v is not None: add("日内最高", f"{v:,.2f}")
        if has("最低", "低点", "支撑位"):
            v = self._lookup(quote, keys=("low",))
            if v is not None: add("日内最低", f"{v:,.2f}")
        if has("回撤", "回调", "风险", "下行", "下跌空间") and len(closes) >= 5:
            peak = closes[0]; worst = 0.0
            for c in closes:
                peak = max(peak, c)
                if peak:
                    worst = min(worst, c / peak - 1)
            if worst < 0:
                add("区间最大回撤", f"{worst * 100:.1f}%", "down", f"近{len(closes)}个交易日高点回落")
        if has("金叉", "死叉", "均线", "ma5", "ma20", "macd", "指标", "多头", "空头", "技术信号"):
            summary = technical.get("summary") if isinstance(technical.get("summary"), Mapping) else {}
            signal = self._text(summary.get("signal") or summary.get("trend") or None)
            if signal != "—":
                hist = technical.get("history") if isinstance(technical.get("history"), list) else []
                last = hist[-1] if hist and isinstance(hist[-1], Mapping) else {}
                ma = last.get("ma") if isinstance(last.get("ma"), Mapping) else {}
                ma5, ma20 = self._float(ma.get("ma5"), float("nan")), self._float(ma.get("ma20"), float("nan"))
                sub = ""
                if math.isfinite(ma5) and math.isfinite(ma20):
                    sub = f"MA5 {ma5:,.1f} / MA20 {ma20:,.1f}"
                add("技术信号", signal, "", sub)
        if has("区间", "这段时间", "这波", "走势", "累计", "涨了", "跌了") and len(closes) >= 2 and closes[0]:
            interval = (closes[-1] / closes[0] - 1) * 100
            add(f"近{len(closes)}日涨跌", f"{interval:+.2f}%",
                "up" if interval > 0 else "down" if interval < 0 else "")
        # Defaults: anchor every board on the real latest price; surface today's
        # change only when the line actually discusses the daily move or the card
        # would otherwise be sparse, so consecutive cards do not repeat verbatim.
        content_fact_count = len(facts)
        if math.isfinite(price):
            add("最新价", f"{price:,.2f}", tone)
        if math.isfinite(change) and (content_fact_count == 0
                                      or has("今天", "今日", "收盘", "翻红", "收涨", "收跌", "逆势")):
            add("今日涨跌幅", f"{change:+.2f}%", tone)
        return facts[:3]

    def _mini_chart_body(self, bars: list[Any], topic: str, quote: Mapping[str, Any],
                         keywords: list[str] | None = None) -> str:
        """Real last-40-bar candle/close chart with a topic-driven annotation."""
        X0, X1, TOP, BOT = 438, 1130, 78, 392
        data = [b for b in bars if isinstance(b, Mapping)][-40:] if isinstance(bars, list) else []
        closes = [self._float(b.get("close"), float("nan")) for b in data]
        closes = [c for c in closes if math.isfinite(c)]
        if not data or not closes:
            return ('<text x="784" y="250" text-anchor="middle" class="snap-label" style="font-size:22px">'
                    '暂无K线数据</text>')
        has_ohlc = all(math.isfinite(self._float(b.get("open"), float("nan")))
                       and math.isfinite(self._float(b.get("high"), float("nan")))
                       and math.isfinite(self._float(b.get("low"), float("nan"))) for b in data)
        lows = [self._float(b.get("low"), c) for b, c in zip(data, closes)]
        highs = [self._float(b.get("high"), c) for b, c in zip(data, closes)]
        floor, top = min(lows), max(highs)
        spread = max(top - floor, max(abs(top) * .03, .01))
        def y(v: float) -> float: return BOT - ((v - floor) / spread) * (BOT - TOP)
        n = len(data)
        step = (X1 - X0) / n
        out = ['<line x1="438" y1="392" x2="1130" y2="392" stroke="rgba(101,151,196,.28)"/>']
        for g in (0.25, 0.5, 0.75):
            gy = TOP + (BOT - TOP) * g
            out.append(f'<line x1="438" y1="{gy:.0f}" x2="1130" y2="{gy:.0f}" stroke="rgba(101,151,196,.10)"/>')
        volumes = [self._float(b.get("volume"), 0) for b in data]
        max_vol = max(volumes, default=0) or 0
        for i, b in enumerate(data):
            cl = closes[i]
            px = X0 + (i + .5) * step
            w = min(15, step * .58)
            if has_ohlc:
                op = self._float(b.get("open"), cl)
                hi = self._float(b.get("high"), cl)
                lo = self._float(b.get("low"), cl)
                color = "up" if cl >= op else "down"
                out.append(f'<g class="candle {color}"><line x1="{px:.1f}" y1="{y(hi):.1f}" x2="{px:.1f}" y2="{y(lo):.1f}"/>'
                           f'<rect x="{px - w/2:.1f}" y="{min(y(op),y(cl)):.1f}" width="{w:.1f}" height="{max(2,abs(y(op)-y(cl))):.1f}"/></g>')
            if max_vol and topic == "money":
                vh = max(2, volumes[i] / max_vol * 42)
                tone = "up" if i > 0 and cl >= closes[i-1] else "down"
                out.append(f'<rect class="volume candle {tone}" x="{px - w/2:.1f}" y="{BOT + 44 - vh:.1f}" width="{w:.1f}" height="{vh:.1f}"/>')
        if not has_ohlc:
            pts = " ".join(f"{X0 + (i + .5) * step:.1f},{y(c):.1f}" for i, c in enumerate(closes))
            direction = "up" if closes[-1] >= closes[0] else "down"
            out.append(f'<polyline class="close-line {direction}" points="{pts}" fill="none" stroke-width="2.5"/>')
        # latest-price marker
        last_px = X0 + (n - .5) * step
        last_y = y(closes[-1])
        out.append(f'<line x1="438" y1="{last_y:.1f}" x2="1122" y2="{last_y:.1f}" stroke="#ffd166" stroke-dasharray="4 4" opacity=".7"/>')
        out.append(f'<text x="1126" y="{last_y + 5:.1f}" text-anchor="end" fill="#ffd166" font-size="17" font-weight="700">{closes[-1]:,.2f}</text>')
        # topic-specific highlight
        if topic == "risk":
            trough_i = min(range(n), key=lambda i: closes[i])
            tx, ty2 = X0 + (trough_i + .5) * step, y(closes[trough_i])
            out.append(f'<circle cx="{tx:.1f}" cy="{ty2:.1f}" r="6" fill="#5ee0a7" stroke="#04121d" stroke-width="2"/>')
            out.append(f'<text x="{tx:.1f}" y="{ty2 - 14:.1f}" text-anchor="middle" fill="#5ee0a7" font-size="15" font-weight="700">区间低点</text>')
        if topic == "money" and max_vol:
            peak_i = max(range(n), key=lambda i: volumes[i])
            px2 = X0 + (peak_i + .5) * step
            out.append(f'<circle cx="{px2:.1f}" cy="{y(closes[peak_i]):.1f}" r="12" fill="none" stroke="#ffd166" stroke-width="2.5"/>')
            out.append(f'<text x="{px2:.1f}" y="{TOP - 10:.1f}" text-anchor="middle" fill="#ffd166" font-size="15" font-weight="700">放量日</text>')
        if topic == "technical" and n >= 5:
            ma_pts = []
            for i in range(4, n):
                ma = sum(closes[i - 4:i + 1]) / 5
                ma_pts.append(f"{X0 + (i + .5) * step:.1f},{y(ma):.1f}")
            out.append(f'<polyline points="{" ".join(ma_pts)}" fill="none" stroke="#ffd166" stroke-width="2.2" opacity=".95"/>')
            last_ma_x = X0 + (n - .5) * step
            last_ma_y = y(sum(closes[-5:]) / 5)
            out.append(f'<circle cx="{last_ma_x:.1f}" cy="{last_ma_y:.1f}" r="4.5" fill="#ffd166"/>')
            out.append(f'<text x="{last_ma_x - 8:.1f}" y="{last_ma_y - 10:.1f}" text-anchor="end" fill="#ffd166" font-size="14" font-weight="700">MA5</text>')
        # header readout
        interval = (closes[-1] / closes[0] - 1) * 100 if closes[0] else 0.0
        itone = "#ff7188" if interval >= 0 else "#5ee0a7"
        out.append(f'<text x="442" y="52" fill="#91a8bf" font-size="17">近{n}个交易日 · 真实日K</text>')
        out.append(f'<text x="1126" y="52" text-anchor="end" fill="{itone}" font-size="20" font-weight="800">{interval:+.2f}%</text>')
        # date labels
        dates = [str(b.get("date") or "") for b in data]
        for i in (0, n // 2, n - 1):
            if dates[i]:
                label = dates[i][5:10] if len(dates[i]) >= 10 else dates[i]
                out.append(f'<text x="{X0 + (i + .5) * step:.1f}" y="424" text-anchor="middle" fill="#91a8bf" font-size="15">{html.escape(label)}</text>')
        return ''.join(out)

    def _turn_news_body(self, news: Mapping[str, Any]) -> str:
        """Real headline list for news turns (strictly from fetched items)."""
        items = news.get("items") if isinstance(news.get("items"), list) else []
        rows = []
        y = 120
        for item in [x for x in items if isinstance(x, Mapping)][:4]:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            date = str(item.get("publish_time") or "")
            date_label = date[5:10] if len(date) >= 10 else ""
            kind = str(item.get("type") or "资讯")
            head = f"{date_label + ' · ' if date_label else ''}{title[:30]}"
            rows.append(f'<circle cx="452" cy="{y - 7}" r="5" fill="#ffd166"/>'
                        f'<text x="470" y="{y}" class="news-title" style="font-size:23px">{html.escape(head)}</text>'
                        f'<text x="1126" y="{y}" text-anchor="end" class="snap-label" style="font-size:15px">{html.escape(kind)}</text>')
            y += 82
            if len(rows) == 4:
                break
        if not rows:
            rows.append('<text x="784" y="250" text-anchor="middle" class="snap-label" style="font-size:22px">暂无相关资讯</text>')
        return ''.join(rows)

    def _visuals(self, quote: Mapping[str, Any], financials: Mapping[str, Any], bars: list[Any], f10: Mapping[str, Any], technical: Mapping[str, Any], news: Mapping[str, Any] | None = None) -> dict[str, str]:
        """SVG topic visuals for every discussion topic, derived from fetched data only.

        Cards fall back to the real quote snapshot when a topic's own metrics
        are unavailable, so no slide ever shows an empty “暂无数据” placeholder
        while real market data exists.
        """
        return {
            "financial": self._financial_svg(financials, f10, quote, technical),
            "valuation": self._valuation_svg(quote, financials, f10, technical),
            "industry": self._industry_svg(f10, quote),
            "money": self._money_svg(quote, bars, technical),
            "technical": self._candles(bars, highlight_last=True),
            "risk": self._risk_svg(bars, quote, technical),
            "sentiment": self._sentiment_svg(quote, bars),
            "outlook": self._outlook_svg(quote, technical),
            "news": self._news_svg(news or {}, quote, technical),
            "intro": self._snapshot_svg(quote, technical, "行情速览"),
            "outro": self._recap_svg(quote, technical, bars),
        }

    def _news_svg(self, news: Mapping[str, Any], quote: Mapping[str, Any] | None = None, technical: Mapping[str, Any] | None = None) -> str:
        """Headline card rendered strictly from real fetched news items."""
        items = news.get("items") if isinstance(news.get("items"), list) else []
        rows: list[str] = []
        for item in [x for x in items if isinstance(x, Mapping)][:3]:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            date = str(item.get("publish_time") or "").replace("-", "-")
            date_label = f"{date[5:]} " if len(date) >= 10 else ""
            kind = str(item.get("type") or "资讯")
            rows.append(f'<text class="news-title" x="20" y="{34 + len(rows) * 27}">'
                        f'{html.escape((date_label + title)[:22])}</text>'
                        f'<text class="news-source" x="310" y="{34 + (len(rows) - 1) * 27}" text-anchor="end">{html.escape(kind)}</text>')
        if not rows:
            return self._snapshot_svg(quote or {}, technical or {}, "今日行情")
        return (f'<svg viewBox="0 0 330 116" role="img" aria-label="近期资讯标题">{''.join(rows)}</svg>')

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

    def _snapshot_svg(self, quote: Mapping[str, Any], technical: Mapping[str, Any], label: str = "行情快照") -> str:
        """Real quote snapshot card — the universal data-backed fallback."""
        rows: list[tuple[str, str, str]] = []
        price = self._float(quote.get("price"), float("nan"))
        change = self._float(quote.get("change_pct"), float("nan"))
        tone = self._price_tone(quote)
        tone = tone if tone in {"up", "down"} else ""
        if math.isfinite(price):
            rows.append(("最新价", f"{price:,.2f}", tone))
        if math.isfinite(change):
            rows.append(("涨跌幅", f"{change:+.2f}%", tone))
        for name, keys in (("开盘", ("open",)), ("成交额", ("amount", "turnover"))):
            value = self._lookup(quote, keys=keys)
            if value is not None:
                rows.append((name, self._format_metric("今日开盘" if name == "开盘" else "成交额", value), ""))
        summary = technical.get("summary") if isinstance(technical.get("summary"), Mapping) else {}
        signal = self._text(summary.get("signal") or summary.get("trend") or None)
        if signal != "—":
            rows.append(("信号", signal, ""))
        if not rows:
            # Nothing real is available anywhere; render a clean neutral label
            # card instead of an apologetic “暂无数据” placeholder.
            return (f'<svg viewBox="0 0 330 116" role="img" aria-label="{html.escape(label)}">'
                    f'<text class="svg-value" x="165" y="64" text-anchor="middle">{html.escape(label)}</text></svg>')
        cells = ''.join(
            f'<text class="snap-label" x="{22 + i * 104}" y="44">{html.escape(key)}</text>'
            f'<text class="snap-value {row_tone}" x="{22 + i * 104}" y="76">{html.escape(value[:10])}</text>'
            for i, (key, value, row_tone) in enumerate(rows[:3]))
        if len(rows) > 3:
            key, value, _ = rows[3]
            cells += f'<text class="svg-muted" x="22" y="102">{html.escape(key)} {html.escape(value)}</text>'
        return f'<svg viewBox="0 0 330 116" role="img" aria-label="{html.escape(label)}">{cells}</svg>'

    def _recap_svg(self, quote: Mapping[str, Any], technical: Mapping[str, Any], bars: list[Any]) -> str:
        """Closing card: real last close and interval change, nothing invented."""
        closes = [self._float(x.get("close"), float("nan")) for x in bars if isinstance(x, Mapping)] if isinstance(bars, list) else []
        closes = [x for x in closes if math.isfinite(x)]
        rows: list[tuple[str, str, str]] = []
        if closes:
            rows.append(("区间收盘", f"{closes[-1]:,.2f}", ""))
            if closes[0]:
                interval_change = (closes[-1] / closes[0] - 1) * 100
                interval_tone = "up" if interval_change > 0 else "down" if interval_change < 0 else ""
                rows.append(("区间涨跌", f"{interval_change:+.2f}%", interval_tone))
        else:
            price = self._float(quote.get("price"), float("nan"))
            if math.isfinite(price):
                day_tone = self._price_tone(quote)
                rows.append(("最新价", f"{price:,.2f}", day_tone if day_tone in {"up", "down"} else ""))
        change = self._float(quote.get("change_pct"), float("nan"))
        if math.isfinite(change):
            day_tone = "up" if change > 0 else "down" if change < 0 else ""
            rows.append(("当日涨跌", f"{change:+.2f}%", day_tone))
        if not rows:
            return (f'<svg viewBox="0 0 330 116" role="img" aria-label="行情回顾">'
                    f'<text class="svg-value" x="165" y="64" text-anchor="middle">行情回顾</text></svg>')
        cells = ''.join(
            f'<text class="snap-label" x="{22 + i * 104}" y="44">{html.escape(key)}</text>'
            f'<text class="snap-value {row_tone}" x="{22 + i * 104}" y="76">{html.escape(value[:10])}</text>'
            for i, (key, value, row_tone) in enumerate(rows[:3]))
        return f'<svg viewBox="0 0 330 116" role="img" aria-label="行情回顾">{cells}</svg>'

    def _headline_svg(self, quote: Mapping[str, Any]) -> str:
        """Company name card with the real price line when F10 profile is absent."""
        name = self._plain(quote.get("name") or quote.get("code"))
        price = self._float(quote.get("price"), float("nan"))
        change = self._float(quote.get("change_pct"), float("nan"))
        if not name and not math.isfinite(price):
            return (f'<svg viewBox="0 0 330 116" role="img" aria-label="公司概览">'
                    f'<text class="svg-value" x="165" y="64" text-anchor="middle">公司概览</text></svg>')
        detail = "　".join(part for part in (
            f"最新价 {price:,.2f}" if math.isfinite(price) else "",
            f"涨跌幅 {change:+.2f}%" if math.isfinite(change) else "",
        ) if part)
        return (f'<svg viewBox="0 0 330 116" role="img" aria-label="公司概览">'
                f'<text class="svg-value" x="20" y="48" font-size="20">{html.escape((name or "公司概览")[:16])}</text>'
                f'<text class="svg-muted" x="20" y="78">{html.escape(detail)}</text></svg>')

    @staticmethod
    def _f10_snippet_svg(text: str, label: str) -> str:
        return (f'<svg viewBox="0 0 330 116" role="img" aria-label="{html.escape(label)}">'
                f'<text class="svg-value" x="20" y="36" font-size="13">{html.escape(label)}</text>'
                f'<text class="svg-muted" x="20" y="64">{html.escape(text[:26])}</text>'
                f'<text class="svg-muted" x="20" y="88">{html.escape(text[26:52])}</text></svg>')

    @staticmethod
    def _plain(value: Any) -> str:
        return str(value).strip() if value not in (None, "") else ""

    def _financial_svg(self, financials: Mapping[str, Any], f10: Mapping[str, Any], quote: Mapping[str, Any] | None = None, technical: Mapping[str, Any] | None = None) -> str:
        """Financial topic card; falls back to the real quote snapshot."""
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
        return self._snapshot_svg(quote or {}, technical or {})

    def _valuation_svg(self, quote: Mapping[str, Any], financials: Mapping[str, Any], f10: Mapping[str, Any], technical: Mapping[str, Any] | None = None) -> str:
        """PE/PB gauge; falls back to the real quote snapshot."""
        pe = self._lookup(quote, financials, f10, keys=("pe", "PE", "pe_ttm", "市盈率"))
        pb = self._lookup(quote, financials, f10, keys=("pb", "PB", "市净率"))
        if pe is None and pb is None:
            return self._snapshot_svg(quote, technical or {})
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

    def _industry_svg(self, f10: Mapping[str, Any], quote: Mapping[str, Any] | None = None) -> str:
        """Company business profile card; falls back to the real name/price card."""
        directory = f10.get("directory") if isinstance(f10.get("directory"), Mapping) else {}
        fields: list[str] = []
        for label, keys in (("行业", ("行业", "所属行业", "industry")), ("主营", ("主营", "主营业务", "main_business", "产品")), ("公司", ("公司名称", "name", "公司"))):
            for key in keys:
                value = directory.get(key)
                if value not in (None, ""):
                    fields.append(f"{label}：{self._text(value)}")
                    break
        if fields:
            lines = ''.join(f'<text class="svg-muted" x="20" y="{40 + i * 24}">{html.escape(line[:26])}</text>' for i, line in enumerate(fields[:3]))
            return f'<svg viewBox="0 0 330 116" role="img" aria-label="公司业务资料">{lines}</svg>'
        return self._headline_svg(quote or {})

    def _money_svg(self, quote: Mapping[str, Any], bars: list[Any], technical: Mapping[str, Any] | None = None) -> str:
        volumes = [self._float(x.get("volume"), float("nan")) for x in bars if isinstance(x, Mapping)] if isinstance(bars, list) else []
        volumes = [x for x in volumes if math.isfinite(x)][-20:]
        amount = self._lookup(quote, keys=("amount", "turnover"))
        if not volumes and amount is None:
            return self._snapshot_svg(quote, technical or {})
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

    def _risk_svg(self, bars: list[Any], quote: Mapping[str, Any] | None = None, technical: Mapping[str, Any] | None = None) -> str:
        closes = [self._float(x.get("close"), float("nan")) for x in bars if isinstance(x, Mapping)] if isinstance(bars, list) else []
        closes = [x for x in closes if math.isfinite(x)]
        if len(closes) < 2:
            return self._snapshot_svg(quote or {}, technical or {})
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
            return self._snapshot_svg(quote, {}, "市场情绪")
        magnitude = min(100, abs(change or 0) * 10)
        color = "sentiment-positive" if (change or 0) >= 0 else "sentiment-negative"
        volume_text = f'成交量 {volumes[-1]:,.0f}手' if volumes else ""
        bar = (f'<rect class="sentiment-track" x="20" y="38" width="290" height="18" rx="9"/>'
               f'<rect class="{color}" x="20" y="38" width="{magnitude / 100 * 290:.1f}" height="18" rx="9"/>') if change is not None else ""
        label = f'<text class="svg-value" x="20" y="29">涨跌幅 {change:.2f}%</text>' if change is not None else ""
        note = f'<text class="svg-muted" x="20" y="82">{volume_text}</text>' if volume_text else ""
        return f'<svg viewBox="0 0 330 116" role="img" aria-label="市场情绪指标">{bar}{label}{note}</svg>'

    def _outlook_svg(self, quote: Mapping[str, Any] | None = None, technical: Mapping[str, Any] | None = None) -> str:
        """Watch-list card from the provider's signal words; falls back to the snapshot."""
        technical = technical or {}
        summary = technical.get("summary") if isinstance(technical.get("summary"), Mapping) else {}
        history = technical.get("history") if isinstance(technical.get("history"), list) else []
        latest = next((x for x in reversed(history) if isinstance(x, Mapping) and isinstance(x.get("price"), Mapping)), {})
        change = self._float((latest.get("price") or {}).get("change_pct") if latest else float("nan"), float("nan"))
        signal = self._text(summary.get("signal") or None)
        trend = self._text(summary.get("trend") or None)
        strength = summary.get("strength")
        if signal == "—" and trend == "—" and not math.isfinite(change):
            return self._snapshot_svg(quote or {}, technical)
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
        """Return the first readable F10 text block, flattening dict payloads."""
        sections = f10.get("sections") if isinstance(f10.get("sections"), Mapping) else {}
        for block in blocks:
            value = sections.get(block)
            if isinstance(value, Mapping):
                value = " ".join(str(v) for v in value.values() if v)
            if isinstance(value, str) and value.strip():
                return value.strip()
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
