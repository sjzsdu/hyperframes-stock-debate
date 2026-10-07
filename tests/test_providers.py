"""AI provider 抽象层（stocktalk/modules/providers）的契约与实现测试。

覆盖三条主线：
1. 工厂按配置挑选实现（缺省 = 旧行为，未知名 = 明确报错）；
2. 各实现的请求构造与错误路径（不真连网/不真调 CLI）；
3. 编排层（DialogueGenerator / TTSAgent）对注入 provider 的委托行为。
"""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from stocktalk.modules.dialogue_generator import DialogueGenerationError, DialogueGenerator
from stocktalk.modules.providers import (
    LLMProviderError,
    TTSProviderError,
    create_llm_client,
    create_tts_provider,
)
from stocktalk.modules.providers.bailian_chat import BailianChatClient
from stocktalk.modules.providers.bailian_tts import BailianTTSProvider
from stocktalk.modules.providers.base import TTSJob
from stocktalk.modules.providers.dashscope_ws_tts import DashScopeWSTTSProvider
from stocktalk.modules.providers.openai_chat import OpenAIChatClient
from stocktalk.modules.tts_agent import TTSAgent


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def test_default_llm_provider_is_bailian_cli():
    client = create_llm_client({"dialogue": {"model": "qwen3.6-plus"}})
    assert isinstance(client, BailianChatClient)
    assert client.model == "qwen3.6-plus"


def test_openai_compatible_llm_provider_from_config():
    client = create_llm_client({"dialogue": {
        "provider": "openai-compatible", "model": "deepseek-chat",
        "options": {"base_url": "http://localhost:8000/v1/", "api_key_env": "MY_KEY"}}})
    assert isinstance(client, OpenAIChatClient)
    assert client.model == "deepseek-chat"
    assert client.base_url == "http://localhost:8000/v1"  # 尾斜杠归一
    assert client.api_key_env == "MY_KEY"


def test_unknown_llm_provider_names_the_valid_choices():
    with pytest.raises(LLMProviderError, match="openai-compatible"):
        create_llm_client({"dialogue": {"provider": "nope"}})


def test_tts_factory_auto_resolves_by_model_name():
    assert isinstance(create_tts_provider({"model": "cosyvoice-v3-flash"}), BailianTTSProvider)
    assert isinstance(create_tts_provider({"model": "qwen3-tts-vd-realtime-2025-12-16"}),
                      DashScopeWSTTSProvider)


def test_tts_factory_respects_explicit_provider():
    assert isinstance(create_tts_provider({"model": "cosyvoice-v3-flash", "provider": "dashscope-ws"}),
                      DashScopeWSTTSProvider)
    assert isinstance(create_tts_provider({"provider": "bailian-cli"}), BailianTTSProvider)


def test_unknown_tts_provider_names_the_valid_choices():
    with pytest.raises(TTSProviderError, match="bailian-cli"):
        create_tts_provider({"provider": "nope"})


# ---------------------------------------------------------------------------
# 百炼对话实现
# ---------------------------------------------------------------------------

def test_bailian_chat_builds_the_expected_argv():
    captured: list[list[str]] = []

    def runner(argv, timeout):
        captured.append((argv, timeout))
        return '{"output": {"text": "ok"}}'

    client = BailianChatClient(model="qwen3.6-plus", runner=runner, default_timeout=300.0)
    raw = client.chat("系统提示", "用户消息", max_tokens=8000, temperature=0.8, timeout=42)

    assert raw == '{"output": {"text": "ok"}}'
    argv, timeout = captured[0]
    assert argv[0:4] == ["bl", "text", "chat", "--model"]
    assert "--max-tokens" in argv and "8000" in argv
    assert "--temperature" in argv and "0.8" in argv
    assert argv[argv.index("--output") + 1] == "json"
    assert timeout == 42.0  # 显式 timeout 优先于 default


def test_bailian_chat_falls_back_to_default_timeout():
    captured: list[tuple[list[str], float]] = []
    client = BailianChatClient(runner=lambda argv, timeout: captured.append((argv, timeout)) or "{}",
                               default_timeout=123.0)
    client.chat("s", "m")
    assert captured[0][1] == 123.0


def test_bailian_chat_reports_missing_cli():
    with patch("stocktalk.modules.providers.bailian_chat.shutil.which", return_value=None):
        client = BailianChatClient()
        with pytest.raises(LLMProviderError, match="'bl' is not installed"):
            client.chat("s", "m")


def test_bailian_chat_with_injected_runner_skips_the_cli_check():
    """注入 runner 即自持传输：bl 在不在 PATH 与调用方无关，检查必须跳过。"""
    client = BailianChatClient(runner=lambda argv, timeout: "ok")
    with patch("stocktalk.modules.providers.bailian_chat.shutil.which", return_value=None):
        assert client.chat("s", "m") == "ok"


def test_bailian_chat_wraps_nonzero_exit(monkeypatch):
    monkeypatch.setattr("stocktalk.modules.providers.bailian_chat.shutil.which",
                        lambda _name: "/usr/local/bin/bl")
    monkeypatch.setattr("stocktalk.modules.providers.bailian_chat.subprocess.run",
                        lambda args, **_: subprocess.CompletedProcess(args, 1, "", "boom"))
    client = BailianChatClient()
    with pytest.raises(LLMProviderError, match="boom"):
        client.chat("s", "m")


# ---------------------------------------------------------------------------
# OpenAI-compatible 对话实现
# ---------------------------------------------------------------------------

def test_openai_chat_posts_the_expected_payload(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-test")
    seen: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "你好"}}]}).encode()

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["data"] = json.loads(request.data.decode())
        seen["auth"] = request.headers.get("Authorization")
        return FakeResponse()

    client = OpenAIChatClient(model="m1", base_url="http://x/v1", api_key_env="MY_KEY",
                              urlopen=fake_urlopen)
    assert client.chat("sys", "usr", max_tokens=99, temperature=0.5) == "你好"
    assert seen["url"] == "http://x/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["data"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["data"]["messages"][1] == {"role": "user", "content": "usr"}
    assert seen["data"]["max_tokens"] == 99
    assert seen["data"]["temperature"] == 0.5


def test_openai_chat_requires_the_api_key(monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    client = OpenAIChatClient(model="m1", api_key_env="MY_KEY", urlopen=lambda *a, **k: None)
    with pytest.raises(LLMProviderError, match="MY_KEY"):
        client.chat("s", "m")


def test_openai_chat_rejects_an_unexpected_body(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-test")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"error": {"message": "quota"}}'

    client = OpenAIChatClient(model="m1", api_key_env="MY_KEY", urlopen=lambda *a, **k: FakeResponse())
    with pytest.raises(LLMProviderError, match="unexpected body"):
        client.chat("s", "m")


# ---------------------------------------------------------------------------
# 百炼 TTS 实现
# ---------------------------------------------------------------------------

def _tts_job(tmp_path: Path, **overrides) -> TTSJob:
    defaults = dict(text="测试语音", voice="longxiaochun_v3",
                    out_path=tmp_path / "01_bull.wav", audio_format="wav",
                    speed=1.08, pitch=1.04, volume=1.0, use_ssml=True,
                    sample_rate=None, seed=7, instruction="年轻自然",
                    language="zh", label="bull")
    defaults.update(overrides)
    return TTSJob(**defaults)


def test_bailian_tts_builds_the_expected_command(tmp_path, monkeypatch):
    output = tmp_path / "01_bull.wav"
    captured: list[list[str]] = []

    def fake_run(args, **_kwargs):
        captured.append(args)
        output.write_bytes(b"audio")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = BailianTTSProvider(model="cosyvoice-v3-flash")
    provider.synthesize_lines([_tts_job(tmp_path)])

    argv = captured[0]
    assert argv[argv.index("--voice") + 1] == "longxiaochun_v3"
    assert argv[argv.index("--rate") + 1] == "1.08"
    assert argv[argv.index("--pitch") + 1] == "1.04"
    assert argv[argv.index("--instruction") + 1] == "年轻自然"
    assert argv[argv.index("--seed") + 1] == "7"
    assert argv[argv.index("--volume") + 1] == "100"  # 1.0 → ×100
    assert "--enable-ssml" in argv
    assert "--sample-rate" not in argv  # 未配置就不传
    assert argv[argv.index("--language") + 1] == "zh"


def test_bailian_tts_reports_failure_with_the_character_label(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda args, **_: subprocess.CompletedProcess(args, 1, "", "428"))
    provider = BailianTTSProvider()
    with pytest.raises(TTSProviderError, match="for bull"):
        provider.synthesize_lines([_tts_job(tmp_path)])


def test_bailian_tts_requires_the_cli():
    with patch("stocktalk.modules.providers.bailian_tts.shutil.which", return_value=None):
        provider = BailianTTSProvider()
        with pytest.raises(TTSProviderError, match="'bl' is not installed"):
            provider.check_ready()


def test_dashscope_ensure_wav_normalizes_bare_pcm():
    pcm = b"\x00\x00" * 240  # 240 帧 16-bit
    wrapped = DashScopeWSTTSProvider._ensure_wav(pcm)
    assert wrapped[:4] == b"RIFF"
    assert len(wrapped) == 44 + len(pcm)
    assert DashScopeWSTTSProvider._ensure_wav(b"RIFFxxxx") == b"RIFFxxxx"  # 已有头原样通过


# ---------------------------------------------------------------------------
# 编排层委托
# ---------------------------------------------------------------------------

class _RecordingProvider:
    """记录收到的 job 并写出哑音频，模拟任意 TTS 实现。"""

    supports_line_prosody = True

    def __init__(self):
        self.jobs: list[TTSJob] = []

    def check_ready(self) -> None:
        pass

    def synthesize_lines(self, jobs):
        for job in jobs:
            self.jobs.append(job)
            job.out_path.write_bytes(b"RIFF" + b"\x00" * 64)


def test_tts_agent_delegates_to_the_injected_provider(tmp_path, monkeypatch):
    config = {
        "output": {"dir": str(tmp_path)},
        "characters": {"bull": {"name": "多头", "voice_id": "v_bull", "speed": 1.08, "pitch": 1.04},
                       "bear": {"name": "空头", "voice_id": "v_bear"}},
        "tts": {"trim_silence": False},
    }
    agent = TTSAgent(config, tts_provider=_RecordingProvider())
    monkeypatch.setattr(agent, "_audio_duration", lambda _path: 1.0)  # 哑音频不含合法头
    result = agent.synthesize({"turns": [
        {"speaker": "bull", "line": "第一句。"},
        {"speaker": "bear", "line": "第二句。"},
    ]})

    assert [job.voice for job in agent._provider.jobs] == ["v_bull", "v_bear"]
    assert [seg["character"] for seg in result["segments"]] == ["bull", "bear"]
    assert all(seg["duration"] == 1.0 for seg in result["segments"])
    # prosody 路径：speed 取角色配置的 1.08（无标点修饰时无增量）
    assert agent._provider.jobs[0].speed == 1.08


def test_dialogue_generator_accepts_an_injected_llm_client():
    class FakeLLM:
        def __init__(self):
            self.calls: list[dict] = []

        def chat(self, system, message, *, max_tokens=None, temperature=None, timeout=None):
            self.calls.append({"max_tokens": max_tokens, "temperature": temperature, "timeout": timeout})
            return json.dumps({"turns": [{"speaker": "bull", "line": "这家公司靠卖水赚钱。"}]},
                              ensure_ascii=False)

    fake = FakeLLM()
    generator = DialogueGenerator({"characters": {"bull": {"name": "新手"}}}, llm=fake)
    script = generator.generate({"code": "1", "quote": {"name": "示例公司"}})

    assert script["turns"][0]["line"] == "这家公司靠卖水赚钱。"
    assert fake.calls[0]["max_tokens"] == 8000
    assert fake.calls[0]["temperature"] == 0.8
    assert fake.calls[0]["timeout"] == 300.0


def test_dialogue_generator_wraps_provider_errors():
    class FailingLLM:
        def chat(self, system, message, **kwargs):
            raise LLMProviderError("Bailian CLI 'bl' is not installed or not on PATH")

    generator = DialogueGenerator(llm=FailingLLM())
    with pytest.raises(DialogueGenerationError, match="'bl' is not installed"):
        generator.generate({"code": "1"})


def test_dialogue_generator_runner_contract_still_works():
    """旧测试的 runner=(argv, timeout) 注入必须继续可用且 argv 结构不变。"""
    calls: list[list[str]] = []

    def runner(argv, timeout):
        calls.append(argv)
        return json.dumps({"turns": [{"speaker": "bull", "line": "旧契约。"}]}, ensure_ascii=False)

    generator = DialogueGenerator({"characters": {"bull": {"name": "新手"}}}, runner=runner)
    script = generator.generate({"code": "1"})

    assert script["turns"][0]["line"] == "旧契约。"
    assert calls[0][0] == "bl"
    assert calls[0][calls[0].index("--output") + 1] == "json"


# ---------------------------------------------------------------------------
# 火山引擎豆包 TTS 实现
# ---------------------------------------------------------------------------

def _ndjson_urlopen(chunks: list[bytes], fail_line: str | None = None):
    """构造一个 fake urlopen：返回豆包 NDJSON 协议的响应。"""
    lines = [json.dumps({"code": 0, "data": base64.b64encode(c).decode()}) for c in chunks]
    if fail_line:
        lines.append(fail_line)
    lines.append(json.dumps({"code": 20000000}))
    payload = "\n".join(lines).encode()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return payload

    return lambda request, timeout=None: FakeResponse()


def _capture_request(fake_urlopen):
    holder: list[object] = []

    def wrapper(request, timeout=None):
        holder.append(request)
        return fake_urlopen(request, timeout)

    return wrapper, holder


def test_volcengine_tts_builds_request_and_writes_audio(tmp_path):
    import stocktalk.modules.providers.volcengine_tts as mod

    audio = b"RIFF-fake-audio"
    fake = _ndjson_urlopen([b"RIFF-", b"fake-audio"])
    urlopen, holder = _capture_request(fake)
    provider = mod.VolcengineTTSProvider(api_key="vk-test", urlopen=urlopen)
    provider.synthesize_lines([_tts_job(tmp_path, voice="zh_female_vv_uranus_bigtts",
                                       speed=1.07, volume=1.0, instruction="兴奋的语气")])

    request = holder[0]
    assert request.full_url == mod.TTS_ENDPOINT
    assert request.headers.get("X-api-key") == "vk-test"
    assert request.headers.get("X-api-resource-id") == "seed-tts-2.0"
    body = json.loads(request.data)
    params = body["req_params"]
    assert params["speaker"] == "zh_female_vv_uranus_bigtts"
    assert params["audio_params"]["speech_rate"] == 7        # speed 1.07 → +7
    assert params["audio_params"]["loudness_rate"] == 0      # volume 1.0 → 0
    additions = json.loads(params["additions"])
    assert additions == {"context_texts": ["兴奋的语气"]}     # additions 是 JSON 字符串
    assert (tmp_path / "01_bull.wav").read_bytes() == audio


def test_volcengine_tts_strips_ssml_break_tags(tmp_path):
    import stocktalk.modules.providers.volcengine_tts as mod

    urlopen, holder = _capture_request(_ndjson_urlopen([b"x"]))
    provider = mod.VolcengineTTSProvider(api_key="vk-test", urlopen=urlopen)
    provider.synthesize_lines([_tts_job(tmp_path, text="前半句<break time=\"300ms\"/>后半句")])

    params = json.loads(holder[0].data)["req_params"]
    assert "<break" not in params["text"]
    assert params["text"] == "前半句后半句"


def test_volcengine_tts_requires_the_api_key(tmp_path, monkeypatch):
    import stocktalk.modules.providers.volcengine_tts as mod

    monkeypatch.delenv("VOLCENGINE_TTS_API_KEY", raising=False)
    provider = mod.VolcengineTTSProvider(api_key_env="VOLCENGINE_TTS_API_KEY",
                                         urlopen=lambda *a, **k: None)
    with pytest.raises(TTSProviderError, match="VOLCENGINE_TTS_API_KEY"):
        provider.check_ready()
    with pytest.raises(TTSProviderError, match="VOLCENGINE_TTS_API_KEY"):
        provider.synthesize_lines([_tts_job(tmp_path)])


def test_volcengine_tts_surfaces_api_errors(tmp_path):
    import stocktalk.modules.providers.volcengine_tts as mod

    urlopen, _ = _capture_request(_ndjson_urlopen([], fail_line=json.dumps(
        {"code": 45000010, "message": "Invalid X-Api-Key"})))
    provider = mod.VolcengineTTSProvider(api_key="bad", urlopen=urlopen)
    with pytest.raises(TTSProviderError, match="45000010"):
        provider.synthesize_lines([_tts_job(tmp_path)])


def test_volcengine_tts_rejects_empty_audio(tmp_path):
    import stocktalk.modules.providers.volcengine_tts as mod

    urlopen, _ = _capture_request(_ndjson_urlopen([]))
    provider = mod.VolcengineTTSProvider(api_key="vk", urlopen=urlopen)
    with pytest.raises(TTSProviderError, match="no audio"):
        provider.synthesize_lines([_tts_job(tmp_path)])


def test_volcengine_provider_is_registered():
    from stocktalk.modules.providers import TTS_PROVIDERS

    assert "volcengine-doubao" in TTS_PROVIDERS


def test_volcengine_tts_skips_metadata_lines(tmp_path):
    """豆包会先返回 code=0/data=null 的元数据行（sentence/phonemes），不是错误。"""
    import stocktalk.modules.providers.volcengine_tts as mod

    lines = [
        json.dumps({"code": 0, "message": "", "data": None,
                    "sentence": {"phonemes": [], "text": "元数据"}}),
        json.dumps({"code": 0, "data": base64.b64encode(b"audio").decode()}),
        json.dumps({"code": 20000000}),
    ]
    payload = "\n".join(lines).encode()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return payload

    provider = mod.VolcengineTTSProvider(api_key="vk", urlopen=lambda *a, **k: FakeResponse())
    provider.synthesize_lines([_tts_job(tmp_path)])
    assert (tmp_path / "01_bull.wav").read_bytes() == b"audio"

def test_tts_agent_merges_per_turn_emotion_into_instruction(tmp_path, monkeypatch):
    """按轮情绪：基础指令 + EMOTION_DIRECTIVES 短语合成单句 instruction；
    缺省情绪只有基础指令；未开 supports_instruction 的角色整个通道关闭。"""
    config = {
        "output": {"dir": str(tmp_path)},
        "characters": {"bull": {"name": "多头", "voice_id": "v_bull",
                                 "supports_instruction": True,
                                 "instruction": "基础演绎指令"},
                       "bear": {"name": "空头", "voice_id": "v_bear"}},
        "tts": {"trim_silence": False},
    }
    agent = TTSAgent(config, tts_provider=_RecordingProvider())
    monkeypatch.setattr(agent, "_audio_duration", lambda _path: 1.0)
    agent.synthesize({"turns": [
        {"speaker": "bull", "line": "第一句。", "emotion": "惊叹"},
        {"speaker": "bull", "line": "第二句。"},
        {"speaker": "bear", "line": "第三句。", "emotion": "反驳"},
    ]})

    jobs = agent._provider.jobs
    assert jobs[0].instruction == ("基础演绎指令；这一句用惊讶、难以置信的语气，"
                                   "像刚看到一个意外的数字")
    assert jobs[1].instruction == "基础演绎指令"
    assert jobs[2].instruction is None  # bear 未开 supports_instruction，通道关闭
