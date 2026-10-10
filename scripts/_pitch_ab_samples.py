"""生成男声音调 A/B 样本：刘飞 +0/+2/+3/+4 半音（2026-10-07 用户反馈男声偏低）。"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")

from stocktalk.modules.providers.base import TTSJob
from stocktalk.modules.providers.volcengine_tts import VolcengineTTSProvider

OUT = Path("output/_voice_ab_pitch")
OUT.mkdir(parents=True, exist_ok=True)

TEXT = ("应收账款周转天数从四十五天拉到九十天，这个信号不妙。"
        "毛利看着漂亮，现金流却跟不上，说明利润可能只是纸面数字。")
INSTRUCTION = "用沉稳冷静、带审视和质疑的语气指出风险，语气笃定"
VOICE = "zh_male_liufei_uranus_bigtts"

# (标签, 倍率, 半音)
CASES = [("a0_duizhao", 1.0, 0), ("a1_plus2", 1.1225, 2),
         ("a2_plus3", 1.1892, 3), ("a3_plus4", 1.2599, 4)]

provider = VolcengineTTSProvider(api_key=os.environ["VOLCENGINE_TTS_API_KEY"])
jobs = []
for label, ratio, semis in CASES:
    jobs.append(TTSJob(text=TEXT, voice=VOICE, out_path=OUT / f"{label}.wav",
                       audio_format="wav", speed=1.0, pitch=ratio,
                       instruction=INSTRUCTION, label=label))
provider.synthesize_lines(jobs)

for job, (label, ratio, semis) in zip(jobs, CASES):
    size = job.out_path.stat().st_size
    print(f"{label}: pitch ratio={ratio} semitones=+{semis} size={size}")
