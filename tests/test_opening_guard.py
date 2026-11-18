"""开场门禁的契约测试（2026-11-15 涨粉改造 P0-1 / P0-5）。

门禁判三件事：口头禅命中、hook/front_load 未过、与近几期开场同款。
生成器侧的契约是「不合格重生成一次，两次都不行带警告放行」——闸提概率，
不把生成变成偶发失败点。
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

from stocktalk.modules import opening_guard
from stocktalk.modules.dialogue_generator import DialogueGenerator


def _script(first_line: str, second_line: str = "第二句把话头递过去，问一个具体问题。") -> dict:
    return {"turns": [
        {"speaker": "bull", "line": first_line},
        {"speaker": "bear", "line": second_line},
    ]}


GOOD_OPENING = "九成收入押在一条产品线上，这生意怎么扛？"


class TestProblems:
    def test_a_clean_opening_passes(self) -> None:
        assert opening_guard.problems(_script(GOOD_OPENING)) == []

    def test_the_trader_catchphrase_is_flagged(self) -> None:
        issues = opening_guard.problems(_script("这价我先上车，毛利率九成二的生意我认。"))
        assert any("套话" in item for item in issues), issues

    def test_a_background_opening_fails_the_hook_check(self) -> None:
        issues = opening_guard.problems(
            _script("这是一家做色釉陶瓷的公司，主要产品是日用陶瓷和陶瓷材料。"))
        assert any("钩子" in item or "铺背景" in item for item in issues), issues

    def test_a_repeat_of_a_recent_opening_is_flagged(self) -> None:
        past = ["九成收入押在一条产品线上，这生意怎么扛？"]
        issues = opening_guard.problems(_script(GOOD_OPENING), past)
        assert any("同款句式" in item for item in issues), issues


class TestLedger:
    def test_remember_and_load_round_trip(self, tmp_path: pathlib.Path) -> None:
        ledger = tmp_path / ".opening_rotation.json"
        opening_guard.remember(ledger, code="600519", opening=GOOD_OPENING, day="2026-11-15")
        entries = opening_guard.load_ledger(ledger, keep_days=0)
        assert entries and entries[0]["code"] == "600519"
        assert opening_guard.recent_openings(ledger) == [GOOD_OPENING]

    def test_corrupt_ledger_reads_as_empty(self, tmp_path: pathlib.Path) -> None:
        ledger = tmp_path / "broken.json"
        ledger.write_text("{not json", encoding="utf-8")
        assert opening_guard.load_ledger(ledger) == []


class TestGate:
    def _bad_response(self) -> str:
        return json.dumps({"title": "示例（000001）：怎么看", "turns": [
            {"speaker": "bull", "beat": "business", "line": "这价我先上车，这门生意它占了八成。"},
            {"speaker": "bear", "beat": "industry", "line": "你先说清这八成怎么来的。"},
        ]}, ensure_ascii=False)

    def test_a_flagged_opening_triggers_exactly_one_regeneration(self) -> None:
        calls = []
        generator = DialogueGenerator(
            {"dialogue": {"max_line_chars": 80, "opening_gate": True},
             "characters": {"bull": {"name": "新手"}, "bear": {"name": "老手"}}},
            runner=lambda argv, timeout: calls.append(1) or self._bad_response())
        with patch("stocktalk.modules.providers.bailian_chat.shutil.which", return_value="bl"):
            script = generator.generate({"code": "000001", "quote": {"name": "平安银行"}})
        assert len(calls) == 2, "不合格开场应重生成一次（共两次 LLM 调用）"
        notes = [n for n in script.get("arc_notes", []) if str(n).startswith("[开场门禁]")]
        assert notes, script.get("arc_notes")

    def test_gate_off_keeps_single_call(self) -> None:
        calls = []
        generator = DialogueGenerator(
            {"dialogue": {"max_line_chars": 80, "opening_gate": False},
             "characters": {"bull": {"name": "新手"}, "bear": {"name": "老手"}}},
            runner=lambda argv, timeout: calls.append(1) or self._bad_response())
        with patch("stocktalk.modules.providers.bailian_chat.shutil.which", return_value="bl"):
            script = generator.generate({"code": "000001", "quote": {"name": "平安银行"}})
        assert len(calls) == 1
        assert not [n for n in script.get("arc_notes", []) if str(n).startswith("[开场门禁]")]
