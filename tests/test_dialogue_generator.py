import json
import unittest
from unittest.mock import patch

from stocktalk.modules.dialogue_generator import DialogueGenerationError, DialogueGenerator


class DialogueGeneratorTests(unittest.TestCase):
    def _response(self, rounds=2):
        return json.dumps({
            "title": "平安银行观点碰撞",
            "rounds": [
                {"bull": {"line": "基本面数据有改善。", "visual_prompt": "财务卡片"},
                 "bear": {"line": "还要看风险和估值。", "visual_prompt": "风险图标"}}
                for _ in range(rounds)
            ],
        }, ensure_ascii=False)

    def test_generates_tts_compatible_rounds_and_bounded_lines(self):
        calls = []
        generator = DialogueGenerator(
            {"dialogue": {"rounds": 2, "max_line_chars": 10}, "characters": {"bull": {"name": "新手"}}},
            runner=lambda argv, timeout: calls.append((argv, timeout)) or self._response(),
        )
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="/usr/bin/bl"):
            script = generator.generate({"code": "000001", "quote": {"name": "平安银行", "price": 10.5}})
        self.assertEqual(len(script["rounds"]), 2)
        self.assertEqual(script["rounds"][0]["bull"]["character_name"], "新手")
        self.assertLessEqual(len(script["rounds"][0]["bull"]["line"]), 10)
        self.assertEqual(calls[0][0][:3], ["bl", "text", "chat"])
        self.assertIn("--non-interactive", calls[0][0])
        self.assertEqual(script["disclaimer"], "内容为虚拟人物观点碰撞，不构成投资建议。")

    def test_accepts_bailian_json_envelope(self):
        payload = json.dumps({"content": self._response()})
        generator = DialogueGenerator({"rounds": 2}, runner=lambda argv, timeout: payload)
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            self.assertEqual(len(generator.generate({"code": "1"})["rounds"]), 2)

    def test_rejects_wrong_number_of_rounds(self):
        generator = DialogueGenerator({"rounds": 2}, runner=lambda argv, timeout: self._response(rounds=1))
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            with self.assertRaises(DialogueGenerationError):
                generator.generate({"code": "1"})

    def test_prompt_excludes_technical_history(self):
        generator = DialogueGenerator({"rounds": 2}, runner=lambda argv, timeout: self._response())
        with patch("stocktalk.modules.dialogue_generator.shutil.which", return_value="bl"):
            generator.generate({"code": "1", "technical": {"summary": {"rsi": 60}, "history": [{"close": 1}]}})
        message = calls = generator._user_prompt(generator._compact_data({"technical": {"summary": {"rsi": 60}, "history": [{"close": 1}]}}))
        self.assertIn("rsi", message)
        self.assertNotIn("history", message)


if __name__ == "__main__":
    unittest.main()
