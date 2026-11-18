"""抖音快评版剪辑（2026-11-15 P1-2）：把 2 分钟正片剪成 60-90 秒的完播友好版。

为什么：抖音信息流的完播率是推流第一杠杆，而正片的中段铺垫（背景展开、
行业介绍）在「刷到的陌生人」眼里全是划走点。快评版只留四件事：

1. **开场钩子**（第一轮）——决定 3 秒留不留人；
2. **最狠的交锋轮**（情绪烈 + 带数字的中段轮）——决定「原来如此」；
3. **收束 + 风险提示轮**（片尾）——决定互动与合规；
4. 剩余预算按情绪/数字打分填中段，时序不变。

剪辑零重合成：台词不改一个字，逐轮 wav 直接复用（时间轴按选中段重新归零），
画面走既有 HyperFrames 管线——合规审查、AI 标注、封面、发布元数据全部沿用
正片，短版只是「轮次子集」。选不进快评版的轮次不是被否定，只是没进这条片。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from stocktalk.modules.dialogue_generator import SPEECH_CHARS_PER_SECOND

# 片尾无论如何要保住的收束轮数：wrap/closing 的收束一来一回（风险提示轮若存在
# 会被单独识别并额外保留，见 select_turns）。收束决定「观众带走什么问题」。
KEEP_TAIL = 2
# 风险提示轮的识别标记：程序追加的那轮台词必含「股市有风险」。
_RISK_ANCHOR = "股市有风险"
# 快评版最少要有几轮：开场 + 一轮交锋 + 收尾，低于这个数剪出来不成片。
MIN_TURNS = 4
# 中段轮的打分词表：这些情绪是「交锋真发生了」的标记。
HOT_EMOTIONS = ("反驳", "质疑", "惊叹", "顿悟")

_DIGIT = re.compile(r"[0-9０-９]")


def select_turns(turns: Sequence[Mapping[str, Any]], *, max_seconds: float,
                 speech_rate: float = SPEECH_CHARS_PER_SECOND,
                 keep_tail: int = KEEP_TAIL, min_turns: int = MIN_TURNS) -> list[int]:
    """选出快评版要保留的轮次下标（升序、时序不变）。

    片尾保「收束一来一回」，外加**若存在风险提示轮**（台词含「股市有风险」）也
    一并保留——风险轮默认已关闭（2026-11-17），但开回来时快评版不能把它剪掉。
    预算按字数算（字数 / 语速 ≈ 秒）。正片本来就在预算内时全保留——
    快评版只对「比预算长」的正片动刀。
    """
    items = [t for t in turns if isinstance(t, Mapping)]
    lines = [str(t.get("line") or "").strip() for t in items]
    total = len(items)
    if total <= min_turns:
        return list(range(total))
    budget = float(max_seconds) * speech_rate
    if sum(len(line) for line in lines) <= budget:
        return list(range(total))

    keep: set[int] = {0}                       # 开场钩子
    for index in range(max(1, total - keep_tail), total):
        keep.add(index)                        # 收束一来一回
    for index in range(total):
        if _RISK_ANCHOR in lines[index]:       # 风险提示轮（若开了口播风险轮）
            keep.add(index)

    def score(index: int) -> int:
        turn = items[index]
        value = 0
        if str(turn.get("emotion") or "") in HOT_EMOTIONS:
            value += 2
        if _DIGIT.search(lines[index]):
            value += 1
        if str(turn.get("beat") or "") != "closing":
            value += 1
        return value

    used = sum(len(lines[index]) for index in keep)
    middle = [index for index in range(1, max(1, total - keep_tail)) if lines[index]]
    for index in sorted(middle, key=lambda i: (-score(i), i)):
        if used + len(lines[index]) <= budget:
            keep.add(index)
            used += len(lines[index])
    # 开场 + 片尾都进不去的极端预算下，保开场和片尾优先（集合已保证）。
    return sorted(keep)


def build_short_cut(script: Mapping[str, Any], timeline: Mapping[str, Any], *,
                    max_seconds: float, pause: float = 0.12,
                    speech_rate: float = SPEECH_CHARS_PER_SECOND) -> dict[str, Any]:
    """把正片剪成快评版：返回 {script, timeline, kept, dropped, estimated_seconds}。

    ``timeline`` 就是 TTS 的产出（``{"segments": [...]}``）；选中的段按轮次顺序
    重新归零计时，audio_path 原样复用——渲染层按段放音频，不需要拼接 wav。
    """
    turns = [t for t in script.get("turns", ()) if isinstance(t, Mapping)]
    keep = select_turns(turns, max_seconds=max_seconds, speech_rate=speech_rate)
    kept_turns = [dict(turns[index]) for index in keep]

    pool = [dict(item) for item in (timeline.get("segments") or ()) if isinstance(item, Mapping)]
    segments: list[dict[str, Any]] = []
    for turn in kept_turns:
        line = str(turn.get("line") or "").strip()
        for offset, seg in enumerate(pool):
            if str(seg.get("line") or "").strip() == line:
                segments.append(pool.pop(offset))
                break
    cursor = 0.0
    for seg in segments:
        duration = max(0.1, float(seg.get("duration") or 0.0))
        seg["start_time"] = cursor
        seg["end_time"] = cursor + duration
        cursor += duration + max(0.0, float(pause))
    spoken = sum(max(0.1, float(seg.get("duration") or 0.0)) for seg in segments)
    if segments:
        spoken += max(0.0, float(pause)) * (len(segments) - 1)

    short_script = dict(script)
    short_script["turns"] = kept_turns
    short_script["char_count"] = sum(len(str(t.get("line") or "")) for t in kept_turns)
    short_script["estimated_seconds"] = round(spoken or short_script["char_count"] / speech_rate, 1)
    return {
        "script": short_script,
        "timeline": {"segments": segments,
                     "total_duration": round(spoken, 3)},
        "kept": len(kept_turns),
        "dropped": len(turns) - len(kept_turns),
        "estimated_seconds": short_script["estimated_seconds"],
    }
