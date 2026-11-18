"""标题换血（2026-11-15 P0-2）：三写法 + 双候选挑分，研报腔扣分。"""

from __future__ import annotations

from stocktalk.modules.dialogue_generator import DialogueGenerator


class TestTitleScore:
    def test_question_beats_report_style(self) -> None:
        question = "中芯国际（688981）：钱到底去哪了？"
        report = "中芯国际（688981）：代工溢价与折旧的账本博弈"
        assert DialogueGenerator._title_score(question) > DialogueGenerator._title_score(report)

    def test_contrast_number_scores_high(self) -> None:
        contrast = "某某股份（600000）：毛利率三年掉了十七个点，股价反而涨了"
        flat = "某某股份（600000）：公司经营情况稳定向好"
        assert DialogueGenerator._title_score(contrast) > DialogueGenerator._title_score(flat)

    def test_report_words_are_penalized(self) -> None:
        clean = "某某股份（600000）：存货的坑谁来填？"
        dirty = "某某股份（600000）：存货与现金流的错位博弈"
        assert DialogueGenerator._title_score(clean) > DialogueGenerator._title_score(dirty)


class TestPickTitle:
    def test_picks_the_best_candidate(self) -> None:
        response = {
            "title": "海正药业（600267）：管线换血与存货的溢价博弈",
            "title_candidates": [
                "海正药业（600267）：存货的坑谁来填？",
                "海正药业（600267）：六成收入靠自产",
            ],
        }
        picked = DialogueGenerator._pick_title(response, "海正药业", "600267")
        assert "？" in picked, picked

    def test_single_title_still_works(self) -> None:
        picked = DialogueGenerator._pick_title({"title": "平安银行（000001）：靠息差吃饭"},
                                              "平安银行", "000001")
        assert picked.startswith("平安银行（000001）")

    def test_empty_falls_back(self) -> None:
        picked = DialogueGenerator._pick_title({}, "平安银行", "000001")
        assert picked == "平安银行（000001）这家公司怎么看"


class TestSeriesLabel:
    """合集归类（2026-11-15 P1-3）：确定性词面匹配，不烧 LLM。"""

    def test_inventory_series(self) -> None:
        script = {"title": "某公司（600000）：存货的坑", "turns": []}
        assert DialogueGenerator._series_label(script) == "存货真相"

    def test_cash_flow_series(self) -> None:
        script = {"title": "某公司（600000）：现金流与回款", "turns": []}
        assert DialogueGenerator._series_label(script) == "现金流排雷"

    def test_default_series(self) -> None:
        assert DialogueGenerator._series_label({"title": "某公司（600000）：随便看看", "turns": []}) == "生意拆解"
