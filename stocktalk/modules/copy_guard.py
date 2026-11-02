"""套话与复用表述的闸门（2026-10-27 用户反馈：成片「套路感」明显）。

用户点名的两句是「这门生意」「把…聊透了」。只删这两句没用，因为产出它们
的不是某一处笔误，而是两类机制：

1. **确定性模板池**。``risk_exchange`` 的片尾风险提示是程序追加的口播轮，
   模板池里就写着「聊完{industry}这门生意」和「把{name}聊透了」——每期必念一句，
   抽样 4 只票 × 30 天里 40% 的期数会命中这两条。模板池是"每期都要说一遍"的
   地方，套话写在那里就是每期复读。
2. **提示词自身的用词**。生成器的系统提示、骨架节拍 intent 反复用「这门生意」
   来描述要讲的内容（「先说清…这门生意怎么运转」「对这门生意做一次小结」）。
   模型读完几十遍，当然照着说——提示词里的元语言会直接漏进台词。

所以这里放一份**集中清单 + 扫描器**，供三处复用：

* 成片台词（``audit``）——风控数据，写进剧本 JSON 与审查页；
* 确定性模板池与提示词（``scan(scope="source")``）——测试扫源码，往任何模块
  的字符串里新写一句套话就红，不靠人肉记忆；
* 跨轮重复片段（``repeated_phrases``）——**不需要预案清单**：新长出来的套话
  只要在片子里复读三次以上就会被抬出来，这是清单之外的自动兜底。

边界：这是**报告**而不是改写。改写台词等于二次创作，会伤到事实措辞（数字
读法、来源归属），质量判断该由人做；这里只保证"看不见的套话会现形"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class Cliche:
    """一条套话规则。

    ``in_source`` 区分两种语境：True 表示这句话一旦被写进**模板池或提示词**就
    已经错了（模型会照着念）；False 表示它只在**成片台词**里算违规——设问、
    熟语作为提示词里的指令（"别用设问式开场"）是合法的元语言。
    """

    id: str
    pattern: re.Pattern[str]
    why: str
    in_source: bool = True


# 熟语：形容词代替事实。单列一份，因为它是"词"而不是"句式"，且数量会长。
IDIOMS: tuple[str, ...] = (
    "一针见血", "锦上添花", "顺势而为", "乘风破浪", "稳中有进", "大有可为",
    "水涨船高", "如火如荼", "不言而喻", "毋庸置疑", "可见一斑",
)


# 清单本身要短、要有理由：一条规则的 ``why`` 写得出来，才说明知道它为什么是套话。
CLICHES: tuple[Cliche, ...] = (
    Cliche("meta-business", re.compile(r"这门(生意|买卖|生意经|行当|营生|买卖儿)"),
           "把公司抽象成一件「生意」摆在台面上命名，观众学到的先是这个词而不是这家公司；"
           "改成具体的谁在掏钱、钱在哪一环被卡住"),
    Cliche("meta-digest", re.compile(r"(把|将)[^，。！？；:]{0,12}(聊|讲)透"),
           "「把X聊透了」是节目对自己的总结，不是内容；观众要的是还剩什么问题没解决"),
    Cliche("meta-digest-bare", re.compile(
        r"(聊|讲)透"
        # 「聊完了」只在后面跟着"节目的对象"时才算自总结（「聊完全部生意」「讲完这期」）；
        # 「把这一轮讲完」「说完，就把话头递出去」是正常的动作描述——逐命中豁免之后
        # 这些短语被逐个抓出来，才知道原来的 (透|完) 太宽（2026-11-02 WAN-28）。
        r"|(聊|讲|说|谈)(?:透|完)(?:全部|整个|所有|这家|这行|生意|这期|本期|今天)"),
        "同上：只要在说「讲透了/聊透了」「聊完全部生意」，说话的对象就是节目自己"
        "而不是内容；观众要的是还剩什么问题没解决"),
    Cliche("meta-signoff", re.compile(r"(就|先)?聊到这就|今天就聊到这|先聊到这"),
           "固定道别句，每期同一句，第三期就被预判"),
    Cliche("filler-frame", re.compile(
        r"说白了|总的来说|说到底|归根结底|值得注意的是|不难发现"
        r"|在当前市场环境下|毋庸置疑|不言而喻"),
        "评论员填充词：占掉字数却不给信息，也把判断让给了语气"),
    Cliche("viewfinder", re.compile(r"(我们|咱们)(先|接下来)(看|聊|说|讲)"),
           "「咱们先看…」是报幕腔，观众在信息流里对这四个字的反应就是划走"),
    Cliche("ask-what-it-does", re.compile(r"靠什么赚钱[？?]"),
           "设问式提问：把最该用事实回答的问题又抛回给观众；这句只在台词里算违规，"
           "提示词里要它作为反面例子才写",
           in_source=False),
    Cliche("idiom", re.compile("|".join(IDIOMS)),
           "熟语堆砌：用形容词代替事实，多了就是 AI 腔",
           in_source=False),
)

# 「这句里明说了这是禁用词」的标记：提示词必须能引用被禁的词才能禁住它
# （只说"别用套话"模型读不出指的是什么）。
TEACHING_MARKERS: tuple[str, ...] = ("禁止", "不许", "不要", "反例", "套话", "判死", "别用")

# 标记词只有在**同一分句**里才算数（2026-11-02 WAN-28）。
# 整串判豁免会把「先把这门生意讲清楚，不要像上期那样报流水账」放过去——那个
# 「不要」管的是后半句，套话本身仍是实质性使用。所以从命中点往两边看：
#
# * 前方到上一个逗号/句号/换行为止——禁令写在被禁短语之前（「禁止写『XX 这门
#   生意』」）是中文最常见的写法；
# * 后方到下一个逗号/句号为止——「『这门生意』这四个字不许出现」是另一种同样
#   常见的写法，标记词紧跟在被禁短语后面；
# * 命中被引号括起来、且**同一句话**里有禁令词——提示词常常先写一个「禁止」再
#   连着举好几个例句（「禁止…：“咱们先看……”整个句式、…、“XX 这门生意”式的
#   名词解释」），例句之间隔着逗号顿号，但都受同一个禁令管。
#
# 三种都要求标记词与命中靠得足够近：隔着句号的禁令不算（那是另一句话的事），
# 所以「实质使用 + 无关标记词」不会因为别处刚好有个「不要」就整串放行。
TEACHING_CLAUSE_STOPS = re.compile(r"[，,。！？!?\n]")
TEACHING_SENTENCE_STOPS = re.compile(r"[。！？!?\n]")
QUOTE_MARKS = "“”「」『』"


def _split_around(text: str, position: int) -> tuple[str, str, str]:
    """命中点前后各自的分句 + 命中点前方那句话（跨句号为止）。"""
    before, after = text[:position], text[position:]
    sentence_breaks = list(TEACHING_SENTENCE_STOPS.finditer(before))
    clause_breaks = list(TEACHING_CLAUSE_STOPS.finditer(before))
    sentence = before[sentence_breaks[-1].end():] if sentence_breaks else before
    clause_before = before[clause_breaks[-1].end():] if clause_breaks else before
    next_break = TEACHING_CLAUSE_STOPS.search(after)
    clause_after = after[:next_break.start()] if next_break else after
    return sentence, clause_before, clause_after


def is_teaching_hit(text: str, position: int) -> bool:
    """**这一处**命中是不是教学引用（提示词在引用它去禁它）。

    ``position`` 是命中在 ``text`` 里的起点（``scan`` 上报的字符偏移）。只看
    这一处命中的邻居：前方同一分句、后方同一分句、或（命中带引号时）同一句话。
    """
    text = text or ""
    position = max(0, int(position))
    sentence, clause_before, clause_after = _split_around(text, position)
    if any(marker in clause_before or marker in clause_after for marker in TEACHING_MARKERS):
        return True
    quoted = sum(1 for char in text[:position] if char in QUOTE_MARKS) % 2 == 1
    return quoted and any(marker in sentence for marker in TEACHING_MARKERS)


# 标点/空白不进重复片段：同一个意思换个标点还是同一句。
_PUNCT = re.compile(r"[\s，。、！？；：“”‘’（）()《》【】…—·,.!?;:\"'\-~～/]+")
_DIGITS = re.compile(r"[0-9０-９]+")


def scan(text: str, *, scope: str = "script") -> list[dict[str, str]]:
    """扫描一段文案，返回命中的套话。

    ``scope="script"``（默认）：成片台词，全部规则生效。
    ``scope="source"``：模板池与提示词，只查 ``in_source`` 的规则——那里本来
    就会引用被禁的句式来说明"别这么写"。
    """
    hits: list[dict[str, str]] = []
    for cliche in CLICHES:
        if scope == "source" and not cliche.in_source:
            continue
        for match in cliche.pattern.finditer(text or ""):
            # 逐次计：命中次数就是"成片里念了几遍"，同一句念两遍是两次，不是一次。
            hits.append({"id": cliche.id, "phrase": match.group(0), "why": cliche.why,
                         "position": str(match.start())})
    return hits


def source_violations(text: str) -> list[dict[str, str]]:
    """模板池 / 提示词里的违规：教学引用不算。

    逐处判（2026-11-02 WAN-28）：一处教学引用只豁免它自己，不让整串放行。
    """
    return [hit for hit in scan(text, scope="source")
            if not is_teaching_hit(text, int(hit["position"]))]


def _skeleton(text: str) -> str:
    return _PUNCT.sub("", text or "")


def repeated_phrases(lines: Sequence[str], *, size: int = 6, min_turns: int = 3,
                     skip_digits: bool = True) -> list[dict[str, Any]]:
    """跨轮重复的长片段——**不需要预案清单**的套话探测器。

    一份清单只能抓住已经被点名的那几句；真正让观众觉得"这套词我听过"的，
    往往还没被任何人写进清单。同一段 6 个字以上、去掉标点后一字不差的表述
    出现在三轮以上，就是新长出来的套话（专有名词与业务名的自然复现通常
    会同轮成串出现，而不是跨三轮一模一样的定长片段；含数字的片段默认跳过，
    数字复述是内容不是套话）。
    """
    buckets: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        text = _skeleton(line)
        for start in range(0, max(0, len(text) - size + 1)):
            gram = text[start:start + size]
            if skip_digits and _DIGITS.search(gram):
                continue
            turns = buckets.setdefault(gram, [])
            if not turns or turns[-1] != index:
                turns.append(index)
    repeated = [
        {"phrase": gram, "turns": [index + 1 for index in turns], "count": len(turns)}
        for gram, turns in buckets.items() if len(turns) >= max(2, min_turns)
    ]
    # 长片段优先：同一个套话会切出好几个窗口，短的没有额外信息。
    repeated.sort(key=lambda item: (-item["count"], -len(item["phrase"]), item["phrase"]))
    return repeated[:8]


def audit(lines_or_turns: Iterable[Any], *,
          on_screen: Iterable[Any] = ()) -> dict[str, Any]:
    """成片文案的套话/重复度体检（原 ``DialogueGenerator._style_audit``）。

    只报告、不改写：二次改写会伤事实措辞，而人可以看到命中项后精确改那一句。

    ``on_screen`` 是**上屏但不进配音**的文字：封面/发布标题、首帧大卡的
    label/value。它们同样印在观众眼前（还是第一眼看到的那几个字），所以套话要
    一起查——但不计入"念了几遍"，也不参与台词层的重复与短句统计：那些指标的
    分母是轮次，几张上屏卡片不是轮次（2026-11-02 WAN-28）。
    """
    lines: list[str] = []
    for item in lines_or_turns:
        text = item.get("line") if isinstance(item, Mapping) else item
        text = str(text or "").strip()
        if text:
            lines.append(text)
    screen = [str(text or "").strip() for text in on_screen]
    screen = [text for text in screen if text]
    joined = "\n".join(lines)
    script_hits = scan(joined)
    screen_hits = [hit for text in screen for hit in scan(text)]
    cliche_hits: list[str] = []
    for hit in script_hits:
        if hit["id"] == "idiom":
            continue
        if hit["phrase"] not in cliche_hits:
            cliche_hits.append(hit["phrase"])
    screen_cliches: list[str] = []
    for hit in screen_hits:
        if hit["id"] == "idiom":
            continue
        if hit["phrase"] not in screen_cliches:
            screen_cliches.append(hit["phrase"])
    idiom_count = sum(1 for hit in script_hits if hit["id"] == "idiom")
    cliche_count = sum(1 for hit in script_hits if hit["id"] != "idiom")
    repeated_lines = sum(1 for left, right in zip(lines, lines[1:]) if left == right)
    repeated = repeated_phrases(lines)
    flags: list[str] = []
    if cliche_hits:
        flags.append("套话：" + "、".join(cliche_hits))
    if screen_cliches:
        flags.append("上屏文字套话：" + "、".join(screen_cliches))
    if idiom_count > max(1, len(lines) // 8):
        flags.append(f"熟语密度偏高（{idiom_count}处）")
    if repeated_lines:
        flags.append(f"相邻台词重复（{repeated_lines}处）")
    if repeated:
        sample = "、".join(item["phrase"] for item in repeated[:3])
        flags.append(f"跨轮复读片段（{len(repeated)}处，如 {sample}）")
    return {
        "cliche_count": cliche_count,
        "cliche_hits": cliche_hits,
        # 上屏文字里的套话单独列一项：审查页要能一眼看出"这句是写在封面上的"，
        # 而不是混进台词命中里让人改错地方。
        "on_screen_hits": [{"id": hit["id"], "phrase": hit["phrase"]} for hit in screen_hits],
        "idiom_count": idiom_count,
        "repeated_adjacent_lines": repeated_lines,
        "repeated_phrases": [item["phrase"] for item in repeated],
        "short_turn_ratio": round(sum(len(line) <= 20 for line in lines) / len(lines), 3) if lines else 0.0,
        "flags": flags,
    }
