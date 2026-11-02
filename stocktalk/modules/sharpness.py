"""成片文案的「锐度」判定清单（2026-10-27 WAN-27）。

用户的原话是「像在报流水账」「看完不想看下一集」，还说不出该怎么描述那种
状态。不可描述的标准就没法验收，所以这里把它拆成 7 条能机读、能引用原文
的判定——每一条都对应一个具体的失败形状，而不是「更好看」：

===========  ==================================================================
id           失败的形状
===========  ==================================================================
hook         开头在报幕/行业定性/会计科目，前 25 字里没有一个让人想问「为什么」的东西
front_load   前两句就铺背景、报出处、做自我介绍，钩子被消耗在铺垫上
opposing     两边立场其实一样（都夸，或都担心），没有真正的对立面
refutable    论点是结论式的形容（"护城河很深"），拿不出可被反驳的具体依据
anomaly      全片没有一个反常识点或数字锚点——讲完等于什么都没留下
no_rerun     套话命中、或同一句表述在片里复读三次以上（`copy_guard`）
sharp_close  片尾预告「等下一份财报」，把结论推给未来
===========  ==================================================================

用途写清楚，避免被当成质量裁判：这是**对比工具**——同一只票改动前后各跑
一次，看哪几条从红转绿、哪几条仍然红；也是**写作约束的可核对化**，让"锐"
从口头要求变成成片旁边看得见的一栏。它不判断观点对错，也不因为分数低就
拦下渲染：一期内容好不好，最终仍由人看片决定。

口径是启发式的（正则 + 计数），刻意保守：宁可漏报也不要误杀——一条把合格
台词判成不合格的规则，最后一定会被人关掉。

**``refutable`` 的口径重设（2026-11-02 WAN-28）**：原口径要求「≥50% 的轮次
带具体数字/环节」，与 ``episode_plan`` 的数字纪律正面冲突——全片 anchor ≤4、
且任意连续两轮不得都有新数字，12 轮片子的合法上限就是 33%（19 轮才勉强 53%）。
它实际测的是「模型有没有违反数字纪律」，不是「论点可不可反驳」；而且唯一能
合法过线的路子是往半数轮次塞「占比/现金流/存货」这类裸词骗过词表，那正是
数字纪律要禁止的事。一条永远红的灯最后一定会被整列关掉，所以改的是口径，
不是门槛。

现在的分母换成**出现数字的轮次**，判定落在「报了数字就要讲出含义」：

* 一处数字要么被翻译成含义（因果/反差/反问/直接推翻），要么就是裸报——裸报
  不进 ``judged``，因此直接拉低覆盖率；
* 过线要求覆盖率 ≥75% 且至少两处带论证的数字论点（ANOMALY 之外的另一条底线：
  整片只有一处论断仍然可能在报流水账）。

12 轮 4 个 anchor 的片子只要 4 处都讲清含义就是 100%，与 anchor 额度**同向**
而不是对冲；「往轮次里塞裸数字」只会增加裸报、把判定踩红，骗不过去。
``JUDGEMENT_WORDS`` 对中文口语的识别不全是已知局限（宁可漏报也不要误杀），
所以留了一处余量（75% 而不是 100%），而不是把裸报硬判成 0 容忍。

**两条"容易绿"的判定（记账，2026-11-02 WAN-28）**：``anomaly`` 在
``spotlight.value`` 非空时即为真，而生产提示词强制产出 spotlight——它实际
检查的是「画面字段有没有配齐」，与台词质量无关；``opposing`` 的反驳词表含
高频词「可是」，两边互相客套、各说两个「可是」也能过。两条都还能被单独踩红
（不是永真项），所以暂不改判定，但读分数时别把它们当成可信的质量信号。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from stocktalk.modules import copy_guard

# 开场不许出现的东西：与 episode_plan 的 r1_hook 硬约束同一份词表。开场就报
# 会计科目/财务比率/报告期，是"这份片子是给分析师看的"最直接的信号。
ACCOUNTING_TERMS: tuple[str, ...] = (
    "合同负债", "预收款", "应收账款", "应收款", "存货", "现金流", "经营性现金流",
    "毛利率", "净利率", "市盈率", "市净率", "ROE", "每股收益", "报告期", "财报", "年报",
)
PRICE_TERMS: tuple[str, ...] = ("股价", "收盘", "开盘", "涨幅", "跌幅", "涨停", "跌停", "K线")

# 反差/反常识：这些词一出现，说明说话人把一个期待中的事实拧过来了。
CONTRAST_WORDS: tuple[str, ...] = (
    "反而", "反倒", "反过来", "竟然", "居然", "偏偏", "却是", "却只", "只有", "只剩",
    "一半", "九成", "八成", "不到", "全靠", "全靠它", "越", "宁可", "换不来", "撑不住",
)
# 直接推翻对方：对立面不是"我认同，但是"，是"你这条依据站不住"。
REBUTTAL_WORDS: tuple[str, ...] = (
    "不对", "错了", "未必", "站不住", "说不通", "凭什么", "你漏了", "恰恰相反",
    "可是", "问题是", "这解释不了", "反过来", "别急", "等等", "不能这么算",
)
# 判断/因果：把数据翻译成含义的动作词。只有数字没有它，就是报数。
JUDGEMENT_WORDS: tuple[str, ...] = (
    "意味着", "说明", "因为", "所以", "取决于", "靠的是", "其实是", "才是", "换来",
    "压在", "卡在", "撑", "绑", "说明它", "换句话说", "等于", "代价",
)

# 具体锚点：带量纲的数字（阿拉伯/中文数字 + 单位），或"同行家数"这类结构化尺度。
# 口播里的中文分级（"四成四""十二个点""五百亿"）必须认得出来，否则最会讲数字的
# 那种台词反而被判成"没依据"——清单判错一次，就会被人直接关掉。
_NUM = r"[0-9０-９][0-9０-９.,]*|[一二三四五六七八九十百千万亿两半]{1,}"
_WITH_UNIT = rf"{_NUM}\s*(?:%|％|亿|万|元|倍|家|天|年|月|日|人|吨|个点|个百分点|成|次)"
# 真数了一遍的数：有值、有量纲。``refutable`` 只用这个——「存货」「占比」这种
# 裸指标名给不出论点，只能说明"提到了这一项"。
_NUMBER = re.compile(_WITH_UNIT)
_ANCHOR = re.compile(_WITH_UNIT + r"|同行家数|占比|毛利率|净利率|现金流|应收|存货")

HOOK_MAX_CHARS = 25

# 句末切分：观众听到的"第一句"是第一个句号/问号/省略号之前那一段，不是第一轮。
# 一轮台词通常有 2-4 句，拿整轮去看钩子会把"前 25 字"判成 70 字（第一版就这么判错过）。
_SENTENCE_END = re.compile(r"(?<=[。！？…])")


def _turns(script: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(turn) for turn in (script.get("turns") or ()) if isinstance(turn, Mapping)]


def _hit(text: str, words: Sequence[str]) -> list[str]:
    return [word for word in words if word in (text or "")]


def _has_anchor(text: str) -> bool:
    return bool(_ANCHOR.search(text or ""))


def _has_number(text: str) -> bool:
    """有没有一个**带量纲的数**（不是"提到了存货"）。"""
    return bool(_NUMBER.search(text or ""))


def _opening_sentences(lines: Sequence[str], limit: int = 2) -> list[str]:
    """成片真正的前 N 句（跨轮取：第一轮讲两句也算两句）。"""
    sentences: list[str] = []
    for line in lines:
        for part in _SENTENCE_END.split(line):
            part = part.strip()
            if not part:
                continue
            sentences.append(part)
            if len(sentences) >= limit:
                return sentences
    return sentences


def _check(check_id: str, label: str, ok: bool, detail: str, evidence: str = "") -> dict[str, Any]:
    return {"id": check_id, "label": label, "ok": bool(ok), "detail": detail,
            **({"evidence": evidence} if evidence else {})}


def checks(script: Mapping[str, Any]) -> list[dict[str, Any]]:
    """逐条判定，返回带原文证据的清单（审查页按这个渲染）。"""
    turns = _turns(script)
    lines = [str(turn.get("line") or "").strip() for turn in turns if str(turn.get("line") or "").strip()]
    opening = _opening_sentences(lines, 2)
    first = opening[0] if opening else ""
    head = "".join(opening)
    result: list[dict[str, Any]] = []

    # 1. hook：前 25 字里必须有一个让人想问「为什么」的东西（数字或反差或追问）。
    hook_ok = bool(first) and len(first) <= HOOK_MAX_CHARS and (
        _has_anchor(first) or bool(_hit(first, CONTRAST_WORDS)) or first.rstrip().endswith(("？", "?")))
    result.append(_check(
        "hook", "开场前 25 字给出钩子", hook_ok,
        f"第一句 {len(first)} 字" + ("，带数字/反差/追问" if hook_ok else "，没有数字、反差或追问"),
        first))

    # 2. front_load：前两句是不是在报幕、报科目、报来历。
    opening_noise = _hit(head, ACCOUNTING_TERMS) + _hit(head, PRICE_TERMS)
    meta = [hit["phrase"] for hit in copy_guard.scan(head)]
    result.append(_check(
        "front_load", "前两句不铺背景、不报科目", not opening_noise and not meta,
        "开场干净" if not (opening_noise or meta)
        else "开场出现" + "、".join(sorted(set(opening_noise + meta))),
        head[:60]))

    # 3. opposing：两边必须是真对立，且台词里真发生过推翻。
    sides = script.get("sides") if isinstance(script.get("sides"), Mapping) else {}
    bull = str(sides.get("bull") or "").strip()
    bear = str(sides.get("bear") or "").strip()
    stated = bool(bull and bear and bull != bear)
    rebuttals = sum(len(_hit(line, REBUTTAL_WORDS)) for line in lines)
    emotions = sum(1 for turn in turns if str(turn.get("emotion") or "") in ("反驳", "质疑"))
    exchanged = bool(turns) and len({str(turn.get("speaker") or "") for turn in turns}) >= 2
    opposing_ok = stated and exchanged and (rebuttals + emotions) >= 2
    result.append(_check(
        "opposing", "有明确对立面且真的交锋", opposing_ok,
        f"立场 {'两方都给了' if stated else '缺失或一致'}；"
        f"推翻/质疑 {rebuttals} 处、情绪标注 {emotions} 处",
        f"多方：{bull or '—'} / 空方：{bear or '—'}"))

    # 4. refutable：报了数字就要说清含义（2026-11-02 WAN-28 口径重设）。
    #    分母是**出现数字的轮次**，不是全部轮次：anchor ≤4 的配额决定了大多数
    #    轮次本来就不该有数字，拿全片轮次做分母，等于要求模型违反数字纪律——
    #    这正是旧口径「过半轮次带数字」永远红、并诱导人塞裸词的原因。
    #    "纯报数"的反面不止因果/判断词：反问、反差、直接推翻同样把它变成论点。
    def _is_argument(line: str) -> bool:
        return (_hit(line, JUDGEMENT_WORDS) or _hit(line, CONTRAST_WORDS)
                or _hit(line, REBUTTAL_WORDS) or line.rstrip().endswith(("？", "?")))

    numeric = [line for line in lines if _has_number(line)]
    judged = [line for line in numeric if _is_argument(line)]
    bare = [line for line in numeric if not _is_argument(line)]
    coverage = round(len(judged) / len(numeric), 2) if numeric else 0.0
    # 下限两处：整片只有一处论断的片子照样可能在报流水账。余量 75%：词表对中文
    # 口语的识别不全是已知局限，宁可漏报也不要误杀（留一处裸报不被判死）。
    refutable_ok = len(judged) >= 2 and coverage >= 0.75
    if numeric:
        detail = (f"{len(judged)}/{len(numeric)} 处数字给了依据（{coverage:.0%}）"
                  + (f"，{len(bare)} 处是裸报" if bare else "，没有裸报"))
    else:
        detail = "全片没有一处上场的数字——论点没有可被反驳的依据"
    result.append(_check(
        "refutable", "上场的数字都讲成了可被反驳的论点", refutable_ok, detail,
        judged[0] if judged else (bare[0] if bare else "")))

    # 5. anomaly：至少一处反常识或数字锚点。
    spotlight = script.get("spotlight") if isinstance(script.get("spotlight"), Mapping) else {}
    spotlight_ok = bool(str(spotlight.get("value") or "").strip())
    contrast = [line for line in lines if _hit(line, CONTRAST_WORDS) and _has_anchor(line)]
    result.append(_check(
        "anomaly", "有一处反常识或数字锚点", spotlight_ok or bool(contrast),
        ("首帧冲突数字：" + str(spotlight.get("label") or "") + str(spotlight.get("value") or ""))
        if spotlight_ok else f"台词里的反差点 {len(contrast)} 处（首帧冲突数字缺失）",
        contrast[0] if contrast else ""))

    # 6. no_rerun：套话与跨轮复读（copy_guard 是同一份清单，两处口径一致）。
    #    上屏文字（封面标题、首帧大卡）与台词同等对待：它们同样是观众看到的文案，
    #    只是不进配音（2026-11-02 WAN-28）。
    quality = script.get("quality") if isinstance(script.get("quality"), Mapping) else {}
    hits = list(quality.get("cliche_hits") or ())
    hits += [str(hit.get("phrase") or "") for hit in (quality.get("on_screen_hits") or ())
             if isinstance(hit, Mapping) and str(hit.get("phrase") or "")]
    repeated = list(quality.get("repeated_phrases") or ())
    result.append(_check(
        "no_rerun", "没有套话、没有跨轮复读", not hits and not repeated,
        "干净" if not (hits or repeated) else "命中 " + "、".join(hits + repeated[:3])))

    # 7. sharp_close：片尾不许把结论推给下一份财报。
    tail = "".join(lines[-2:])
    defer = _hit(tail, ("下一份财报", "下个季度", "下一季度", "下期", "下次财报", "拭目以待", "继续关注"))
    result.append(_check(
        "sharp_close", "片尾不预告数据、不推给未来", not defer,
        "收在当期" if not defer else "片尾出现 " + "、".join(defer), tail[:60]))
    return result


def evaluate(script: Mapping[str, Any]) -> dict[str, Any]:
    """判定清单 + 加权分。写进 ``script["sharpness"]``，成片旁边逐条可核对。"""
    items = checks(script)
    passed = sum(1 for item in items if item["ok"])
    total = len(items) or 1
    return {
        "checks": items,
        "passed": passed,
        "total": len(items),
        "score": round(passed / total, 3),
        "failed": [item["id"] for item in items if not item["ok"]],
    }


def summary(report: Mapping[str, Any]) -> str:
    """一行摘要，给流水线控制台与评论用（不渲染整张表）。"""
    failed = list(report.get("failed") or ())
    head = f"锐度 {report.get('passed')}/{report.get('total')}"
    return head + ("，未过：" + "/".join(failed) if failed else "，全部通过")
