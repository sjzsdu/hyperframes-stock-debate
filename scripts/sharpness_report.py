"""锐度判定清单的离页读数（2026-11-02 WAN-28）。

给一份剧本 JSON（``output/<代码>_<时间戳>.json``，整份运行结果或剧本本身都行）
打印七条判定，用来做"同一只票改动前后各跑一次，哪几条从红转绿"的对比。

``refutable`` 换了口径之后（分母从"全片轮次"改成"出现数字的轮次"），这条判定
的读数需要拿真实样本复算——这个脚本就是为它留的：跑完一期就有读数，不必改代码
去凑一个好看的样本。

    python3 scripts/sharpness_report.py output/600519_20261027_101500.json
    python3 scripts/sharpness_report.py output/600519_*.json   # 多期一起比
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocktalk.modules import sharpness  # noqa: E402


def _script(payload: dict) -> dict:
    """运行结果 JSON 里剧本挂在 ``script`` / ``approved_script`` 下。"""
    for key in ("approved_script", "script"):
        value = payload.get(key)
        if isinstance(value, dict) and value.get("turns"):
            return value
    return payload


def main(paths: list[str]) -> int:
    for path in paths:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        report = sharpness.evaluate(_script(payload))
        print(f"\n{path}  {sharpness.summary(report)}")
        for item in report["checks"]:
            print(f"  [{'PASS' if item['ok'] else 'FAIL'}] {item['id']:<12} {item['detail']}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
