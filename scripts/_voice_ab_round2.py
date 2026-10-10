"""第二轮 A/B：刘飞+5 半音 vs 洪亮男声备选（云舟/解说小明/磁性解说男声）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")

from stocktalk.modules.providers.base import TTSJob
from stocktalk.modules.providers.volcengine_tts import VolcengineTTSProvider

OUT = Path("output/_voice_ab_pitch/round2")
OUT.mkdir(parents=True, exist_ok=True)

TEXT = ("应收账款周转天数从四十五天拉到九十天，这个信号不妙。"
        "毛利看着漂亮，现金流却跟不上，说明利润可能只是纸面数字。")
INSTRUCTION = "用沉稳冷静、带审视和质疑的语气指出风险，语气笃定"

CASES = [
    ("b0_liufei_plus5", "zh_male_liufei_uranus_bigtts", 1.335),
    ("b1_yunzhou", "zh_male_m191_uranus_bigtts", 1.0),
    ("b2_jieshuoxiaoming", "zh_male_jieshuoxiaoming_uranus_bigtts", 1.0),
    ("b3_cixingjieshuo", "zh_male_cixingjieshuonan_uranus_bigtts", 1.0),
]

provider = VolcengineTTSProvider(api_key=os.environ["VOLCENGINE_TTS_API_KEY"])
jobs = [TTSJob(text=TEXT, voice=voice_id, out_path=OUT / f"{label}.wav",
               audio_format="wav", speed=1.0, pitch=ratio,
               instruction=INSTRUCTION, label=label)
        for label, voice_id, ratio in CASES]
provider.synthesize_lines(jobs)
for job, (label, _, ratio) in zip(jobs, CASES):
    print(f"{label}: pitch={ratio} size={job.out_path.stat().st_size}")
