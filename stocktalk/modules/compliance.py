"""Content safety checks for generated StockTalk dialogue.

The compliance pass is deliberately deterministic: it identifies language that
could be interpreted as investment advice, replaces it with neutral wording,
and records every intervention alongside the approved script.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Dict, Iterable, List, Mapping, Tuple


DEFAULT_DISCLAIMERS = (
    "本内容仅供学习交流，不构成投资建议",
    "投资有风险，入市需谨慎",
    "视频中观点不代表真实投资意见",
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
        rounds = approved_script.get("rounds", [])
        if isinstance(rounds, list):
            for round_data in rounds:
                if not isinstance(round_data, dict):
                    continue
                for character in ("bull", "bear"):
                    dialogue = round_data.get(character)
                    if not isinstance(dialogue, dict) or not isinstance(dialogue.get("line"), str):
                        continue
                    line = dialogue["line"]
                    line_issues = self._review_line(line, character)
                    issues.extend(line_issues)
                    if line_issues:
                        dialogue["line"] = self._fix_line(line)

        existing = approved_script.get("disclaimers", [])
        if not isinstance(existing, list):
            existing = []
        approved_script["disclaimers"] = self._append_disclaimers(existing)
        approved_script["compliance_issues"] = issues
        return approved_script

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
