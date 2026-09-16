"""谈股论金 CLI — 一条命令从股票代码到发布视频。

    tangulunjin 601689 --publish          # 生成 MP4 并发布到全部已配置平台
    tangulunjin 601689 600519 --publish   # 批量：逐个生成并发布
    tangulunjin --watchlist my.txt        # 批量：从文件读代码
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stocktalk.modules.publisher import PLATFORM_SPECS
from stocktalk.pipeline import Pipeline, PipelineError, load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tangulunjin",
        description="谈股论金 — 全自动 AI 财报对话视频生成工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tangulunjin 601689 --publish                 # 生成 + 发布到全部平台\n"
            "  tangulunjin 601689 600519 --publish          # 批量跑两只票\n"
            "  tangulunjin --watchlist watchlist.txt        # 从文件读代码批量跑\n"
            "  tangulunjin 601689 --platforms douyin        # 只发指定平台\n"
            "  tangulunjin 601689 --publish-only out/x.mp4  # 只重发已生成的 MP4\n"
        ),
    )
    parser.add_argument("stock_codes", nargs="*", metavar="CODE",
                        help="6位A股代码；可一次给多个，逐个生成并发布")
    parser.add_argument("--name", default=None, help="股票名称（可选，会自动获取；仅在单个代码时生效）")
    parser.add_argument("--config", default=None, help="自定义配置文件路径")
    parser.add_argument("--output-dir", default=None, help="输出目录（默认 output/）")
    parser.add_argument("--duration-minutes", type=int, default=None, help="对话目标时长（分钟，默认 2-5）")
    parser.add_argument("--no-render", action="store_true", help="仅生成 HTML 项目，跳过 MP4 渲染")
    parser.add_argument("--preview", action="store_true", help="仅生成 HTML 预览项目（等同于 --no-render）")
    parser.add_argument("--publish", action="store_true", help="渲染完成后自动发布到已配置的平台（抖音/B站/快手/小红书/视频号）")
    parser.add_argument("--publish-only", default=None, metavar="MP4", help="跳过生成，直接把指定 MP4 发布到已配置的平台")
    parser.add_argument("--platforms", default=None, metavar="LIST",
                        help=f"只发布这些平台，逗号分隔，可选项: {', '.join(PLATFORM_SPECS)}")
    parser.add_argument("--watchlist", default=None, metavar="FILE",
                        help="从文件读取股票代码，每行一个（可写成「601689 拓普集团」），# 之后为注释")

    args = parser.parse_args(argv)

    config = load_config(args.config)

    if args.output_dir:
        config.setdefault("output", {})["dir"] = args.output_dir

    if args.duration_minutes is not None:
        if args.duration_minutes < 1:
            parser.error("--duration-minutes 必须为正整数")
        seconds = args.duration_minutes * 60
        config.setdefault("dialogue", {}).update({"min_duration_seconds": seconds, "max_duration_seconds": seconds})

    if args.platforms:
        names = [p.strip() for p in args.platforms.replace("，", ",").split(",") if p.strip()]
        unknown = [p for p in names if p not in PLATFORM_SPECS]
        if unknown:
            parser.error(f"未知平台: {', '.join(unknown)}（可选: {', '.join(PLATFORM_SPECS)}）")
        config.setdefault("publish", {})["platforms"] = names

    pipeline = Pipeline(config)

    if args.publish_only:
        _publish_only(pipeline, args, config, parser)
        return

    targets = _collect_targets(args, parser)
    failures = _run_all(pipeline, targets, args)

    if len(targets) > 1:
        _print_batch_summary(targets, failures)
    if failures:
        # Non-zero exit so a cron job / automation can tell a bad run from a good one.
        sys.exit(1)


def _publish_only(pipeline: Pipeline, args: argparse.Namespace, config: dict, parser: argparse.ArgumentParser) -> None:
    video = Path(args.publish_only)
    if not video.is_file():
        parser.exit(1, f"\n发布失败：找不到视频文件 {video}\n")
    script, stock_name, platform_videos = _recover_publish_context(video, args)
    try:
        report = pipeline.publisher.publish(video, script, stock_name=stock_name,
                                            stock_code=args.stock_codes[0] if args.stock_codes else "",
                                            platform_videos=platform_videos or None,
                                            schedule=str(config.get("publish", {}).get("schedule") or "") or None)
    except Exception as exc:
        parser.exit(1, f"\n发布失败：{exc}\n")
    _print_publish_report(parser, report)
    if report["failed"]:
        sys.exit(1)


def _collect_targets(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[tuple[str, str | None]]:
    """Resolve positional codes and --watchlist entries into (code, name) pairs."""
    targets: list[tuple[str, str | None]] = [
        (code.strip(), args.name if len(args.stock_codes) == 1 else None)
        for code in args.stock_codes if code.strip()
    ]
    if args.watchlist:
        path = Path(args.watchlist)
        if not path.is_file():
            parser.error(f"--watchlist 文件不存在: {path}")
        for raw in path.read_text(encoding="utf-8").splitlines():
            text = raw.split("#", 1)[0].strip()
            if not text:
                continue
            parts = text.split()
            targets.append((parts[0], parts[1] if len(parts) > 1 else None))
    if not targets:
        parser.error("请给出至少一个股票代码（tangulunjin 601689），或用 --watchlist 指定文件")
    seen: set[str] = set()
    ordered: list[tuple[str, str | None]] = []
    for code, name in targets:
        if code not in seen:
            seen.add(code)
            ordered.append((code, name))
    return ordered


def _run_all(pipeline: Pipeline, targets: list[tuple[str, str | None]], args: argparse.Namespace) -> list[tuple[str, str]]:
    """Run every target in order; one bad stock never stops the rest."""
    failures: list[tuple[str, str]] = []
    total = len(targets)
    for index, (code, name) in enumerate(targets, 1):
        if total > 1:
            print(f"\n{'=' * 8} [{index}/{total}] {code} {name or ''} {'=' * 8}")
        try:
            result = pipeline.run(code, stock_name=name, render=not args.no_render, preview=args.preview,
                                  publish=args.publish)
        except PipelineError as exc:
            print(f"\n✗ {code} 生成失败：{exc}")
            failures.append((code, f"生成失败：{exc}"))
            continue
        except KeyboardInterrupt:
            print(f"\n已中断（剩余 {total - index} 只未处理）")
            failures.append((code, "被用户中断"))
            break

        _print_run_result(result)
        if result.get("publish"):
            _print_publish_report(None, result["publish"])
            failed = [f["platform"] for f in result["publish"]["failed"]]
            if failed:
                failures.append((code, "发布失败：" + "、".join(PLATFORM_SPECS.get(p, {}).get("label", p) for p in failed)))
    return failures


def _print_run_result(result: dict) -> None:
    print("\n✓ 视频生成完成")
    print(f"  股票: {result['stock_name']} ({result['stock_code']})")
    print(f"  发言: {len(result['script'].get('turns', []))}")
    print(f"  输出: {result.get('project_dir', 'output/')}")
    if result["rendered"]:
        print(f"  视频: {result['video_path']}")
    else:
        print("  MP4 渲染已跳过")


def _print_batch_summary(targets: list[tuple[str, str | None]], failures: list[tuple[str, str]]) -> None:
    reasons = dict(failures)
    print(f"\n{'=' * 8} 汇总 {len(targets)} 只 / 失败 {len(failures)} 只 {'=' * 8}")
    for code, name in targets:
        reason = reasons.get(code)
        mark = "✗" if reason else "✓"
        print(f"  {mark} {code} {name or ''}{'  ' + reason if reason else ''}")
    if failures:
        print("  提示: 生成成功的片子可单独重发: tangulunjin <code> --publish-only <mp4>")


def _recover_publish_context(video: Path, args: argparse.Namespace) -> tuple[dict, str, dict[str, str]]:
    """Restore the metadata the last full run wrote next to the MP4.

    ``--publish-only`` runs after a failure, so it must republish with the
    original title/description rather than degrade to the filename.
    """
    script: dict = {"title": video.stem, "turns": []}
    stock_name = args.name or video.stem
    platform_videos: dict[str, str] = {}
    sidecar = video.with_suffix(".json")
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        recovered = data.get("approved_script") or {}
        if isinstance(recovered, dict) and recovered.get("turns"):
            script = recovered
        stock_name = args.name or data.get("stock_name") or stock_name
        for platform, path in (data.get("platform_videos") or {}).items():
            if Path(path).is_file():
                platform_videos[platform] = path
    return script, stock_name, platform_videos


def _print_publish_report(parser: argparse.ArgumentParser | None, report: dict) -> None:
    print(f"\n✓ 发布完成: {', '.join(report['succeeded']) or '无'}")
    for item in report["platforms"]:
        mark = "✓" if item["ok"] else "✗"
        attempts = f"（{item['attempts']} 次尝试）" if item.get("attempts", 1) > 1 else ""
        print(f"  {mark} {item['label']}({item['platform']}){attempts}")
        if not item["ok"]:
            print(f"    原因: {item['detail'][:200]}")
    if report["failed"]:
        print("  提示: 失败的平台可单独重试: tangulunjin <code> --publish-only <mp4>")


if __name__ == "__main__":
    main()
