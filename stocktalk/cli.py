"""StockTalk CLI — command-line interface for debate video generation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stocktalk.pipeline import Pipeline, PipelineError, load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="stocktalk",
        description="StockTalk — 全自动 AI 财报对话视频生成工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  stocktalk 600519 --name 贵州茅台\n"
            "  stocktalk 000001 --config my_config.yaml\n"
            "  stocktalk 600519 --duration-minutes 3 --output-dir ./videos\n"
        ),
    )
    parser.add_argument("stock_code", help="6位A股代码，如 600519、000001")
    parser.add_argument("--name", default=None, help="股票名称（可选，会自动获取）")
    parser.add_argument("--config", default=None, help="自定义配置文件路径")
    parser.add_argument("--output-dir", default=None, help="输出目录（默认 output/）")
    parser.add_argument("--duration-minutes", type=int, default=None, help="对话目标时长（分钟，默认 2-5）")
    parser.add_argument("--no-render", action="store_true", help="仅生成 HTML 项目，跳过 MP4 渲染")
    parser.add_argument("--preview", action="store_true", help="仅生成 HTML 预览项目（等同于 --no-render）")

    args = parser.parse_args(argv)

    config = load_config(args.config)

    if args.output_dir:
        config.setdefault("output", {})["dir"] = args.output_dir

    if args.duration_minutes is not None:
        if args.duration_minutes < 1:
            parser.error("--duration-minutes 必须为正整数")
        seconds = args.duration_minutes * 60
        config.setdefault("dialogue", {}).update({"min_duration_seconds": seconds, "max_duration_seconds": seconds})

    pipeline = Pipeline(config)
    try:
        result = pipeline.run(
            args.stock_code, stock_name=args.name, render=not args.no_render, preview=args.preview,
        )
    except PipelineError as exc:
        parser.exit(1, f"\n生成失败：{exc}\n")

    print(f"\n✓ 视频生成完成")
    print(f"  股票: {result['stock_name']} ({result['stock_code']})")
    print(f"  发言: {len(result['script'].get('turns', []))}")
    print(f"  输出: {result.get('project_dir', 'output/')}")
    if result["rendered"]:
        print(f"  视频: {result['video_path']}")
    else:
        print("  MP4 渲染已跳过")


if __name__ == "__main__":
    main()
