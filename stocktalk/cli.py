"""谈股论金 CLI — 一条命令从股票代码到发布视频。

    tangulunjin 601689 --publish          # 生成 MP4 并发布到全部已配置平台
    tangulunjin --check                   # 发布预检：工具链 + 各平台登录态
    tangulunjin 601689 --republish        # 补发：只重发上次失败的平台
    tangulunjin 601689 600519 --publish   # 批量：逐个生成并发布
    tangulunjin --watchlist my.txt        # 批量：从文件读代码
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from stocktalk.modules.publisher import PLATFORM_SPECS, SauPublisher, check_environment
from stocktalk.pipeline import Pipeline, PipelineError, load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tangulunjin",
        description="谈股论金 — 全自动 AI 财报对话视频生成工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tangulunjin 601689 --publish                 # 生成 + 发布到全部平台\n"
            "  tangulunjin --check                          # 发布前预检各平台登录态\n"
            "  tangulunjin 601689 --republish               # 只补发上次失败的平台\n"
            "  tangulunjin 601689 --republish --platforms douyin   # 只补抖音\n"
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
    parser.add_argument("--republish", action="store_true",
                        help="补发：自动找该代码最近一次的成片（无需抄路径），默认只补发上次失败的平台")
    parser.add_argument("--check", action="store_true", dest="check_only",
                        help="发布预检：检查 sau CLI / Node / Chrome 与各平台登录态，全部通过才退出码 0")
    parser.add_argument("--no-preflight", action="store_true",
                        help="带 --publish 时跳过开跑前的平台登录态探测（默认探测，失效即提醒）")
    parser.add_argument("--platforms", default=None, metavar="LIST",
                        help=f"只发布这些平台，逗号分隔，可选项: {', '.join(PLATFORM_SPECS)}")
    parser.add_argument("--watchlist", default=None, metavar="FILE",
                        help="从文件读取股票代码，每行一个（可写成「601689 拓普集团」），# 之后为注释")
    parser.add_argument("--no-interactive", action="store_true",
                        help="不询问任何问题（抖音要短信验证码时只等待手工写入 verify_code.txt）")

    args = parser.parse_args(argv)
    # Config first: --no-interactive mutates the publish section before anything runs.
    config = load_config(args.config)
    if args.no_interactive:
        config.setdefault("publish", {})["interactive_verify_code"] = False

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

    # --check probes the toolchain only, so it must not be gated on a stock code.
    if args.check_only:
        sys.exit(_check(config))

    pipeline = Pipeline(config)

    # Ctrl-C anywhere should read as a clean stop, not a traceback: people press
    # it because the run looks stuck, and the useful next step is always the
    # same — republish whatever did not go out.
    try:
        if args.republish:
            _republish(pipeline, args, config, parser)
            return

        if args.publish_only:
            _publish_only(pipeline, args, config, parser)
            return

        if args.publish:
            # 探活必须在渲染前：一次 15 分钟的生成不该烧在一张已失效的 cookie 上。
            _gate_publish(config, args, parser)

        targets = _collect_targets(args, parser)
        failures = _run_all(pipeline, targets, args)

        if len(targets) > 1:
            _print_batch_summary(targets, failures)
        if failures:
            # Non-zero exit so a cron job / automation can tell a bad run from a good one.
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n已中断。已成功的平台不会重发；失败的用 `tangulunjin <code> --republish` 补发。")
        sys.exit(130)


def _check(config: dict) -> int:
    """Preflight the toolchain and every platform's login; 0 only if all pass."""
    print("发布预检")
    publisher = SauPublisher(config)
    environment = check_environment(config, publisher)
    print("  环境")
    for item in environment:
        mark = "✓" if item["ok"] else ("✗" if item["required"] else "!")
        print(f"    {mark} {item['label']}\n        {item['detail']}")
    blocked = [item for item in environment if item["required"] and not item["ok"]]

    platforms = [p for p in config.get("publish", {}).get("platforms", ()) if p in PLATFORM_SPECS]
    if not platforms:
        print("\n  未配置任何平台（publish.platforms）")
        return 1

    print(f"  平台登录态（{len(platforms)} 个）")
    checks = publisher.check_platforms(platforms)
    failed: list[str] = []
    for platform in platforms:
        label = PLATFORM_SPECS[platform]["label"]
        outcome = checks.get(platform, {"ok": False, "detail": "未检查"})
        if outcome["ok"]:
            print(f"    ✓ {label}({platform})")
        else:
            failed.append(platform)
            print(f"    ✗ {label}({platform})\n        {_one_line(outcome['detail'])}")

    print(f"\n  结论: 平台 {len(platforms) - len(failed)}/{len(platforms)} 可发布"
          + (f"，环境缺 {len(blocked)} 项必需工具" if blocked else ""))
    if failed:
        account = config.get("publish", {}).get("accounts", {}) or {}
        print("  需要重新登录的平台（在 third_party/social-auto-upload 下执行）:")
        for platform in failed:
            print(f"    uv run sau {platform} login --account {account.get(platform, 'default')}")
    if blocked:
        for item in blocked:
            print(f"  必需工具缺失: {item['label']} — {item['detail']}")
    return 1 if (failed or blocked) else 0


def _gate_publish(config: dict, args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Probe every configured platform's login before any expensive work starts.

    A full run costs ~15 minutes of generation plus render; finding a dead
    cookie at publish time throws all of it away (视频号隔天失效是常态，
    2026-09-19/20 实测).  Probe up front: on failure print the login remedy
    and let the operator either quit to re-login or explicitly continue with
    the healthy platforms only.
    """
    if getattr(args, "no_preflight", False):
        return
    platforms = [p for p in config.get("publish", {}).get("platforms", ()) if p in PLATFORM_SPECS]
    if not platforms:
        return
    checks = SauPublisher(config).check_platforms(platforms)
    failed = [p for p in platforms if not checks.get(p, {}).get("ok")]
    if not failed:
        print("发布预检: " + "、".join(f"{PLATFORM_SPECS[p]['label']}" for p in platforms) + " 登录态均有效")
        return
    labels = "、".join(f"{PLATFORM_SPECS[p]['label']}({p})" for p in failed)
    print(f"\n⚠ 发布预检：{labels} 登录态失效，直接发布会跳过它们：")
    account = config.get("publish", {}).get("accounts", {}) or {}
    for platform in failed:
        print(f"    uv run sau {platform} login --account {account.get(platform, 'default')}")
    if args.no_interactive or not (sys.stdin and sys.stdin.isatty()):
        parser.exit(1, "\n已退出。请先重新登录再重跑本命令，或用 --platforms 只发布健康平台。\n")
    try:
        answer = input("继续发布到其余平台？[y=继续 / N=退出，先去登录] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        parser.exit(1, "已退出。重新登录后重跑即可；已生成的片子随时可用 --publish-only 补发。\n")


def _republish(pipeline: Pipeline, args: argparse.Namespace, config: dict, parser: argparse.ArgumentParser) -> None:
    """Re-send an already rendered video, defaulting to last run's failures.

    The whole point is that a failed 抖音 upload should cost one more upload,
    not another render: the MP4, the metadata and the covers are all recovered
    from the previous run's sidecar JSON.
    """
    if len(args.stock_codes) != 1:
        parser.error("--republish 需要且只需要一个股票代码（tangulunjin 601689 --republish）")
    code = args.stock_codes[0].strip()
    output_dir = Path(config.get("output", {}).get("dir", "output"))
    run = _latest_run(output_dir, code)
    if run is None:
        parser.exit(1, f"\n补发失败：在 {output_dir} 中找不到 {code} 的成片。\n"
                       f"先跑一次 `tangulunjin {code} --publish`，或用 --publish-only 指定 MP4。\n")
    video, data = run
    if args.platforms:
        platforms = list(config.get("publish", {}).get("platforms", []))
    else:
        platforms = [item["platform"] for item in (data.get("publish") or {}).get("failed", [])
                     if item.get("platform") in PLATFORM_SPECS]
        if not platforms:
            print(f"上次发布没有失败的平台，无需补发：{video}")
            print("  如需强制重发某个平台，加上 --platforms <平台>（如 --platforms douyin）")
            return
    script, stock_name, platform_videos, covers = _recover_publish_context(video, args)
    # Same reason as --publish-only: art only exists once a run has published.
    covers = _ensure_covers(pipeline, covers, script, code, video.stem, stock_name)
    print(f"补发 {stock_name}（{code}）→ {', '.join(PLATFORM_SPECS[p]['label'] for p in platforms)}")
    print(f"  成片: {video}")
    try:
        report = pipeline.publisher.publish(
            video, script, stock_name=stock_name, stock_code=code, platforms=platforms,
            platform_videos=platform_videos or None, covers=covers or None,
            schedule=str(config.get("publish", {}).get("schedule") or "") or None)
    except Exception as exc:
        parser.exit(1, f"\n补发失败：{exc}\n")
    _record_publish(video, report, covers)
    _print_publish_report(parser, report)
    if report["failed"]:
        sys.exit(1)


def _latest_run(output_dir: Path, code: str) -> tuple[Path, dict] | None:
    """Find the most recent rendered run for ``code``.

    Tags are ``{code}_{YYYYmmdd_HHMMSS}``, so a reverse name sort is a reverse
    chronological sort — no need to stat every file.
    """
    for sidecar in sorted(output_dir.glob(f"{code}_*.json"), reverse=True):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        video = sidecar.with_suffix(".mp4")
        if not video.is_file():
            recorded = data.get("video_path")
            if not (recorded and Path(recorded).is_file()):
                continue
            video = Path(recorded)
        return video, data
    return None


def _publish_only(pipeline: Pipeline, args: argparse.Namespace, config: dict, parser: argparse.ArgumentParser) -> None:
    video = Path(args.publish_only)
    if not video.is_file():
        parser.exit(1, f"\n发布失败：找不到视频文件 {video}\n")
    script, stock_name, platform_videos, covers = _recover_publish_context(video, args)
    code = args.stock_codes[0] if args.stock_codes else str(_sidecar(video).get("stock_code") or "")
    # A run that never published left `covers` empty; make the art now instead
    # of shipping a cover-less video to every platform.
    covers = _ensure_covers(pipeline, covers, script, code, video.stem, stock_name)
    try:
        report = pipeline.publisher.publish(video, script, stock_name=stock_name,
                                            stock_code=code,
                                            platform_videos=platform_videos or None,
                                            covers=covers or None,
                                            schedule=str(config.get("publish", {}).get("schedule") or "") or None)
    except Exception as exc:
        parser.exit(1, f"\n发布失败：{exc}\n")
    _record_publish(video, report, covers)
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
                print(f"  补发: tangulunjin {code} --republish")
    return failures


# Every canvas cut is rendered next to the master (``<tag>.<canvas>.mp4``), so
# the summary has to name them all — otherwise the mobile cut looks missing.
CANVAS_LABELS = {"horizontal": "桌面版（横屏）", "vertical": "手机版（竖屏）"}


def _canvas_of(path: str, default: str) -> str:
    """Read the canvas back out of ``<tag>.<canvas>.mp4``; master has none."""
    suffixes = Path(path).suffixes
    if len(suffixes) >= 2 and suffixes[-2].lstrip(".") in CANVAS_LABELS:
        return suffixes[-2].lstrip(".")
    return default


def _video_lines(result: dict) -> list[str]:
    """One line per rendered canvas, naming the platforms each one feeds."""
    canvas_map = {p: str(c) for p, c in (result.get("platform_canvas") or {}).items()}
    cuts = result.get("platform_videos") or {}
    master = str(result["video_path"])
    main_canvas = str(result.get("canvas") or "") or _canvas_of(master, "")
    order: list[str] = []
    info: dict[str, dict[str, Any]] = {}

    def add(path: str, canvas: str) -> None:
        if path not in info:
            info[path] = {"canvas": canvas, "platforms": []}
            order.append(path)

    add(master, main_canvas)
    for platform, path in cuts.items():
        add(str(path), canvas_map.get(platform) or _canvas_of(str(path), main_canvas))
    for platform, canvas in canvas_map.items():
        label = PLATFORM_SPECS.get(platform, {}).get("label", platform)
        cut_path = cuts.get(platform)
        if cut_path is not None:
            info[str(cut_path)]["platforms"].append(label)
        elif canvas in (main_canvas, ""):
            info[master]["platforms"].append(label)
    lines = []
    for path in order:
        bucket = info[path]
        targets = "、".join(bucket["platforms"])
        suffix = f"   → 用于 {targets}" if targets else ""
        label = CANVAS_LABELS.get(bucket["canvas"], bucket["canvas"] or "主片")
        lines.append(f"{label}: {path}{suffix}")
    return lines


def _print_run_result(result: dict) -> None:
    print("\n✓ 视频生成完成")
    print(f"  股票: {result['stock_name']} ({result['stock_code']})")
    print(f"  发言: {len(result['script'].get('turns', []))}")
    print(f"  输出: {result.get('project_dir', 'output/')}")
    if result["rendered"]:
        for line in _video_lines(result):
            print(f"  视频: {line}")
    else:
        print("  MP4 渲染已跳过")
    if result.get("covers"):
        print(f"  封面: {', '.join(result['covers'].values())}")


def _print_batch_summary(targets: list[tuple[str, str | None]], failures: list[tuple[str, str]]) -> None:
    reasons = dict(failures)
    print(f"\n{'=' * 8} 汇总 {len(targets)} 只 / 失败 {len(failures)} 只 {'=' * 8}")
    for code, name in targets:
        reason = reasons.get(code)
        mark = "✗" if reason else "✓"
        print(f"  {mark} {code} {name or ''}{'  ' + reason if reason else ''}")
    if failures:
        print("  提示: 成功的片子可单独补发: tangulunjin <code> --republish")


def _recover_publish_context(video: Path, args: argparse.Namespace) -> tuple[dict, str, dict[str, str], dict[str, str]]:
    """Restore the metadata the last full run wrote next to the MP4.

    ``--publish-only`` / ``--republish`` run after a failure, so they must
    republish with the original title/description and the covers already
    rendered, rather than degrade to the filename and no artwork.
    """
    script: dict = {"title": video.stem, "turns": []}
    stock_name = args.name or video.stem
    platform_videos: dict[str, str] = {}
    covers: dict[str, str] = {}
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
        for preset, path in (data.get("covers") or {}).items():
            if Path(path).is_file():
                covers[preset] = path
    return script, stock_name, platform_videos, covers


def _sidecar(video: Path) -> dict:
    """The metadata JSON the last full run wrote next to the MP4 ({} if none)."""
    path = video.with_suffix(".json")
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ensure_covers(pipeline: Pipeline, covers: Mapping[str, str], script: Mapping[str, Any],
                   stock_code: str, tag: str, stock_name: str) -> dict[str, str]:
    """Make cover art when the original run never got as far as publishing."""
    if covers:
        return dict(covers)
    try:
        return pipeline.render_covers(script, stock_code, tag, stock_name=stock_name)
    except Exception as exc:  # A cover is a nice-to-have; never block the upload.
        print(f"  封面图生成失败，将不带封面发布: {exc}")
        return {}


def _record_publish(video: Path, report: Mapping[str, Any], covers: Mapping[str, str]) -> None:
    """Write the result back next to the MP4 so `--republish` knows what failed."""
    sidecar = video.with_suffix(".json")
    if not sidecar.is_file():
        return
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        data["publish"] = dict(report)
        if covers:
            data["covers"] = dict(covers)
        sidecar.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass


def _one_line(text: str, limit: int = 160) -> str:
    """Collapse a multi-line tool failure into something worth printing."""
    collapsed = " / ".join(line.strip() for line in str(text or "").splitlines() if line.strip())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _print_publish_report(parser: argparse.ArgumentParser | None, report: dict) -> None:
    print(f"\n✓ 发布完成: {', '.join(report['succeeded']) or '无'}")
    for item in report["platforms"]:
        mark = "✓" if item["ok"] else "✗"
        attempts = f"（{item['attempts']} 次尝试）" if item.get("attempts", 1) > 1 else ""
        print(f"  {mark} {item['label']}({item['platform']}){attempts}")
        if not item["ok"]:
            print(f"    原因: {item['detail'][:200]}")
    if report["failed"]:
        print("  提示: 失败的平台可直接补发: tangulunjin <code> --republish")


if __name__ == "__main__":
    main()
