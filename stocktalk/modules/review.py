"""审查页：把一次生成的成片、封面与发布文案汇总成一个本地 HTML。

生成流程跑完后，人真正需要过目的是三样东西：画面（成片）、信息流里的样子
（封面缩略图）、以及要发出去的文案。散落在 ``output/`` 里的 MP4 与 PNG 得逐个
双击才能看全，所以这里把它们收进一张自包含的页面（``<tag>.review.html``），
和成片同目录、双击即开，确认无误再走 ``--publish-only`` 发布。

页面只是**审查辅助**：生成它失败绝不中断主流程（封面/视频都已经落盘）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from stocktalk.modules.cover import COVER_PRESETS
from stocktalk.modules.publisher import DEFAULT_SPEC, PLATFORM_LABELS, PLATFORM_SPECS

# 画幅的展示名。视频与封面共用同一套词，免得审查页和命令行摘要各说各的。
CANVAS_LABELS: dict[str, str] = {"horizontal": "桌面版（横屏）", "vertical": "手机版（竖屏）"}
COVER_LABELS: dict[str, str] = {"portrait": "竖版封面", "landscape": "横版封面（4:3）", "wide": "横版封面（16:9）"}

# 宽高比（宽/高）。审查页按它分配列宽：同一行里 flex-grow ∝ 宽高比，
# 画面高度便天然对齐——横版分到更宽的列，竖版窄列，一行卡片齐平。
VIDEO_ASPECTS: dict[str, float] = {"horizontal": 16 / 9, "vertical": 9 / 16}
COVER_ASPECTS: dict[str, float] = {"portrait": 9 / 16, "landscape": 4 / 3, "wide": 16 / 9}
DEFAULT_ASPECT = 16 / 9


def _aspect(value: str, table: dict[str, float]) -> str:
    return f"{table.get(value, DEFAULT_ASPECT):.4f}"


def canvas_of(path: str | Path, default: str) -> str:
    """Read the canvas back out of ``<tag>.<canvas>.mp4``; the master has none."""
    suffixes = Path(path).suffixes
    if len(suffixes) >= 2 and suffixes[-2].lstrip(".") in CANVAS_LABELS:
        return suffixes[-2].lstrip(".")
    return default


def video_inventory(master: str | Path, platform_videos: Mapping[str, str],
                    platform_canvas: Mapping[str, str], main_canvas: str = "") -> list[dict[str, Any]]:
    """One entry per rendered canvas, naming the platforms each one feeds.

    Every canvas cut is rendered next to the master (``<tag>.<canvas>.mp4``), so
    a summary that only names the master makes the mobile cut look missing.
    """
    master = str(master)
    canvas_by_platform = {p: str(c) for p, c in (platform_canvas or {}).items()}
    cuts = {p: str(path) for p, path in (platform_videos or {}).items()}
    main = str(main_canvas or "") or canvas_of(master, "")
    order: list[str] = []
    info: dict[str, dict[str, Any]] = {}

    def add(path: str, canvas: str) -> None:
        if path not in info:
            info[path] = {"canvas": canvas, "platforms": []}
            order.append(path)

    add(master, main)
    for platform, path in cuts.items():
        add(path, canvas_by_platform.get(platform) or canvas_of(path, main))
    for platform, canvas in canvas_by_platform.items():
        label = PLATFORM_LABELS.get(platform, platform)
        cut = cuts.get(platform)
        if cut is not None:
            info[cut]["platforms"].append(label)
        elif canvas in (main, ""):
            info[master]["platforms"].append(label)
    return [
        {
            "path": path,
            "file": Path(path).name,
            "canvas": info[path]["canvas"],
            "label": CANVAS_LABELS.get(info[path]["canvas"], "主片"),
            "platforms": "、".join(info[path]["platforms"]),
            "aspect": _aspect(info[path]["canvas"], VIDEO_ASPECTS),
        }
        for path in order
    ]


def cover_usage(platforms: Sequence[str]) -> dict[str, list[str]]:
    """Preset -> the platforms that consume it, for the caption under each cover."""
    usage: dict[str, list[str]] = {}
    for platform in platforms:
        spec = PLATFORM_SPECS.get(platform, DEFAULT_SPEC)
        for _, preset in spec.get("covers", ()):
            usage.setdefault(preset, []).append(PLATFORM_LABELS.get(platform, platform))
    return usage


def _cover_entries(covers: Mapping[str, str], platforms: Sequence[str]) -> list[dict[str, str]]:
    usage = cover_usage(platforms)
    entries: list[dict[str, str]] = []
    for preset, path in covers.items():
        size = COVER_PRESETS.get(preset)
        entries.append({
            "file": Path(path).name,
            "label": COVER_LABELS.get(preset, preset),
            "size": f"{size[0]}×{size[1]}" if size else "",
            "platforms": "、".join(usage.get(preset, ())),
            "aspect": _aspect(preset, COVER_ASPECTS),
        })
    return entries


def _environment() -> Environment:
    assets = Path(__file__).resolve().parents[1] / "templates" / "review"
    return Environment(loader=FileSystemLoader(str(assets)), autoescape=select_autoescape(["html", "j2"]))


def _join(items: Sequence[str] | str) -> str:
    if isinstance(items, str):
        return items
    return " ".join(str(item) for item in items if str(item).strip())


def render_review_html(*, tag: str, stock_name: str, stock_code: str, created_at: str,
                       turns: int, duration: float, videos: Sequence[Mapping[str, Any]],
                       covers: Sequence[Mapping[str, str]], canvas_rows: Sequence[Mapping[str, str]],
                       title: str, description: str, tags: str,
                       issues: Sequence[str], disclaimers: str) -> str:
    return _environment().get_template("review.html.j2").render(
        tag=tag, video_tag=tag, stock_name=stock_name, stock_code=stock_code, created_at=created_at,
        turns=turns, duration=f"{duration:.0f}" if duration else "—",
        videos=videos, covers=covers, canvas_rows=canvas_rows,
        title=title, description=description, tags=tags,
        issues=list(issues), disclaimers=disclaimers,
    )


def write_review_page(output_dir: str | Path, tag: str, *, stock_name: str, stock_code: str,
                      script: Mapping[str, Any], video_path: str | Path,
                      platform_videos: Mapping[str, str] | None = None,
                      covers: Mapping[str, str] | None = None,
                      platform_canvas: Mapping[str, str] | None = None,
                      platforms: Sequence[str] = (), main_canvas: str = "",
                      title: str = "", description: str = "", tags: Sequence[str] | str = (),
                      duration: float = 0.0, created_at: datetime | None = None) -> Path:
    """Write ``<tag>.review.html`` next to the MP4 and return its path."""
    platform_canvas = dict(platform_canvas or {})
    videos = video_inventory(video_path, platform_videos or {}, platform_canvas, main_canvas)
    rows = []
    for platform in platforms:
        canvas = platform_canvas.get(platform, "")
        rows.append({
            "label": PLATFORM_LABELS.get(platform, platform),
            "canvas": (CANVAS_LABELS.get(canvas) or CANVAS_LABELS.get(main_canvas) or "主片") if canvas or main_canvas else "—",
        })
    html = render_review_html(
        tag=tag, stock_name=stock_name, stock_code=stock_code,
        created_at=(created_at or datetime.now()).strftime("%Y-%m-%d %H:%M"),
        turns=len(script.get("turns", ()) or ()), duration=duration,
        videos=videos, covers=_cover_entries(covers or {}, platforms), canvas_rows=rows,
        title=title, description=description, tags=_join(tags),
        issues=[str(issue) for issue in script.get("compliance_issues", ()) or ()],
        disclaimers=_join(script.get("disclaimers", ()) or ()),
    )
    path = Path(output_dir) / f"{tag}.review.html"
    path.write_text(html, encoding="utf-8")
    return path
