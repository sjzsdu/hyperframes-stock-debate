from copy import deepcopy

from stocktalk.modules.compliance import ComplianceAgent, DEFAULT_DISCLAIMERS


def test_review_sanitises_advice_preserves_source_and_adds_notices():
    source = {
        "rounds": [{
            "bull": {"line": "我肯定这只股票将会必涨，建议买入！"},
            "bear": {"line": "稳赚不赔，立即买入。"},
        }],
        "disclaimers": ["已有免责声明"],
    }
    original = deepcopy(source)

    reviewed = ComplianceAgent({}).review(source)

    assert source == original
    assert "肯定" not in reviewed["rounds"][0]["bull"]["line"]
    assert "将会" not in reviewed["rounds"][0]["bull"]["line"]
    assert "必涨" not in reviewed["rounds"][0]["bull"]["line"]
    assert "建议买入" not in reviewed["rounds"][0]["bull"]["line"]
    assert "稳赚" not in reviewed["rounds"][0]["bear"]["line"]
    assert "立即买入" not in reviewed["rounds"][0]["bear"]["line"]
    assert any("禁用词" in issue for issue in reviewed["compliance_issues"])
    assert any("绝对性表述" in issue for issue in reviewed["compliance_issues"])
    assert any("预测性表述" in issue for issue in reviewed["compliance_issues"])
    assert reviewed["disclaimers"] == ["已有免责声明", *DEFAULT_DISCLAIMERS]


def test_custom_terms_and_disclaimers_are_used_without_duplicates():
    agent = ComplianceAgent({"compliance": {
        "forbidden_words": ["火箭票"],
        "disclaimers": ["仅作交流", "仅作交流"],
    }})

    reviewed = agent.review({"rounds": [{"bull": {"line": "这是火箭票"}}]})

    assert "火箭票" not in reviewed["rounds"][0]["bull"]["line"]
    assert reviewed["disclaimers"] == ["仅作交流"]
    assert reviewed["compliance_issues"] == ["[bull] 包含禁用词: 火箭票"]


def test_malformed_dialogue_records_do_not_break_review():
    reviewed = ComplianceAgent({}).review({"rounds": [None, {"bull": {}}, {"bear": {"line": 1}}]})

    assert reviewed["compliance_issues"] == []
    assert reviewed["disclaimers"] == list(DEFAULT_DISCLAIMERS)
