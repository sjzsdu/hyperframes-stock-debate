"""谈股论金 CLI — 一条命令从股票代码到发布视频。

    tangulunjin 601689 --publish          # 生成 MP4 并发布到全部已配置平台
    tangulunjin 601689 -m "重点讲海外收入占比"   # 本期提示词：给 LLM 的选材与侧重参考
    tangulunjin 601689 --publish --auto    # 发布前的失效平台先自动重新登录（视频号免扫码）
    tangulunjin --check                   # 发布预检：工具链 + 各平台登录态
    tangulunjin --daemon                  # 守护进程：到点自动选题+生成+发布（推荐常驻）
    tangulunjin --daemon-install          # 把守护进程装成 launchd 服务（开机自启/崩溃自拉起）
    tangulunjin --daemon-status           # 看今天的排期、下次发布时刻、累计成败
    tangulunjin --notify-test             # 反馈通道自检：发一条测试通知，确认收得到
    tangulunjin --keepalive               # 登录态心跳（防视频号会话失效）
    tangulunjin 601689 --republish        # 补发：只重发上次失败的平台
    tangulunjin 601689 600519 --publish   # 批量：逐个生成并发布
    tangulunjin --watchlist my.txt        # 批量：从文件读代码
    tangulunjin --auto-pick --publish     # 选题交给 tongstock 热门榜（守护进程跑的就是这条）
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from stocktalk.modules.daemon import (
    DaemonError,
    build_daemon,
    install_launchd,
    launchd_state,
    uninstall_launchd,
)
from stocktalk.modules.notify import TEST, RunReport, build_notifier
from stocktalk.modules.publisher import (
    AUTO_LOGIN_PLATFORMS,
    PLATFORM_SPECS,
    SauPublisher,
    ScheduleError,
    check_environment,
    parse_publish_at,
)
from stocktalk.modules.review import COVER_LABELS, video_inventory
from stocktalk.modules.topic_picker import HotTopic, TopicError, TopicPicker
from stocktalk.pipeline import Pipeline, PipelineError, load_config

# 守护进程要用它当工作目录：output/、config、相对路径都从这里算起，于是无论从哪个
# 目录把它拉起来，行为都一样（launchd 的 WorkingDirectory 也写这个）。
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tangulunjin",
        description="谈股论金 — 全自动 AI 财报对话视频生成工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  tangulunjin 601689 --publish                 # 生成 + 发布到全部平台\n"
            "  tangulunjin 601689 -m \"重点讲海外收入占比\"    # 本期提示词（可重复，@文件也行）\n"
            "  tangulunjin 601689 --publish --auto          # 失效平台先自动重新登录再发\n"
            "  tangulunjin --check                          # 发布前预检各平台登录态\n"
            "  tangulunjin --daemon                         # 守护进程（前台，到点自动发布）\n"
            "  tangulunjin --daemon-install                 # 装成 launchd 服务，开机自启\n"
            "  tangulunjin --daemon-status                  # 看今天的排期与累计成败\n"
            "  tangulunjin --notify-test                    # 反馈通道自检（跑完的结果推到哪去）\n"
            "  tangulunjin --keepalive                      # 登录态心跳（建议 4 小时一次）\n"
            "  tangulunjin 601689 --republish               # 只补发上次失败的平台\n"
            "  tangulunjin 601689 --republish --platforms douyin   # 只补抖音\n"
            "  tangulunjin 601689 600519 --publish          # 批量跑两只票\n"
            "  tangulunjin --watchlist watchlist.txt        # 从文件读代码批量跑\n"
            "  tangulunjin --auto-pick --publish            # 选题交给 tongstock 热门榜\n"
            "  tangulunjin --auto-pick --publish --continue-anyway  # 有平台掉线也照发其余平台\n"
            "  tangulunjin 601689 --platforms douyin        # 只发指定平台\n"
            "  tangulunjin 601689 --publish-only out/x.mp4  # 只重发已生成的 MP4\n"
        ),
    )
    parser.add_argument("stock_codes", nargs="*", metavar="CODE",
                        help="6位A股代码；可一次给多个，逐个生成并发布")
    parser.add_argument("--name", default=None, help="股票名称（可选，会自动获取；仅在单个代码时生效）")
    parser.add_argument("--config", default=None,
                        help="自定义配置文件路径（与 default.yaml 深度合并）。"
                             "文件不存在会直接报错——静默退回默认配置会让你以为在跑独白，实际出了辩论片")
    parser.add_argument("--output-dir", default=None, help="输出目录（默认 output/）")
    parser.add_argument("--canvas", choices=["horizontal", "vertical"], default=None,
                        help="画幅：horizontal=桌面版（1920×1080 横屏），"
                             "vertical=手机版（1080×1920 竖屏，适配抖音/快手全屏）；"
                             "覆盖配置里的 video.canvas")
    parser.add_argument("--format", choices=["debate", "monologue"], default=None, dest="dialogue_format",
                        help="内容形态：debate=双人对辩，monologue=单人独白讲一家公司；"
                             "覆盖配置里的 dialogue.format，等价于 --config config.monologue.yaml，"
                             "但不用每次带配置文件")
    parser.add_argument("--duration-minutes", type=int, default=None, help="对话目标时长（分钟，默认 2-5）")
    parser.add_argument("--short-form", action="store_true",
                        help="信息流短版：将目标时长设为 45-75 秒，适合与默认长版做 A/B 测试")
    parser.add_argument("-m", "--message", action="append", default=None, metavar="TEXT",
                        help="本期提示词：注入剧本生成，给 LLM 当选材与侧重的参考"
                             "（如「重点讲海外收入占比」）。可重复，多条换行拼接；"
                             "写成 @文件路径 就从文本文件读（长提示词免转义）。"
                             "批量时对所有代码生效；不改变合规红线与数据真实性要求，"
                             "也改不了骨架节拍与字数额度")
    parser.add_argument("--no-render", action="store_true", help="仅生成 HTML 项目，跳过 MP4 渲染")
    parser.add_argument("--preview", action="store_true", help="仅生成 HTML 预览项目（等同于 --no-render）")
    parser.add_argument("--publish", action="store_true", help="渲染完成后自动发布到已配置的平台（抖音/B站/快手/小红书/视频号）")
    parser.add_argument("--publish-only", default=None, metavar="MP4", help="跳过生成，直接把指定 MP4 发布到已配置的平台")
    parser.add_argument("--republish", action="store_true",
                        help="补发：自动找该代码最近一次的成片（无需抄路径），默认只补发上次失败的平台")
    parser.add_argument("--check", action="store_true", dest="check_only",
                        help="发布预检：检查 sau CLI / Node / Chrome 与各平台登录态，全部通过才退出码 0")
    parser.add_argument("--keepalive", action="store_true",
                        help="登录态心跳：给每个平台做一次真实活动并续期，记录到 output/.session_state.json"
                             "（含视频号会话年龄）；适合挂定时任务")
    parser.add_argument("--daemon", action="store_true",
                        help="守护进程：常驻但基本不动，在 daemon.windows 的发布窗口内随机挑时刻，"
                             "自动跑 --auto-pick --publish。失败自动重试；窗口过了不补发。")
    parser.add_argument("--daemon-install", action="store_true",
                        help="把守护进程装成 macOS launchd 服务（开机自启、退出自动拉起）")
    parser.add_argument("--daemon-uninstall", action="store_true",
                        help="卸载 launchd 服务并删掉 plist")
    parser.add_argument("--daemon-status", action="store_true",
                        help="打印守护进程状态：今天的排期、计划时刻、下次睁眼时刻、累计成败")
    parser.add_argument("--notify-test", action="store_true", dest="notify_test",
                        help="反馈通道自检：按 notify 配置发一条测试通知，确认真的收得到"
                             "（守护进程每次跑完的结果就是走这条链路推给你的）")
    parser.add_argument("--continue-anyway", action="store_true",
                        help="发布预检发现有平台登录失效时，跳过它们继续发布其余平台"
                             "（无人值守用；默认是「有平台掉线就整条不发」）")
    parser.add_argument("--auto-login", "--auto", action="store_true", dest="auto_login",
                        help="发布预检发现有平台登录失效时，先自动重新登录再继续（别名 --auto）。"
                             "目前只有视频号能无人值守完成：它的登录页有「微信快捷登录」，"
                             "本机微信在跑就不必扫码；其余平台仍会提示人工扫码")
    parser.add_argument("--no-preflight", action="store_true",
                        help="带 --publish 时跳过开跑前的平台登录态探测（默认探测，失效即提醒）")
    parser.add_argument("--platforms", default=None, metavar="LIST",
                        help=f"只发布这些平台，逗号分隔，可选项: {', '.join(PLATFORM_SPECS)}")
    parser.add_argument("--watchlist", default=None, metavar="FILE",
                        help="从文件读取股票代码，每行一个（可写成「601689 拓普集团」），# 之后为注释")
    parser.add_argument("--add-watchlist", action=argparse.BooleanOptionalAction, default=True,
                        dest="add_watchlist",
                        help="开跑前把本期目标个股写进 tongstock 自选股（分组「谈股论金」，"
                             "备注运行日期；支持手动代码/--watchlist 文件/--auto-pick 三种选题）。"
                             "写入失败只提醒不阻断。默认开，--no-add-watchlist 关掉")
    parser.add_argument("--no-interactive", action="store_true",
                        help="不询问任何问题（抖音要短信验证码时只等待手工写入 verify_code.txt）")
    parser.add_argument("--auto-pick", action="store_true",
                        help="选题交给 tongstock 热门榜：取榜首未发过的那只（记录在 output/.topic_rotation.json），"
                             "适合定时任务；与代码/--watchlist 互斥")
    parser.add_argument("--topic-top", type=int, default=10, metavar="N",
                        help="选题时看榜单前 N 名（默认 10）")
    parser.add_argument("--publish-at", default=None, metavar="WHEN",
                        help="走平台的定时发表而不是立即发布：19:30（下一个该时刻）、+11h（相对现在）、"
                             "或 '2026-09-24 19:30'。至少提前 2 小时（平台限制）。"
                             "配合 --auto-pick 就能一次会话里把后面几条也排好期")

    args = parser.parse_args(argv)
    # Config first: --no-interactive mutates the publish section before anything runs.
    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        # --config 指到不存在的文件时绝不静默退回默认配置：那会让人以为在跑独白，
        # 实际出了一条辩论片（形态在成片、封面、骨架上是判若两人的差别）。
        parser.error(f"--config 指定的配置文件不存在：{exc}")
    if args.no_interactive:
        config.setdefault("publish", {})["interactive_verify_code"] = False

    if args.publish_at:
        # 写回 config，让主流程 / --publish-only / --republish 三条路径都拿到同一个值。
        try:
            config.setdefault("publish", {})["schedule"] = parse_publish_at(args.publish_at)
        except ScheduleError as exc:
            parser.error(str(exc))

    if args.output_dir:
        config.setdefault("output", {})["dir"] = args.output_dir

    if args.canvas:
        config.setdefault("video", {})["canvas"] = args.canvas

    if args.dialogue_format:
        # --format 是 --config config.monologue.yaml 的等价快捷方式：只切形态，
        # 其余（独白时长额度、narrator 人设、封面版式）都由 default.yaml 决定。
        # 命令行显式给了就以命令行为准，覆盖配置文件里的 dialogue.format。
        config.setdefault("dialogue", {})["format"] = args.dialogue_format

    if args.duration_minutes is not None:
        if args.duration_minutes < 1:
            parser.error("--duration-minutes 必须为正整数")
        seconds = args.duration_minutes * 60
        _apply_duration_bounds(config, seconds, seconds)
    elif args.short_form:
        # Keep the long-form default intact for existing publishing workflows;
        # short-form is an explicit experiment so retention can be compared on
        # the same stock and day instead of silently changing the product.
        _apply_duration_bounds(config, 45, 75)

    if args.platforms:
        names = [p.strip() for p in args.platforms.replace("，", ",").split(",") if p.strip()]
        unknown = [p for p in names if p not in PLATFORM_SPECS]
        if unknown:
            parser.error(f"未知平台: {', '.join(unknown)}（可选: {', '.join(PLATFORM_SPECS)}）")
        config.setdefault("publish", {})["platforms"] = names

    # --message 也写回 config：主流程、--auto-pick、批量、以及配置里已有的值都从
    # dialogue.message 这一处读，谁都不会漏（同 --publish-at 的理由）。
    message = _resolve_message(args, parser)
    if message:
        config.setdefault("dialogue", {})["message"] = message
        _report_message(args, message)

    # --check / --keepalive / --notify-test / --daemon* probe or schedule only, so they
    # must not be gated on a stock code.
    if args.check_only:
        sys.exit(_check(config))
    if args.keepalive:
        sys.exit(_keepalive(config))
    if args.notify_test:
        sys.exit(_notify_test(config))
    if args.daemon or args.daemon_status or args.daemon_install or args.daemon_uninstall:
        sys.exit(_daemon(config, args, parser))

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

        # 选题在目标收集之前：定时任务只知道「今天该发哪只」，不知道代码。
        picked = _auto_pick(config, args, parser)

        targets = _collect_targets(args, parser)
        _add_to_watchlist(targets, args, config)
        failures = _run_all(pipeline, targets, args)
        if picked is not None:
            _record_pick(config, picked, ok=not any(code == picked.code for code, _ in failures))

        if len(targets) > 1:
            _print_batch_summary(targets, failures)
        if failures:
            # Non-zero exit so a cron job / automation can tell a bad run from a good one.
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n已中断。已成功的平台不会重发；失败的用 `tangulunjin <code> --republish` 补发。")
        sys.exit(130)


def _apply_duration_bounds(config: dict, min_seconds: int, max_seconds: int) -> None:
    """把时长目标写回 config（对辩与独白两层都写）。

    --duration-minutes / --short-form 是操作者的显式意图，必须在两种形态下都生效。
    独白形态读的是 ``dialogue.monologue`` 那一层（它有自己的默认 120-240 秒），
    只写顶层会被那一层盖掉——"我明明给了 --duration-minutes 3 却还是 4 分钟"。
    """
    dialogue = config.setdefault("dialogue", {})
    dialogue.update({"min_duration_seconds": min_seconds, "max_duration_seconds": max_seconds})
    dialogue.setdefault("monologue", {}).update(
        {"min_duration_seconds": min_seconds, "max_duration_seconds": max_seconds})


# 提示词整条进 LLM 上下文（prompt 本身已有骨架说明和几万字数据 JSON），超过这个
# 长度只是提醒一声——它不是删减阈值，内容一个字都不会丢。
MESSAGE_NOTICE_CHARS = 2000


def _resolve_message(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    """把 -m/--message 的多个片段合成一条提示词；``@文件`` 从文本文件读。

    它是「本期让 LLM 多讲点什么」的入口：侧重哪块业务、想强调哪些数据维度、想
    避开的说法都能写进来。长提示词（比如一份选题说明）用 ``@文件`` 传，免得在
    shell 里跟引号和换行较劲。
    """
    parts: list[str] = []
    for raw in (getattr(args, "message", None) or ()):
        text = str(raw).strip()
        if not text:
            parser.error("--message 不能是空字符串（要么给内容，要么整条别写）")
        if text.startswith("@") and len(text) > 1:
            path = Path(text[1:]).expanduser()
            if not path.is_file():
                parser.error(f"--message 指定的文件不存在: {path}")
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                parser.error(f"--message 读取 {path} 失败: {exc}")
            if not text:
                parser.error(f"--message 指定的文件是空的: {path}")
        parts.append(text)
    return "\n".join(parts)


def _report_message(args: argparse.Namespace, message: str) -> None:
    """说清这条提示词这次到底会不会起作用——写了却不生效是最气人的。"""
    if args.republish or args.publish_only:
        print(f"  • 本期提示词 {len(message)} 字：补发/仅发布不重新生成剧本，本次用不到它。")
        return
    if args.check_only or args.keepalive or args.notify_test:
        print(f"  • 本期提示词 {len(message)} 字：本次是预检/心跳/自检，用不到它。")
        return
    if args.daemon or args.daemon_status or args.daemon_install or args.daemon_uninstall:
        # 守护进程跑的是子进程里另一条命令行（daemon.command()），-m 不会跟着走；
        # 想让它长期生效只能写配置，写成 --daemon 的参数会静默失效。
        print(f"  • 本期提示词 {len(message)} 字：守护进程不会带上 -m（它跑的是另一条命令），"
              f"要长期生效请写进配置 dialogue.message。")
        return
    print(f"  本期提示词 {len(message)} 字 → 已注入剧本生成（dialogue.message）")
    if len(message) > MESSAGE_NOTICE_CHARS:
        print(f"    提示词较长（超过 {MESSAGE_NOTICE_CHARS} 字），会整条进 LLM 上下文，"
              f"剧本生成的耗时与成本随之上升。")


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
    account = config.get("publish", {}).get("accounts", {}) or {}
    failed: list[str] = []
    unconfirmed: list[str] = []
    for platform in platforms:
        label = PLATFORM_SPECS[platform]["label"]
        outcome = checks.get(platform, {"ok": False, "detail": "未检查"})
        if outcome["ok"]:
            print(f"    ✓ {label}({platform})")
        elif outcome.get("timed_out"):
            # 超时 ≠ 失效。混在一起说会把人骗去重新登录，而重新登录会把**有效的**
            # cookie 覆盖掉 —— 一个本来能发的平台就这么变成真的需要重登。
            unconfirmed.append(platform)
            print(f"    ! {label}({platform})\n        探活超时，状态未知（不等于失效）")
        else:
            failed.append(platform)
            print(f"    ✗ {label}({platform})\n        {_one_line(outcome['detail'])}")

    healthy = len(platforms) - len(failed) - len(unconfirmed)
    print(f"\n  结论: 平台 {healthy}/{len(platforms)} 可发布"
          + (f"，{len(unconfirmed)} 个状态未知" if unconfirmed else "")
          + (f"，环境缺 {len(blocked)} 项必需工具" if blocked else ""))
    if failed:
        print("  需要重新登录的平台（在 third_party/social-auto-upload 下执行）:")
        for platform in failed:
            print(f"    uv run sau {platform} login --account {account.get(platform, 'default')}")
    if unconfirmed:
        print("  探活没问出结果的平台（先单独重试一次；**别急着重新登录**，那会覆盖现有 cookie）:")
        for platform in unconfirmed:
            print(f"    uv run sau {platform} check --account {account.get(platform, 'default')}")
    if blocked:
        for item in blocked:
            print(f"  必需工具缺失: {item['label']} — {item['detail']}")
    return 1 if (failed or blocked or unconfirmed) else 0


def _keepalive(config: dict) -> int:
    """登录态心跳：一次真实活动 + 落盘时间戳，供定时任务判断。

    视频号是唯一必须这么做的平台：本地 cookie 的 expires 是 2027（看文件永远
    不过期），真正会死的是服务端会话。心跳有两个作用：把窗口尽量往后推，以及
    把失效**提前到发布之前**（发布时才发现，赔掉的是一整次渲染）。

    每次心跳还会报出**会话年龄**（距上次扫码登录多久）。这是唯一能区分两种世界的
    指标：如果会话在 20h+ 的年龄上仍然 alive，说明活动能续期，心跳就够了；如果
    它照样在十来个小时后死掉，那就说明服务端不认这些活动，方案必须改成「在新鲜的
    会话里把后续发布一次性排好期」（`sau tencent upload-video --schedule`）。

    失效时的补救不需要人坐在电脑前：`sau tencent login` 本来就是无头登录，会把
    二维码存成 cookies/<account>_tencent_login_qrcode_<ts>.png 并等待扫码约
    5 分钟——把这个 PNG 推给手机，扫一下就能满血复活。
    """
    publisher = SauPublisher(config)
    platforms = [p for p in config.get("publish", {}).get("platforms", ()) if p in PLATFORM_SPECS]
    if not platforms:
        print("未配置任何平台（publish.platforms）")
        return 1

    print("登录态心跳")
    results = publisher.keepalive_platforms(platforms)
    dead: list[str] = []
    unconfirmed: list[str] = []
    for platform in platforms:
        label = PLATFORM_SPECS[platform]["label"]
        outcome = results.get(platform, {"ok": False, "detail": "未检查", "action": None})
        action = outcome.get("action") or "check"
        age = outcome.get("session_age_hours")
        since = f"  会话已 {age:g}h（{outcome.get('session_started_at')} 扫码）" if age is not None else ""
        if outcome["ok"]:
            print(f"    ✓ {label}({platform})  {action}{since}")
        elif outcome.get("timed_out"):
            # 超时没问出结果，与「确认失效」是两回事：后者要重新登录，前者只需要重试。
            unconfirmed.append(platform)
            print(f"    ! {label}({platform})  {action} 超时，状态未知（不等于失效）{since}")
        else:
            dead.append(platform)
            print(f"    ✗ {label}({platform})  {action}{since}\n        {_one_line(outcome['detail'])}")

    if unconfirmed:
        account = config.get("publish", {}).get("accounts", {}) or {}
        print("\n  探活超时的平台（先单独重试；**别急着重新登录**，那会覆盖现有 cookie）:")
        for platform in unconfirmed:
            print(f"    uv run sau {platform} check --account {account.get(platform, 'default')}")

    if dead:
        account = config.get("publish", {}).get("accounts", {}) or {}
        print("\n  失效补救（无头登录 + 二维码，手机扫一下即可）:")
        for platform in dead:
            print(f"    uv run sau {platform} login --account {account.get(platform, 'default')}")
        print(f"  二维码会落在 {publisher.workdir}/cookies/ 下（*_login_qrcode_*.png）")
    print(f"  心跳记录: {publisher.session_path}")
    return 1 if (dead or unconfirmed) else 0


def _notify_test(config: dict) -> int:
    """按 notify 配置真发一条。**这条命令的意义就在于「别等出事那天才发现收不到」。**

    有两个坑它专门负责提前暴露：一是通道填了但没填全（webhook 的 url 空着），
    看起来配了其实一次都不会发；二是 macOS 那边通知权限没给（launchd 起的进程
    没有 bundle，通知算在「脚本编辑器」名下），脚本不报错但横幅根本不出现 ——
    所以这里立刻告诉你「去看看系统设置」。
    """
    notifier = build_notifier(config, root=PROJECT_ROOT)
    print("反馈通道自检")
    if not notifier.channels:
        print("  notify.channels 是空的 —— 没有任何通道，跑完不会推任何东西")
        return 1
    if not notifier.enabled:
        print("  notify.enabled 是 false —— 反馈是关的。")
        print("  在配置里把它打开（或在 --config 指定的配置里覆盖）再自检。")
        return 1

    report = RunReport(
        kind=TEST,
        at=datetime.now().isoformat(timespec="seconds"),
        detail="通道自检：收到这条，说明运行结果也能推到这里",
    )
    results = notifier.send(report)
    sent = [item for item in results if item["ok"]]
    for item in results:
        if item.get("skipped"):
            print(f"  · {item['kind']}: 跳过（{item['detail']}）")
        elif item["ok"]:
            print(f"  ✓ {item['kind']}: 已发出")
        else:
            print(f"  ✗ {item['kind']}: {item['detail']}")
    if not sent:
        print("\n一条都没发出去。检查上面的通道配置；本机通知还要看「系统设置 → 通知 →"
              " 脚本编辑器」有没有被允许。")
        return 1
    print(f"\n已通过 {len(sent)} 个通道发出。没收到？本机通知看「系统设置 → 通知 → 脚本编辑器」，"
          "手机推送看群机器人是否被移除。")
    return 0


def _daemon(config: dict, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """守护进程的四个动作：跑、装、卸、看。

    它是 ``tangulunjin --auto-pick --publish`` 的调度器 —— 子进程跑的就是那条命令，
    所以「守护进程发了什么」和「你手敲会发什么」永远是同一件事，不会分叉。
    """
    try:
        daemon = build_daemon(config, root=PROJECT_ROOT)
    except DaemonError as exc:
        parser.exit(1, f"\n守护进程配置有问题：{exc}\n")

    if args.daemon_uninstall:
        print("卸载守护进程")
        for line in uninstall_launchd():
            print(f"  {line}")
        return 0

    if args.daemon_status:
        for line in daemon.status_lines():
            print(line)
        print(f"  {launchd_state()}")
        return 0

    if args.daemon_install:
        try:
            outcome = install_launchd(daemon)
        except DaemonError as exc:
            parser.exit(1, f"\n{exc}\n")
        print("安装守护进程")
        for line in outcome.steps:
            print(f"  {line}")
        # 没真正加载上就退非零：脚本/自动化才不会以为装好了。
        return 0 if outcome.loaded else 1

    if not daemon.scheduler.windows:
        parser.exit(1, "\n守护进程没有任何发布窗口（daemon.windows 为空），跑了也不会发东西。\n"
                       "  在配置里写两个窗口，例如：\n"
                       "    daemon:\n"
                       '      windows: ["07:30-09:30", "18:30-21:00"]\n')
    return daemon.run_forever()


def _gate_publish(config: dict, args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Probe every configured platform's login before any expensive work starts.

    A full run costs ~15 minutes of generation plus render; finding a dead
    cookie at publish time throws all of it away (视频号隔天失效是常态，
    2026-09-19/20 实测).  Probe up front: on failure print the login remedy
    and let the operator either quit to re-login or explicitly continue with
    the healthy platforms only.

    ``--continue-anyway`` 是给无人值守用的那一档：守护进程凌晨三点跑，没人能回答
    「继续吗」，而「发到 4/5 个平台」永远好过「一个都不发」。失效的平台会被
    :meth:`SauPublisher.publish` 在建预检时跳过并记进 sidecar，所以它们之后仍然
    可以 ``--republish`` 补上。

    ``--auto-login`` 是更高一档：失效的平台先**自己试着登回来**（见
    :func:`_auto_relogin`），修好就照常发布，修不好再落到上面那套。视频号隔天失效
    是常态，而它的登录页有「微信快捷登录」——本机微信在跑就能静默重登，没人值守也
    能自愈。
    """
    if getattr(args, "no_preflight", False):
        return
    platforms = [p for p in config.get("publish", {}).get("platforms", ()) if p in PLATFORM_SPECS]
    if not platforms:
        return
    checks = SauPublisher(config).check_platforms(platforms)
    failed = [p for p in platforms
              if not checks.get(p, {}).get("ok") and not checks.get(p, {}).get("timed_out")]
    unconfirmed = [p for p in platforms if checks.get(p, {}).get("timed_out")]

    if unconfirmed:
        # 超时只是没问出来，发布本身照试：拿「不知道」拦下整条命令，等于因为一次
        # 网络卡顿放弃四个健康平台。真发不出去会在发布阶段记进 sidecar，照样能
        # 用 --republish 补，代价只是一次上传而不是一次渲染。
        names = "、".join(f"{PLATFORM_SPECS[p]['label']}" for p in unconfirmed)
        print(f"  • 发布预检：{names} 探活超时（状态未知，不等于失效）—— 照发，失败会记进 sidecar。")

    if not failed:
        if not unconfirmed:
            print("发布预检: " + "、".join(f"{PLATFORM_SPECS[p]['label']}" for p in platforms)
                  + " 登录态均有效")
        return

    if getattr(args, "auto_login", False):
        # 会自己好的那部分先自己修好：视频号隔天失效是常态，而它的登录页有
        # 「微信快捷登录」，本机微信在跑就能静默重登（~33s，不用扫码）。修好再
        # 复核；仍然坏的那些继续走下面的原有分支（继续发/退出/询问）。
        failed = _auto_relogin(config, failed)
        if not failed:
            print(f"\n✓ 发布预检：自动登录后 {len(platforms)}/{len(platforms)} 个平台均可用，继续。")
            return

    labels = "、".join(f"{PLATFORM_SPECS[p]['label']}({p})" for p in failed)
    account = config.get("publish", {}).get("accounts", {}) or {}
    remedy = [f"    uv run sau {platform} login --account {account.get(platform, 'default')}"
              for platform in failed]

    if getattr(args, "continue_anyway", False):
        print(f"\n⚠ 发布预检：{labels} 登录态失效 —— 按 --continue-anyway 跳过它们，"
              f"其余 {len(platforms) - len(failed)}/{len(platforms)} 照发。")
        for line in remedy:
            print(line)
        print("  这些平台会记进 sidecar，登录好之后可用 `tangulunjin <code> --republish` 补上。")
        return

    print(f"\n⚠ 发布预检：{labels} 登录态失效，直接发布会跳过它们：")
    for line in remedy:
        print(line)
    if args.no_interactive or not (sys.stdin and sys.stdin.isatty()):
        parser.exit(1, "\n已退出。请先重新登录再重跑本命令，或用 --platforms 只发布健康平台。\n"
                       "  无人值守场景加 --continue-anyway 就会自动跳过掉线的平台继续发。\n")
    try:
        answer = input("继续发布到其余平台？[y=继续 / N=退出，先去登录] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        parser.exit(1, "已退出。重新登录后重跑即可；已生成的片子随时可用 --publish-only 补发。\n")


def _auto_relogin(config: dict, failed: list[str]) -> list[str]:
    """Try to bring dead platforms back without a human; return the still-dead ones.

    只有能无人值守完成的平台会被真的尝试（:data:`AUTO_LOGIN_PLATFORMS`：目前只有
    视频号，靠登录页的「微信快捷登录」）。其余平台在这里只是被点出来，原样返回 ——
    它们要么等一次扫码，要么根本不该在没人看着的时候被"试一下"。

    登录动作退出码 0 只说明流程跑完了，**能不能发由复核说了算**，所以这里再探一次；
    探活超时按「没问出来」处理（与 :func:`_gate_publish` 同一口径）：不因为一次网络
    抖动把刚修好的平台又判死，真发不出去会在发布阶段记进 sidecar，照样能 --republish。
    """
    publisher = SauPublisher(config)

    def name(platform: str) -> str:
        return PLATFORM_SPECS[platform]["label"]

    manual = [p for p in failed if p not in AUTO_LOGIN_PLATFORMS]
    targets = [p for p in failed if p in AUTO_LOGIN_PLATFORMS]
    if manual:
        print("  • 需要人工扫码登录（自动登录不适用）: " + "、".join(name(p) for p in manual))
    if not targets:
        return failed

    print("⚡ 自动重新登录: " + "、".join(name(p) for p in targets))
    outcomes = publisher.auto_login_platforms(targets)
    attempted: list[str] = []
    for platform in targets:
        outcome = outcomes.get(platform) or {}
        if not outcome.get("attempted"):
            print(f"    - {name(platform)}: {_one_line(str(outcome.get('detail') or '未尝试'))}")
            continue
        attempted.append(platform)
        mark = "✓" if outcome.get("ok") else "✗"
        detail = "" if outcome.get("ok") else f" — {_one_line(str(outcome.get('detail') or ''))}"
        print(f"    {mark} {name(platform)}: {'重新登录成功' if outcome.get('ok') else '重新登录失败'}{detail}")

    if not attempted:
        return failed

    print("    复核登录态…")
    checks = publisher.check_platforms(attempted)
    still_dead: list[str] = []
    for platform in failed:
        if platform not in attempted:
            still_dead.append(platform)
            continue
        check = checks.get(platform) or {}
        if check.get("ok") or check.get("timed_out"):
            print(f"    ✓ {name(platform)} 已恢复（探活通过）")
        else:
            print(f"    ✗ {name(platform)} 仍未恢复: {_one_line(str(check.get('detail') or '探活失败'))}")
            still_dead.append(platform)
    return still_dead


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


def _auto_pick(config: dict, args: argparse.Namespace, parser: argparse.ArgumentParser) -> Any | None:
    """--auto-pick：从 tongstock 热门榜里取榜首未发过的那只当今天的目标。

    定时任务不知道代码，也不该让我每次去猜。榜单是当日新闻聚合的结果（只算标题
    命中与数据源原生关联），所以榜首是一个能写进申明的选题理由；轮转记录在
    output/.topic_rotation.json，同一天早晚两条会依次取榜首、榜眼，而不是重复同一只。
    """
    if not args.auto_pick:
        return None
    if args.stock_codes or args.watchlist:
        parser.error("--auto-pick 和股票代码/--watchlist 二选一：前者自己选票，后者是你指定")

    picker = TopicPicker(config)
    try:
        topics = picker.topics(top=args.topic_top)
    except TopicError as exc:
        parser.exit(1, f"\n选题失败：{exc}\n{_topic_retry_hint()}")
    topic = picker.select(topics)
    if topic is None:
        parser.exit(1, f"\n选题失败：榜单前 {len(topics)} 名都已发过（或连续失败 "
                       f"{picker.max_runs} 次）。\n"
                       f"  榜单: {'、'.join(t.name for t in topics[:5])}\n"
                       f"  记录: {picker.rotation_path}（删掉对应条目即可重发）\n")
    print(f"选题（tongstock 热门榜）：{topic.describe()}")
    args.stock_codes = [topic.code]
    args.name = topic.name or None
    return topic


def _record_pick(config: dict, topic: Any, ok: bool) -> None:
    """把这次结果写进轮转记录：成功标记已发，失败累计次数（够次数就跳过）。"""
    record = TopicPicker(config).record(topic, ok)
    state = "已发布" if record["published"] else f"未成功（第 {record['runs']} 次）"
    print(f"选题记录：{topic.name}({topic.code}) {state} → {TopicPicker(config).rotation_path}")


def _topic_retry_hint() -> str:
    return "  手动指定代码即可绕过选题：tangulunjin 601689 --publish"


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
        parser.error("请给出至少一个股票代码（tangulunjin 601689），"
                     "或用 --watchlist 指定文件，或加 --auto-pick 让热门榜选票")
    seen: set[str] = set()
    ordered: list[tuple[str, str | None]] = []
    for code, name in targets:
        if code not in seen:
            seen.add(code)
            ordered.append((code, name))
    return ordered


# ---------------------------------------------------------------------------
# tongstock 自选股：每期目标个股开跑前入列（best-effort，失败不阻断制作/发布）
# ---------------------------------------------------------------------------

WATCHLIST_GROUP = "谈股论金"
WATCHLIST_TIMEOUT_SECONDS = 60


def _tongstock_executable(config: Mapping[str, Any]) -> str:
    """Resolve the tongstock binary, tolerating a bare PATH that misses ~/.local/bin."""
    configured = str(((config or {}).get("topics") or {}).get("executable") or "tongstock")
    if Path(configured).is_absolute():
        return configured
    found = shutil.which(configured)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / configured
    return str(fallback) if fallback.is_file() else configured


def _add_to_watchlist(targets: list[tuple[str, str | None]], args: argparse.Namespace,
                      config: Mapping[str, Any]) -> None:
    """把本期目标写进 tongstock 自选股，分组「谈股论金」、备注运行日期。

    它是旁路（同通知层原则）：写失败只提醒一声，绝不把一次能跑的生成/发布变成
    失败——自选股是记账，不是链路的一部分。目标集合在 _collect_targets 之后已
    确定，所以手动代码、--watchlist 文件、--auto-pick 三种选题都覆盖。
    """
    if not getattr(args, "add_watchlist", True):
        return
    codes = [code for code, _ in targets]
    if not codes:
        return
    note = f"tangulunjin {datetime.now():%Y-%m-%d}"
    command = [_tongstock_executable(config), "watchlist", "add", *codes,
               "--group", WATCHLIST_GROUP, "--note", note]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=WATCHLIST_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  • 加入自选股失败（不阻断）：{exc}")
        return
    if completed.returncode == 0:
        print(f"  • 已加入 tongstock 自选股（分组「{WATCHLIST_GROUP}」）：{'、'.join(codes)}")
        return
    lines = [line for line in (completed.stdout or "").splitlines() + (completed.stderr or "").splitlines()
             if line.strip()]
    detail = lines[-1] if lines else f"退出码 {completed.returncode}"
    print(f"  • 加入自选股失败（不阻断）：{detail}")


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


def _video_lines(result: dict) -> list[str]:
    """One line per rendered canvas, naming the platforms each one feeds.

    The inventory itself lives in ``modules.review`` so the summary and the
    review page can never disagree about which cut goes where.
    """
    lines = []
    for entry in video_inventory(result["video_path"], result.get("platform_videos") or {},
                                 result.get("platform_canvas") or {}, result.get("canvas") or ""):
        targets = f"   → 用于 {entry['platforms']}" if entry["platforms"] else ""
        lines.append(f"{entry['label']}: {entry['path']}{targets}")
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
    for preset, path in (result.get("covers") or {}).items():
        print(f"  封面: {COVER_LABELS.get(preset, preset)}: {path}")
    if result.get("review_path"):
        print(f"  审查: {result['review_path']}（浏览器打开可同屏看成片与封面）")


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
