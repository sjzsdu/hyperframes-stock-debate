"""中文数字的「口播」与「显示」分层。

LLM 写台词时偶尔会产出「四亿五一」「两亿三二」这类缩略读法（2026-09-26 003040
实锤），TTS 念出来莫名其妙，同一个短语被抽去当 visual 关键词或封面标签后观众更
看不懂。这里提供两个入口：

* :func:`repair_spoken_numbers` —— **口播层**：只修缩略读法（「四亿五一」→
  「4.51亿」，TTS 念成"四点五一亿"）；完整读法（「三亿一千九百万」）原样保留。
* :func:`to_display` —— **显示层**：把数字语境里的汉字数字转成阿拉伯数字
  （字幕、visual 关键词、SRT、发布摘要共用）：「十八点二九」→「18.29」、
  「九月二十六号」→「9月26号」、「两千七百多万」→「2700多万」。

转换是保守的：纯数字成语（十有八九、三十六计、百年不遇）里的数字不贴单位词，
一律不动；只有紧邻单位（亿/万/成/点/％/倍/月/日…）或以亿/万收尾的短语才转换。
"""

from __future__ import annotations

import re

# 数字同音字（不含单位）。两按 2 处理。
_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_UNITS = {"十": 10, "百": 100, "千": 1000}
_BIG = {"万": 10_000, "亿": 100_000_000}
_RUN = "零一二两三四五六七八九十百千万亿"
_TAIL = "零一二三四五六七八九"

# 紧跟数字短语才算数字语境的单位词。「年」仅对无十百千万的位拼数字放开
# （二零二六年→2026年；三十年河东这类不动），守卫见 general_repl。
# 刻意不含「分」（十分、万分是副词）。
_UNIT_AFTER = ("个百分点", "公里", "%", "％", "倍", "家", "元", "天",
               "次", "折", "月", "日", "号", "吨", "斤", "米", "页", "年")

# 「千万」作副词（千万别/千万要）不是数字。
_ADVERBIAL_NEXT = "别要记不注小"


def _trim(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _parse_run(run: str) -> float | None:
    """解析一段连续汉字数字。无单位字时按位拼接（二零二六→2026）。"""
    if not run:
        return None
    if not any(ch in _UNITS or ch in _BIG for ch in run):
        try:
            return float("".join(str(_DIGITS[ch]) for ch in run))
        except KeyError:
            return None
    total, section, digit = 0.0, 0.0, 0
    for ch in run:
        if ch in _DIGITS:
            digit = _DIGITS[ch]
        elif ch in _UNITS:
            section += (digit or 1) * _UNITS[ch]
            digit = 0
        elif ch in _BIG:
            section = (section + digit) * _BIG[ch]
            total += section
            section, digit = 0.0, 0
    return total + section + digit


def _format_scaled(value: float, unit: str) -> str:
    """带亿/万单位的显示：保单位、保精度（4.000005亿 不许被抹成 4亿）。"""
    scale = _BIG[unit]
    if value % scale == 0 and value >= scale:
        return f"{int(value // scale)}{unit}"
    return f"{_trim(value / scale)}{unit}"


# 缩略读法：数字（懒惰量词，让亿/万落进下一组）+ (亿|万) + 1-2 个纯数字尾音，
# 且后面不再跟数字（否则是「三亿一千九百万」这类完整读法）。
_ABBREV = re.compile(
    f"([{_RUN}]+?)"
    f"(?:(?P<u>亿|万)(?P<tail>[{_TAIL}]{{1,2}})(?![{_RUN}0-9]))?"
)


def repair_spoken_numbers(text: str) -> str:
    """口播层：只把「四亿五一」式缩略读法修成「4.51亿」，其余原样保留。"""

    def repl(match: re.Match[str]) -> str:
        unit, tail = match.group("u"), match.group("tail")
        if unit is None or tail is None:
            return match.group(0)  # 完整读法或无关短语，不动
        whole = _parse_run(match.group(1))
        if whole is None:
            return match.group(0)
        digits = "".join(str(_DIGITS[ch]) for ch in tail)
        value = whole + float(digits) / (10 ** len(digits))  # 已是「亿/万」单位
        return f"{_trim(value)}{unit}"

    return _ABBREV.sub(repl, text)


# 显示层按序匹配：百分之X（可带点小数）→ 点小数 → 成数 → 通用数字短语。
_PERCENT = re.compile(f"百分之([{_RUN}]+(?:点[{_TAIL}]+)?)")
_DECIMAL = re.compile(f"([{_RUN}]+)点([{_TAIL}]+)")
_CHENG = re.compile(f"([{_RUN}]+?)成([{_TAIL}]{{0,2}})(?![{_RUN}0-9])")
_GENERAL = re.compile(
    f"([{_RUN}]+)"
    f"(?P<multi>多[亿万]?(?![{_RUN}0-9]))?"
    f"(?P<unit>{'|'.join(re.escape(u) for u in sorted(_UNIT_AFTER, key=len, reverse=True))})?"
)


def to_display(text: str) -> str:
    """显示层：把数字语境里的汉字数字转成阿拉伯数字，成语与副词不动。"""
    text = repair_spoken_numbers(str(text or ""))

    def percent_repl(match: re.Match[str]) -> str:
        whole_text, _, frac_text = match.group(1).partition("点")
        whole = _parse_run(whole_text)
        if whole is None:
            return match.group(0)
        if frac_text:
            digits = "".join(str(_DIGITS[ch]) for ch in frac_text if ch in _DIGITS)
            if digits:
                whole += float(digits) / (10 ** len(digits))
        return f"{_trim(whole)}%"

    def decimal_repl(match: re.Match[str]) -> str:
        whole, fraction = _parse_run(match.group(1)), match.group(2)
        if whole is None:
            return match.group(0)
        digits = "".join(str(_DIGITS[ch]) for ch in fraction if ch in _DIGITS)
        return f"{_trim(whole)}.{digits}" if digits else match.group(0)

    def cheng_repl(match: re.Match[str]) -> str:
        base, tail = _parse_run(match.group(1)), match.group(2)
        if base is None:
            return match.group(0)
        value = base * 10.0
        if tail:
            first = _DIGITS.get(tail[0])
            value += float(first) if first is not None else 0.0
            if len(tail) > 1:
                second = _DIGITS.get(tail[1])
                value += (second or 0) / 10.0
        return f"{_trim(value)}%"

    def general_repl(match: re.Match[str]) -> str:
        run, multi, unit = match.group(1), match.group("multi"), match.group("unit")
        # 锚定条件：贴「多/单位」后缀，或短语自带亿/万；纯数字成语不满足，不动。
        if not multi and not unit and not ("亿" in run or "万" in run):
            return match.group(0)
        # 千万别/千万要这类副词不转。
        if run == "千万" and text[match.end(1):match.end(1) + 1] in _ADVERBIAL_NEXT:
            return match.group(0)
        value = _parse_run(run)
        if value is None or (value == 0 and not any(ch in _DIGITS for ch in run)):
            return match.group(0)  # 万万/千千这类叠字副词，不是数字
        # 「三十年河东」这类带十百千万的短语不与「年」锚定；位拼年份（二零二六）才转。
        if unit == "年" and any(ch in _UNITS or ch in _BIG for ch in run):
            return match.group(0)
        if multi:
            return f"{_trim(value)}{multi}"
        # 自带大单位时优先用亿（三亿一千九百万 → 3.19亿，而不是 31900万）。
        for big in sorted(_BIG, key=lambda u: -_BIG[u]):
            if big in run:
                return _format_scaled(value, big) + (unit or "")
        return f"{_trim(value)}{unit or ''}"

    text = _PERCENT.sub(percent_repl, text)
    text = _DECIMAL.sub(decimal_repl, text)
    text = _CHENG.sub(cheng_repl, text)
    text = _GENERAL.sub(general_repl, text)
    return text


# 「零售」里的零不算数字，其余汉字数字语素（含点/成）出现在检索标签里都意味着
# 这是一个数字碎片（「毛利率十八点二九」），没人会在抖音/B站搜它。
_TAG_DIGIT_RE = re.compile(r"[0-9０-９%％]")
_TAG_NUMERAL_CHARS = "一二两三四五六七八九十百千万亿点成"


def has_digit(text: str) -> bool:
    """关键词里是否带数字成分——这类词适合上屏，不适合当平台检索标签。"""
    if _TAG_DIGIT_RE.search(text):
        return True
    return any(ch in _TAG_NUMERAL_CHARS for ch in text)
