"""百炼 CLI（``bl text chat``）的对话实现：子进程调用。

runner 注入契约与旧版 ``DialogueGenerator`` 完全一致：
``runner(argv, timeout) -> stdout``，既有测试无需改动即可继续注入。
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Any, Callable, Mapping, Sequence

from stocktalk.modules.providers.base import LLMProviderError, register_llm

Runner = Callable[[Sequence[str], float], str]


def _run_subprocess(argv: Sequence[str], timeout: float) -> str:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LLMProviderError(f"Failed to run Bailian chat: {exc}") from exc
    if completed.returncode != 0:
        raise LLMProviderError(
            f"Bailian chat failed: {completed.stderr.strip() or completed.stdout.strip() or 'unknown error'}")
    return completed.stdout


class BailianChatClient:
    """通过百炼 CLI 子进程做对话补全。

    可用性检查只在**自持传输**（未注入 runner）时做：注入 runner 的一方
    自己负责传输，是否存在 ``bl`` 与它无关（测试注入因此无需 mock PATH）。
    """

    def __init__(self, model: str = "qwen3.6-plus", runner: Runner | None = None,
                 default_timeout: float = 300.0) -> None:
        self.model = model
        self._runner: Runner = runner or _run_subprocess
        self._owns_transport = runner is None
        self._default_timeout = float(default_timeout)

    def chat(self, system: str, message: str, *,
             max_tokens: int | None = None,
             temperature: float | None = None,
             timeout: float | None = None) -> str:
        if self._owns_transport and not shutil.which("bl"):
            raise LLMProviderError("Bailian CLI 'bl' is not installed or not on PATH")
        argv = ["bl", "text", "chat",
                "--model", self.model,
                "--system", system,
                "--message", message,
                "--max-tokens", str(max_tokens),
                "--temperature", str(temperature),
                "--output", "json", "--quiet"]
        effective_timeout = float(timeout if timeout is not None else self._default_timeout)
        return self._runner(argv, effective_timeout)


@register_llm("bailian-cli")
def bailian_chat(source: Mapping[str, Any], default_timeout: float = 300.0,
                 runner: Runner | None = None) -> BailianChatClient:
    options = dict(source.get("options") or {})
    return BailianChatClient(
        model=str(source.get("model", "qwen3.6-plus")),
        runner=runner if runner is not None else options.get("runner"),
        default_timeout=default_timeout,
    )
