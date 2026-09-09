import json
import unittest
from unittest.mock import patch

from stocktalk.modules.dialogue_generator import DialogueGenerationError, DialogueGenerator


class DialogueGeneratorTests(unittest.TestCase):
    def _response(self):
        return json.dumps({"title": "平安银行：增长叙事遇上风险定价", "turns": [
            {"speaker": "bull", "beat": "数据表象", "line": "净利润的改善像给银行换上更高效的发动机，但还要看这台发动机的油耗，也就是息差能否守住。", "visual_prompt": "绿色人物特写，营收与净利润增长数据卡片从右侧滑入，镜头推进。"},
            {"speaker": "bear", "beat": "风险追问", "line": "等等，利润增速不能脱离估值和不良贷款率看；市场情绪最容易把一段好数据外推成永恒趋势。", "visual_prompt": "红色人物抬手打断，K线和风险指标分屏，字幕放大“永恒趋势”。"},
            {"speaker": "bull", "beat": "观点修正", "line": "这点我认同。增长不是直线，真正值得讨论的是管理层能否在周期波动里把客户关系变成更深的护城河。", "visual_prompt": "双方由对峙转为同框，护城河示意图叠加客户关系网络，色调转为中性蓝。"},
        ]}, ensure_ascii=False)

    def test_generates_natural_ordered_turns_with_visual_direction(self):
        calls = []
        generator = DialogueGenerator({"dialogue": {"max_line_chars": 80}, "characters": {"bull": {"name": "新手"}}},
                                      runner=lambda argv, timeout: calls.append((argv, timeout)) or self._response())
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = generator.generate({"code": "000001", "quote": {"name": "平安银行", "price": 10.5}})
        self.assertNotIn("rounds", script)
        self.assertEqual([turn["speaker"] for turn in script["turns"]], ["bull", "bear", "bull"])
        self.assertEqual(script["turns"][0]["character_name"], "新手")
        self.assertTrue(all(turn["visual_prompt"] for turn in script["turns"]))
        self.assertTrue(all(len(turn["line"]) <= 80 for turn in script["turns"]))
        self.assertIn("不要编号、不要 Round", calls[0][0][calls[0][0].index("--message") + 1])
        self.assertIn("心理", calls[0][0][calls[0][0].index("--system") + 1])

    def test_accepts_bailian_json_envelope_and_visual_fallback(self):
        response = json.dumps({"content": json.dumps({"turns": [{"speaker": "bear", "line": "数据的边界比结论更重要。"}]}, ensure_ascii=False)})
        generator = DialogueGenerator(runner=lambda argv, timeout: response)
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            script = generator.generate({"code": "1"})
        self.assertIn("数据卡片", script["turns"][0]["visual_prompt"])

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
