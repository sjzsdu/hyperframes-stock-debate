"""Build an editable, data-driven HyperFrames stock discussion composition."""
from __future__ import annotations

import html
import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape

from stocktalk.modules.arcs import MONOLOGUE_SPEAKER
from stocktalk.modules.business_rows import NON_PRODUCT_ROW_SUFFIXES
from stocktalk.modules.numbers import to_display, unbreakable_spans


class HyperFramesBuildError(RuntimeError):
    """Raised when a project cannot be built or rendered."""


def _parallel_capture_timed_out(detail: str) -> bool:
    """渲染器并行抓帧的 worker 超时特征（可用 --workers 1 重试解决的那类）。

    600825 实锤日志两处特征：error 行 "Parallel worker operation timeout
    exceeded after 30000ms: worker=N phase=session_init"，以及 WARN 行
    "Parallel capture timed out with N workers. ... Retry with --workers 1"。
    只认这两种签名，别的渲染失败不浪费一次重试。
    """
    return ("Parallel worker operation timeout" in detail
            or "Parallel capture timed out" in detail)


Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]

# 内容形态（与 dialogue_generator 的 FORMATS 同值）：debate 双人对辩，
# monologue 单人独白。这里不 import dialogue_generator——那边 import 了
# 本模块的 short_business_label，会成环；按值判定即可。
FORMAT_MONOLOGUE = "monologue"

# One-line captions: the spoken line is split at punctuation so every caption
# fits a single row.  Horizontal: 22 chars at ~40px in a 1220px strip.
# Vertical: 14 chars at 46px (~644px + 72px inner padding) fits whatever side
# inset is in force — the strip has been 720px (180×2), 880px (100×2),
# 1030px (25×2) and now 850px (115×2, real-device crop) wide across the
# 2026-09 layout rounds (2026-09-18/22).
CAPTION_MAX_CHARS = 14
CAPTION_BREAKS = "，。；！？、：,.!?;:"
# Punctuation that ends a sentence — never merge a chunk across one of these.
CAPTION_SENTENCE_ENDS = "。；！？.!?;"

# Insets (top, bottom) kept clear per canvas: every short-video app overlays
# the author row on top and the caption / action bar at the bottom, so content
# laid out to the very edge gets covered once it is live.  Values are pixels on
# the 1080×1920 vertical and 1920×1080 horizontal canvases.
DEFAULT_SAFE_AREA: dict[str, tuple[int, int]] = {"vertical": (240, 460), "horizontal": (120, 100)}
# Side inset (left, right).  抖音/快手 stack like / comment / share down the
# RIGHT edge of a vertical cut, so small insets risk sitting under that overlay
# (56 was covered, 2026-09-18 user feedback).  Vertical 115: phones at
# 19.5:9–20:9 crop ~97–108px per side when a 9:16 video is scaled to fill the
# screen height (Kuaishou confirmed on a real device 2026-09-22: side=25 turned
# "最新价" into "新价").  115 covers the worst crop with headroom → 850px
# column.  History: 100×2 read as huge empty bands on the desktop preview
# (2026-09-19) but the preview browser never crops — the real device is the
# hard constraint.  If the right-edge chart labels still sit under the action
# bar, raise the right side via config `video.safe_area.vertical.side_right`.
# Horizontal cuts only clear the progress bar, so 70 both sides is enough.
DEFAULT_SAFE_SIDE: dict[str, tuple[int, int]] = {"vertical": (115, 115), "horizontal": (70, 70)}

# 论点 → 中央图表的显式绑定（2026-09-29 用户评审后重写）。
# 旧方案是「关键词表定 topic + 图表轮播队列」：topic 表按顺序命中第一个关键词
# （「资金」会把经营现金流判成量价资金、「概率」会把毛利率论点判成风险），命中的
# topic 只决定候选池，具体出哪张图由 borrow/LRU 队列决定——结果就是旁白讲存货、
# 画面轮到行业排名这种错位。新方案把因果倒过来：
#
#   1. 先按台词给这一段「论点」归类（ARGUMENTS，强/弱词打分，并列取靠前者）；
#   2. 论点直接映射语义正确的中央图（ARGUMENT_CHARTS），数据缺失才依次后退；
#   3. 同一论点连续出现时，在语义等价的候选图之间轮换（避免同图连播），但绝不
#      为了换图而借一张和论点无关的图——重复一张对的图好过一张错的新图。
#
# 顺序即并列时的优先级：具体数据论点（存货/现金流/毛利）排在泛化论点之前。
ARGUMENTS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    # (id, 图板标题, 强信号词×2分, 弱信号词×1分)
    #
    # 多年财务论点（2026-09-29 扩容）：F10 财务分析块自带 5 年年度序列与
    # 三大报表科目，这里是它们进入「讨论范围」的入口。趋势类必须排在同名
    # 当期论点**之前**——并列时先到者胜，而「毛利率从 7.5% 爬到 14.6%」
    # 这种句子同时命中两者，该给走势图而不是结构图。
    ("profit_trend", "业绩趋势",
     ("历年", "逐年", "这几年", "五年", "三年来", "连年", "一路", "年报"),
     ("净利", "利润", "业绩", "营收", "下滑")),
    ("margin_trend", "毛利率走势",
     ("毛利率走", "毛利率一", "毛利率连", "毛利率从", "毛利率掉", "毛利率爬", "毛利率降"),
     ("毛利率",)),
    ("roe_trend", "股东回报",
     ("净资产收益率", "roe", "赚钱能力", "股东回报"), ()),
    ("balance_sheet", "资产与负债",
     ("负债率", "资产负债", "借款", "偿债", "杠杆", "家底", "货币资金", "流动比率"),
     ("负债", "资产")),
    ("inventory", "存货与减值",
     ("存货", "库存", "减值", "积压", "滞销"), ()),
    ("receivable", "回款与占用",
     ("应收", "回款", "占款", "挂账", "回笼", "压钱", "垫资"), ()),
    ("cashflow", "现金流",
     ("现金流", "经营活动", "投资活动", "筹资活动", "现金净额", "自由现金流"), ()),
    ("margin", "盈利结构",
     ("毛利率", "毛利"), ()),
    ("peers", "同业竞争",
     ("议价权", "同行", "竞对", "竞争对手", "价格战", "市占", "份额", "同业", "对手", "排名"),
     ("竞争", "格局", "护城河", "替代")),
    ("news", "政策与资讯",
     ("快讯", "政策", "新闻", "公告", "规划", "集群", "园区", "印发", "试点", "会议", "研报"),
     ("概念", "题材", "消息面", "消息", "报道", "资讯")),
    ("profit", "利润表现",
     ("净利", "亏损", "盈利", "利润", "盈亏"), ()),
    ("revenue", "收入构成",
     ("收入", "营收", "占比", "业务结构", "构成"), ("占", "贡献", "主业")),
    ("valuation", "估值水平",
     ("估值", "市盈", "市净", "pe", "pb"), ("贵", "泡沫", "溢价")),
    ("technical", "技术走势",
     ("k线", "均线", "macd", "kdj", "rsi", "布林", "金叉", "死叉", "压力位", "支撑位"),
     ("走势", "趋势", "回调", "突破", "振幅")),
    ("money", "量价资金",
     ("量能", "换手", "成交额", "主力", "筹码", "流入", "流出", "封单"),
     ("放量", "缩量", "成交")),
    ("sentiment", "市场情绪",
     ("情绪", "恐慌", "信心", "叙事", "共识", "上头", "失望"), ("预期", "故事", "乐观", "悲观")),
    ("business", "业务与客户",
     ("客户", "订单", "续单", "复购", "续购"),
     ("方案", "产业链", "渠道", "经销商", "交付", "业务", "产品", "模式", "产能", "供应")),
    ("outlook", "后市关注",
     ("下一份", "下一期", "下期", "接下来"), ("未来", "观察", "验证", "跟踪", "后续")),
)

# 每个论点的中央图候选，按语义贴合度排序。只有语义上真能支撑该论点的图才许
# 进候选：存货/现金流只有营运占用图，绝不为了「换换花样」借排名或瀑布图。
ARGUMENT_CHARTS: dict[str, tuple[str, ...]] = {
    # 多年财务：数据缺失时退到语义相邻的下一张图，不退到无关的图。
    "profit_trend": ("profit_trend", "waterfall"),
    "margin_trend": ("margin_trend", "composition"),
    "roe_trend": ("roe_trend", "profit_trend"),
    "balance_sheet": ("balance_sheet", "cashflow_statement"),
    "inventory": ("working_capital",),
    "receivable": ("working_capital",),
    # 讲「经营/投资/筹资活动」时先给真正的现金流量表；同一论点连播再换到
    # 讲「钱压在哪」的营运占用图。
    "cashflow": ("cashflow_statement", "working_capital"),
    "margin": ("composition", "mindmap"),      # 两张图都逐业务标注毛利率
    "peers": ("ranking", "composition"),
    "news": ("timeline",),
    # 亏损股画不了利润阶梯（负值柱进不了漏斗）→ 落到历年净利走势，
    # composition 只是最后兜底；002912 这期「讲亏损却配收入构成」就是这么来的。
    "profit": ("waterfall", "profit_trend", "composition"),
    "revenue": ("composition", "mindmap"),
    "valuation": ("waterfall", "ranking"),
    "technical": ("kline",),
    "money": ("kline",),
    "sentiment": ("kline", "waterfall"),
    "business": ("mindmap", "composition"),
    "outlook": ("kline", "waterfall"),
    # 兜底：没有命中任何论点时按「公司与行业」处理
    "industry": ("mindmap", "composition"),
}

# 论点候选全缺数据时的后退序列：业务总览 → 收入构成 → 资讯时间线 → 中性来源卡。
# 这里**没有** K 线：K 线只回答"价格怎么走"，把它当万能兜底会让讲存货、讲渠道
# 的轮次配一张行情图——看起来有图，实际答非所问（2026-10-19 定稿）。
ARGUMENT_FALLBACK_CHARTS: tuple[str, ...] = ("mindmap", "composition", "source_card")

# Board membership is derived from the symbol prefix, which is the only
# classification available without an F10 feed (that endpoint is not installed
# in every tongstock release).  Order matters: longer prefixes first.
BOARD_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("688", "689"), "科创板", "科创"),
    (("300", "301", "302"), "创业板", "创业"),
    (("600", "601", "603", "605"), "沪市主板", "沪主板"),
    (("000", "001", "002", "003"), "深市主板", "深主板"),
    (("43", "83", "87", "88", "920"), "北交所", "北交所"),
)

# How many days each "性格" tag looks back over.  Kept short so a 60-day K-line
# (the default fetch) always covers them.
IDENTITY_WINDOWS: tuple[tuple[int, str], ...] = ((5, "近5日"), (20, "近20日"))


@dataclass(frozen=True)
class HyperFramesProject:
    directory: Path
    composition_path: Path
    duration: float


@dataclass(frozen=True)
class StockIdentity:
    """The per-stock fingerprint that keeps two runs from looking identical.

    Everything here is derived from data the provider actually returned: the
    symbol prefix decides the board, and the K-line decides the temperament
    tags.  When the K-line is missing the tags list is simply empty and the
    template falls back to the plain board badge, so a thin data day degrades
    to a plainer card instead of inventing numbers.
    """

    board: str
    board_short: str
    tags: tuple[dict[str, str], ...]
    hook: str
    hue: int

    @property
    def accent(self) -> str:
        """Ambient accent for this symbol — hue-shifted, never a signal colour.

        Red/green stay reserved for 涨/跌 semantics, so this only ever tints the
        backdrop: two symbols look different, but a viewer can still read the
        candles and the quote panel the same way.
        """
        return f"hsl({self.hue} 72% 62%)"

    @property
    def accent_soft(self) -> str:
        return f"hsl({self.hue} 72% 62% / .16)"


# ---------------------------------------------------------------------------
# 业务简称：F10 主营构成的官方明细名常是「自动化装配与检测生产线」这类长名，
# 图板截成「自动化装配与检测生」难读，旁白又各叫各的（「产线集成」）。渲染与
# dialogue prompt 两侧统一走这一个函数，观众看到和听到的就是同一个词。
_LABEL_SUFFIXES = ("生产线", "成套设备", "自动化成", "成套", "集成系统", "系统", "设备", "工程", "服务", "业务", "项目")


def short_business_label(name: str) -> str:
    """官方业务名 → 图板与旁白共用的短名。确定性规则，不丢主语。"""
    text = str(name or "").strip()
    text = text.replace("(产品)", "").replace("(地区)", "").replace("(销售模式)", "").strip()
    for suffix in _LABEL_SUFFIXES:
        if text.endswith(suffix) and len(text) - len(suffix) >= 4:
            text = text[:-len(suffix)]
            break
    if len(text) > 6:
        head = text.split("与", 1)[0]
        if len(head) >= 4:
            text = head
    return text


def _number_safe_break_positions(text: str) -> set[int]:
    """``text`` 里落在一个数字内部的字符下标——在这些位置断句会劈开数字。"""
    positions: set[int] = set()
    for start, end in unbreakable_spans(text):
        positions.update(range(start, end - 1))
    return positions


def _number_safe_cut(atom: str, max_chars: int) -> int:
    """在 ``max_chars`` 处断开，但绝不落在数字内部。

    落在数字里就退到数字开头（这一条字幕短一点无所谓，劈开的数字没法读）；
    数字本身比一行还长时只能按上限硬切——让整行字幕溢出安全条比劈开数字更糟。
    """
    for start, end in unbreakable_spans(atom):
        if start < max_chars < end:
            return start if start > 0 else max_chars
    return max_chars


def split_caption_lines(line: str, max_chars: int = CAPTION_MAX_CHARS) -> list[str]:
    """Split one spoken line into single-row caption chunks.

    Chunks break at punctuation where possible and hard-split an over-long
    clause, then greedily merge short neighbours — a caption that flashes past
    faster than it can be read is worse than a slightly denser one.

    数字是例外：CAPTION_BREAKS 里的 ASCII 「.」「,」同时是小数点与千分位逗号，
    所以断句点和硬切都必须绕开数字整体，否则屏幕上是「净利率47.」+「07%」
    （2026-09-28 用户反馈，601066 早档成片）。
    """
    text = " ".join(str(line or "").split())
    if not text:
        return []
    protected = _number_safe_break_positions(text)
    atoms: list[str] = []
    buffer = ""
    for index, char in enumerate(text):
        buffer += char
        if char in CAPTION_BREAKS and index not in protected:
            atoms.append(buffer)
            buffer = ""
    if buffer:
        atoms.append(buffer)

    pieces: list[str] = []
    for atom in atoms:
        while len(atom) > max_chars:
            cut = _number_safe_cut(atom, max_chars)
            pieces.append(atom[:cut])
            atom = atom[cut:]
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
        # 内容形态：dialogue.format（默认 debate）。builder 只是按值读一次——
        # 归一化在 dialogue_generator.dialogue_format，这里不重复实现。
        dialogue_cfg = self.config.get("dialogue", {})
        self.format = str((dialogue_cfg or {}).get("format", "debate") or "debate").strip().lower()
        if self.format != FORMAT_MONOLOGUE:
            self.format = "debate"
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
        # strip is 1030px at 25×2 insets (CAPTION_MAX_CHARS=14 uses ~716px of
        # it), the horizontal one 1220px at 34px.
        self.caption_max_chars = 22 if self.layout == "horizontal" else CAPTION_MAX_CHARS
        # Insets the platform's own player UI will cover: a vertical feed puts
        # the author row on top and the caption + action bar at the bottom.
        self.safe_top, self.safe_bottom = self._safe_area(self.video_config.get("safe_area"))
        self.safe_side_left, self.safe_side_right = self._safe_sides(self.video_config.get("safe_area"))
        self._runner = runner or self._run_subprocess
        # 本期叙事统筹（episode_plan）：由 pipeline 随脚本一起交给构建器，
        # 画面按 turn 上的 visual_id 在它里面取图。没有计划（旧脚本、单测）
        # 时按论点关键词选图的旧路由仍然可用，但不再是主路径。
        self._episode_plan: Mapping[str, Any] = {}

    def _safe_sides(self, raw: Any) -> tuple[int, int]:
        """Resolve the left/right insets kept clear of the platform UI.

        Vertical platform overlays (like/comment/share) hug the right edge
        only, so the two sides resolve independently.  Config accepts a
        symmetric ``side`` or separate ``side_left`` / ``side_right``.
        """
        left, right = DEFAULT_SAFE_SIDE.get(self.layout, (0, 0))
        if isinstance(raw, Mapping):
            per_canvas = raw.get(self.layout)
            source = per_canvas if isinstance(per_canvas, Mapping) else raw
            if isinstance(source, Mapping):
                symmetric = source.get("side")
                if isinstance(symmetric, (int, float)):
                    left = right = int(symmetric)
                for key, current in (("side_left", left), ("side_right", right)):
                    if key in source:
                        try:
                            value = int(source[key])
                        except (TypeError, ValueError):
                            continue
                        if key == "side_left":
                            left = value
                        else:
                            right = value
        limit = self.canvas_width // 4
        return max(0, min(int(left), limit)), max(0, min(int(right), limit))

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
        script = script if isinstance(script, Mapping) else {}
        # 脚本自带的 format（dialogue_generator 归一化时写入）优先于 config：
        # 测试与二次构建可以只在脚本里声明形态。
        script_format = str(script.get("format") or "").strip().lower()
        if script_format in ("debate", FORMAT_MONOLOGUE):
            self.format = script_format
        # 计划随脚本一起到（pipeline 写进 script["episode_plan"]），横竖两版共用
        # 同一份——否则两个画幅会各自重排，同一句话配出两张不同的图。
        plan = script.get("episode_plan")
        self._episode_plan = plan if isinstance(plan, Mapping) else {}
        segments = self._segments(script, tts_timeline or {})
        for seg in segments:
            ap = seg.get("audio_path")
            if ap:
                seg["audio_path"] = self._project_audio_path(Path(ap), directory)
        duration = self._duration(segments, tts_timeline or {})
        self._copy_assets(directory)
        composition = directory / "index.html"; composition.write_text(self._html(stock_data, segments, duration, script), encoding="utf-8")
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
        detail = (completed.stderr or completed.stdout or "").strip()
        if completed.returncode != 0 and _parallel_capture_timed_out(detail):
            # 2026-10-08 实锤（600825）：并行抓帧起 3 个 headless Chrome，
            # 偶发某个 worker 的 session_init 30s 超时（资源竞争，与合成内容无关
            # ——0 个视频也一样挂）。渲染器自己的 WARN 就是答案：--workers 1。
            # 顺序抓 3724 帧约 3 分钟，可接受；自动重试一次，失败再把原始报错抛出。
            print("[tangulunjin] 并行抓帧超时，自动改用 --workers 1 重试渲染……", file=sys.stderr)
            try:
                completed = self._runner((*command, "--workers", "1"), directory, env=env)
            except subprocess.TimeoutExpired as exc:
                raise HyperFramesBuildError(
                    f"HyperFrames render timed out after {timeout_seconds:g}s — "
                    "raise video.render_timeout_seconds in the config (long scripts need far more than 15 minutes)"
                ) from exc
            except (OSError, subprocess.SubprocessError) as exc:
                raise HyperFramesBuildError(f"HyperFrames render could not start: {exc}") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "unknown renderer failure").strip()
        if completed.returncode != 0:
            raise HyperFramesBuildError(f"HyperFrames render failed: {detail or 'unknown renderer failure'}")
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
            # 图板标题与中央图都由「论点」决定（ARGUMENTS）：旁白讲什么，
            # 画面就出什么。这里只做归类；选图在 _turn_chart。
            turn = turns_by_line.get(line)
            keywords = [to_display(str(x)) for x in ((turn or {}).get("visual") or []) if str(x).strip()]
            if not keywords:
                keywords = self._keywords_for(line, character)
            topic_key, topic_title = self._turn_argument(line)
            return topic_key, topic_title, keywords

        def visual_for(line: str) -> tuple[str, str]:
            turn = turns_by_line.get(line) or {}
            return str(turn.get("visual_id") or ""), str(turn.get("round_id") or "")

        def short_for(line: str) -> bool:
            """这一段是不是"短回应"（"嗯""有道理"），决定要不要换图板。

            标记由 dialogue_generator 打在脚本的轮次上（``turn["short"]``），而 TTS
            的时间轴只带 speaker/line——所以这里按台词回查脚本原轮次，否则渲染层
            永远看不到这个标记，"短回应不换图板"就成了死代码。
            """
            return bool((turns_by_line.get(line) or {}).get("short"))

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
                    character = str(item.get("character") or self._cast()[0])
                    topic, topic_title, keywords = meta_for(line, character)
                    visual_id, round_id = visual_for(line)
                    segments.append({"line": line, "character": character, "start": max(0.0, start), "duration": duration, "audio_path": item.get("audio_path"), "topic": topic, "topic_title": topic_title, "keywords": keywords, "short": short_for(line), "visual_id": visual_id, "round_id": round_id})
        if segments:
            segments = sorted(segments, key=lambda item: item["start"])
            return self._decorate_segments(segments)

        cursor = 0.0
        cast = set(self._cast())
        for turn in script_turns:
            character, line = str(turn.get("speaker", "")), str(turn.get("line", "")).strip()
            if character in cast and line:
                topic, topic_title, keywords = meta_for(line, character)
                duration = max(2.5, min(30.0, len(line) * 0.23))
                segments.append({"line": line, "character": character, "start": cursor, "duration": duration, "audio_path": None, "topic": topic, "topic_title": topic_title, "keywords": keywords, "short": bool(turn.get("short")), "visual_id": str(turn.get("visual_id") or ""), "round_id": str(turn.get("round_id") or "")})
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
        """Lay single-row caption chunks out across one turn's spoken duration.

        字幕是显示层：口播念「三亿一千九百万」，屏幕上出现「3.19亿」——
        汉字数字统一转阿拉伯（stocktalk.modules.numbers.to_display）。
        """
        text = to_display(" ".join(str(line or "").split()))
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
    SLOT_NAMES = ("tint", "kicker", "keywords", "visual", "caption", "progress", "round")
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

    def _character_name(self, role: str) -> str:
        """配置里的角色名（股市新手/股市老登），缺省退回角色代号。"""
        characters = self.characters if isinstance(self.characters, Mapping) else {}
        value = characters.get(role, {})
        value = value if isinstance(value, Mapping) else {}
        return str(value.get("name") or role)

    def _cast(self) -> tuple[str, ...]:
        """本期出镜阵容：独白只有 narrator，辩论是 bull/bear。

        台词白名单、首帧角色行、speakers 渲染上下文都从这里取，不再散落
        ("bull","bear") 硬编码——否则独白成片要么零分段、要么首帧画两个空角色。
        """
        return (MONOLOGUE_SPEAKER,) if self.format == FORMAT_MONOLOGUE else ("bull", "bear")

    def _slots(self, segments: Sequence[Mapping[str, Any]], duration: float = 0.0) -> dict[str, list[dict[str, Any]]]:
        """Split the dialogue into independently-updating stage slots."""
        slots: dict[str, list[dict[str, Any]]] = {name: [] for name in self.SLOT_NAMES}
        total = float(duration or 0.0)
        first = dict(segments[0]) if segments else {}
        market_end = (float(first.get("visual_start") or 0.0) + float(first.get("visual_duration") or 1.0)
                      if first else max(1.0, float(duration) - 0.9))
        # The opening beat shows the real market stage (K-line + quote panel)
        # instead of a talking-point board, so the clip opens on actual data.
        market_start = min(self.STAGE_IN, max(0.2, market_end - 0.6))
        market_span = max(0.6, market_end - market_start)
        visual_entries: list[tuple[float, float, str, dict[str, Any]]] = [
            (market_start, market_span, "__market__", {"kind": "market"})]
        first_character = str(first.get("character") or self._cast()[0])
        tint_entries: list[tuple[float, float, str, dict[str, Any]]] = [
            (market_start, market_span, first_character,
             {"character": first_character, "name": self._character_name(first_character)})]
        keyword_entries: list[tuple[float, float, str, dict[str, Any]]] = []
        for index, segment in enumerate(segments):
            start = float(segment.get("visual_start") or 0.0)
            # The last turn holds its slot content through the recap pad, so the
            # stage never sits bare between the final turn and the outro card.
            duration = max(0.25, float(segment.get("visual_duration") or 1.0))
            if index == len(segments) - 1:
                duration += 0.5
            character = str(segment.get("character") or self._cast()[0])
            topic = str(segment.get("topic") or "industry")
            if index:
                tint_entries.append((start, duration, character,
                                     {"character": character, "name": self._character_name(character)}))
            keywords = [str(k) for k in (segment.get("keywords") or []) if str(k).strip()]
            keyword_entries.append((start, duration, "||".join(keywords),
                                    {"keywords": keywords, "character": character, "topic": topic}))
            # 短回应（"嗯""有道理"）不换图板：一句话的认可没必要把整块图板重画一遍。
            # 片尾互动图板例外——它挂在最后一轮上，不能因为那一轮短就被跳过。
            # 注意这里不能 continue：短回应轮照样要有字幕和进度。
            if index and (not segment.get("short") or segment.get("cta")):
                visual = str(segment.get("visual") or "")
                # 片尾互动图板要留够阅读时间：一直挂到成片结束。
                run = max(duration, max(0.6, total - start)) if segment.get("cta") else duration
                # 渲染器按 data-start/data-duration 窗口硬性显隐 clip，不会手下留情
                # （002058_20261009 实锤：短回应轮没有自己的图板条目，上一块图板
                # 到点整块消失，中央画面出现 2-6s 空窗）。新图板进场前，把上一块
                # 的窗口显式延长到本块进场时刻——短回应轮与轮间换气都由上一块
                # 图板继续持有，visual 轨道从第一块进场起无缝。
                last_start, last_run, last_key, last_payload = visual_entries[-1]
                if last_start + last_run < start:
                    visual_entries[-1] = (last_start, round(start - last_start, 3), last_key, last_payload)
                visual_entries.append((start, run, visual, {"kind": "board", "svg": visual, "topic": topic}))
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
            # A small, persistent orientation cue prevents the debate from
            # feeling like an unbounded stream.  It is derived from the actual
            # rendered turns (never invented by the model), so it stays correct
            # when a short response is merged into the previous board.
            slots["round"].append({
                "start": round(turn_start, 3),
                "duration": round(max(0.2, float(segment.get("duration") or 1.0)), 3),
                # 独白没有「观点交锋」，用「第 i 节 / n」讲清叙事进度；
                # 辩论保留「观点 i / n」（tests/test_hyperframes_builder.py:177 钉死）。
                "label": (f"第 {index + 1} 节 / {len(segments)}" if self.format == FORMAT_MONOLOGUE
                          else f"观点 {index + 1} / {len(segments)}"),
                "character": character,
            })
        slots["tint"] = self._merge_slot(tint_entries)
        slots["keywords"] = self._merge_slot(keyword_entries)
        slots["visual"] = self._merge_slot(visual_entries)
        return slots

    @classmethod
    def _turn_argument(cls, line: str) -> tuple[str, str]:
        """把一段台词归到「论点」，返回 (论点 id, 图板标题)。

        强信号词每词 2 分、弱信号词每词 1 分，取总分最高的论点；并列时取
        ARGUMENTS 里靠前的（具体数据论点优先于泛化论点）。一个词都没命中
        就落回「公司与行业」。只看台词本身——visual 关键词是展示层标签，
        不是旁白内容，不参与归类（否则 -1.89亿净利润 会把快讯论点拽向利润）。
        """
        normalized = str(line or "").lower().replace(" ", "")
        best_id, best_title, best_score = "industry", "公司与行业", 0
        for arg_id, title, strong, weak in ARGUMENTS:
            score = 2 * cls._proof_count(strong, normalized) + cls._proof_count(weak, normalized)
            if score > best_score:
                best_id, best_title, best_score = arg_id, title, score
        return best_id, best_title

    @staticmethod
    def _proof_count(terms: Sequence[str], text: str) -> int:
        """命中的词条数，但被更长命中词包含的词条不重复计数。

        「毛利率」与「毛利」同时命中是**同一个证据的两种写法**，不是两条证据。
        不去重的话「销售毛利率从 7.57% 爬到 14.62%」会因为多命中一个「毛利」
        而把走势论点（强词「毛利率从」）压回结构论点，画面就成了结构图而不是
        走势图（2026-09-29 实测）。
        """
        matched = sorted((term for term in terms if term in text), key=len, reverse=True)
        counted: list[str] = []
        for term in matched:
            if not any(term in longer for longer in counted):
                counted.append(term)
        return len(counted)

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
        """Return the first keyword that actually appears in the spoken line.

        关键词是显示层（阿拉伯数字），台词是口播层（汉字读法），所以按
        显示层转换后的台词做包含判断。
        """
        display = to_display(line)
        for keyword in keywords:
            if keyword and keyword in display:
                return keyword
        return keywords[0] if keywords else ""

    def _duration(self, segments: list[Mapping[str, Any]], timeline: Mapping[str, Any]) -> float:
        end = max((float(x["start"]) + float(x["duration"]) for x in segments), default=0.)
        total = round(max(1., self._float(timeline.get("total_duration"), 0.), end, self._float(self.video_config.get("duration"), 0.)), 3)
        # Intro fade-in (0.45s) pads the head; the tail holds the outro card for
        # `outro_hold_seconds` (default 2.4s) after the last word lands — a video
        # that cuts to black the instant the voice stops reads as truncated
        # (2026-09-29 review: 收尾只有 2.6 秒、话音没落就结束).
        return round(total + 0.45 + self._outro_hold, 3)

    @property
    def _outro_hold(self) -> float:
        try:
            hold = float(self.video_config.get("outro_hold_seconds", 2.4))
        except (TypeError, ValueError):
            hold = 2.4
        return max(0.45, hold)

    def _copy_assets(self, directory: Path) -> None:
        for name in ("stock-debate.css", "stock-debate.js", "gsap.min.js"):
            shutil.copyfile(self._asset_dir / name, directory / name)

    @staticmethod
    def _board(code: str) -> tuple[str, str]:
        """Map a symbol to its board from the prefix alone."""
        symbol = str(code or "").strip()
        for prefixes, full, short in BOARD_RULES:
            if symbol.startswith(prefixes):
                return full, short
        return "A股", "A股"

    def _identity(self, code: str, bars: Sequence[Any], quote: Mapping[str, Any]) -> StockIdentity:
        """Derive the per-stock fingerprint from real quote and K-line data.

        Every tag is a measured number (interval change, range, streak, volume
        ratio) — none of them are opinions, and none of them touch the
        compliance red lines, so the card stays informative without reading as
        a recommendation.  A missing or too-short K-line yields no tags at all.
        """
        board, board_short = self._board(code)
        closes = [self._float(bar.get("close"), float("nan"))
                  for bar in bars if isinstance(bar, Mapping)]
        closes = [value for value in closes if math.isfinite(value) and value > 0]
        tags: list[dict[str, str]] = []
        if len(closes) >= 6:
            last = closes[-1]
            for window, label in IDENTITY_WINDOWS:
                if len(closes) <= window:
                    continue
                base = closes[-1 - window]
                if base <= 0:
                    continue
                change = (last / base - 1) * 100
                tags.append({"label": label, "value": f"{change:+.1f}%",
                             "tone": "up" if change > 0 else ("down" if change < 0 else "")})
                break  # the shortest window present is the freshest read
            window_values = closes[-60:]
            floor, top = min(window_values), max(window_values)
            if floor > 0:
                swing = (top / floor - 1) * 100
                tags.append({"label": f"{len(window_values)}日振幅", "value": f"{swing:.0f}%", "tone": ""})
            streak = self._close_streak(closes)
            if streak:
                direction, length = streak
                tags.append({"label": "连阳" if direction > 0 else "连阴",
                             "value": f"{length}天", "tone": "up" if direction > 0 else "down"})
            ratio = self._volume_ratio(bars)
            if ratio is not None:
                tags.append({"label": "量能", "value": f"{ratio:.1f}×",
                             "tone": "up" if ratio >= 1 else "down"})
        hook = self._identity_hook(board, tags, quote)
        return StockIdentity(board=board, board_short=board_short,
                             tags=tuple(tags[:4]), hook=hook, hue=self._hue(code))

    @staticmethod
    def _close_streak(closes: list[float]) -> tuple[int, int] | None:
        """Count the run of consecutive up (or down) closes ending today."""
        if len(closes) < 3:
            return None
        direction = 0
        length = 0
        # Take the tail first, then pair neighbours: slicing both sides with
        # negative offsets (closes[-12:-1] vs closes[-11:]) clamps to the same
        # start on a short series, which silently compares each close to itself.
        window = closes[-12:]
        for previous, current in zip(window[:-1], window[1:]):
            step = 1 if current > previous else (-1 if current < previous else 0)
            if step == 0:
                continue
            if step != direction:
                direction, length = step, 1
            else:
                length += 1
        if direction == 0 or length < 2:
            return None
        return direction, length

    @classmethod
    def _volume_ratio(cls, bars: Sequence[Any]) -> float | None:
        """Recent volume versus the earlier baseline, when volume is present."""
        values = [cls._float(bar.get("volume"), float("nan"))
                  for bar in bars if isinstance(bar, Mapping)]
        values = [value for value in values if math.isfinite(value) and value > 0]
        if len(values) < 12:
            return None
        recent = values[-5:]
        baseline = values[-25:-5] or values[:-5]
        if not baseline:
            return None
        mean_recent = sum(recent) / len(recent)
        mean_base = sum(baseline) / len(baseline)
        if mean_base <= 0:
            return None
        return mean_recent / mean_base

    def _identity_hook(self, board: str, tags: Sequence[Mapping[str, str]], quote: Mapping[str, Any]) -> str:
        """One opening line built from whatever the provider actually returned."""
        change = self._float(quote.get("change_pct"), float("nan"))
        if math.isfinite(change) and change != 0:
            direction = "涨" if change > 0 else "跌"
            return f"{board} · 今日{direction} {abs(change):.2f}%"
        if tags:
            return f"{board} · {tags[0]['label']} {tags[0]['value']}"
        return f"{board} · 一起看看这门生意"

    @staticmethod
    def _hue(code: str) -> int:
        """A stable hue per symbol, so each run has its own ambient colour.

        Deterministic (same symbol = same hue, every run and every machine) and
        nudged off the red/green band so the ambience never competes with the
        涨/跌 semantics.
        """
        digest = 0
        for char in str(code or ""):
            digest = (digest * 31 + ord(char)) & 0xFFFFFFFF
        return 190 + (digest % 130)  # 190–319: cyan → blue → violet

    def _html(self, stock: Mapping[str, Any], segments: list[Mapping[str, Any]], duration: float,
              script: Mapping[str, Any] | None = None) -> str:
        script = script if isinstance(script, Mapping) else {}
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
        # One-element state: which chart the previous turn showed, so a same
        # argument can rotate between semantically equal charts instead of
        # repeating itself — but never borrow an unrelated chart.
        chart_state: list[str] = [""]
        rendered_segments: list[dict[str, Any]] = []
        for segment in segments:
            rendered_segments.append(dict(segment,
                                          visual=self._turn_board_svg(segment, quote, history, technical, news,
                                                                      financials, f10, chart_state)))
        cta = self._cta_payload(script)
        # 独白没有两方立场，双列互动图板会画出两个空列——直接跳过，片尾的
        # hook/question 仍走 outro 标题与提示行（不受影响）。
        if cta and len(rendered_segments) > 1 and self.format != FORMAT_MONOLOGUE:
            # 片尾把最后一块图板换成互动图板（多空立场 + 下期盯什么 + 一个真问题）。
            # 这不是新槽位——复用 visual 槽，所以模板/CSS/JS 一行都不用改。
            rendered_segments[-1] = dict(rendered_segments[-1],
                                         visual=self._cta_board_svg(cta, quote),
                                         cta=True)
        slots = self._slots(rendered_segments, duration)
        stock_name = str(quote.get("name") or stock.get("name") or stock.get("code") or "股票")
        identity = self._identity(stock.get("code") or quote.get("code") or "", history, quote)
        env = Environment(loader=FileSystemLoader(self._asset_dir), autoescape=select_autoescape(("html", "xml")))
        return env.get_template("index.html.j2").render(
            name=self._text(stock_name),
            code=self._text(stock.get("code") or quote.get("code") or ""),
            price_line=self._quote_line(quote),
            price_tone=self._price_tone(quote),
            headline_stats=self._headline_stats(stock.get("stockinfo")),
            identity=identity,
            # 收尾不再是一句每期都一样的固定文案：有 hook（下期要盯的可验证变量）就把它
            # 放在收尾大标题上、"下次一起跟踪验证"这句承诺才有实际内容；没有问题就退回
            # 原来的写法。
            outro_title=self._text(self._outro_title(cta, stock_name)),
            outro_tip=self._text(self._outro_tip(cta)),
            # 首帧冲突数字（2026-10-07）：分享卡=首帧，intro-card 静态展示、无入场动画；
            # 模型没给（或结构不对）就是 None，intro-card 保持原三行布局。
            spotlight=(dict(script["spotlight"]) if isinstance(script.get("spotlight"), Mapping)
                       and str(script["spotlight"].get("value") or "").strip() else None),
            # 收尾留白：片尾卡在最后一句话音落下后还要停留多久（2026-09-29 评审：
            # 0.45s 的片尾等于话音没落就黑屏）。_duration 会把同样的留白加进
            # 总时长，所以这里只改片尾卡出场时刻，不挤占正文。
            outro_hold=self._outro_hold,
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
            safe_side_left=self.safe_side_left,
            safe_side_right=self.safe_side_right,
            stage_in=self.STAGE_IN,
            # 出镜阵容（独白=narrator 一人）：首帧角色行循环 cast，不再写死
            # speakers.bull/bear——否则独白首帧画两个空角色位。
            cast=list(self._cast()),
            speakers={role: self._character_name(role) for role in self._cast()},
            financials=self._financials(quote, financials, technical),
            chart=self._candles(history),
            f10_text=self._f10_text(f10, ("公司概况", "经营分析")),
            segments=rendered_segments,
            slots=slots,
        )

    # ------------------------------------------------------------------
    # 片尾互动图板 —— 内容层（脚本里的 sides/hook/question）落到画面上
    # ------------------------------------------------------------------
    # 这不是新槽位：它复用 visual 槽的最后一块图板，所以 Jinja 模板、CSS、GSAP
    # 时间轴一行都不用改，也就没有任何新的版面风险。图板上的每一句都来自脚本
    # （LLM 产出的、已经过合规清洗的文本），不在这里拼任何数字。
    @staticmethod
    def _cta_payload(script: Mapping[str, Any]) -> dict[str, Any]:
        """收集片尾要展示的互动内容；三样都没有就返回空（不硬凑一个空图板）。"""
        sides = script.get("sides") if isinstance(script.get("sides"), Mapping) else {}
        payload = {
            "sides": {role: str(sides.get(role) or "").strip() for role in ("bull", "bear")},
            "hook": str(script.get("hook") or "").strip(),
            "question": str(script.get("question") or "").strip(),
            "arc": "",
        }
        arc = script.get("arc") if isinstance(script.get("arc"), Mapping) else {}
        payload["arc"] = str(arc.get("name") or "")
        if not any(payload["sides"].values()) and not payload["hook"] and not payload["question"]:
            return {}
        return payload

    @staticmethod
    def _wrap_cn(text: str, per_line: int, limit: int = 3) -> list[str]:
        """按字数折行——SVG 没有自动换行，只能自己切。"""
        text = str(text or "").strip()
        lines = [text[index:index + per_line] for index in range(0, len(text), per_line)]
        return lines[:limit]

    def _cta_board_svg(self, cta: Mapping[str, Any], quote: Mapping[str, Any]) -> str:
        """多空立场 + 下期盯什么 + 一个真问题。

        两条硬约束（成片帧内的文案红线，见 tests/test_hyperframes_builder.py）：
        ① 不出现"看多/看空/免责/不构成/投资建议"等词——所以列名用"更乐观/更谨慎"
           这种不带判断立场的说法，风险提示只留在发布简介里；
        ② 不出现角色名（config 里的人设名是内部称谓，不上屏）。
        立场两列用说话人自己的配色（bull/bear 的粉/蓝），刻意不用红绿——红涨绿跌
        是行情语义，用在"观点"上会被误读成看涨/看跌信号。
        """
        stacked = self.layout != "horizontal"
        W, H = (1160, 800) if stacked else (1956, 500)
        sides = cta.get("sides") or {}
        hook = str(cta.get("hook") or "")
        question = str(cta.get("question") or "")
        out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="两方的分歧与下一期关注">']
        # header
        out.append('<circle cx="36" cy="42" r="5" fill="#ffd166"/>')
        out.append('<text x="52" y="50" class="snap-label" style="font-size:20px;letter-spacing:2px">'
                   '两方还没谈拢的地方</text>')
        note = f'本期骨架 · {html.escape(str(cta.get("arc") or ""))}' if cta.get("arc") else ""
        if note:
            out.append(f'<text x="{W - 34}" y="48" text-anchor="end" class="snap-label" '
                       f'style="font-size:16px">{note}</text>')
        # 两列立场
        if stacked:
            col_w, per_line, gap = 512, 17, 36
            label_y, body_y = 122, 178
        else:
            col_w, per_line, gap = 880, 21, 60
            label_y, body_y = 122, 176
        cols = [(34, "#ff78bc", "更乐观的一边", sides.get("bull", "")),
                (34 + col_w + gap, "#65baff", "更谨慎的一边", sides.get("bear", ""))]
        divider_x = 34 + col_w + gap // 2
        out.append(f'<line x1="{divider_x}" y1="86" x2="{divider_x}" y2="300" '
                   'stroke="rgba(101,151,196,.22)" stroke-width="1"/>')
        for x, tone, name, text in cols:
            out.append(f'<text x="{x}" y="{label_y}" fill="{tone}" font-size="24" '
                       f'font-weight="700">{html.escape(name)}</text>')
            for index, line in enumerate(self._wrap_cn(text, per_line, 3)):
                out.append(f'<text x="{x}" y="{body_y + index * 42}" fill="#dbe7f5" '
                           f'font-size="30">{html.escape(line)}</text>')
        # 下期盯什么 / 你怎么看
        foot_y = 470 if stacked else 372
        if hook:
            out.append(f'<text x="34" y="{foot_y}" fill="#ffd166" font-size="22" font-weight="700">'
                       '下期盯什么</text>')
            out.append(f'<text x="176" y="{foot_y}" fill="#dbe7f5" font-size="26">'
                       f'{html.escape(hook[:44])}</text>')
        if question:
            qy = foot_y + (58 if stacked else 56)
            out.append(f'<text x="34" y="{qy}" fill="#5ee0a7" font-size="22" font-weight="700">'
                       '你怎么看</text>')
            out.append(f'<text x="176" y="{qy}" fill="#dbe7f5" font-size="26">'
                       f'{html.escape(question[:44])}</text>')
        # 固定风险提示（2026-10-08）：压在图板右下角，弱化但不缺席。合规审查的
        # 画面禁词是「免责/不构成/投资建议」，这句标准风险用语不含其中任何一个。
        out.append(f'<text x="{W - 34}" y="{H - 26}" text-anchor="end" fill="rgba(219,231,245,.42)" '
                   f'font-size="18" letter-spacing="2">股市有风险，投资需谨慎</text>')
        out.append('</svg>')
        return ''.join(out)

    def _outro_title(self, cta: Mapping[str, Any], stock_name: str) -> str:
        """收尾大标题：直接用本期的 hook（下期要核对的具体变量）。

        2026-09-22 用户反馈「下期盯：这几个字有必要吗」——没必要：图板上
        已经有小字栏目名交代语境，大标题再带一遍前缀是重复，直接说内容。
        """
        hook = str((cta or {}).get("hook") or "").strip()
        return hook[:40] if hook else f"以上就是{stock_name}的生意观察"

    @staticmethod
    def _outro_tip(cta: Mapping[str, Any]) -> str:
        """收尾副标题：优先用抛给观众的那个问题。"""
        question = str((cta or {}).get("question") or "").strip()
        return question[:40] if question else "行情会变，生意逻辑才是主线——下次再一起跟踪验证"

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
    def _headline_stats(stockinfo: Any) -> list[tuple[str, str]]:
        """总市值 / 换手率 for the headline's right side — fills the dead band
        right of the stock name with real, unit-verified stockinfo metrics.
        Missing stockinfo simply renders nothing (never a placeholder)."""
        metrics = stockinfo.get("metrics") if isinstance(stockinfo, Mapping) and isinstance(stockinfo.get("metrics"), Mapping) else {}
        stats: list[tuple[str, str]] = []
        for label in ("总市值(亿元)", "市值(亿元)"):
            value = HyperFramesBuilder._float(metrics.get(label), float("nan"))
            if math.isfinite(value) and value > 0:
                stats.append((label.replace("(亿元)", ""), f"{value:,.2f}亿"))
                break
        turnover = HyperFramesBuilder._float(metrics.get("换手率(%)"), float("nan"))
        if math.isfinite(turnover) and turnover > 0:
            stats.append(("换手率", f"{turnover:.2f}%"))
        return stats

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
        # Opening panels are wider than tall on both canvases (horizontal
        # ≈3.3:1, vertical ≈2.6:1 after the 2026-09-19 asymmetric side insets);
        # a 760-wide SVG floats between dead margins.  Stretch per canvas and
        # let the candles spread.
        W = 1340 if self.layout == "horizontal" else 900
        left, right = 30.0, float(W - 30)
        data = [x for x in bars if isinstance(x, Mapping)][-60:] if isinstance(bars, list) else []
        if len(data) < 2:
            return self._empty_svg(W, 410, "价格走势")
        close_values = [self._float(x.get("close"), float("nan")) for x in data]
        if not all(math.isfinite(value) for value in close_values):
            return self._empty_svg(W, 410, "价格走势")
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
        step = (right - left) / len(data)
        out = [f'<path class="axis draw-line" d="M{left:g} 20H{right:g}M{left:g} 180H{right:g}M{left:g} 340H{right:g}"/>']
        closes: list[str] = []
        dates = [str(x.get("date") or x.get("timestamp") or "") for x in data]
        max_volume = max((self._float(x.get("volume"), 0) for x in data), default=0) or 0
        has_volume = max_volume > 0
        highlight_from = len(data) - max(4, len(data) // 4)
        if highlight_last:
            out.append(f'<rect class="discussion-range" x="{left + highlight_from * step:.1f}" y="20" width="{(len(data) - highlight_from) * step:.1f}" height="320"/>')
        for i, x in enumerate(data):
            cl = close_values[i]
            px = left + (i + .5) * step
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
                out.append(f'<text class="svg-axis" x="{left + (index + .5) * step:.1f}" y="366" text-anchor="middle">{html.escape(label)}</text>')
        # A股配色：无 OHLC 时的收盘折线按区间涨跌着色（红涨绿跌）。
        direction = "up" if close_values[-1] >= close_values[0] else "down"
        line_class = "ma draw-line" if has_ohlc else f"close-line draw-line {direction}"
        return f'<svg viewBox="0 0 {W} 410" role="img" aria-label="价格走势（真实行情数据）">' + ''.join(out) + f'<polyline class="{line_class}" points="{" ".join(closes)}"/></svg>'

    # ------------------------------------------------------------------
    # Per-turn talking-point board
    # ------------------------------------------------------------------
    def _turn_board_svg(self, segment: Mapping[str, Any], quote: Mapping[str, Any],
                        bars: list[Any], technical: Mapping[str, Any], news: Mapping[str, Any] | None,
                        financials: Mapping[str, Any] | None = None, f10: Mapping[str, Any] | None = None,
                        chart_state: list[str] | None = None) -> str:
        """A visual unique to the spoken turn: the real facts the line mentions
        plus the chart BOUND to the turn's argument.

        The argument is classified from the line in `_segments` (存货→营运占用,
        毛利率→收入构成/业务拆解, 同行→排名标尺…), so the chart always shows
        what the narration is talking about.  ``chart_state`` is a one-element
        list carrying the previous turn's chart id across the whole video.
        Horizontal boards are wide, so facts sit in a left column beside the
        chart; vertical panels are taller than wide, so the board STACKS — a
        facts strip across the top, the chart below at full width (2026-09-19
        user feedback: the side-by-side split crushed the chart into a narrow
        right column).
        """
        line = str(segment.get("line") or "")
        topic = str(segment.get("topic") or "industry")
        topic_title = html.escape(str(segment.get("topic_title")
                                      or dict((a[0], a[1]) for a in ARGUMENTS).get(topic, "公司与行业")))
        facts = self._turn_facts(line, quote, bars, technical)
        # Horizontal panels are height-bound (inner ≈1394×356, aspect 3.9): a
        # 1160-wide board floats between dead margins.  Stretch the board to
        # match the panel aspect so charts spread out instead.  Vertical panels
        # are ≈790×576 (aspect 1.4 at 115×2 side insets), so the stacked board
        # is 1160×800 and scales down into the panel.
        stacked = self.layout != "horizontal"
        W, H = (1160, 800) if stacked else (1956, 500)
        _, zone_right = self._chart_zone()
        parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{topic_title}·真实数据">']
        # header kicker
        parts.append('<circle cx="36" cy="42" r="5" fill="#ffd166"/>')
        parts.append(f'<text x="52" y="50" class="snap-label" style="font-size:20px;letter-spacing:2px">{topic_title}</text>')
        keywords = [str(k).strip() for k in (segment.get("keywords") or []) if str(k).strip()]
        ctx = {"bars": bars, "quote": quote, "keywords": keywords,
               "argument": topic,
               "f10": f10 if isinstance(f10, Mapping) else {},
               "financials": financials if isinstance(financials, Mapping) else {},
               "news": news if isinstance(news, Mapping) else {},
               # 本期叙事统筹：按 visual_id 在这一份计划里找到这一轮该出的图。
               # 它是画面选图的**主路由**（缺数据时只在该 visual 自带的 fallback
               # 里后退），不能再退回"从台词关键词猜一张图"。
               "episode_plan": self._episode_plan if isinstance(self._episode_plan, Mapping) else {}}
        last_chart = (chart_state or [""])[0]
        chart, shown_chart = self._turn_chart(topic, ctx, last_chart, str(segment.get("visual_id") or ""))
        if chart_state is not None:
            chart_state[0] = shown_chart
        # Speech-tied focus caption: guarantees each turn's card is distinct and
        # visibly tied to exactly what is being said (keywords are LLM labels,
        # never fabricated numbers).
        focus_row = ""
        if topic != "news" and keywords:
            focus = " · ".join(keywords[:2])[:30]
            focus_row = ('<text x="34" y="{y}" fill="#ffd166" font-size="18" font-weight="700">◆ 讨论点</text>'
                         '<text x="128" y="{y}" fill="#dbe7f5" font-size="18">{focus}</text>').format(
                             y=786 if stacked else 478, focus=html.escape(focus))
        if stacked:
            # facts as a horizontal strip: up to three label/value columns
            col_x = 34
            for fact in facts[:3]:
                parts.append(f'<text x="{col_x}" y="106" class="snap-label" style="font-size:19px">{html.escape(fact["label"])}</text>')
                value = html.escape(fact["value"])
                parts.append(f'<text x="{col_x}" y="164" class="snap-value {fact.get("tone","")}" style="font-size:44px">{value}</text>')
                if fact.get("sub"):
                    parts.append(f'<text x="{col_x}" y="198" class="snap-label" style="font-size:16px">{html.escape(fact["sub"])}</text>')
                col_x += 330
            if not facts:
                parts.append('<text x="34" y="134" class="snap-label" style="font-size:20px">真实行情数据</text>')
            # divider under the facts strip, chart below at full width.
            parts.append('<line x1="24" y1="226" x2="1136" y2="226" stroke="rgba(101,151,196,.22)" stroke-width="1"/>')
            # Charts draw in their own 0-500 local space; a NESTED <svg y=...>
            # shifts it under the strip.  Deliberately NOT <g transform>:
            # transform is a presentation attribute, so any CSS like
            # `.clip * { transform: none }` (animation-neutralising in
            # screenshot verification) would silently flatten the offset.
            parts.append(f'<svg x="0" y="246" width="{W}" height="500" overflow="visible">{chart}</svg>')
            parts.append(focus_row)
            parts.append(f'<text x="{W - 34}" y="786" text-anchor="end" class="snap-label" style="font-size:14px">数据来源：通达信实时行情</text>')
        else:
            # divider between fact zone and chart zone
            parts.append('<line x1="392" y1="24" x2="392" y2="476" stroke="rgba(101,151,196,.22)" stroke-width="1"/>')
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
            parts.append(chart)
            parts.append(focus_row)
            parts.append(f'<text x="{W - 34}" y="492" text-anchor="end" class="snap-label" style="font-size:14px">数据来源：通达信实时行情</text>')
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
        zone_left, zone_right = self._chart_zone()
        X0, X1, TOP, BOT = zone_left - 4, zone_right + 4, 78, 392
        data = [b for b in bars if isinstance(b, Mapping)][-40:] if isinstance(bars, list) else []
        closes = [self._float(b.get("close"), float("nan")) for b in data]
        closes = [c for c in closes if math.isfinite(c)]
        if not data or not closes:
            cx = (zone_left + zone_right) / 2
            return (f'<text x="{cx:g}" y="250" text-anchor="middle" class="snap-label" style="font-size:22px">'
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
        out = [f'<line x1="{X0:g}" y1="392" x2="{X1:g}" y2="392" stroke="rgba(101,151,196,.28)"/>']
        for g in (0.25, 0.5, 0.75):
            gy = TOP + (BOT - TOP) * g
            out.append(f'<line x1="{X0:g}" y1="{gy:.0f}" x2="{X1:g}" y2="{gy:.0f}" stroke="rgba(101,151,196,.10)"/>')
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
        out.append(f'<line x1="{X0:g}" y1="{last_y:.1f}" x2="{X1 - 8:g}" y2="{last_y:.1f}" stroke="#ffd166" stroke-dasharray="4 4" opacity=".7"/>')
        out.append(f'<text x="{zone_right:g}" y="{last_y + 5:.1f}" text-anchor="end" fill="#ffd166" font-size="17" font-weight="700">{closes[-1]:,.2f}</text>')
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
        out.append(f'<text x="{zone_left:g}" y="52" fill="#91a8bf" font-size="17">近{n}个交易日 · 真实日K</text>')
        out.append(f'<text x="{zone_right:g}" y="52" text-anchor="end" fill="{itone}" font-size="20" font-weight="800">{interval:+.2f}%</text>')
        # date labels
        dates = [str(b.get("date") or "") for b in data]
        for i in (0, n // 2, n - 1):
            if dates[i]:
                label = dates[i][5:10] if len(dates[i]) >= 10 else dates[i]
                out.append(f'<text x="{X0 + (i + .5) * step:.1f}" y="424" text-anchor="middle" fill="#91a8bf" font-size="15">{html.escape(label)}</text>')
        return ''.join(out)

    # ------------------------------------------------------------------
    # Chart library — the central chart is BOUND to the turn's argument,
    # not dealt from a rotation queue (2026-09-29 review: 7 of 10 turns
    # showed a chart unrelated to the narration because a borrow/LRU queue
    # picked whatever the video hadn't shown yet).  Selection order now:
    # (1) the argument's own candidates, most specific first, skipping the
    # chart that is already on screen when an equally-valid alternative
    # exists; (2) the neutral business fallbacks; (3) the neutral evidence
    # card (2026-10-19 定稿：末级不再退回 K 线——K 线只回答"价格怎么走")。
    # Every chart renders strictly from fetched data; a chart whose data is
    # missing returns None and the next candidate takes over — nothing is
    # ever invented to fill a slot.  A repeat of the RIGHT chart beats a
    # fresh but irrelevant one, so there is no cross-argument borrowing.
    # ------------------------------------------------------------------

    def _chart_renderers(self, ctx: Mapping[str, Any]) -> dict[str, Callable[[], str | None]]:
        """图名 → 渲染器。这是"有没有这张图"的**唯一**登记处。

        计划里的 ``template`` / ``fallback`` 只能取这里的键：写了一个没登记的名字，
        它和"有这张图但缺数据"长得一模一样（都返回 None），整章会静默掉到来源卡
        ——阶段 B1 就是这么踩的。所以登记处只此一处，测试直接拿它比对计划里的
        每一个名字。
        """
        return {
            "kline": lambda: self._mini_chart_body(ctx["bars"], ctx.get("argument", "technical"), ctx["quote"], ctx.get("keywords")),
            "mindmap": lambda: self._business_mindmap_body(ctx["quote"], ctx["f10"]),
            "composition": lambda: self._composition_bars_body(ctx["f10"]),
            "waterfall": lambda: self._profit_waterfall_body(ctx["financials"]),
            "timeline": lambda: self._news_timeline_body(
                ctx["news"], str((ctx.get("visual") or {}).get("verify") or "")),
            "working_capital": lambda: self._working_capital_body(ctx["financials"]),
            "ranking": lambda: self._industry_rank_body(ctx["f10"]),
            # 多年财务（F10 财务分析块）：数据不到三期就返回 None，交给下一张图。
            "profit_trend": lambda: self._profit_trend_body(ctx["f10"]),
            "margin_trend": lambda: self._percent_trend_body(
                ctx["f10"], "销售毛利率(%)", "毛利率走势", "近几年销售毛利率", "#ffd166"),
            "roe_trend": lambda: self._percent_trend_body(
                ctx["f10"], "净资产收益率(%)", "股东回报", "近几年净资产收益率", "#8fd3ff"),
            "cashflow_statement": lambda: self._cashflow_statement_body(ctx["f10"]),
            "balance_sheet": lambda: self._balance_sheet_body(ctx["f10"]),
            # 末级兜底：任何候选都出不了图时，出一张只写"已核验素材"的中性卡，
            # 而不是拿一张与这轮无关的图（过去的 K 线兜底就是这么来的）。
            "source_card": lambda: self._source_card_body(ctx.get("argument", ""), ctx.get("keywords") or []),
            # ---- 阶段 B2 三张新图：都按计划的 data_binding（mat/metric 契约）
            # 取数，缺数据返回 None，由该章自己的 fallback 序列接手。----
            "product_portfolio": lambda: self._product_portfolio_body(ctx),
            "channel_flow": lambda: self._channel_flow_body(ctx),
            "ledger_pair": lambda: self._ledger_pair_body(ctx),
        }

    def _render_chart(self, name: str, ctx: Mapping[str, Any]) -> str | None:
        """Render one named chart; None when its data does not exist."""
        renderer = self._chart_renderers(ctx).get(name)
        return renderer() if renderer else None

    def _turn_chart(self, argument: str, ctx: Mapping[str, Any], last_chart: str = "", visual_id: str = "") -> tuple[str, str]:
        """按这一轮的计划出图；返回 (svg, 图名)。

        两条路由：
        * **有 episode_plan 且 visual_id 命中某一章**——只在该章声明的
          ``template`` 与其 ``fallback`` 序列里后退，末级 ``source_card``。
          绝不从台词关键词猜图、也绝不退回万能 K 线：画面该服从"这期讲什么"
          的统筹结果，而不是反过来。
        * **没有计划**（旧脚本、单测、未接入 pipeline 的调用）——退回按论点
          关键词选图的旧逻辑，末级同样是 ``source_card``。

        ``last_chart`` 是上一块图板用的图：同一论点连续出现时优先换成语义
        等价的另一张（margin 的 composition↔mindmap 都逐业务标注毛利率），
        避免同图连播；候选只有一张时（存货→营运占用图）就坦然重复——
        重复一张对的图，好过一张错的新图。
        """
        plan = ctx.get("episode_plan") if isinstance(ctx.get("episode_plan"), Mapping) else None
        planned = self._planned_visual(plan, visual_id)
        if isinstance(planned, Mapping):
            # 这一章的图板定义下发到渲染器：新图按它的 data_binding 取数，
            # timeline 的「待验证什么」标签也挂在它上面。渲染器只认 ctx，
            # 不再自己去猜"这轮讲的是什么"。
            ctx = {**ctx, "visual": planned}
            template = str(planned.get("template") or "")
            fallbacks = [str(name) for name in (planned.get("fallback") or []) if str(name)]
            # 计划的 fallback 末级必须能兜住；没写就补一张来源卡，避免空板。
            if "source_card" not in fallbacks:
                fallbacks.append("source_card")
            for name in self._ordered(template, last_chart) + self._ordered(fallbacks, last_chart):
                body = self._render_chart(name, ctx)
                if body:
                    return body, name
            # 兜底序列本身也出不了图（理论上不会，source_card 恒成立）时，
            # 仍然出来源卡而不是行情图。
            return self._render_chart("source_card", ctx) or "", "source_card"

        candidates = list(ARGUMENT_CHARTS.get(argument) or ("mindmap", "composition"))
        for name in self._ordered(candidates, last_chart) + self._ordered(ARGUMENT_FALLBACK_CHARTS, last_chart):
            body = self._render_chart(name, ctx)
            if body:
                return body, name
        return self._render_chart("source_card", ctx) or "", "source_card"

    @staticmethod
    def _planned_visual(plan: Mapping[str, Any] | None, visual_id: str) -> Mapping[str, Any] | None:
        """在 episode_plan 里按 visual_id 找到这一章的图板定义。"""
        if not isinstance(plan, Mapping) or not visual_id:
            return None
        for chapter in plan.get("rounds") or []:
            if not isinstance(chapter, Mapping):
                continue
            visual = chapter.get("visual")
            if isinstance(visual, Mapping) and str(visual.get("id") or "") == visual_id:
                return visual
        return None

    @staticmethod
    def _ordered(names: str | Sequence[str], last_chart: str) -> list[str]:
        """把候选排成"先试试没用过的那张"，避免同图连播又不至于无图可出。"""
        items = [str(name) for name in ([names] if isinstance(names, str) else names) if str(name)]
        return [name for name in items if name != last_chart] + [name for name in items if name == last_chart]

    def _source_card_body(self, argument: str, keywords: list[Any]) -> str:
        """Neutral evidence card used when a planned visual has no source-backed renderer.

        它只陈述"这轮讲到哪儿"，不补任何数字：拿一张无关的图填槽比空板更糟，
        而编一个数字上屏则是红线。宽度跟着图表区走（横版与竖版图表区不同宽）。
        """
        zone_left, zone_right = self._chart_zone()
        width = max(320.0, zone_right - zone_left - 20.0)
        x = zone_left + 10.0
        title = html.escape(str(argument or "公开资料"))
        detail = html.escape(" · ".join(str(k) for k in keywords[:3]) or "暂无可用结构化视觉")
        return (f'<g class="source-card">'
                f'<rect x="{x:g}" y="90" width="{width:g}" height="300" rx="24" fill="#142235" stroke="#5d7894"/>'
                f'<text x="{x + 40:g}" y="170" fill="#ffd166" font-size="34">来源卡 · {title}</text>'
                f'<text x="{x + 40:g}" y="240" fill="#dbe7f5" font-size="28">{detail}</text>'
                f'<text x="{x + 40:g}" y="330" fill="#91a8bf" font-size="22">仅展示已核验素材，缺少结构化来源时不补猜</text>'
                f'</g>')

    def _chart_zone(self) -> tuple[float, float]:
        """X extent of the chart area inside the turn-board viewBox.

        Vertical canvases are width-bound (board 1160 wide fills the panel);
        horizontal canvases stretch the board to 1956, so the chart zone runs
        to 1922 and every renderer spreads out instead of clustering left.
        """
        return (442.0, 1922.0) if self.layout == "horizontal" else (44.0, 1126.0)

    def _svg_header(self, title: str, note: str = "", note_tone: str = "#91a8bf") -> str:
        zone_left, zone_right = self._chart_zone()
        out = [f'<text x="{zone_left:g}" y="54" fill="#91a8bf" font-size="24">{html.escape(title)}</text>']
        if note:
            out.append(f'<text x="{zone_right:g}" y="54" text-anchor="end" fill="{note_tone}" font-size="22">{html.escape(note)}</text>')
        return ''.join(out)

    @staticmethod
    def _f10_profile(f10: Mapping[str, Any]) -> dict[str, Any]:
        profile = f10.get("profile") if isinstance(f10.get("profile"), Mapping) else {}
        return profile if isinstance(profile, Mapping) else {}

    def _business_mindmap_body(self, quote: Mapping[str, Any], f10: Mapping[str, Any]) -> str | None:
        """业务脑图：中心是公司，分支是主营构成里真实的业务块（含毛利率）。

        主营构成不足两项时退而用 F10 概念标签当分支；两者都没有则返回 None
        让下一个候选图表顶上。
        """
        profile = self._f10_profile(f10)
        composition = profile.get("主营构成") if isinstance(profile.get("主营构成"), Mapping) else {}
        # 只画真正的产品行：F10 把 `(销售模式)` / `(地区)` / `(行业)` 行和产品行挤在
        # 同一张表里，不滤掉的话脑图上会出现「直销」「华东」「汽车零部件」这种
        # 假业务分支（B2 只在产品卡组和渠道链路里排掉了它们，这里是兜底渲染器）。
        rows = [dict(row) for row in composition.get("明细", [])
                if isinstance(row, Mapping)
                and not str(row.get("项目") or "").endswith(NON_PRODUCT_ROW_SUFFIXES)][:4]
        from_composition = len(rows) >= 2
        if not from_composition:
            concepts = [str(c) for c in (profile.get("题材标签") or {}).get("概念", []) if str(c)][:4]
            rows = [{"项目": name, "收入": "", "收入占比(%)": "", "毛利率(%)": ""} for name in concepts]
        if len(rows) < 2:
            return None
        name = str(quote.get("name") or "该公司")
        period = str(composition.get("报告期") or "")
        header_note = f"主营构成 · {period}" if from_composition else "题材标签"
        out = [self._svg_header(f"{name}业务拆解", header_note)]
        zone_left, zone_right = self._chart_zone()
        cx, cy = (zone_left + zone_right) / 2, 252
        half = (zone_right - zone_left) / 2 - 214
        spots = [(cx - half, 122), (cx - half, 386), (cx + half, 122), (cx + half, 386)]
        # edges first so node boxes cover their endpoints
        for nx, ny in spots:
            out.append(f'<line x1="{cx}" y1="{cy}" x2="{nx}" y2="{ny}" stroke="rgba(105,183,255,.35)" stroke-width="1.6"/>')
        out.append(f'<rect x="{cx - 128}" y="{cy - 44}" width="256" height="88" rx="24" fill="rgba(12,30,52,.95)" stroke="#ffd166" stroke-width="2"/>')
        out.append(f'<text x="{cx}" y="{cy + 11}" text-anchor="middle" fill="#ffd166" font-size="32" font-weight="700">{html.escape(name[:8])}</text>')
        for (nx, ny), row in zip(spots, rows):
            label = short_business_label(str(row.get("项目") or ""))
            share, margin = str(row.get("收入占比(%)") or ""), str(row.get("毛利率(%)") or "")
            sub = " · ".join(part for part in (
                f"收入占比 {share}%" if share not in ("", "0") else "",
                f"毛利率 {margin}%" if margin else "") if part)
            out.append(f'<rect x="{nx - 152}" y="{ny - 40}" width="304" height="80" rx="18" fill="rgba(12,30,52,.94)" stroke="rgba(105,183,255,.55)" stroke-width="1.6"/>')
            out.append(f'<text x="{nx}" y="{ny - 2}" text-anchor="middle" fill="#dbe7f5" font-size="27" font-weight="700">{html.escape(label[:9])}</text>')
            if sub:
                out.append(f'<text x="{nx}" y="{ny + 28}" text-anchor="middle" fill="#91a8bf" font-size="19">{html.escape(sub[:24])}</text>')
        return ''.join(out)

    def _composition_bars_body(self, f10: Mapping[str, Any]) -> str | None:
        """主营构成条形图：每行一根真实占比条，右端带收入额与毛利率。"""
        composition = self._f10_profile(f10).get("主营构成")
        composition = composition if isinstance(composition, Mapping) else {}
        # 同 mindmap：收入构成图也只画产品行，渠道/地区/行业行不是"钱从哪来"的业务块。
        rows = [row for row in composition.get("明细", [])
                if isinstance(row, Mapping)
                and not str(row.get("项目") or "").endswith(NON_PRODUCT_ROW_SUFFIXES)]
        parsed: list[tuple[str, float, str, str]] = []
        for row in rows:
            try:
                share = float(str(row.get("收入占比(%)") or "").replace("%", ""))
            except ValueError:
                continue
            if share <= 0:
                continue
            parsed.append((str(row.get("项目") or ""), share,
                           str(row.get("收入") or ""), str(row.get("毛利率(%)") or "")))
            if len(parsed) == 5:
                break
        if len(parsed) < 2:
            return None
        period = str(composition.get("报告期") or "")
        out = [self._svg_header("钱从哪来 · 收入构成", f"报告期 {period}")]
        zone_left, zone_right = self._chart_zone()
        bar_x, row_h, y0 = 712, 76, 116
        bar_max_w = max(300.0, zone_right - bar_x - 280.0)
        revenue_right = zone_right - 130  # 毛利列右对齐于 zone_right、宽约 110px，金额最晚必须在这里收笔
        top_share = max(share for _, share, _, _ in parsed)
        tones = {"(产品)": "#6ab7ff", "(地区)": "#ffd166", "(销售模式)": "#5ee0a7"}

        def text_w(value: str) -> float:
            return sum(19.0 if ord(ch) > 0x2E7F else 10.5 for ch in value)

        for index, (label, share, revenue, margin) in enumerate(parsed):
            y = y0 + index * row_h
            tone = next((color for suffix, color in tones.items() if label.endswith(suffix)), "#6ab7ff")
            shown_label = short_business_label(label)
            width = max(8.0, share / top_share * bar_max_w)
            out.append(f'<text x="700" y="{y + 24}" text-anchor="end" fill="#dbe7f5" font-size="24">{html.escape(shown_label[:11])}</text>')
            out.append(f'<rect x="{bar_x}" y="{y}" width="{width:.1f}" height="34" rx="9" fill="{tone}" opacity=".82"/>')
            if revenue:
                shown = revenue[:8]
                if bar_x + width + 12 + text_w(shown) <= revenue_right:
                    out.append(f'<text x="{bar_x + width + 12:.1f}" y="{y + 24}" fill="#91a8bf" font-size="19">{html.escape(shown)}</text>')
                else:
                    # 条子太长时金额画进条内右端，绝不越过毛利列
                    out.append(f'<text x="{bar_x + width - 10:.1f}" y="{y + 24}" text-anchor="end" fill="#0b1220" font-size="19" font-weight="600">{html.escape(shown)}</text>')
            if margin:
                # Right-aligned column: revenue text after the bar varies in
                # width, and a running tail would clip at the SVG edge.
                out.append(f'<text x="{zone_right:g}" y="{y + 24}" text-anchor="end" fill="#91a8bf" font-size="19">毛利 {html.escape(margin[:5])}%</text>')
            out.append(f'<text x="{bar_x + width / 2:.1f}" y="{y + 58}" text-anchor="middle" fill="#dbe7f5" font-size="17">{share:.1f}%</text>')
        return ''.join(out)

    def _profit_waterfall_body(self, financials: Mapping[str, Any]) -> str | None:
        """利润漏斗：营收 → 营业利润 → 利润总额 → 净利润。

        Column count adapts to whichever of these finance fields exist (3–4).
        主营利润(ZhuYingLiRun) is deliberately excluded: its 口径 disagrees with
        the F10 主营构成 by an order of magnitude, and a funnel bar that claims
        75% of revenue "evaporated" between two steps would be a lie on screen.
        """
        def metric(label: str) -> float | None:
            value = financials.get(label)
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None

        revenue = metric("营业收入(亿元)")
        steps = [(label, metric(label)) for label in
                 ("营业收入(亿元)", "营业利润(亿元)", "利润总额(亿元)", "净利润(亿元)")]
        columns_spec = [(label.replace("(亿元)", ""), value) for label, value in steps if value]
        if not revenue or len(columns_spec) < 3:
            return None
        period = str(financials.get("报告期") or "")
        net = columns_spec[-1][1]
        out = [self._svg_header("钱怎么落袋 · 利润阶梯", f"报告期 {period}", "#91a8bf")]
        base_y, chart_h = 396, 268
        zone_left, zone_right = self._chart_zone()
        left, right = zone_left + 58.0, zone_right - 6.0
        step_w = (right - left) / len(columns_spec)
        bar_w = min(150.0, step_w * 0.62)
        tones = ("#6ab7ff", "#ffd166", "#8fd3ff", "#ff7188")
        tops: list[float] = []
        for index, (label, value) in enumerate(columns_spec):
            cx = left + (index + 0.5) * step_w
            height = max(6.0, value / revenue * chart_h)
            top = base_y - height
            tops.append(top)
            tone = tones[index % len(tones)] if index < len(columns_spec) - 1 else "#ff7188"
            out.append(f'<rect x="{cx - bar_w / 2:.1f}" y="{top:.1f}" width="{bar_w:.1f}" height="{height:.1f}" rx="10" fill="{tone}" opacity=".85"/>')
            out.append(f'<text x="{cx:.1f}" y="{top - 16:.1f}" text-anchor="middle" fill="#dbe7f5" font-size="30" font-weight="700">{value:,.2f}亿</text>')
            out.append(f'<text x="{cx:.1f}" y="{base_y + 36}" text-anchor="middle" fill="#91a8bf" font-size="23">{label}</text>')
        for start in range(len(columns_spec) - 1):
            out.append(f'<line x1="{left + (start + 0.5) * step_w + bar_w / 2:.1f}" y1="{tops[start]:.1f}"'
                       f' x2="{left + (start + 1.5) * step_w - bar_w / 2:.1f}" y2="{tops[start + 1]:.1f}"'
                       ' stroke="rgba(101,151,196,.5)" stroke-dasharray="5 4"/>')
        out.append(f'<text x="442" y="456" fill="#ffd166" font-size="21">每 100 元收入落下 {net / revenue * 100:.1f} 元净利</text>')
        return ''.join(out)

    def _working_capital_body(self, financials: Mapping[str, Any]) -> str | None:
        """营运占用条：存货 / 应收账款各相当于多少收入，讲“钱压在哪”。

        Pure balance-sheet fact, no judgement — fits 风险 turns without touching
        the 画面红线 (no 买卖点位/收益暗示 anywhere on screen).
        """
        def metric(label: str) -> float | None:
            value = financials.get(label)
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None

        revenue = metric("营业收入(亿元)")
        rows = [(label, metric(label)) for label in ("存货(亿元)", "应收账款(亿元)")]
        rows = [(label.replace("(亿元)", ""), value) for label, value in rows if value]
        if not revenue or not rows:
            return None
        period = str(financials.get("报告期") or "")
        out = [self._svg_header("钱压在哪 · 营运占用", f"报告期 {period}", "#91a8bf")]
        _, zone_right = self._chart_zone()
        bar_x, row_h, y0 = 712, 96, 150
        bar_max_w = max(330.0, zone_right - bar_x - 200.0)
        ratios = [(label, value, value / revenue * 100) for label, value in rows]
        top_ratio = max(r for _, _, r in ratios)
        tones = ("#ffd166", "#6ab7ff")
        for index, (label, value, ratio) in enumerate(ratios):
            y = y0 + index * row_h
            width = max(8.0, ratio / top_ratio * bar_max_w)
            out.append(f'<text x="700" y="{y + 24}" text-anchor="end" fill="#dbe7f5" font-size="24">{html.escape(label[:11])}</text>')
            out.append(f'<rect x="{bar_x}" y="{y}" width="{width:.1f}" height="34" rx="9" fill="{tones[index % 2]}" opacity=".82"/>')
            out.append(f'<text x="{bar_x + width + 12:.1f}" y="{y + 24}" fill="#dbe7f5" font-size="22">{value:,.2f}亿</text>')
            out.append(f'<text x="{bar_x + width + 12:.1f}" y="{y + 56}" fill="#91a8bf" font-size="19">占收入 {ratio:.0f}%</text>')
        parts = "、".join(f"{r:.0f} 元压在{label}" for label, _, r in ratios)
        out.append(f'<text x="442" y="456" fill="#ffd166" font-size="21">每 100 元年收入，{parts}</text>')
        return ''.join(out)

    # ------------------------------------------------------------------
    # 多年财务图表（数据源：F10「财务分析」块解析出的年度序列与三表科目）
    #
    # 全部要求**至少三期**数据：两个点连成的线会让人以为是走势，其实只是
    # 两个数字。数据不够就返回 None，由论点候选取下一张语义相邻的图。
    # ------------------------------------------------------------------
    @staticmethod
    def _finance_annual(f10: Mapping[str, Any]) -> tuple[list[str], Mapping[str, Any]]:
        profile = f10.get("profile") if isinstance(f10.get("profile"), Mapping) else {}
        history = profile.get("财务历史") if isinstance(profile.get("财务历史"), Mapping) else {}
        annual = history.get("年度") if isinstance(history.get("年度"), Mapping) else {}
        periods = [str(p) for p in (annual.get("报告期") or []) if str(p).strip()]
        metrics = annual.get("指标") if isinstance(annual.get("指标"), Mapping) else {}
        return periods, metrics

    @classmethod
    def _annual_points(cls, f10: Mapping[str, Any], name: str) -> list[tuple[str, float]]:
        """某个年度指标的有效点（报告期, 值）；没有就返回空表。"""
        periods, metrics = cls._finance_annual(f10)
        values = metrics.get(name) if isinstance(metrics, Mapping) else None
        if not isinstance(values, list) or len(values) != len(periods):
            return []
        return [(periods[index], float(value)) for index, value in enumerate(values)
                if isinstance(value, (int, float)) and not isinstance(value, bool)]

    def _profit_trend_body(self, f10: Mapping[str, Any]) -> str | None:
        """历年归母净利润（柱）+ 营业收入（折线，独立刻度）。

        两条序列量级差一个数量级，共用一根 y 轴会让利润柱缩成一条线，所以
        营收走独立刻度画在上方（画面里明确标了单位，不靠视觉暗示）。
        """
        profit = self._annual_points(f10, "归母净利润(亿元)")
        if len(profit) < 3:
            return None
        revenue = dict(self._annual_points(f10, "营业总收入(亿元)"))
        zone_left, zone_right = self._chart_zone()
        plot_left, plot_right = zone_left + 34.0, zone_right - 34.0
        top, bottom = 190.0, 392.0
        values = [value for _, value in profit]
        lo, hi = min(values + [0.0]), max(values + [0.0])
        span = (hi - lo) or 1.0
        lo -= span * 0.16
        hi += span * 0.16
        scale = lambda value: top + (hi - value) / (hi - lo) * (bottom - top)
        zero_y = scale(0.0)
        step = (plot_right - plot_left) / len(profit)
        bar_w = min(104.0, step * 0.52)
        out = [self._svg_header("业绩走势 · 归母净利润", "年报 · 亿元")]
        out.append(f'<line x1="{plot_left:.1f}" y1="{zero_y:.1f}" x2="{plot_right:.1f}" y2="{zero_y:.1f}"'
                   ' stroke="rgba(101,151,196,.45)" stroke-width="1.6" stroke-dasharray="6 5"/>')
        for index, (period, value) in enumerate(profit):
            cx = plot_left + (index + 0.5) * step
            bar_top = scale(value) if value >= 0 else zero_y
            height = max(4.0, abs(scale(value) - zero_y))
            tone = "#ff7188" if value >= 0 else "#5ee0a7"
            out.append(f'<rect x="{cx - bar_w / 2:.1f}" y="{bar_top:.1f}" width="{bar_w:.1f}"'
                       f' height="{height:.1f}" rx="8" fill="{tone}" opacity=".85"/>')
            label_y = scale(value) - 16 if value >= 0 else scale(value) + 32
            out.append(f'<text x="{cx:.1f}" y="{label_y:.1f}" text-anchor="middle" fill="#dbe7f5"'
                       f' font-size="24" font-weight="700">{value:,.2f}</text>')
            out.append(f'<text x="{cx:.1f}" y="{bottom + 40:.1f}" text-anchor="middle" fill="#91a8bf"'
                       f' font-size="21">{period[:4]}</text>')
        if len(revenue) == len(profit) and all(period in revenue for period, _ in profit):
            series = [revenue[period] for period, _ in profit]
            r_lo, r_hi = min(series), max(series)
            r_span = (r_hi - r_lo) or 1.0
            r_scale = lambda value: 172.0 - (value - r_lo) / r_span * 68.0
            points = [(plot_left + (index + 0.5) * step, r_scale(value))
                      for index, value in enumerate(series)]
            out.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in points)}" fill="none"'
                       ' stroke="#6ab7ff" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" opacity=".9"/>')
            for (cx, cy), value in zip(points, series):
                out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="6" fill="#6ab7ff" stroke="#04121d" stroke-width="2"/>')
                out.append(f'<text x="{cx:.1f}" y="{cy - 14:.1f}" text-anchor="middle" fill="#91a8bf"'
                           f' font-size="18">{value:,.1f}</text>')
            out.append(f'<text x="{plot_left:.1f}" y="110" fill="#6ab7ff" font-size="19">蓝线：营业收入（亿元）</text>')
        return ''.join(out)

    def _percent_trend_body(self, f10: Mapping[str, Any], metric: str, title: str,
                            note: str, tone: str) -> str | None:
        """单指标多年走势折线（毛利率/净资产收益率共用），每点标真实数值。"""
        points = self._annual_points(f10, metric)
        if len(points) < 3:
            return None
        unit = "%" if metric.endswith("(%)") else ""
        zone_left, zone_right = self._chart_zone()
        plot_left, plot_right = zone_left + 52.0, zone_right - 52.0
        top, bottom = 158.0, 378.0
        values = [value for _, value in points]
        lo, hi = min(values), max(values)
        span = (hi - lo) or max(abs(hi) * 0.25, 1.0)
        lo -= span * 0.26
        hi += span * 0.26
        scale = lambda value: top + (hi - value) / (hi - lo) * (bottom - top)
        step = (plot_right - plot_left) / max(1, len(points) - 1)
        coords = [(plot_left + index * step, scale(value)) for index, (_, value) in enumerate(points)]
        out = [self._svg_header(title, note)]
        for _, value in (points[0], points[-1]):
            gy = scale(value)
            out.append(f'<line x1="{plot_left:.1f}" y1="{gy:.1f}" x2="{plot_right:.1f}" y2="{gy:.1f}"'
                       ' stroke="rgba(101,151,196,.22)" stroke-width="1.2" stroke-dasharray="4 5"/>')
        out.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in coords)}" fill="none"'
                   f' stroke="{tone}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>')
        for (period, value), (cx, cy) in zip(points, coords):
            out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="8" fill="{tone}" stroke="#04121d" stroke-width="2"/>')
            out.append(f'<text x="{cx:.1f}" y="{cy - 22:.1f}" text-anchor="middle" fill="#dbe7f5"'
                       f' font-size="23" font-weight="700">{value:g}{unit}</text>')
            out.append(f'<text x="{cx:.1f}" y="{bottom + 46:.1f}" text-anchor="middle" fill="#91a8bf"'
                       f' font-size="21">{period[:4]}</text>')
        return ''.join(out)

    _CASHFLOW_SERIES: tuple[tuple[str, str, str], ...] = (
        ("经营活动现金流量净额(亿元)", "经营", "#ffd166"),
        ("投资活动现金流量净额(亿元)", "投资", "#6ab7ff"),
        ("筹资活动现金流量净额(亿元)", "筹资", "#c9a0ff"),
    )

    def _cashflow_statement_body(self, f10: Mapping[str, Any]) -> str | None:
        """现金流量表：近三年经营/投资/筹资三活动的净流入流出。

        正负号是这张图的全部意义（经营为负、靠筹资补血 vs 经营自分红），
        所以柱以零线为基准双向生长，正值暖色、负值冷色，不靠颜色暗示好坏。
        """
        series = [(label, tone, self._annual_points(f10, name)[-3:])
                  for name, label, tone in self._CASHFLOW_SERIES]
        series = [item for item in series if len(item[2]) >= 2]
        if len(series) < 2:
            return None
        periods = [period for period, _ in series[0][2]]
        count = len(periods)
        zone_left, zone_right = self._chart_zone()
        plot_left, plot_right = zone_left + 40.0, zone_right - 40.0
        top, bottom = 168.0, 386.0
        all_values = [value for _, _, points in series for _, value in points]
        lo, hi = min(all_values + [0.0]), max(all_values + [0.0])
        span = (hi - lo) or 1.0
        lo -= span * 0.14
        hi += span * 0.14
        scale = lambda value: top + (hi - value) / (hi - lo) * (bottom - top)
        zero_y = scale(0.0)
        group_w = (plot_right - plot_left) / count
        bar_w = min(56.0, group_w * 0.24)
        out = [self._svg_header("现金流三张账 · 近三年", "亿元 · 正负为净流入/净流出")]
        out.append(f'<line x1="{plot_left:.1f}" y1="{zero_y:.1f}" x2="{plot_right:.1f}" y2="{zero_y:.1f}"'
                   ' stroke="rgba(101,151,196,.5)" stroke-width="1.6"/>')
        legend_x = plot_left
        for _, label, tone in self._CASHFLOW_SERIES:
            if not any(label == item[0] for item in series):
                continue
            out.append(f'<rect x="{legend_x:.1f}" y="100" width="20" height="20" rx="5" fill="{tone}"/>')
            out.append(f'<text x="{legend_x + 30:.1f}" y="117" fill="#91a8bf" font-size="20">{label}</text>')
            legend_x += 150.0
        for index, period in enumerate(periods):
            group_center = plot_left + (index + 0.5) * group_w
            span_w = bar_w * len(series) + 12 * (len(series) - 1)
            cursor = group_center - span_w / 2
            for label, tone, points in series:
                value = dict(points).get(period)
                if value is None:
                    cursor += bar_w + 12
                    continue
                bar_top = scale(value) if value >= 0 else zero_y
                height = max(3.0, abs(scale(value) - zero_y))
                out.append(f'<rect x="{cursor:.1f}" y="{bar_top:.1f}" width="{bar_w:.1f}"'
                           f' height="{height:.1f}" rx="6" fill="{tone}" opacity=".88"/>')
                label_y = scale(value) - 14 if value >= 0 else scale(value) + 28
                out.append(f'<text x="{cursor + bar_w / 2:.1f}" y="{label_y:.1f}" text-anchor="middle"'
                           f' fill="#dbe7f5" font-size="19" font-weight="700">{value:,.2f}</text>')
                cursor += bar_w + 12
            out.append(f'<text x="{group_center:.1f}" y="{bottom + 44:.1f}" text-anchor="middle"'
                       f' fill="#91a8bf" font-size="21">{period[:4]}</text>')
        return ''.join(out)

    def _balance_sheet_body(self, f10: Mapping[str, Any]) -> str | None:
        """资产负债表：挑本期变动最剧烈的几个资产/负债科目，本期 vs 上期对比。"""
        profile = self._f10_profile(f10)
        history = profile.get("财务历史") if isinstance(profile.get("财务历史"), Mapping) else {}
        changes = history.get("科目变动") if isinstance(history.get("科目变动"), Mapping) else {}
        rows = [row for row in (changes.get("明细") or []) if isinstance(row, Mapping)]
        watch = ("货币资金", "存货", "应收款项", "合同负债", "短期借款", "长期借款",
                 "固定资产", "在建工程", "研发投入", "长期股权投资")
        picked = [row for row in rows if row.get("科目") in watch
                  and isinstance(row.get("本期(亿元)"), (int, float))
                  and isinstance(row.get("上期(亿元)"), (int, float))
                  and isinstance(row.get("变动率(%)"), (int, float))]
        picked.sort(key=lambda row: -abs(float(row["变动率(%)"])))
        picked = picked[:4]
        if len(picked) < 2:
            return None
        zone_left, zone_right = self._chart_zone()
        row_h, y0 = 82, 136
        bar_x = zone_left + 250.0
        bar_max = max(180.0, (zone_right - bar_x) - 250.0)
        top_value = max(max(float(row["本期(亿元)"]), float(row["上期(亿元)"])) for row in picked) or 1.0
        period = str(changes.get("报告期") or "")
        out = [self._svg_header("资产与负债 · 本期对比", f"{period} vs 上期 · 亿元")]
        for index, row in enumerate(picked):
            y = y0 + index * row_h
            current, previous = float(row["本期(亿元)"]), float(row["上期(亿元)"])
            rate = float(row["变动率(%)"])
            tone = "#ffd166" if rate >= 0 else "#6ab7ff"
            out.append(f'<text x="{zone_left + 30:.1f}" y="{y + 42:.1f}" fill="#dbe7f5"'
                       f' font-size="25" font-weight="700">{html.escape(str(row["科目"])[:8])}</text>')
            for offset, (label, value, colour) in enumerate((("上期", previous, "rgba(105,183,255,.35)"),
                                                              ("本期", current, tone))):
                by = y + offset * 30
                width = max(10.0, value / top_value * bar_max)
                out.append(f'<rect x="{bar_x:.1f}" y="{by:.1f}" width="{width:.1f}" height="24"'
                           f' rx="7" fill="{colour}" opacity=".85"/>')
                out.append(f'<text x="{bar_x + width + 14:.1f}" y="{by + 19:.1f}" fill="#dbe7f5"'
                           f' font-size="20">{label} {value:,.2f}</text>')
            out.append(f'<text x="{zone_right:.1f}" y="{y + 42:.1f}" text-anchor="end" fill="{tone}"'
                       f' font-size="26" font-weight="700">{rate:+.1f}%</text>')
        return ''.join(out)

    def _industry_rank_body(self, f10: Mapping[str, Any]) -> str | None:
        """行业排名标尺：一根刻度尺一个维度，标出本股在同行里的真实位置。

        Data comes verbatim from F10 行业分析 ranking tables (top-30 rows naming
        this stock).  A dimension the stock is not listed in simply gets no
        ruler — nothing is inferred.
        """
        profile = self._f10_profile(f10)
        ranking = profile.get("行业地位")
        ranking = ranking if isinstance(ranking, Mapping) else {}
        peers = ranking.get("同行家数")
        try:
            peers = int(peers)
        except (TypeError, ValueError):
            return None
        dims = [(label, entry.get("排名")) for label, entry in ranking.items()
                if isinstance(entry, Mapping) and isinstance(entry.get("排名"), int)]
        dims = dims[:4]
        if not dims:
            return None
        industry = str(ranking.get("研究行业") or "")
        out = [self._svg_header("行业里站哪 · 排名", f"{industry} 同行 {peers} 家", "#91a8bf")]
        zone_right = self._chart_zone()[1]
        track_left, track_right, row_h, y0 = 620, zone_right - 50, 96, 140
        for index, (label, rank) in enumerate(dims):
            y = y0 + index * row_h
            ratio = min(1.0, max(0.0, (rank - 1) / max(1, peers - 1)))
            cx = track_left + ratio * (track_right - track_left)
            tone = "#ffd166" if rank <= 3 else "#6ab7ff"
            out.append(f'<text x="700" y="{y - 18}" fill="#dbe7f5" font-size="24">{html.escape(label)}</text>')
            out.append(f'<text x="{zone_right:g}" y="{y - 18}" text-anchor="end" fill="{tone}" font-size="24" font-weight="700">第 {rank} 名</text>')
            out.append(f'<line x1="{track_left}" y1="{y + 12}" x2="{track_right}" y2="{y + 12}" stroke="rgba(101,151,196,.35)" stroke-width="3" stroke-linecap="round"/>')
            out.append(f'<line x1="{track_left}" y1="{y + 2}" x2="{track_left}" y2="{y + 22}" stroke="rgba(101,151,196,.5)" stroke-width="2"/>')
            out.append(f'<line x1="{track_right}" y1="{y + 2}" x2="{track_right}" y2="{y + 22}" stroke="rgba(101,151,196,.5)" stroke-width="2"/>')
            out.append(f'<circle cx="{cx:.1f}" cy="{y + 12}" r="11" fill="{tone}" stroke="#04121d" stroke-width="2"/>')
            out.append(f'<text x="{track_left}" y="{y + 46}" fill="#91a8bf" font-size="18">第 1 名</text>')
            out.append(f'<text x="{track_right}" y="{y + 46}" text-anchor="end" fill="#91a8bf" font-size="18">第 {peers} 名</text>')
        return ''.join(out)

    def _news_timeline_body(self, news: Mapping[str, Any], verify: str = "") -> str:
        """新闻时间线：竖轴 + 日期节点，比旧列表更像「事情在发生」。

        ``verify`` 是这一章「待验证什么」的标签（来自计划里末章的
        ``visual.verify``）：末章的落点不是结论，是"下一份报告该盯哪个数"，
        画面上得看得见它。空就不画——编一条待验证项和编一个数字是同一条红线。
        """
        items = [x for x in (news.get("items") if isinstance(news.get("items"), list) else [])
                 if isinstance(x, Mapping) and str(x.get("title") or "").strip()][:4]
        zone_left, zone_right = self._chart_zone()
        if not items:
            cx = (zone_left + zone_right) / 2
            out = [f'<text x="{cx:g}" y="250" text-anchor="middle" class="snap-label" style="font-size:22px">'
                   '近期没有相关资讯，保持跟踪</text>']
            if verify:
                out.append(f'<text x="{cx:g}" y="330" text-anchor="middle" fill="#ffd166" font-size="22">'
                           f'◆ 待验证：{html.escape(verify)}</text>')
            return ''.join(out)
        out = [self._svg_header("最近发生的事")]
        axis_x, y0, step = zone_left + 28, 124, 92
        title_cap = 22 if zone_right < 1500 else 44
        out.append(f'<line x1="{axis_x}" y1="{y0 - 14}" x2="{axis_x}" y2="{y0 + (len(items) - 1) * step + 14}" stroke="rgba(101,151,196,.35)" stroke-width="2"/>')
        for index, item in enumerate(items):
            y = y0 + index * step
            date = str(item.get("publish_time") or "")
            date_label = date[5:10] if len(date) >= 10 else ""
            kind = str(item.get("type") or "资讯")
            out.append(f'<circle cx="{axis_x}" cy="{y}" r="7" fill="#ffd166" stroke="#04121d" stroke-width="2"/>')
            if date_label:
                out.append(f'<text x="{axis_x + 26}" y="{y - 10}" fill="#91a8bf" font-size="19">{html.escape(date_label)} · {html.escape(kind)}</text>')
            out.append(f'<text x="{axis_x + 26}" y="{y + 24}" fill="#dbe7f5" font-size="25" font-weight="600">{html.escape(str(item.get("title"))[:title_cap])}</text>')
        if verify:
            y = y0 + (len(items) - 1) * step + 66
            out.append(f'<text x="{axis_x + 26}" y="{y}" fill="#ffd166" font-size="22">'
                       f'◆ 待验证：{html.escape(verify)}</text>')
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

    # ------------------------------------------------------------------
    # 阶段 B2 三张新图：产品卡组 / 渠道链路 / 两数对照
    #
    # 共同的两条规矩：
    # 1. 数据只认 ``visual.data_binding`` 里绑好的素材与指标（mat/metric 契约）。
    #    没有计划时（旧脚本/单测）才退回 F10 原文——那是"图还在，计划丢了"的
    #    容错，不是给渲染器另开一条自己挑数据的路。
    # 2. 画不出就返回 None。缺数据**不是**画一张空框或一个猜的数字的理由。
    #
    # 横竖两版共用这一套渲染器：所有横向坐标都从 ``_chart_zone()`` 推出来，
    # 竖版图表区 1082px、横版 1480px，卡片宽度跟着走。
    # ------------------------------------------------------------------
    @staticmethod
    def _bound_materials(ctx: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        """按 ``visual.data_binding`` 取出这一章绑好的素材与指标。"""
        plan = ctx.get("episode_plan")
        visual = ctx.get("visual")
        if not isinstance(plan, Mapping) or not isinstance(visual, Mapping):
            return [], []
        binding = visual.get("data_binding")
        binding = binding if isinstance(binding, Mapping) else {}
        wanted_materials = {str(x) for x in (binding.get("material_ids") or []) if str(x)}
        wanted_metrics = {str(x) for x in (binding.get("metric_ids") or []) if str(x)}
        materials: list[Mapping[str, Any]] = []
        metrics: list[Mapping[str, Any]] = []
        for material in plan.get("materials") or []:
            if not isinstance(material, Mapping) or str(material.get("id") or "") not in wanted_materials:
                continue
            materials.append(material)
            for metric in material.get("metrics") or []:
                if not isinstance(metric, Mapping):
                    continue
                if wanted_metrics and str(metric.get("id") or "") not in wanted_metrics:
                    continue
                metrics.append(metric)
        return materials, metrics

    @staticmethod
    def _metric_number(metric: Mapping[str, Any]) -> float | None:
        value = metric.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    @staticmethod
    def _ellipsis(text: str, limit: int) -> str:
        limit = max(1, int(limit))
        return text if len(text) <= limit else text[:limit - 1] + "…"

    def _composition_rows(self, ctx: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
        """F10 主营构成的原始行与报告期（计划缺失时的兜底数据源）。"""
        f10 = ctx.get("f10") if isinstance(ctx.get("f10"), Mapping) else {}
        composition = self._f10_profile(f10).get("主营构成")
        composition = composition if isinstance(composition, Mapping) else {}
        rows = [dict(row) for row in (composition.get("明细") or []) if isinstance(row, Mapping)]
        return rows, str(composition.get("报告期") or "")

    def _portfolio_cards(self, ctx: Mapping[str, Any]) -> tuple[list[tuple[str, float | None, float | None]], str]:
        """产品卡组的数据：(名称, 收入占比, 毛利率) + 报告期。"""
        materials, metrics = self._bound_materials(ctx)
        cards: list[tuple[str, float | None, float | None]] = []
        period = ""
        names: dict[str, str] = {}
        slots: dict[str, dict[str, float]] = {}
        for material in materials:
            for index, entity in enumerate(material.get("entities") or []):
                if isinstance(entity, Mapping) and str(entity.get("role") or "") == "product":
                    names[f"m.product.{index}"] = str(entity.get("name") or "")
        for metric in metrics:
            mid = str(metric.get("id") or "")
            if not mid.startswith("m.product."):
                continue
            value = self._metric_number(metric)
            if value is None:
                continue
            item, _, label = mid[len("m.product."):].partition(".")
            slot = slots.setdefault(f"m.product.{item}", {})
            if label == "收入占比":
                slot["share"] = value
            elif "毛利率" in label:
                slot["margin"] = value
            period = period or str(metric.get("period") or "")
        cards = [(names.get(item, ""), slot.get("share"), slot.get("margin"))
                 for item, slot in slots.items()]
        # 计划丢了但 F10 还在：图照出（图在、计划不在是可恢复的），数据源降级到原文。
        rows, f10_period = self._composition_rows(ctx)
        f10_cards: list[tuple[str, float | None, float | None]] = []
        for row in rows:
            name = str(row.get("项目") or row.get("名称") or row.get("业务") or "").strip()
            if not name or name.endswith(NON_PRODUCT_ROW_SUFFIXES):
                continue
            try:
                share = float(str(row.get("收入占比(%)") or "").replace("%", ""))
            except ValueError:
                share = None  # type: ignore[assignment]
            try:
                margin = float(str(row.get("毛利率(%)") or "").replace("%", ""))
            except ValueError:
                margin = None
            if share is None:
                continue
            f10_cards.append((name, share, margin))
        # 计划丢了才退 F10 原文：判据是"这一章没绑到任何指标"，不是"F10 的行更多"。
        # 用数量比较会让计划被静默绕过——B3 一旦按素材裁剪绑定（这期只讲两块业务），
        # 没绑定的业务就会自己爬回卡组上。
        if not metrics:
            cards, period = f10_cards, f10_period
        cards = [card for card in cards if card[1] is not None]
        cards.sort(key=lambda card: card[1] or 0.0, reverse=True)
        return cards, period

    def _channel_sides(self, ctx: Mapping[str, Any]) -> tuple[list[tuple[str, float | None]], list[tuple[str, float | None]], str]:
        """渠道链路的两侧：(销售模式行, 地区行, 报告期)。"""
        materials, metrics = self._bound_materials(ctx)
        channels: list[tuple[str, float | None]] = []
        regions: list[tuple[str, float | None]] = []
        period = ""
        roles: dict[str, tuple[str, str]] = {}
        shares: dict[str, float] = {}
        for material in materials:
            for index, entity in enumerate(material.get("entities") or []):
                if not isinstance(entity, Mapping):
                    continue
                role = str(entity.get("role") or "")
                # 只认渠道/地区的实体：产品线素材绑在这一章时（R2 同时绑两类），
                # 把产品行当成"地区"画上去，卡片上就会出现「茅台酒」这种假渠道。
                if role in ("channel", "region"):
                    roles[f"m.channel.{index}"] = (str(entity.get("name") or ""), role)
        for metric in metrics:
            mid = str(metric.get("id") or "")
            if not mid.startswith("m.channel."):
                continue
            value = self._metric_number(metric)
            if value is None:
                continue
            item, _, label = mid[len("m.channel."):].partition(".")
            if label == "收入占比":
                shares[f"m.channel.{item}"] = value
                period = period or str(metric.get("period") or "")
        for key, (name, role) in roles.items():
            if not name:
                continue
            (channels if role == "channel" else regions).append((name, shares.get(key)))
        rows, f10_period = self._composition_rows(ctx)
        f10_channels: list[tuple[str, float | None]] = []
        f10_regions: list[tuple[str, float | None]] = []
        for row in rows:
            name = str(row.get("项目") or row.get("名称") or row.get("业务") or "").strip()
            if not name:
                continue
            try:
                share = float(str(row.get("收入占比(%)") or "").replace("%", ""))
            except ValueError:
                share = None  # type: ignore[assignment]
            if name.endswith("(销售模式)"):
                f10_channels.append((name[:-len("(销售模式)")], share))
            elif name.endswith("(地区)"):
                f10_regions.append((name[:-len("(地区)")], share))
        # 同上：只有这一章没绑到任何指标时才退 F10 原文。绑定存在却比 F10 窄，
        # 正是计划主动裁掉的结果，画面必须跟着计划走。
        if not metrics:
            channels, regions, period = f10_channels, f10_regions, f10_period
        for side in (channels, regions):
            side.sort(key=lambda item: item[1] or 0.0, reverse=True)
        return channels, regions, period

    def _ledger_pair(self, ctx: Mapping[str, Any]) -> tuple[str, str, list[tuple[str, float]]] | None:
        """两数对照要用的那一对：(指标名, 单位, [(报告期, 值), (报告期, 值)])。

        同一条指标的两期才算"对照"：把两个不同指标并排放是两张互不相干的数，
        观众会自己脑补它们之间的关系——那正是"念数字"的观感来源。
        """
        _, metrics = self._bound_materials(ctx)
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for metric in metrics:
            label = str(metric.get("label") or "").strip()
            if not label or self._metric_number(metric) is None:
                continue
            groups.setdefault(label, []).append(metric)
        pairs = [(label, sorted(items, key=lambda m: str(m.get("period") or ""))[-2:])
                 for label, items in groups.items() if len(items) >= 2]
        # anchor 优先：visual_only 的公司级毛利率是"只上屏"的，不该当对照的主角
        pairs.sort(key=lambda pair: 0 if any(str(m.get("spoken_policy")) == "anchor"
                                             for m in pair[1]) else 1)
        if pairs:
            label, items = pairs[0]
            return (label, str(items[0].get("unit") or ""),
                    [(str(m.get("period") or ""), self._metric_number(m) or 0.0) for m in items])
        # 计划丢了：退回 F10 年度序列的归母净利润
        f10 = ctx.get("f10") if isinstance(ctx.get("f10"), Mapping) else {}
        points = self._annual_points(f10, "归母净利润(亿元)")
        return ("归母净利润", "亿元", points[-2:]) if len(points) >= 2 else None

    def _product_portfolio_body(self, ctx: Mapping[str, Any]) -> str | None:
        """产品卡组：一张卡一个产品，卡上是名称 + 收入占比 + 毛利率条。

        两张卡以下就不叫"卡组"——只讲得出一块业务时，观众要的是那块业务的
        结构，交给收入构成图（fallback 的第一位）。
        """
        cards, period = self._portfolio_cards(ctx)
        cards = [card for card in cards if card[1] is not None][:4]
        if len(cards) < 2:
            return None
        zone_left, zone_right = self._chart_zone()
        gap = 18.0
        card_w = (zone_right - zone_left - gap * (len(cards) - 1)) / len(cards)
        top, height = 108.0, 286.0
        out = [self._svg_header("卖的是什么 · 产品卡组", f"报告期 {period}")]
        tones = ("#ffd166", "#6ab7ff", "#8fd3ff", "#5ee0a7")
        for index, (name, share, margin) in enumerate(cards):
            x = zone_left + index * (card_w + gap)
            tone = tones[index % len(tones)]
            out.append(f'<rect x="{x:.1f}" y="{top}" width="{card_w:.1f}" height="{height}" rx="20" '
                       f'fill="rgba(12,30,52,.92)" stroke="{tone}" stroke-width="1.6"/>')
            out.append(f'<text x="{x + 18:.1f}" y="{top + 46}" fill="#dbe7f5" font-size="24">'
                       f'{html.escape(self._ellipsis(short_business_label(name), (card_w - 36) / 24))}</text>')
            out.append(f'<text x="{x + 18:.1f}" y="{top + 128}" fill="{tone}" font-size="52" '
                       f'font-weight="700">{share:.1f}%</text>')
            out.append(f'<text x="{x + 18:.1f}" y="{top + 166}" fill="#91a8bf" font-size="19">收入占比</text>')
            if margin is None:
                out.append(f'<text x="{x + 18:.1f}" y="{top + 224}" fill="#91a8bf" font-size="20">毛利率未披露</text>')
                continue
            out.append(f'<text x="{x + 18:.1f}" y="{top + 222}" fill="#dbe7f5" font-size="25">毛利 {margin:.1f}%</text>')
            # 毛利条：同一张卡里和占比同尺度，一眼看出这块"卖得多但赚得少"
            track_w = max(40.0, card_w - 36.0)
            out.append(f'<rect x="{x + 18:.1f}" y="{top + 238}" width="{track_w:.1f}" height="10" rx="5" fill="rgba(101,151,196,.25)"/>')
            out.append(f'<rect x="{x + 18:.1f}" y="{top + 238}" width="{max(4.0, min(1.0, margin / 100.0) * track_w):.1f}" '
                       f'height="10" rx="5" fill="{tone}" opacity=".85"/>')
        return ''.join(out)

    def _channel_flow_body(self, ctx: Mapping[str, Any]) -> str | None:
        """渠道链路：厂商 → 渠道 → 客户（按地区）。

        缺一侧就画半条链：那一格画成虚线「未披露」，箭头也改成虚线——明确告诉
        观众"这一段没数"，而不是把两头直接连起来假装中间不存在。两侧都没数
        就返回 None（那该由业务拆解图来讲，不是这张图）。
        """
        channels, regions, period = self._channel_sides(ctx)
        if not channels and not regions:
            return None
        zone_left, zone_right = self._chart_zone()
        col_w = (zone_right - zone_left) / 3.0
        box_w = max(150.0, col_w - 44.0)
        box_h, box_gap, top = 64.0, 14.0, 150.0
        tone = "#6ab7ff"
        out = [self._svg_header("货怎么到客户手上 · 渠道链路", f"主营构成 · {period}")]

        def column(index: int, title: str, items: list[tuple[str, float | None]]) -> None:
            cx = zone_left + col_w * (index + 0.5)
            out.append(f'<text x="{cx:.1f}" y="126" text-anchor="middle" fill="#91a8bf" font-size="22">'
                       f'{html.escape(title)}</text>')
            if not items:
                y = top
                out.append(f'<rect x="{cx - box_w / 2:.1f}" y="{y}" width="{box_w:.1f}" height="{box_h}" rx="16" '
                           f'fill="rgba(12,30,52,.55)" stroke="rgba(101,151,196,.55)" stroke-width="1.6" stroke-dasharray="7 6"/>')
                out.append(f'<text x="{cx:.1f}" y="{y + 40}" text-anchor="middle" fill="#91a8bf" font-size="21">未披露</text>')
                return
            for row, (name, share) in enumerate(items[:3]):
                y = top + row * (box_h + box_gap)
                out.append(f'<rect x="{cx - box_w / 2:.1f}" y="{y:.1f}" width="{box_w:.1f}" height="{box_h}" rx="16" '
                           f'fill="rgba(12,30,52,.92)" stroke="{tone}" stroke-width="1.6"/>')
                chars = max(4, int((box_w - 90) / 22))
                out.append(f'<text x="{cx - box_w / 2 + 16:.1f}" y="{y + 40:.1f}" fill="#dbe7f5" font-size="23">'
                           f'{html.escape(self._ellipsis(short_business_label(name), chars))}</text>')
                if share is not None:
                    out.append(f'<text x="{cx + box_w / 2 - 16:.1f}" y="{y + 40:.1f}" text-anchor="end" fill="{tone}" '
                               f'font-size="23" font-weight="700">{share:.0f}%</text>')

        def link(from_index: int, to_index: int, broken: bool) -> None:
            x1 = zone_left + col_w * (from_index + 1) - 16.0
            x2 = zone_left + col_w * to_index + 16.0
            y = top + box_h / 2
            dash = ' stroke-dasharray="7 6"' if broken else ''
            out.append(f'<line x1="{x1:.1f}" y1="{y}" x2="{x2:.1f}" y2="{y}" stroke="rgba(101,151,196,.5)" '
                       f'stroke-width="2"{dash}/>')
            if not broken:
                out.append(f'<polygon points="{x2:.1f},{y:.1f} {x2 - 13:.1f},{y - 7:.1f} {x2 - 13:.1f},{y + 7:.1f}" '
                           f'fill="rgba(101,151,196,.7)"/>')

        vendor = str((ctx.get("quote") or {}).get("name") or "该公司")
        column(0, "厂商", [(vendor, None)])
        column(1, "渠道 · 销售模式", channels)
        column(2, "客户 · 地区", regions)
        link(0, 1, not channels)
        link(1, 2, not regions)
        return ''.join(out)

    def _ledger_pair_body(self, ctx: Mapping[str, Any]) -> str | None:
        """两数对照：同一条指标的两期并排，中间给出真实差额。

        R4 的"账有没有配合故事"要的就是这两期的差——一条曲线讲的是趋势，
        两个数字并排讲的是"变了没有、变了多少"。
        """
        pair = self._ledger_pair(ctx)
        if not pair:
            return None
        label, unit, points = pair
        zone_left, zone_right = self._chart_zone()
        gap = 40.0
        width = (zone_right - zone_left - gap) / 2
        top, height = 132.0, 236.0
        out = [self._svg_header("账有没有配合 · 两数对照", f"{label}（{unit}）".rstrip("（）"))]
        for index, (period, value) in enumerate(points):
            x = zone_left + index * (width + gap)
            tone = "#6ab7ff" if index == 0 else "#ffd166"
            out.append(f'<rect x="{x:.1f}" y="{top}" width="{width:.1f}" height="{height}" rx="20" '
                       f'fill="rgba(12,30,52,.92)" stroke="{tone}" stroke-width="1.6"/>')
            out.append(f'<text x="{x + 26:.1f}" y="{top + 50}" fill="#91a8bf" font-size="21">'
                       f'{html.escape(str(period) or "报告期缺失")}</text>')
            out.append(f'<text x="{x + 26:.1f}" y="{top + 138}" fill="{tone}" font-size="58" font-weight="700">'
                       f'{value:,.2f}</text>')
            out.append(f'<text x="{x + 26:.1f}" y="{top + 186}" fill="#91a8bf" font-size="21">'
                       f'{html.escape(unit or "—")}</text>')
        first, last = points[0][1], points[1][1]
        delta = last - first
        pct = (delta / abs(first) * 100) if first else 0.0
        tone = "#ff7188" if delta < 0 else "#5ee0a7"
        out.append(f'<text x="{zone_left:g}" y="{top + height + 52}" fill="{tone}" font-size="24">'
                   f'◆ {html.escape(label)} {delta:+,.2f}{html.escape(unit)}'
                   f'（{pct:+.1f}%）{html.escape(str(points[0][0]))} → {html.escape(str(points[1][0]))}</text>')
        return ''.join(out)

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
