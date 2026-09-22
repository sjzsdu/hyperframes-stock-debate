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


def test_review_sanitises_natural_turn_contract():
    reviewed = ComplianceAgent({}).review({"turns": [{"speaker": "bull", "line": "这只股票必涨，建议买入。"}]})

    assert "必涨" not in reviewed["turns"][0]["line"]
    assert "买入" not in reviewed["turns"][0]["line"]


def test_malformed_dialogue_records_do_not_break_review():
    reviewed = ComplianceAgent({}).review({"rounds": [None, {"bull": {}}, {"bear": {"line": 1}}]})

    assert reviewed["compliance_issues"] == []
    assert reviewed["disclaimers"] == list(DEFAULT_DISCLAIMERS)


# ---- 标题与片尾互动文案：此前完全没过审的字段 -----------------------------

def test_review_covers_the_title_and_the_ending_board_copy():
    """标题会原样发到各平台，hook/question/sides 会打在片尾画面上。

    在此之前这三个字段谁都没管：标题里的"必涨"能直接发出去，片尾图板上的
    "看多/看空"能直接上屏——这是实打实的缺口。
    """
    reviewed = ComplianceAgent({}).review({
        "turns": [{"speaker": "bull", "line": "这家公司靠色釉陶瓷赚钱。"}],
        "title": "必涨的色釉陶瓷龙头",
        "hook": "下一期核对外销收入占比",
        "question": "你更信渠道还是产能？",
        "sides": {"bull": "看多一方：产品力扎实", "bear": "看空一方：客户集中就是命门"},
    })

    assert "必涨" not in reviewed["title"]
    # 画面红线：立场标签换成中性说法，而不是只报告问题
    assert reviewed["sides"]["bull"] == "乐观一边：产品力扎实"
    assert reviewed["sides"]["bear"] == "谨慎一边：客户集中就是命门"
    # 无声的逐字替换必须留痕：审查的人要看得见自己那期被动了哪几句
    rewrites = {item["where"]: item for item in reviewed["compliance_rewrites"]}
    assert rewrites["片尾·bull立场"]["before"] == "看多一方：产品力扎实"
    assert rewrites["片尾·bull立场"]["after"] == "乐观一边：产品力扎实"
    assert any("禁用词" in issue for issue in reviewed["compliance_issues"])


def test_frame_copy_flags_trade_actions_the_line_check_would_miss():
    """止损/目标价这类词不在台词禁用词表里，但打在片尾图板上比字幕更醒目。

    所以片尾文案单独多过一道：出现买卖动作或点位就直接记问题。
    """
    reviewed = ComplianceAgent({}).review({
        "turns": [{"speaker": "bull", "line": "这门生意的客户主要是海外经销商。"}],
        "hook": "明年的目标价",
        "sides": {"bear": "跌破 12 元就止损"},
    })

    issues = " ".join(reviewed["compliance_issues"])
    assert "片尾文案触碰画面红线: 目标价" in issues
    assert "片尾文案触碰画面红线: 止损" in issues
