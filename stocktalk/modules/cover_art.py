"""AI 封面底图：为个股生成一张专属背景图，失败一律回落到现有封面。

**为什么走 ``bl image generate`` 子进程**：与对话 provider 同一条 bailian-cli
链路。凭据由 CLI 自己解析（``DASHSCOPE_API_KEY`` 环境变量优先，其次
``bl auth login`` 写下的登录态），渲染链路上不新增 HTTP 客户端、不新增 Python
依赖，与 ``BailianChatClient`` 的注入契约（``runner(argv, timeout) -> stdout``）
完全一致，测试不需要网络也不需要真 CLI。

三条硬约束（``tests/test_cover_art.py`` 逐条锁住）：

1. **可降级**：缺 ``bl``、缺凭据、超时、非零退出、产物缺失，一律返回 ``None``
   ——模板退回原来的纯 CSS 渐变，渲染/发布流程不感知这次失败。第一次失败后
   本进程内不再重试：一次渲染要出 1-3 个尺寸，逐个等超时会把整条链路拖死。
2. **可复现**：seed 由 ticker 派生、风格锚固定在 prompt 前缀、``prompt_extend``
   关闭——同一支股票反复生成是同一套视觉，而不是每次换一张脸。
3. **不重复付费**：产物按 ticker（+ 尺寸）落在缓存目录，命中即复用，不再调用。
   同一支股票的后续期次默认沿用同一张底图（``cover.ai.reuse: ticker``）；
   想让每期按新叙事重画，改成 ``episode``。

底图只做背景：模板里它是铺满整帧、被渐变压暗的一层，文字与版式一点不动，
所以尺寸必须与封面预设像素一一对应（``COVER_PRESETS``），不引入第二种画幅。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

# 开关：auto（跟 cover.ai.enabled 走，默认有凭据就开）/ 1 / 0。
ENV_TOGGLE = "STOCKTALK_COVER_AI"
# 凭据：与 TTS 的 dashscope provider 同一个变量，bl 也优先读它。
DEFAULT_API_KEY_ENV = "DASHSCOPE_API_KEY"
DEFAULT_MODEL = "wan2.6-t2i"
DEFAULT_TIMEOUT_SECONDS = 240.0
# 模型输出的图片容器由服务端决定（png/jpeg/webp 都见过），靠文件头嗅探 mime。
IMAGE_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")
_MAX_IMAGE_BYTES = 12 * 1024 * 1024

# 风格锚：写在 prompt 最前面且不可配置，保证「同一账号的封面是一家人」。
# 刻意不出现「封面 / 海报 / 标题 / 股价」这类排版暗示——实测模型会照着做整版
# 排版，把中文标语、假图标条、甚至「崩盘预警」这类合规禁语画进图里。
STYLE_ANCHOR = ("电影感氛围背景摄影，深色高级质感，浅景深细化散景，主体清晰、"
                "四周留大面积深色净空，画面只是一层氛围背景")
# 英文硬约束放最后。2026-10-28 实测：prompt 里只要出现公司名，酒坛标签上就会
# 长出「贵州茅台」四个字（qwen-image-3.0 更严重，整版假 UI + 假标题）；这段
# 是唯一压得住它的手段，比中文的「不要文字」有效得多。
NO_TEXT_RULE = ("No text, no letters, no numbers, no Chinese characters, no logos, "
                "no watermarks, no labels, no UI, no icons, no charts, no poster layout, "
                "no typography, no brand packaging.")
# 构图提示按封面画幅给：文字压在留白那侧，AI 图才不会被标题盖住主体。
ORIENTATION: dict[str, str] = {
    "portrait": "竖版构图，主体集中在画面上三分之二，底部留大面积深色净空",
    "landscape": "横版构图，主体集中在画面右侧，左侧留大面积深色净空",
    "wide": "宽幅构图，主体集中在画面右侧，左侧留大面积深色净空",
}
# 涨跌氛围与封面的红涨绿跌约定同源（但不画 K 线，曲线仍由模板画真实收盘价）。
TONE_WORDS: dict[str, str] = {
    "up": "暖红金色调，向上的势能",
    "down": "冷绿青色偏调，承压而克制的氛围",
    "flat": "蓝灰中性色调，平稳克制",
}
# 公司名 → 视觉意象。**公司名与股票代码绝不进 prompt**：实测它们会被原样画成
# 图上的字，而图上的字不受合规审查。这里只把名字当"分类信号"用：同一支股票
# 永远落到同一个意象，不同股票/行业换主题，辨识度来自意象 + seed。
INDUSTRY_IMAGERY: tuple[tuple[tuple[str, ...], str], ...] = (
    (("酒", "茅台", "酿", "啤酒", "饮"), "酒窖、陶坛与琥珀色酒液"),
    (("药", "医", "生物", "疫苗", "诊疗"), "洁净实验室与药液安瓿"),
    (("银行", "证券", "保险", "金融", "信托"), "金融城的玻璃幕墙与光带"),
    (("芯片", "半导体", "电子", "集成", "光电"), "晶圆与精密光刻的冷光"),
    (("汽车", "轮胎", "锂", "电池", "整车"), "自动化产线与金属构件"),
    (("地产", "建设", "建筑", "城建", "工程"), "钢筋森林与塔吊剪影"),
    (("煤", "油", "能源", "电力", "光伏", "风", "燃气"), "能源装置与管线地平线"),
    (("港", "航运", "物流", "铁路", "航空", "快递"), "集装箱堆场与航线灯火"),
    (("食", "乳", "农", "牧", "渔", "种业"), "谷物、田野与冷链仓"),
    (("钢", "有色", "金属", "矿", "稀土"), "熔炉与金属锭的余温"),
    (("机", "电气", "重工", "装备", "轴承"), "重型机械与齿轮结构"),
    (("通信", "网络", "软件", "科技", "数字", "数据"), "数据流光与光纤脉络"),
    (("家电", "家居", "服饰", "零售", "商贸", "超市"), "现代卖场与成排陈列"),
)
FALLBACK_IMAGERY = "现代工业与商业空间的抽象剪影"
NEGATIVE_PROMPT = ("文字, 汉字, 英文字母, 数字, 水印, logo, 商标, 二维码, 界面截图, "
                   "K线图, 坐标轴, 拼贴, 低分辨率, 模糊, 畸形的手, 多余的手指")


Runner = Callable[[Sequence[str], float], str]

_MIME_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),
)


def _normalise(raw: Any) -> str:
    """``1/true/on`` → ``on``；``0/false/off`` → ``off``；其余（含 auto/空）→ ``""``。"""
    text = str(raw if raw is not None else "").strip().lower()
    if text in {"1", "true", "on", "yes", "enabled"}:
        return "on"
    if text in {"0", "false", "off", "no", "disabled"}:
        return "off"
    return ""


def resolve_mode(configured: Any, *, section_present: bool = True) -> str:
    """把开关（环境变量 > 配置）归一成 ``on`` / ``off`` / ``auto``。

    环境变量排前面是有意的：运维临时关掉 AI 封面不该去改 YAML、也不该等一次
    部署；``STOCKTALK_COVER_AI=0`` 当场退回纯模板封面。

    没有 ``cover.ai`` 段 = 关：直连 ``CoverGenerator`` 的调用方（测试、脚本、
    别人 import 本模块）不该因为我们探测到本机装了 ``bl`` 就凭空开始花钱。
    真正的运行都走 ``load_config``，默认配置里那一段写着 ``enabled: auto``。
    """
    env_mode = _normalise(os.environ.get(ENV_TOGGLE))
    if env_mode:
        return env_mode
    if not section_present:
        return "off"
    return _normalise(configured) or "auto"



def art_seed(stock_code: str) -> int:
    """Ticker → 固定 seed：同一支股票每次请求都是同一个随机起点。"""
    digest = hashlib.sha256(str(stock_code).strip().upper().encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 2_147_483_647


def industry_imagery(stock_name: str = "", industry: str = "") -> str:
    """公司名/行业 → 一个可以画出来的视觉意象。

    名字只当**分类信号**用，绝不进 prompt 正文（见 ``INDUSTRY_IMAGERY`` 注释）。
    ``industry`` 来自 F10 的「所属行业」，比名字更准；两者都缺时退回中性意象。
    """
    text = f"{industry} {stock_name}"
    for keywords, imagery in INDUSTRY_IMAGERY:
        if any(keyword in text for keyword in keywords):
            return imagery
    return FALLBACK_IMAGERY


def art_prompt(*, stock_name: str = "", industry: str = "", tone: str = "flat",
               size: str = "portrait", style: str = "") -> str:
    """拼一张底图的 prompt：风格锚 → 构图 → 意象 → 色调 → 英文禁字约束。

    槽位顺序固定、每项可缺省，所以同一支股票永远是同一句话（seed 也固定），
    换一只票只换意象与色调。
    """
    parts = [
        STYLE_ANCHOR,
        ORIENTATION.get(size, ORIENTATION["portrait"]),
        f"画面意象：{industry_imagery(stock_name, industry)}",
        "色调：" + TONE_WORDS.get(tone, TONE_WORDS["flat"]),
    ]
    if style:
        parts.append(style.strip())
    return "。".join(part for part in parts if part) + "。" + NO_TEXT_RULE



def _sniff_mime(data: bytes) -> str:
    for magic, mime in _MIME_BY_MAGIC:
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return "image/png"


def error_detail(raw: str) -> str:
    """从 CLI 输出里挑一行能读的错因。

    ``bl`` 失败时把错误打成 JSON（``{"error": {"code":..., "message":...}}``），
    直接取最后一行只会得到 ``}``——日志里等于没说，运维也就查不出是欠费还是
    模型名写错。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and str(error.get("message") or "").strip():
            return str(error["message"]).strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
        if str(payload.get("message") or "").strip():
            return str(payload["message"]).strip()
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and line.strip() not in "{}[]"]
    return lines[-1] if lines else text[-200:]


def image_data_uri(path: Path) -> str:
    """Read the cached art as a data URI.

    A data URI keeps the cover HTML self-contained: Chrome opens it from a temp
    directory, and ``file://`` sub-resources would depend on flags we do not
    pass. The art is a few MB, which is nothing next to the MP4 it decorates.
    """
    data = path.read_bytes()
    return f"data:{_sniff_mime(data)};base64,{base64.b64encode(data).decode('ascii')}"


class AiCoverArt:
    """Generate (and cache) one background image per ticker and cover preset."""

    def __init__(self, config: Mapping[str, Any] | None = None,
                 runner: Runner | None = None) -> None:
        merged = dict(config or {})
        cover = merged.get("cover") if isinstance(merged.get("cover"), Mapping) else {}
        has_section = isinstance(cover.get("ai"), Mapping)
        ai = cover.get("ai") if has_section else {}
        self.model = str(ai.get("model") or DEFAULT_MODEL).strip()
        self.timeout = float(ai.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        self.negative_prompt = str(ai.get("negative_prompt") or NEGATIVE_PROMPT).strip()
        self.style = str(ai.get("style") or "").strip()
        self.api_key_env = str(ai.get("api_key_env") or DEFAULT_API_KEY_ENV).strip()
        # ticker（默认，省钱且身份稳定）| episode（每期按新叙事重画）
        self.reuse = str(ai.get("reuse") or "ticker").strip().lower()
        self._mode = resolve_mode(ai.get("enabled"), section_present=has_section)
        self._runner = runner
        self._owns_transport = runner is None
        self._failed = False
        self.last_error = ""
        output = merged.get("output") if isinstance(merged.get("output"), Mapping) else {}
        cache = str(ai.get("cache_dir") or "").strip()
        base = Path(cache).expanduser() if cache else Path(str(output.get("dir") or "output")) / "cover-art"
        self.cache_dir = base

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """Is there any point in trying?

        ``off`` wins outright. Otherwise the transport has to exist: an injected
        runner counts (the caller owns it), else the CLI or a key must be
        present. Nothing here calls out — construction and this check stay
        side-effect free, so a missing key costs zero seconds.
        """
        if self._mode == "off":
            return False
        if not self._owns_transport:
            return True
        if os.environ.get(self.api_key_env, "").strip():
            return True
        return shutil.which("bl") is not None

    # ------------------------------------------------------------------
    # Art
    # ------------------------------------------------------------------
    def background_uri(self, *, stock_name: str, stock_code: str, narrative: str = "",
                       industry: str = "", tone: str = "flat", size: str = "portrait",
                       preset: tuple[int, int] = (1080, 1440)) -> str | None:
        """Return a ``data:`` URI for this ticker's background, or None.

        ``narrative`` never reaches the prompt（实测会被画成图上的字），它只在
        ``reuse: episode`` 时参与缓存键——也就是「这一期是否值得重画」的判据。

        None is the whole degradation contract: no art, no exception, the
        template renders exactly as it did before this module existed.
        """
        if not self.enabled or self._failed:
            return None
        prompt = art_prompt(stock_name=stock_name, industry=industry, tone=tone,
                            size=size, style=self.style)
        seed = art_seed(stock_code)
        cached = self._cache_path(stock_code, size, prompt, seed, narrative)

        try:
            if not (cached.is_file() and 0 < cached.stat().st_size <= _MAX_IMAGE_BYTES):
                self._render(prompt, preset, seed, cached)
            return image_data_uri(cached)
        except Exception as exc:  # noqa: BLE001 — 底图是锦上添花，绝不打断渲染
            self._failed = True
            self.last_error = f"AI 封面底图生成失败（{size}）：{exc}"
            return None

    def _cache_path(self, stock_code: str, size: str, prompt: str, seed: int,
                    narrative: str = "") -> Path:
        """Cache by ticker + size, keyed by everything that changes the pixels.

        ``reuse: ticker`` deliberately leaves the narrative out of the digest:
        a new episode of the same stock reuses the art already paid for. Model
        / style / seed changes still invalidate it, so switching the look never
        keeps serving the old picture. ``reuse: episode`` folds the narrative
        back in — that is the only thing a new episode changes.
        """
        ticker = re.sub(r"[^0-9A-Za-z_-]", "", str(stock_code)) or "unknown"
        material = "|".join((self.model, size, str(seed), self.style, self.reuse,
                             f"{prompt}|{narrative}" if self.reuse == "episode" else ""))
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]
        return self.cache_dir / ticker / f"{size}-{digest}"

    def _render(self, prompt: str, preset: tuple[int, int], seed: int, target: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="stocktalk-cover-art-") as temp:
            out_dir = Path(temp)
            self._run(self._argv(prompt, preset, seed, out_dir), self.timeout)
            produced = sorted(
                (path for path in out_dir.iterdir()
                 if path.suffix.lower() in IMAGE_SUFFIXES and path.stat().st_size > 0),
                key=lambda path: path.name,
            )
            if not produced:
                raise RuntimeError("bl image generate 没有产出图片")
            # 成功后才建缓存目录：失败的 ticker 不留下空壳。
            target.parent.mkdir(parents=True, exist_ok=True)
            # 同文件系统内的 rename，半张图不会被后续的缓存命中读到。
            shutil.move(str(produced[0]), str(target))

    def _argv(self, prompt: str, preset: tuple[int, int], seed: int, out_dir: Path) -> list[str]:
        width, height = preset
        return [
            "bl", "image", "generate",
            "--prompt", prompt,
            "--model", self.model,
            # 像素级对齐封面预设：不引入需要改渲染器布局的第二种画幅。
            "--size", f"{int(width)}*{int(height)}",
            "--seed", str(int(seed)),
            "--n", "1",
            # 关闭 prompt 扩写：服务端改写 prompt 会让同一 ticker 的图漂移。
            "--prompt-extend", "false",
            "--watermark", "false",
            "--negative-prompt", self.negative_prompt,
            "--out-dir", str(out_dir),
            "--out-prefix", "cover",
            "--output", "json", "--quiet",
        ]

    def _run(self, argv: Sequence[str], timeout: float) -> str:
        if not self._owns_transport:
            return self._runner(argv, timeout)  # type: ignore[misc]
        try:
            completed = subprocess.run(argv, capture_output=True, text=True,
                                       timeout=max(1.0, timeout), check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"{argv[0]} 调用失败: {exc}") from exc
        if completed.returncode != 0:
            detail = error_detail(completed.stderr or completed.stdout)
            raise RuntimeError(detail or f"退出码 {completed.returncode}")
        return completed.stdout


__all__ = ["AiCoverArt", "ENV_TOGGLE", "DEFAULT_MODEL", "art_prompt", "art_seed",
           "error_detail", "image_data_uri", "industry_imagery", "resolve_mode"]
