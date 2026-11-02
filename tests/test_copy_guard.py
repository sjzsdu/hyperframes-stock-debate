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

# 清单本体当然写着这些词；但只豁免**这几个常量里的字符串**，文件其余部分照常扫
# （2026-11-02 WAN-28）。整体豁免一个文件，等于给 copy_guard.py 的其它文案开了
# 一个永久后门——套话写进那里的提示词或样板句，扫描永远看不见。
EXEMPT_CONSTANTS = {"copy_guard.py": {"CLICHES", "IDIOMS", "TEACHING_MARKERS"}}


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


def test_a_stray_marker_word_does_not_exempt_the_whole_string() -> None:
    """一处无关的标记词不能给整串放行（2026-11-02 WAN-28）。

    这两句当初都被整串豁免了：套话本身是实质性使用，只因句子别处恰好有个
    「不要／反例」——那个词管的是后半句，不是被禁短语。
    """
    for text in ("先把这门生意讲清楚，不要像上期那样报流水账。",
                 "写标题时可以先用这门生意，反例以后再改。"):
        assert copy_guard.source_violations(text), text


def test_a_real_teaching_quote_still_exempts_the_hit_it_governs() -> None:
    """纯教学引用照旧放行：标记词就写在被禁短语前面。"""
    for text in ("禁止写「这门生意」这种把公司抽象成生意的说法。",
                 "不要用「把某某聊透了」这类对节目自己的总结。",
                 "反例：『这门生意』——改成具体的谁在掏钱。"):
        assert copy_guard.source_violations(text) == [], text


def test_one_teaching_quote_does_not_exempt_a_second_real_use() -> None:
    """逐命中豁免的实质：同一串里，被禁引用的那处放行，实质使用的那处照抓。"""
    text = "禁止写「这门生意」这种说法，但正文里还是把这家公司聊透了。"
    phrases = [hit["phrase"] for hit in copy_guard.source_violations(text)]
    assert phrases, text
    assert all("这门生意" not in phrase for phrase in phrases), phrases


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


def test_audit_checks_on_screen_texts_too() -> None:
    """封面标题与首帧大卡是上屏文字，也是观众第一眼看到的字（2026-11-02 WAN-28）。

    台词干净、上屏写套话，是最容易漏的一种——它不进口播，所以旧 ``audit`` 只看
    ``turn[].line`` 时永远抓不到。
    """
    audit = copy_guard.audit(
        ["渠道压货这件事，价盘一松就露馅。"],
        on_screen=["贵州茅台：先说清这门生意", "经营现金流", "512亿≈半年收入"])
    assert [hit["phrase"] for hit in audit["on_screen_hits"]] == ["这门生意"]
    assert any("上屏文字套话" in flag for flag in audit["flags"])
    # 上屏命中不改写"念了几遍"的口径：这几个字没有配音
    assert audit["cliche_count"] == 0
    assert audit["cliche_hits"] == []


def test_on_screen_texts_do_not_skew_turn_level_stats() -> None:
    """上屏卡片不是轮次：重复度/短句比例的分母仍然是台词轮次。"""
    lines = ["第一句讲渠道。", "第二句讲价盘。"]
    plain = copy_guard.audit(lines)
    with_screen = copy_guard.audit(lines, on_screen=["贵州茅台", "存货", "512亿"])
    assert with_screen["short_turn_ratio"] == plain["short_turn_ratio"]
    assert with_screen["repeated_adjacent_lines"] == plain["repeated_adjacent_lines"]
    assert with_screen["repeated_phrases"] == plain["repeated_phrases"]


def test_risk_exchange_templates_are_clean_after_placeholders_are_filled() -> None:
    """片尾风险提示是每期必念的确定性轮：模板池干净才算治住源头。"""
    dirty = {sentence: [hit["phrase"] for hit in copy_guard.source_violations(sentence)]
             for sentence in _live_template_sentences()}
    assert not {k: v for k, v in dirty.items() if v}, dirty


def _exempt_constant_strings(tree: ast.Module, names: set[str]) -> set[int]:
    """模块级被豁免常量里的字符串节点 id（``CLICHES`` 的每条规则也算在内）。"""
    exempt: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id in names for target in targets):
            continue
        exempt.update(id(sub) for sub in ast.walk(value)
                      if isinstance(sub, ast.Constant) and isinstance(sub.value, str))
    return exempt


def _module_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    """模块里所有**非 docstring** 的字符串常量（含 f-string 的常量片段）。

    docstring 是写给人看的注释，不是要喂给模型或打在画面上的文案；把提示词
    与模板都写成普通字符串，正是为了让这条扫描看得见它们。被豁免的只有清单
    常量本身（``EXEMPT_CONSTANTS``）——它们必须写着被禁的词。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    exempt = _exempt_constant_strings(tree, EXEMPT_CONSTANTS.get(path.name, set()))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings and id(node) not in exempt]


def test_only_the_list_constants_are_exempt_inside_copy_guard(tmp_path: pathlib.Path) -> None:
    """豁免收窄到清单常量本身：文件其它地方的字符串照样被扫（2026-11-02 WAN-28）。"""
    source = ('CLICHES = ("先说清这门生意",)\n'
              '\n'
              'def prompt():\n'
              '    return "先把这门生意讲清楚，再往下说。"\n')
    path = tmp_path / "copy_guard.py"
    path.write_text(source, encoding="utf-8")
    offenders = [text for _, text in _module_strings(path) if copy_guard.source_violations(text)]
    assert offenders == ["先把这门生意讲清楚，再往下说。"]


@pytest.mark.parametrize("path", sorted(ROOT.rglob("*.py")), ids=lambda p: p.name)
def test_no_banned_phrase_is_hardcoded_anywhere_in_the_package(path: pathlib.Path) -> None:
    """往任何模块的正文文案里新写一句套话，这条测试就红。

    这是"可维护"的关键：新增套话不需要有人记得回来登记清单，扫描器自己会
    发现——包括以后新加的模块、新的模板池、新的画面文案。清单常量本身是唯一
    的例外（``EXEMPT_CONSTANTS``）。
    """
    offenders = []
    for lineno, text in _module_strings(path):
        hits = copy_guard.source_violations(text)
        if hits:
            offenders.append(f"{path.name}:{lineno} 「{'、'.join(hit['phrase'] for hit in hits)}」")
    assert not offenders, "这些字符串里写了套话（要么改写，要么写成明确的禁用引用）：\n" + "\n".join(offenders)
