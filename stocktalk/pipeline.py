#!/usr/bin/env python3
"""编排谈股论金的股票数据到视频生成流水线。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, TypeVar

import yaml
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from stocktalk.modules.compliance import ComplianceAgent
from stocktalk.modules.cover import CoverGenerator
from stocktalk.modules.dialogue_generator import (create_dialogue_generator,
                                                  dialogue_format,
                                                  ensure_narrator_character)
from stocktalk.modules.episode_planner import EpisodePlanner
from stocktalk.modules.hyperframes_builder import HyperFramesBuilder
from stocktalk.modules.publisher import PLATFORM_LABELS, SauPublisher, cover_sizes_for
from stocktalk.modules.review import write_review_page
from stocktalk.modules.stock_data import StockDataClient, StockDataConfig, StockDataIncompleteError
from stocktalk.modules.tts_agent import TTSAgent


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config") / "default.yaml"
T = TypeVar("T")


class PipelineError(RuntimeError):
    """A concise, user-facing failure from a named pipeline stage."""


def _merge_config(defaults: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge a user config onto the bundled defaults."""
    merged = dict(defaults)
    for key, value in overrides.items():
        merged[key] = _merge_config(merged[key], value) if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping) else value
    return merged


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load bundled defaults, optionally overlaid by a user config file.

    An explicitly requested file that does not exist is an error, not a silent
    fallback to the defaults: ``--config config.monologue.yaml`` run from the
    wrong directory would otherwise quietly produce a *debate* film, and the
    difference only shows up after ten minutes of rendering (形态在骨架、说话
    人、封面版式上都不同，静默降级是最难查的一类坑)。
    """
    with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as f:
        defaults = yaml.safe_load(f) or {}
    if path is None:
        return defaults
    requested = Path(path)
    if not requested.exists():
        raise FileNotFoundError(str(requested))
    with requested.open(encoding="utf-8") as f:
        return _merge_config(defaults, yaml.safe_load(f) or {})


class Pipeline:
    """端到端的谈股论金视频生成流水线。"""

    def __init__(self, config: dict[str, Any] | None = None, *, console: Console | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config or {}
        self.console = console or Console()
        self._sleep = sleep
        # 独白形态要在建任何 agent 之前补齐 characters.narrator：TTS 按角色取音色，
        # 缺角色会把该说话人的台词静默过滤掉（成片零配音且不报错）。
        ensure_narrator_character(self.config)
        # 形态由工厂决定（dialogue.format: debate | monologue）——生成器类型一旦
        # 分家，后面所有按形态分支的地方都必须和它取同一个值，不能各读一次 config。
        self.format = dialogue_format(self.config)
        self.stock_client = StockDataClient(StockDataConfig(
            retries=self.config.get("stock_data", {}).get("retries", 0),
            timeout_seconds=self.config.get("stock_data", {}).get("timeout_seconds", 60.0),
            news_limit=int(self.config.get("stock_data", {}).get("news_limit", 12)),
            news_days=int(self.config.get("stock_data", {}).get("news_days", 0)),
            require_company_data=bool(self.config.get("stock_data", {}).get("require_company_data", True)),
        ))
        self.dialogue_gen = create_dialogue_generator(self.config)
        self.episode_planner = EpisodePlanner()
        self.compliance = ComplianceAgent(self.config)
        self.tts = TTSAgent(self.config)
        self.builder = HyperFramesBuilder(self.config)
        self.publisher = SauPublisher(self.config)
        self.covers = CoverGenerator(self.config)
        self.output_dir = Path(self.config.get("output", {}).get("dir", "output"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _render_covers(self, stock_data: Mapping[str, Any], script: Mapping[str, Any],
                       stock_code: str, tag: str, progress=None, task=None) -> dict[str, str]:
        """Render the cover presets the configured platforms consume.

        Cover art is the one part of the run that is genuinely optional, so a
        missing Chrome or a failed screenshot degrades to publishing without a
        cover instead of throwing away a finished MP4.
        """
        if not self.config.get("cover", {}).get("enabled", True):
            return {}
        platforms = [p for p in self.config.get("publish", {}).get("platforms", ()) if p in PLATFORM_LABELS]
        sizes = cover_sizes_for(platforms)
        if not sizes:
            return {}
        if progress is not None and task is not None:
            progress.update(task, description=f"Rendering covers ({', '.join(sizes)})")
        try:
            rendered = self.covers.generate(stock_data, script, stock_code, sizes, self.output_dir, tag)
        except Exception as exc:
            self.console.print(f"[yellow]封面图生成失败，将不带封面发布: {exc}[/yellow]")
            return {}
        missing = [size for size in sizes if size not in rendered]
        if missing:
            self.console.print(f"[yellow]封面图缺失（{', '.join(missing)}），相关平台将不带封面发布[/yellow]")
        return {size: str(path) for size, path in rendered.items()}

    def _warn_if_over_budget(self, script: Mapping[str, Any]) -> None:
        """脚本估算时长明显超出上限时提前提醒。

        长片最容易出的偏差是模型多写几百字（2-5 分钟目标下就是多出一两分钟），
        渲染前提醒比渲完才发现便宜得多。只提醒不阻断：真要缩短，调
        ``dialogue.max_duration_seconds``（独白是 ``dialogue.monologue``）即可。

        上限取生成器的**生效值**而不是 config 顶层：独白形态的额度在
        ``dialogue.monologue`` 里，拿 300 秒当独白的上限会漏掉一整个区间。
        """
        try:
            cap = float(getattr(self.dialogue_gen.config, "max_duration_seconds", 0) or 0)
            estimated = float(script.get("estimated_seconds") or 0.0)
        except (TypeError, ValueError):
            return
        if cap and estimated > cap * 1.15:
            self.console.print(
                f"[yellow]脚本 {script.get('char_count')} 字，按实测语速约 {estimated:.0f} 秒，"
                f"超出目标上限 {cap:.0f} 秒——成片可能偏长，必要时调小 dialogue.max_duration_seconds。[/yellow]"
            )

    def _write_review(self, *, stock_code: str, stock_name: str, tag: str, script: Mapping[str, Any],
                      video_path: Path, platform_videos: Mapping[str, str],
                      covers: Mapping[str, str], duration: float = 0.0) -> Path | None:
        """把这次的成片 / 封面 / 发布文案汇总成一个本地审查页。

        审查页和封面一样属于锦上添花：任何失败都只打印一行提示，绝不打断
        主流程——视频与封面此时都已经落盘了。
        """
        try:
            metadata = self.publisher.metadata_gen.generate(script, stock_name, stock_code)
            publish = self.config.get("publish", {}) or {}
            return write_review_page(
                self.output_dir, tag,
                stock_name=stock_name or stock_code, stock_code=stock_code, script=script,
                video_path=video_path, platform_videos=platform_videos, covers=covers,
                platform_canvas=publish.get("platform_canvas", {}) or {},
                platforms=[str(p) for p in publish.get("platforms", ())],
                main_canvas=str(self.builder.video_config.get("canvas", "")),
                title=metadata.title, description=metadata.full_description, tags=metadata.tags,
                duration=duration,
            )
        except Exception as exc:
            self.console.print(f"[yellow]审查页生成失败（不影响成片）: {exc}[/yellow]")
            return None

    def render_covers(self, script: Mapping[str, Any], stock_code: str, tag: str,
                      stock_name: str = "") -> dict[str, str]:
        """Render cover art on its own, without a full generation run.

        ``--publish-only`` / ``--republish`` may fire on a video that was
        rendered without ever publishing, so no cover exists yet.  A cover
        with an empty chart and no price row shipped to douyin/kuaishou looks
        broken (2026-09-19 user report), so re-fetch the live quote/kline —
        all the template needs and cheap compared to a full run — and only
        fall back to a bare name if tongstock is unreachable.
        """
        quote: dict[str, Any] = {"name": stock_name}
        technical: dict[str, Any] = {}
        try:
            fetched = dict(self.stock_client.get_quote(stock_code) or {})
            quote = {key: value for key, value in fetched.items() if value not in (None, "")}
            # 走势曲线与最高/最低取自指标历史（cover.py `_bars`），不是 kline。
            technical = self.stock_client.get_indicators(stock_code, count=60)
            # 盘前/盘后 quote 常缺 change_pct，从指标历史补（与全量生成同源）。
            self.stock_client._enrich_quote(quote, technical)
        except Exception as exc:
            self.console.print(f"[yellow]封面行情补抓失败（{exc}），封面将缺价格与走势图[/yellow]")
        if not quote.get("name"):
            # get_quote 的文本输出经常不带名字；封面要顶着一个真实名字发出去。
            # stockinfo/F10 都是维护中的数据源，block show 的表头名可能落后
            # 行情数年（000066 上证商品→中国长城），这里绝不采用它。
            for resolver in (lambda: str(self.stock_client.get_stockinfo(str(stock_code)).get("name") or ""),
                             lambda: self.stock_client._f10_short_name(self.stock_client.get_f10(str(stock_code)))):
                try:
                    name = str(resolver() or "")
                except Exception:
                    continue
                if name:
                    quote["name"] = name
                    break
        quote.setdefault("name", stock_name)
        stock_data = {"quote": quote, "technical": technical}
        return self._render_covers(stock_data, script, stock_code, tag)

    def _retry(self, stage: str, operation: Callable[[], T]) -> T:
        """Retry a remote LLM/TTS operation with capped exponential backoff."""
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                return operation()
            except Exception as exc:  # Concrete remote-client errors vary by provider.
                last_error = exc
                if attempt == 3:
                    break
                delay = 2 ** (attempt - 1)
                self.console.print(f"[yellow]{stage} failed; retrying in {delay}s ({attempt}/3)…[/yellow]")
                self._sleep(delay)
        raise PipelineError(f"{stage} failed after 3 attempts: {last_error}") from last_error

    def _render_platform_cuts(self, stock_data: Mapping[str, Any], script: Mapping[str, Any],
                              audio: Mapping[str, Any], project_dir: Path, tag: str,
                              progress: Progress, task) -> dict[str, str]:
        """Render the desktop / mobile cuts the configured platforms ask for.

        ``publish.platform_canvas`` maps every platform to its preferred
        canvas, so the set of canvases named there is exactly the set of cuts
        rendered: a landscape master plus a vertical cut for 小红书 means two
        MP4s from one script.  A failed cut degrades to the master video
        rather than blocking the run.
        """
        platforms = [p for p in self.config.get("publish", {}).get("platforms", []) if p in PLATFORM_LABELS]
        canvas_map = dict(self.config.get("publish", {}).get("platform_canvas", {}) or {})
        main_canvas = str((self.config.get("video", {}) or {}).get("canvas", "vertical"))
        needed: dict[str, list[str]] = {}
        for platform in platforms:
            canvas = str(canvas_map.get(platform, main_canvas))
            if canvas != main_canvas:
                needed.setdefault(canvas, []).append(platform)
        cuts: dict[str, str] = {}
        for canvas, targets in needed.items():
            progress.update(task, description=f"Rendering {canvas} cut for {', '.join(targets)}")
            video_cfg = dict(self.config.get("video", {}) or {})
            # The cut inherits the master's caption mode: one-line captions are
            # the same on both canvases, and letting the cut drift to a
            # different mode would silently change the layout it was tuned for.
            video_cfg.update({"canvas": canvas, "project_dir": str(project_dir / canvas)})
            builder = HyperFramesBuilder({**self.config, "video": video_cfg})
            try:
                variant = builder.build(stock_data, script, audio)
                path = builder.render_mp4(variant, self.output_dir / f"{tag}.{canvas}.mp4")
            except Exception as exc:
                self.console.print(f"[yellow]{canvas} 分画幅渲染失败，{', '.join(targets)} 将使用主片发布: {exc}[/yellow]")
                continue
            for platform in targets:
                cuts[platform] = str(path)
        return cuts

    def _silent_timeline(self, script: Mapping[str, Any], srt_path: Path) -> dict[str, Any]:
        """Create subtitle timing for a silent render when TTS is unavailable.

        说话人白名单取 ``tts.speakers``（= resolve_speakers，配置里声明的角色
        都算），不写死 ("bull", "bear")：独白片的 narrator 会被硬编码的元组
        静默过滤掉——整条片没有字幕也没有任何提示。
        """
        segments: list[dict[str, Any]] = []
        cursor = 0.0
        for turn in script.get("turns", []):
            if not isinstance(turn, Mapping):
                continue
            character = str(turn.get("speaker", ""))
            line = str(turn.get("line", "")).strip()
            if character not in self.tts.speakers or not line:
                continue
            # 0.23s/字 ≈ 4.3 字/秒。上限从 9 秒放宽到 60：独白单段可到 220 字
            # （≈46 秒），按 9 秒截断会让字幕与画面差出一整个段落。
            duration = max(2.5, min(60.0, len(line) * 0.23))
            segments.append({"character": character, "line": line, "start_time": cursor,
                             "end_time": cursor + duration, "duration": duration, "audio_path": None})
            cursor += duration + 0.25
        lines = []
        for index, segment in enumerate(segments, 1):
            lines.append(f"{index}\n{TTSAgent._format_srt_time(segment['start_time'])} --> "
                         f"{TTSAgent._format_srt_time(segment['end_time'])}\n{segment['line']}")
        srt_path.write_text("\n\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return {"segments": segments, "srt_path": str(srt_path), "total_duration": segments[-1]["end_time"] if segments else 0.0, "fallback": "silent"}

    def run(self, stock_code: str, stock_name: str | None = None, *, render: bool = True,
            preview: bool = False, publish: bool = False) -> dict[str, Any]:
        """Generate the project and, unless disabled, render its MP4 output.

        With ``publish`` the rendered MP4 is uploaded to every configured
        platform after rendering; rendering is implied when publishing.
        """
        if preview:
            render = False
        if publish:
            render = True
        tag = f"{stock_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        project_dir = self.output_dir / tag
        t0 = time.monotonic()
        columns = (SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), TimeElapsedColumn(), TimeRemainingColumn())

        with Progress(*columns, console=self.console, transient=False) as progress:
            task = progress.add_task("Fetching stock data", total=6)
            try:
                stock_data = self.stock_client.get_stock_data(stock_code)
                if stock_name:
                    stock_data.setdefault("quote", {})["name"] = stock_name
            except StockDataIncompleteError as exc:
                # Already explains itself and what to do about it — pass it
                # through verbatim instead of wrapping it in generic wording.
                raise PipelineError(str(exc)) from exc
            except Exception as exc:
                raise PipelineError(f"个股数据获取失败：{exc}") from exc
            progress.advance(task)

            progress.update(task, description="Generating debate script (may take ~1 min)")
            episode_plan = self.episode_planner.plan(stock_data)
            script = self._retry("Dialogue generation", lambda: self.dialogue_gen.generate(stock_data, episode_plan))
            script["episode_plan"] = episode_plan
            self._warn_if_over_budget(script)
            progress.advance(task)

            progress.update(task, description="Reviewing compliance")
            try:
                approved = self.compliance.review(script)
            except Exception as exc:
                raise PipelineError(f"Compliance review failed: {exc}") from exc
            progress.advance(task)

            progress.update(task, description="Synthesizing voice (may take ~1 min)")
            project_dir.mkdir(parents=True, exist_ok=True)
            try:
                self.tts.output_dir = project_dir
                audio = self._retry("TTS synthesis", lambda: self.tts.synthesize(approved))
            except PipelineError as exc:
                # 2026-10-06 事故：静音兜底太安静 —— key 没进进程环境，一条没有声音的
                # 成片照常渲染、照常发布，直到人看了才发现。与「数据缺失就 abort」同一
                # 哲学：废片发出去比不发更糟。默认改为硬失败；确要旧兜底（如纯预览），
                # 显式配 tts.allow_silent_fallback: true，且错误会记进 sidecar。
                if not (self.config.get("tts", {}) or {}).get("allow_silent_fallback", False):
                    raise PipelineError(
                        f"TTS synthesis failed: {exc} —— 拒绝发布静音成片。"
                        "先修 TTS（检查 API key 环境变量/供应商配置）；"
                        "确要静音兜底请显式配置 tts.allow_silent_fallback: true") from exc
                srt_path = project_dir / f"{tag}.srt"
                self.console.print(f"[yellow]{exc}; continuing with silent video and subtitles.[/yellow]")
                audio = self._silent_timeline(approved, srt_path)
                audio["tts_error"] = str(exc)
            progress.advance(task)

            progress.update(task, description="Building HyperFrames project")
            try:
                self.builder.video_config["project_dir"] = str(project_dir)
                project = self.builder.build(stock_data, approved, audio)
            except Exception as exc:
                raise PipelineError(f"HyperFrames build failed: {exc}") from exc
            progress.advance(task)

            video_path: Path | None = None
            platform_videos: dict[str, str] = {}
            cover_files: dict[str, str] = {}
            review_path: Path | None = None
            if render:
                progress.update(task, description="Rendering MP4 (may take several minutes)")
                try:
                    video_path = self.builder.render_mp4(project, self.output_dir / f"{tag}.mp4")
                except Exception as exc:
                    raise PipelineError(f"MP4 render failed: {exc}") from exc
                platform_videos = self._render_platform_cuts(stock_data, approved, audio, project_dir, tag, progress, task)
                # 封面与成片一起产出，而不是只在发布时才渲：审查的人要能同时
                # 看到画面和信息流里的缩略图（2026-09-20 用户要求）。封面失败
                # 只降级为「无封面」，绝不连累已经渲好的 MP4。
                cover_files = self._render_covers(stock_data, approved, stock_code, tag, progress, task)
                review_path = self._write_review(
                    stock_code=stock_code, stock_name=stock_data.get("quote", {}).get("name", stock_name or ""),
                    tag=tag, script=approved, video_path=video_path, platform_videos=platform_videos,
                    covers=cover_files, duration=float(audio.get("total_duration") or 0.0),
                )
            else:
                progress.update(task, description="HTML project ready (render skipped)")
            progress.advance(task)

            result = {"stock_code": stock_code, "stock_name": stock_data.get("quote", {}).get("name", stock_name or ""),
                      "tag": tag, "script": script, "approved_script": approved, "audio": audio,
                      "episode_plan": episode_plan,
                      "srt": audio.get("srt_path"), "project_dir": str(project_dir),
                      "video_path": str(video_path) if video_path else None, "rendered": bool(video_path),
                      "platform_videos": platform_videos, "covers": cover_files,
                      "review_path": str(review_path) if review_path else None,
                      # Kept on the result so the CLI summary can name both cuts and
                      # say which platforms each one feeds; otherwise the alternate
                      # canvas render is invisible unless you open the JSON.
                      "canvas": str(self.builder.video_config.get("canvas", "vertical")),
                      "platform_canvas": dict(self.config.get("publish", {}).get("platform_canvas", {}) or {}),
                      "preview": preview, "publish": None,
                      "elapsed_seconds": round(time.monotonic() - t0, 1)}
            result_path = self.output_dir / f"{tag}.json"
            # 落盘要赶在发布之前（2026-09-29 评审：发布花了 40 分钟，审查的人在那
            # 期间看不到任何 json，剧本审批/合规改写的回溯链是断的）。先写一版
            # publish=null 的完整元数据，发布结束后再补写发布报告。
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

            if publish and video_path:
                platform_list = [p for p in self.config.get("publish", {}).get("platforms", [])]
                progress.update(task, description=f"Publishing to {', '.join(platform_list)}")

                def report_event(platform: str, label: str, state: str) -> None:
                    progress.update(task, description=f"发布 {label}（{platform}）: {state}")

                # Step aside while a 抖音 SMS challenge asks the operator for a
                # code: the banner and their typing must not fight a refreshing
                # progress bar for the same lines.
                self.publisher.set_ui_hooks(pause=progress.stop, resume=progress.start)
                result["publish"] = self.publisher.publish(
                    video_path, approved, stock_data.get("quote", {}).get("name", stock_name or ""), stock_code,
                    schedule=str(self.config.get("publish", {}).get("schedule") or "") or None,
                    platform_videos=platform_videos or None, covers=cover_files or None,
                    on_event=report_event,
                )
                result["elapsed_seconds"] = round(time.monotonic() - t0, 1)
                result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            progress.advance(task)

        self.console.print(f"[green]Done:[/green] {result_path}")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="谈股论金 — AI 对话视频流水线")
    parser.add_argument("stock_code", help="Six-digit A-share code, e.g. 600519")
    parser.add_argument("--name", default=None, help="Override stock name")
    parser.add_argument("--config", default=None, help="YAML config file")
    parser.add_argument("--no-render", action="store_true", help="Generate the HTML project but skip MP4 rendering")
    parser.add_argument("--preview", action="store_true", help="Generate only the HTML preview project (implies --no-render)")
    args = parser.parse_args()
    try:
        Pipeline(load_config(args.config)).run(args.stock_code, args.name, render=not args.no_render, preview=args.preview)
    except PipelineError as exc:
        print(f"谈股论金失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
