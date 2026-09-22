"""谈股论金生成对话的内容安全检查。

合规审核是确定性的：它识别可能被解释为投资建议的语言，
将其替换为中性措辞，并记录每次干预 alongside the approved script。
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Dict, Iterable, List, Mapping, Tuple


# 三条要求一次说清：AI 生成必须如实标注（各平台公约的硬性要求；B站走 biliup，投稿
# 命令没有声明字段，全靠简介里这一句）、不得构成投资建议、风险自担。合起来约 35 字，
# 视频号简介上限 120 字时仍能完整保留（_fit_description 优先保住这段结尾）。
DEFAULT_DISCLAIMERS = (
    "本内容由AI生成，仅供学习交流，不构成投资建议",
    "投资有风险，入市需谨慎",
)

# Longer, more specific phrases must be processed first.  The values are
# neutral descriptions, not softened buy/sell recommendations.
ADVICE_REPLACEMENTS: Tuple[Tuple[str, str], ...] = (
    ("强烈建议买入", "可进一步关注其基本面与风险"),
    ("强烈建议卖出", "可进一步评估其风险与仓位"),
    ("建议买入", "可进一步关注其基本面"),
    ("建议卖出", "可进一步评估其风险"),
    ("立即买入", "可进一步研究"),
    ("立即卖出", "可进一步评估风险"),
    ("全仓买入", "集中关注"),
    ("满仓买入", "集中关注"),
    ("抄底", "关注估值变化"),
    ("加仓", "调整关注重点"),
    ("建仓", "开始跟踪"),
    ("减仓", "降低关注度"),
    ("清仓", "停止跟踪"),
    ("稳赚", "存在获利可能，也可能亏损"),
    ("包赚", "不保证收益"),
    ("保本", "需承担相应风险"),
    ("必涨", "可能上涨"),
    ("必跌", "可能下跌"),
    ("一定涨", "可能上涨"),
    ("一定跌", "可能下跌"),
    ("肯定会", "可能会"),
    ("必将", "可能"),
    ("将会", "可能会"),
    ("保证", "无法保证"),
    ("肯定", "可能"),
    ("必然", "可能"),
    ("一定", "可能"),
    ("绝对", "相对"),
)

# These are reported even when a particular expression has no dedicated
# replacement above.  Configured forbidden words extend (rather than replace)
# this baseline.
DEFAULT_FORBIDDEN_WORDS = (
    "买入", "卖出", "推荐", "稳赚", "包赚", "保本", "必涨", "必跌",
    "保证", "肯定", "一定",
)
ABSOLUTE_PATTERNS = ("肯定", "必然", "一定", "绝对", "保证", "毫无疑问")
PREDICTIVE_PATTERNS = (
    "将会", "必将", "肯定会", "一定会", "一定涨", "一定跌", "必涨", "必跌",
)

# 成片帧内的文案红线（tests/test_hyperframes_builder.py 的断言）：画面上不能出现
# 免责声明、AI 声明，也不能出现"看多/看空"这类立场标签。片尾互动图板是新增的
# **上屏**文案，所以它的字段（sides/hook/question）要比台词多过一道。
FRAME_REPLACEMENTS: Tuple[Tuple[str, str], ...] = (
    ("看多一方", "乐观一边"),
    ("看空一方", "谨慎一边"),
    ("看多方", "乐观一边"),
    ("看空方", "谨慎一边"),
    ("看多", "乐观"),
    ("看空", "谨慎"),
    ("免责", "说明"),
    ("不构成投资建议", "仅供参考"),
    ("不构成", "不作为"),
    ("投资建议", "操作依据"),
    ("虚拟人物", ""),
    ("AI生成", ""),
    ("AI 生成", ""),
    # 画面里不能出现"生成"（AI 声明词），但业务语境里"生成现金流"这类说法很常见，
    # 所以做同义替换而不是整句删除。
    ("生成", "形成"),
)

# 片尾互动文案的红线：分歧只能落在生意层面。买卖动作、点位、收益暗示一旦出现在
# 字幕之外**更醒目的**片尾图板上，比一句台词更容易被读成建议，所以直接拦截。
FRAME_ADVICE_TERMS: Tuple[str, ...] = (
    "买入", "卖出", "加仓", "减仓", "建仓", "清仓", "抄底", "逃顶",
    "目标价", "点位", "止损", "止盈", "满仓", "空仓", "梭哈", "上车",
    "必涨", "必跌", "翻倍", "稳赚", "包赚", "保本",
)


class ComplianceAgent:
    """Review a dialogue script and remove investment-advice-like wording."""

    def __init__(self, config: Mapping[str, Any] | None = None):
        self.config = dict(config or {})
        self.compliance_config = self.config.get("compliance", {}) or {}
        self.rules = list(self.compliance_config.get("rules", []) or [])
        configured_disclaimers = self.compliance_config.get("disclaimers")
        self.disclaimers = list(
            DEFAULT_DISCLAIMERS if configured_disclaimers is None else configured_disclaimers
        )
        configured_words = self.compliance_config.get("forbidden_words", []) or []
        self.forbidden_words = self._unique_words(
            (*DEFAULT_FORBIDDEN_WORDS, *(str(word) for word in configured_words if word))
        )

    @staticmethod
    def _unique_words(words: Iterable[str]) -> List[str]:
        """Return non-empty phrases once, preferring specific phrases first."""
        return sorted(set(words), key=lambda word: (-len(word), word))

    def review(self, script: Dict[str, Any]) -> Dict[str, Any]:
        """Return a deep-copied, sanitised script with audit metadata.

        Only ``bull`` and ``bear`` dialogue records are changed. Malformed or
        missing records are left untouched so a compliance pass never makes a
        generated script unusable.
        """
        if not isinstance(script, Mapping):
            raise TypeError("script must be a mapping")

        approved_script = deepcopy(dict(script))
        issues: List[str] = []
        # 改写了哪些字句要留痕：``_fix_line`` 是纯字符串替换，"一定→可能"这种改动
        # 会无声地改变语气强度，审查的人必须看得见自己那期到底被动了哪几句。
        rewrites: List[Dict[str, str]] = []
        turns = approved_script.get("turns", [])
        if "turns" in approved_script and isinstance(turns, list):
            for turn in turns:
                if not isinstance(turn, dict) or not isinstance(turn.get("line"), str): continue
                character, line = str(turn.get("speaker", "unknown")), turn["line"]
                line_issues = self._review_line(line, character)
                issues.extend(line_issues)
                if line_issues:
                    fixed = self._fix_line(line)
                    if fixed != line:
                        rewrites.append({"where": f"台词 · {character}", "before": line, "after": fixed})
                    turn["line"] = fixed
        else:  # Legacy scripts remain reviewable.
            for round_data in approved_script.get("rounds", []):
                if not isinstance(round_data, dict): continue
                for character in ("bull", "bear"):
                    dialogue = round_data.get(character)
                    if isinstance(dialogue, dict) and isinstance(dialogue.get("line"), str):
                        line_issues = self._review_line(dialogue["line"], character); issues.extend(line_issues)
                        if line_issues: dialogue["line"] = self._fix_line(dialogue["line"])

        # 标题会原样发到各平台；hook/question/sides 会打在片尾画面上。三者此前
        # 完全没过审——标题里的"必涨"能直接发出去，这是实打实的缺口。
        #
        # 两个方向都要覆盖，不能只挑一个：
        # · 有问题但没改写（"目标价/止损"这类词没有对应替换项）→ 必须记进 issues，
        #   否则红线漏报；此前 issues 只在"文本被改写"时才收集，等于白报。
        # · 改写了但没判问题（"看多→乐观"是画面红线替换，不算禁用词）→ 必须写回，
        #   否则词照旧上屏。
        for key, label, frame in (("title", "标题", False),
                                  ("hook", "片尾·下期关注", True),
                                  ("question", "片尾·提问", True)):
            if key in approved_script:
                original = str(approved_script.get(key) or "")
                text, extra, changes = self._review_plain(label, original, frame=frame)
                issues.extend(extra)
                if text != original:
                    approved_script[key] = text
                    rewrites.extend(changes)
        sides = approved_script.get("sides")
        if isinstance(sides, Mapping):
            clean_sides = dict(sides)
            for role, value in sides.items():
                original = str(value or "")
                text, extra, changes = self._review_plain(f"片尾·{role}立场", original, frame=True)
                issues.extend(extra)
                if text != original:
                    clean_sides[role] = text
                    rewrites.extend(changes)
            approved_script["sides"] = clean_sides

        existing = approved_script.get("disclaimers", [])
        if not isinstance(existing, list):
            existing = []
        approved_script["disclaimers"] = self._append_disclaimers(existing)
        approved_script["compliance_issues"] = issues
        approved_script["compliance_rewrites"] = rewrites
        return approved_script

    def _review_plain(self, label: str, value: Any, *, frame: bool = False) -> Tuple[str, List[str], List[Dict[str, str]]]:
        """过审一段上屏文案，返回 (清洗后的文本, 问题清单, 改写对照)。

        ``frame=True`` 用于会出现在画面上的片尾文案，额外拦一条"分歧必须落在
        生意维度"——出现买卖动作/点位/收益暗示即记问题。
        """
        text = str(value or "")
        if not text.strip():
            return text, [], []
        issues = self._review_line(text, label)
        if frame:
            issues.extend(f"[{label}] 片尾文案触碰画面红线: {term}"
                          for term in FRAME_ADVICE_TERMS if term in text)
        fixed = self.sanitize_frame(text) if frame else self.sanitize(text)
        changes = [{"where": label, "before": text, "after": fixed}] if fixed != text else []
        return fixed, issues, changes

    def sanitize(self, text: str) -> str:
        """Neutralise advice-like wording in free text (e.g. a cover headline).

        ``review`` only rewrites dialogue turns, but every user-visible string
        needs the same treatment — the cover image is published too.
        """
        return self._fix_line(str(text or "")) if text else ""

    def sanitize_frame(self, text: str) -> str:
        """台词之外但**打在画面上**的文案（片尾互动图板）专用清洗。

        比 ``sanitize`` 多一层画面红线替换：立场标签、免责声明、AI 声明词都不能
        出现在帧内，所以这里把它们换成中性说法而不是只报告。
        """
        fixed = self._fix_line(str(text or ""))
        for phrase, replacement in FRAME_REPLACEMENTS:
            fixed = fixed.replace(phrase, replacement)
        return re.sub(r"[，,]{2,}", "，", re.sub(r"\s{2,}", " ", fixed)).strip()

    def _append_disclaimers(self, existing: Iterable[Any]) -> List[str]:
        """Append configured notices without duplicating existing notices."""
        notices: List[str] = []
        for notice in (*existing, *self.disclaimers):
            if isinstance(notice, str) and notice and notice not in notices:
                notices.append(notice)
        return notices

    def _review_line(self, line: str, character: str) -> List[str]:
        """Return human-readable audit findings for one dialogue line."""
        if not isinstance(line, str):
            return []

        issues: List[str] = []
        for word in self.forbidden_words:
            if word in line:
                issues.append(f"[{character}] 包含禁用词: {word}")
        for pattern in ABSOLUTE_PATTERNS:
            if pattern in line:
                issues.append(f"[{character}] 包含绝对性表述: {pattern}")
        for pattern in PREDICTIVE_PATTERNS:
            if pattern in line:
                issues.append(f"[{character}] 包含预测性表述: {pattern}")
        return issues

    def _fix_line(self, line: str) -> str:
        """Replace unsafe language while keeping the sentence readable."""
        fixed = line
        for phrase, replacement in ADVICE_REPLACEMENTS:
            fixed = fixed.replace(phrase, replacement)

        # A configured phrase may be proprietary terminology.  Remove it only
        # when no specific safe replacement is known, avoiding regex injection.
        for word in self.forbidden_words:
            if word in fixed:
                fixed = fixed.replace(word, "相关操作")

        # Collapse punctuation/whitespace artefacts created by phrase removal.
        fixed = re.sub(r"[，,]{2,}", "，", fixed)
        fixed = re.sub(r"\s{2,}", " ", fixed).strip()
        return fixed
