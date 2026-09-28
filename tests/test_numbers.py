"""中文数字口播/显示分层的回归测试（2026-09-26 003040 事故驱动）。

事故：LLM 写出「应收账款四亿五一」「存货两亿三二」式缩略读法，TTS 念着别扭，
同一个短语被抽去当 visual 关键词、封面标签和平台 tags 后观众完全看不懂。
"""

from stocktalk.modules.numbers import has_digit, repair_spoken_numbers, to_display, unbreakable_spans


def test_repair_only_fixes_abbreviated_readings():
    """口播层：缩略读法修成可发音的阿拉伯数字，完整读法原样保留。"""
    assert repair_spoken_numbers("应收账款四亿五一，存货两亿三二") == "应收账款4.51亿，存货2.32亿"
    # 完整读法是 TTS 最可靠的输入，一个字都不能动
    assert repair_spoken_numbers("上半年营收三亿一千九百万") == "上半年营收三亿一千九百万"
    assert repair_spoken_numbers("毛利率十八点二九") == "毛利率十八点二九"
    assert repair_spoken_numbers("市盈率九百三十五倍") == "市盈率九百三十五倍"


def test_display_converts_unit_anchored_numbers():
    """显示层：贴单位词的汉字数字转阿拉伯，数字语境完整覆盖。"""
    assert to_display("上半年营收三亿一千九百万") == "上半年营收3.19亿"
    assert to_display("经营现金流却是负的一亿一千五百万") == "经营现金流却是负的1.15亿"
    assert to_display("毛利率十八点二九") == "毛利率18.29"
    assert to_display("市盈率九百三十五倍") == "市盈率935倍"
    assert to_display("通信设备九十二家同行") == "通信设备92家同行"
    assert to_display("九月二十六号刚中标数币硬钱包项目") == "9月26号刚中标数币硬钱包项目"
    assert to_display("二零二六年中报") == "2026年中报"
    assert to_display("机构净卖出两千七百多万") == "机构净卖出2700多万"
    assert to_display("嵌入式安全产品占收入八成二七") == "嵌入式安全产品占收入82.7%"
    assert to_display("百分之八十二点七") == "82.7%"


def test_display_leaves_idioms_and_adverbs_alone():
    """保守性：数字不贴单位词的成语、副词一律不动，转坏了比不转更糟。"""
    assert to_display("十有八九是资金博弈") == "十有八九是资金博弈"
    assert to_display("不管三七二十一") == "不管三七二十一"
    assert to_display("三十六计走为上") == "三十六计走为上"
    assert to_display("百年不遇的行情") == "百年不遇的行情"
    assert to_display("三十年河东三十年河西") == "三十年河东三十年河西"
    assert to_display("千万别追高") == "千万别追高"


def test_has_digit_filters_number_fragments_from_tags():
    """带数字成分的词适合上屏、不适合当平台检索标签。"""
    assert has_digit("毛利率18.29")
    assert has_digit("应收账款4.51亿")
    assert has_digit("毛利率十八点二九")
    assert not has_digit("现金流")
    assert not has_digit("嵌入式安全产品")
    assert not has_digit("eSIM")
    # 「零售」的零不是数字，不能误伤
    assert not has_digit("零售")


def test_unbreakable_spans_covers_decimal_and_thousands():
    """排版层：小数点、千分位逗号、单位后缀都必须留在同一个数字整体里。

    2026-09-28 事故：字幕分块把 ASCII「.」当句读，屏幕上是「净利率47.」+
    「07%」两个半截数字。
    """
    text = "净利率47.07%。总资产8,604.51亿打底。涨幅3.5倍。九月二十六号"
    spans = [text[a:b] for a, b in unbreakable_spans(text)]
    assert "47.07%" in spans
    assert "8,604.51亿" in spans
    assert "3.5倍" in spans

    # 区间必须自洽：左闭右开、不越界、不重叠
    previous = -1
    for start, end in unbreakable_spans(text):
        assert 0 <= start < end <= len(text)
        assert start > previous
        previous = start


def test_unbreakable_spans_does_not_swallow_punctuation():
    """保守性：整数之后的顿号/逗号是句读，不是千分位，不能一起圈进来。"""

    def pieces(text: str) -> list[str]:
        return [text[a:b] for a, b in unbreakable_spans(text)]

    assert pieces("第1，2名") == ["1", "2"]
    assert pieces("涨了，跌了") == []
    # 纯中文短语不该被当成数字
    assert pieces("零售和十分钱") == []


def test_to_display_never_emits_a_split_decimal():
    """两层拼接：汉字数字转阿拉伯之后，小数点两侧仍是同一个整体。"""
    text = to_display("毛利率十八点二九，市净率二点零七")
    assert text == "毛利率18.29，市净率2.07"
    assert [text[a:b] for a, b in unbreakable_spans(text)] == ["18.29", "2.07"]
