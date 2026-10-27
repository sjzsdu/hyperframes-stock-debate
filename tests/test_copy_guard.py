"""套话闸门的契约测试（2026-10-27 WAN-27）。

三层，对应三种"套话回到成片"的路径：

1. **检测器本身有效**：点名的两句要被抓住；「引用它去禁它」的 prompt 不算违规。
2. **模板池干净**：片尾风险提示是每期必念的确定性轮，占位符填上以后逐条扫——
   一句话写错就是整季复读。
3. **源码自动暴露**：扫描 ``stocktalk/`` 下所有非 docstring 字符串常量。这是
   "新增套话能自动暴露"的落点——往任何模块的提示词、模板、画面文案里新写一句
   套话，这里就红，不需要谁记得来登记。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from stocktalk.modules import copy_guard
from stocktalk.modules.risk_exchange import EXCHANGES, TRAITS

ROOT = pathlib.Path(__file__).resolve().parents[1] / "stocktalk"

# 清单本体当然写着这些词；它是唯一允许的地方。
EXEMPT_FILES = {"copy_guard.py"}


def _live_template_sentences() -> list[str]:
    """把风险提示模板的占位符填成最坏情况（最长能填进去的值）再扫。"""
    fill = {"industry": "食品饮料与酿酒", "item": "其他系列酒产品", "name": "贵州茅台"}
    sentences: list[str] = []
    for exchange in EXCHANGES:
        for text in (exchange.a, exchange.b):
            rendered = text
            for key, _ in TRAITS:
                rendered = rendered.replace(f"{{{key}}}", fill.get(key, ""))
            sentences.append(rendered)
    return sentences


def test_the_two_named_phrases_are_caught() -> None:
    """用户点名的两句必须命中——这是这个模块存在的理由。"""
    for sentence in ("聊完酿酒这门生意，股市有风险。", "把贵州茅台聊透了，股市有风险。"):
        hits = copy_guard.scan(sentence)
        assert hits, sentence
        assert {hit["id"] for hit in hits} & {"meta-business", "meta-digest"}, hits


def test_a_prompt_may_quote_a_banned_phrase_only_when_it_bans_it() -> None:
    """提示词必须能引用被禁的词才禁得住它——但引用必须明确写成禁用。"""
    quoted = "禁止“XX 这门生意”式的名词解释，也不要“把某某聊透了”。"
    plain = "先把这门生意讲清楚，再把它聊透了。"
    assert copy_guard.source_violations(quoted) == []
    assert copy_guard.source_violations(plain)


def test_repeated_phrases_finds_new_cliches_without_a_blacklist() -> None:
    """清单之外的新套话：同一段六字表述跨三轮复读就该被抬出来。"""
    lines = ["先说清它到底靠什么赚钱客户是谁",
             "再说清它到底靠什么赚钱客户在哪",
             "最后说清它到底靠什么赚钱客户要什么",
             "这一句完全不同，讲的是存货压在仓库里"]
    repeated = copy_guard.repeated_phrases(lines)
    assert repeated
    assert any(item["count"] >= 3 for item in repeated)
    # 长短片都一样：只有两轮重复不算复读（数据复述、专有名词都会碰巧重合）
    assert copy_guard.repeated_phrases([lines[0], lines[1]]) == []


def test_audit_counts_every_occurrence_not_just_distinct_phrases() -> None:
    """cliche_count 是"成片里念了几遍"：同一句念两遍就是两次。"""
    audit = copy_guard.audit(["说白了，这是一针见血的判断。", "说白了，这是一针见血的判断。"])
    assert audit["cliche_count"] == 2
    # 熟语同口径：同一句念两遍，"一针见血"也是两次
    assert audit["idiom_count"] == 2
    assert audit["repeated_adjacent_lines"] == 1
    assert audit["cliche_hits"] == ["说白了"]


def test_risk_exchange_templates_are_clean_after_placeholders_are_filled() -> None:
    """片尾风险提示是每期必念的确定性轮：模板池干净才算治住源头。"""
    dirty = {sentence: [hit["phrase"] for hit in copy_guard.source_violations(sentence)]
             for sentence in _live_template_sentences()}
    assert not {k: v for k, v in dirty.items() if v}, dirty


def _module_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    """模块里所有**非 docstring** 的字符串常量（含 f-string 的常量片段）。

    docstring 是写给人看的注释，不是要喂给模型或打在画面上的文案；把提示词
    与模板都写成普通字符串，正是为了让这条扫描看得见它们。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


@pytest.mark.parametrize("path", sorted(ROOT.rglob("*.py")), ids=lambda p: p.name)
def test_no_banned_phrase_is_hardcoded_anywhere_in_the_package(path: pathlib.Path) -> None:
    """往任何模块的正文文案里新写一句套话，这条测试就红。

    这是"可维护"的关键：新增套话不需要有人记得回来登记清单，扫描器自己会
    发现——包括以后新加的模块、新的模板池、新的画面文案。
    """
    if path.name in EXEMPT_FILES:
        return
    offenders = []
    for lineno, text in _module_strings(path):
        hits = copy_guard.source_violations(text)
        if hits:
            offenders.append(f"{path.name}:{lineno} 「{'、'.join(hit['phrase'] for hit in hits)}」")
    assert not offenders, "这些字符串里写了套话（要么改写，要么写成明确的禁用引用）：\n" + "\n".join(offenders)
