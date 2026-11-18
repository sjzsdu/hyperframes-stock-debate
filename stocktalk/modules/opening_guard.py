"""开场门禁与跨期开场轮转记忆（2026-11-15 涨粉改造 P0-1 / P0-5）。

背景：提示词里示范语气的例句被模型当模板照抄——「这价我先上车」出现在 20 期
的 15 期开场里；sharpness 的 hook 判定只报告不拦截，问题一路渲到成片。这里把
「开场这一件事」收成一道闸：

1. **体检**（:func:`problems`）：前两轮命中口头禅、hook/front_load 判定未过、
   与近 N 期开场骨架同款——三种情况都算不合格。
2. **轮转记忆**（ledger）：每期开场骨架落盘（``output/.opening_rotation.json``），
   同款句式跨期直接判不合格；最近几期的开场还会喂进提示词当反面参照。

边界：门禁只判开场，且生成器最多重生成一次——两次都不行就带着警告放行。
闸是用来提概率的，不是用来把整条流水线变成偶发失败点的。
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from stocktalk.modules import copy_guard, sharpness

# 开场骨架取前多少个有效字比较。12 个字足够认出「这价我先上车」式同款句式，
# 又不会把「同样是…」这种正常语义重复误判成撞车。
SKELETON_SIZE = 12
# 轮转记忆保留期：开场句式是当下语感的产物，一个月前的「已用过」没有参考价值。
KEEP_DAYS = 14
# 只看前两轮：第三轮起是正文交锋，撞句式不是开场问题。
OPENING_TURNS = 2

_PUNCT = re.compile(r"[\s，。、！？；：\"'“”‘’（）()《》【】…—·,.!?;:\-~～/]+")


def _day_offset(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def skeleton(text: str, size: int = SKELETON_SIZE) -> str:
    """开场的可比指纹：去标点、去数字后的前 N 个字。"""
    compact = _PUNCT.sub("", str(text or ""))
    return compact[:size]


def load_ledger(path: Path | str, *, keep_days: int = KEEP_DAYS) -> list[dict[str, Any]]:
    """读轮转记忆，顺手剪掉过期条目（读不到就当空表）。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    entries = [item for item in raw if isinstance(item, Mapping)]
    if keep_days > 0:
        cutoff = _day_offset(-keep_days)
        entries = [dict(item) for item in entries if str(item.get("day") or "") >= cutoff]
    return entries


def remember(path: Path | str, *, code: str, opening: str, day: str) -> None:
    """把本期开场记进轮转记忆（尾部追加，超期条目顺手剪掉）。"""
    entries = load_ledger(path, keep_days=0)
    entries.append({"day": day, "code": str(code or ""), "opening": str(opening or "")[:80],
                    "skeleton": skeleton(opening)})
    if KEEP_DAYS > 0:
        cutoff = _day_offset(-KEEP_DAYS)
        entries = [item for item in entries if str(item.get("day") or "") >= cutoff]
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(entries[-60:], ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        # 轮转记忆是锦上添花：写不进去只影响「开场防重复」，不许打断生成。
        pass


def recent_openings(path: Path | str, limit: int = 8) -> list[str]:
    """最近几期的开场原句——喂进提示词当反面参照。"""
    return [str(item.get("opening") or "") for item in load_ledger(path)[-limit:]
            if str(item.get("opening") or "").strip()]


def problems(script: Mapping[str, Any], recent: Sequence[str] = ()) -> list[str]:
    """开场体检：不合格项的人话清单（空清单 = 放行）。

    三道判定各自独立，谁红了都点名：

    * 口头禅/套话命中（copy_guard 扫前两轮）；
    * sharpness 的 hook 与 front_load 两条（前 25 字钩子、不许铺背景报科目）；
    * 与近几期开场骨架同款（跨期防复读）。
    """
    turns = [t for t in (script.get("turns") or []) if isinstance(t, Mapping)]
    lines = [str(t.get("line") or "").strip() for t in turns[:OPENING_TURNS] if str(t.get("line") or "").strip()]
    issues: list[str] = []
    if not lines:
        return ["开场为空"]
    head = "\n".join(lines)
    for hit in copy_guard.scan(head):
        issues.append(f"开场命中套话「{hit['phrase']}」")
    checks = {str(item.get("id")): item for item in sharpness.checks(script)
              if isinstance(item, Mapping)}
    for check_id, label in (("hook", "前 25 字钩子"), ("front_load", "开场不铺背景")):
        item = checks.get(check_id)
        if item and not item.get("ok"):
            issues.append(f"{label}未过（{item.get('detail') or ''}）")
    opening_key = skeleton(lines[0])
    if opening_key:
        for past in recent:
            if skeleton(past) == opening_key:
                issues.append(f"开场与近几期同款句式（{lines[0][:16]}…）")
                break
    return issues
