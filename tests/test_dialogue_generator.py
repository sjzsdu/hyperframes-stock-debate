import json
import unittest
from unittest.mock import patch

from stocktalk.modules.dialogue_generator import DialogueGenerationError, DialogueGenerator


class DialogueGeneratorTests(unittest.TestCase):
    def _response(self):
        return json.dumps({"title": "平安银行（000001）：靠息差吃饭的生意", "turns": [
            {"speaker": "bull", "beat": "生意本质", "line": "银行的生意说简单也简单，把存款变成贷款，赚中间的息差，平安银行零售做得多，客户基础是关键。"},
            {"speaker": "bear", "beat": "行业追问", "line": "息差这门生意要看行业周期，利率下行的时候大家都紧，不良贷款率才是真功夫。"},
            {"speaker": "bull", "beat": "观点修正", "line": "这点我认同。增长不是直线，真正值得讨论的是管理层能否在周期波动里把客户关系变成更深的护城河。"},
        ]}, ensure_ascii=False)

    def test_generates_natural_ordered_turns(self):
        calls = []
        generator = DialogueGenerator({"dialogue": {"max_line_chars": 80}, "characters": {"bull": {"name": "新手"}}},
                                      runner=lambda argv, timeout: calls.append((argv, timeout)) or self._response())
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = generator.generate({"code": "000001", "quote": {"name": "平安银行", "price": 10.5}})
        self.assertNotIn("rounds", script)
        self.assertEqual([turn["speaker"] for turn in script["turns"]], ["bull", "bear", "bull"])
        self.assertEqual(script["turns"][0]["character_name"], "新手")
        self.assertTrue(all(len(turn["line"]) <= 80 for turn in script["turns"]))
        self.assertIn("不要编号、不要 Round", calls[0][0][calls[0][0].index("--message") + 1])
        self.assertIn("行业", calls[0][0][calls[0][0].index("--system") + 1])

    def test_prompt_focuses_on_business_not_technicals(self):
        generator = DialogueGenerator(runner=lambda argv, timeout: self._response())
        system = generator._system_prompt()
        message = generator._user_prompt({"code": "1"})
        self.assertIn("主业", system)
        self.assertIn("行业", system)
        self.assertIn("生意", system)
        self.assertIn("至少一半的篇幅围绕公司业务和行业本身", message)
        self.assertIn("技术信号最多作为一句带过的佐证", message)

    def test_script_has_no_disclaimer_or_visual_prompt_fields(self):
        generator = DialogueGenerator(runner=lambda argv, timeout: self._response())
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = generator.generate({"code": "000001", "quote": {"name": "平安银行"}})
        self.assertNotIn("disclaimer", script)
        for turn in script["turns"]:
            self.assertNotIn("visual_prompt", turn)

    def test_accepts_bailian_json_envelope(self):
        response = json.dumps({"content": json.dumps({"turns": [{"speaker": "bear", "line": "数据的边界比结论更重要。"}]}, ensure_ascii=False)})
        generator = DialogueGenerator(runner=lambda argv, timeout: response)
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = generator.generate({"code": "1"})
        self.assertEqual(script["turns"][0]["speaker"], "bear")

    def test_rejects_missing_or_invalid_speaker(self):
        generator = DialogueGenerator(runner=lambda argv, timeout: '{"turns":[{"speaker":"host","line":"x"}]}')
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            with self.assertRaises(DialogueGenerationError): generator.generate({"code": "1"})

    def test_prompt_excludes_technical_history(self):
        generator = DialogueGenerator(runner=lambda argv, timeout: self._response())
        message = generator._user_prompt(generator._compact_data({"technical": {"summary": {"rsi": 60}, "history": [{"close": 1}]}}))
        self.assertIn("rsi", message)
        self.assertNotIn("history", message)


if __name__ == "__main__":
    unittest.main()
