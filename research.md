# research.md — 现状能力盘点（阶段 1 / WAN-206）

> 本文件只做只读盘点，不改代码、不开 PR、不做技术选型定稿。
>
> **证据约定**：以下所有 `path:line` 均相对仓库检出根 `hyperframes-stock-debate/`（本次核对的 HEAD：`e9cddcc`，分支 `agent/codex-juzhongdemacbook-pro-loc/bac79cad5c92`）。第三方 vendored 目录内的文件单独标注。行号可用 `nl -ba <path> | sed -n '<n>p'` 之类只读命令直接核对；命令中的统计数字均来自本次实际执行，见文末「可复现核对命令」。

## 0. 核实结果：与任务输入描述的差异

任务描述里的「现状要点」要求自行核实、不要直接采信。核对结论：

| 输入描述 | 核实结果 | 证据 |
| --- | --- | --- |
| Python 3.12 + setuptools | 属实 | `pyproject.toml:1-3`（setuptools≥68 / wheel）、`:11`（`requires-python = ">=3.12"`） |
| 包名 `stocktalk` | **部分不符**：分发名是 `tangulunjin`，`stocktalk` 是导入包名 | `pyproject.toml:6`（`name = "tangulunjin"`）、`:28`（`include = ["stocktalk*"]`） |
| CLI 入口 `tangulunjin` | 属实 | `pyproject.toml:24-25`、`stocktalk/__main__.py:1-5`（`python -m stocktalk`） |
| providers 有 `register_llm`/`register_tts`、factory 按配置选实现 | 属实，且比描述更完整（见 §3） | `stocktalk/modules/providers/base.py:94-109`、`factory.py:26-51` |
| 默认 `bailian-cli` | LLM 属实；**TTS 默认不是 bailian-cli**，是 `volcengine-doubao` | `stocktalk/config/default.yaml:8`（`dialogue.provider: bailian-cli`）、`:174`（`tts.provider: volcengine-doubao`） |
| `publisher.py` 依赖 `third_party/social-auto-upload`（15 patch） | 属实 | `stocktalk/modules/publisher.py:760`、`:1455-1472`；`third_party/patches/` 15 个 `.patch`；`third_party/README.md:216-246` |
| 无任何 Web/桌面 UI | 属实 | 全仓 grep 无 flask/fastapi/django/uvicorn/electron/tauri/pywebview/tkinter/streamlit/gradio 等；唯一 `websockets` 命中是 `stocktalk/modules/providers/dashscope_ws_tts.py:21`（TTS 传输，非 Web 服务） |
| 大模型统一由 `https://oneapi.wnbgj.com/` 提供 | **当前不成立（属实于「尚未做」）**：一处引用都没有 | 全仓 first-party 文件 grep `oneapi|wnbgj` = 0 命中；见 §3.4 |

补充事实：运行期依赖只有 `jinja2 / pyyaml / rich / websockets`（`pyproject.toml:12-17`），dev 依赖只有 `pytest>=8.0`（`:19-22`）。第三方整包 `third_party/` 实测 6.1M（其中 `social-auto-upload/` 5.9M、27 个顶层条目，`patches/` 148K）。

## 1. Pipeline 阶段（编排链路）

入口：`stocktalk/pipeline.py:71`（`class Pipeline`）、`:368`（`Pipeline.run`）；配置加载 `:43-68`（`load_config`：defaults + 用户配置深度合并；`:65-66` 显式指定的配置文件不存在时报错而非静默回默认，注释专门说明「静默降级成辩论片是最难查的坑」）。

`run()` 用 `total=6` 的进度条（`:385`）串起 6 个强制阶段 + 1 个可选阶段，逐条证据：

| # | 阶段 | 关键调用 | 证据 |
| --- | --- | --- | --- |
| 1 | 拉取个股数据 | `stock_client.get_stock_data(stock_code)`，失败转 `PipelineError` | `stocktalk/pipeline.py:387-395` |
| 2 | 生成剧本 | `episode_planner.plan` + `dialogue_gen.generate`，经 `_retry`（3 次指数退避，`:237-250`） | `stocktalk/pipeline.py:398-402`、`:399-400` |
| 3 | 合规审核 | `compliance.review(script)` | `stocktalk/pipeline.py:413-418` |
| 4 | 语音合成 | `tts.synthesize(approved)`；默认硬失败拒绝静音片，`allow_silent_fallback` 才降级 | `stocktalk/pipeline.py:420-439`（`:430-438`） |
| 5 | 构建 HyperFrames 工程 | `builder.build(stock_data, approved, audio)` | `stocktalk/pipeline.py:441-447` |
| 6 | 渲染 MP4 及衍生物 | `render_mp4`（`:456`）、分画幅版（`:459` → `_render_platform_cuts` `:252-288`）、抖音快评版（`:463` → `_render_short_form` `:290-337`）、封面（`:467` → `_render_covers` `:102-133`）、审查页（`:468` → `_write_review` `:173-196`） | `stocktalk/pipeline.py:453-475` |
| 7（可选） | 多平台发布 | `publisher.publish(...)`，仅在 `publish=True` 且有 MP4 时 | `stocktalk/pipeline.py:497-515` |

结果落盘：先写一版 `publish=null` 的完整 sidecar JSON（`:491-495`），发布结束后补写发布报告（`:514-515`）；`--republish` / `--publish-only` 靠这份 sidecar 恢复标题/封面/分画幅（`stocktalk/cli.py:932-959`、`:696-716`）。

其它入口与周边：CLI `main` 在 `stocktalk/cli.py:56`，参数面 `:82-157`；守护进程与排期 `:222-223`、`:478-517`，实现在 `stocktalk/modules/daemon.py` 与 `scheduler.py`；发布预检 `_check` `stocktalk/cli.py:326-376`；批量 `_run_all` `:862-888`。

## 2. 视频类型硬编码点（本任务的核心约束）

现状里「视频类型」= `dialogue.format`，是一个**二值开关**，不是注册表。证据链：

- **类型集合写死**：`stocktalk/modules/dialogue_generator.py:54-56` `FORMAT_DEBATE`/`FORMAT_MONOLOGUE`/`FORMATS = (debate, monologue)`；`:59-63` `dialogue_format()` 对未知值**静默回退 debate**（「形态是开关不是数据」）。
- **生成器是类分叉不是注册表**：`:1660-1668` `create_dialogue_generator()` 用 `MonologueGenerator if ... else DialogueGenerator` 选类；两个类在 `:247`（`DialogueGenerator`）与 `:1526`（`MonologueGenerator`）。`stocktalk/pipeline.py:82-84` 取 `dialogue_format(self.config)`，`:92` 建生成器。
- **形态分支散落全链路**（按文件统计 `FORMAT_MONOLOGUE|dialogue.format|script.format` 命中）：`dialogue_generator.py` 19 处（如 `:286` 单轮字数上限按形态分、`:300` 说话人集合、`:646`、`:979-990` 片尾风险提示）、`hyperframes_builder.py` 10 处、`cover.py` 2 处；另加 `arcs.py` / `cli.py` / `pipeline.py` / 配置与模板。
- **CLI 把集合写进 choices**：`stocktalk/cli.py:93` `--format choices=["debate","monologue"]`；`:183-187` 写回 `dialogue.format`；`:262-272` `_apply_duration_bounds` 对「对辩/独白两层额度」分别写。
- **配置里两套额度 + 三套角色写死**：`stocktalk/config/default.yaml:21`（`format: debate`）、`:54-68`（`dialogue.monologue` 专用额度层）、`:115-158`（`characters.bull` / `characters.bear` / `characters.narrator` 三个固定角色及音色）；`config.monologue.yaml:10-11` 整份示例配置只做一件事：把 `dialogue.format` 改成 `monologue`。`stocktalk/modules/dialogue_generator.py:86-102` `ensure_narrator_character()` 是独白专属的角色兜底，`:94` 也按 `!= FORMAT_MONOLOGUE` 提前返回。
- **骨架注册表按形态分家**：`stocktalk/modules/arcs.py:59-63`（`Arc.format` 字段）、`:272-273`（`MONOLOGUE_ARCS` 只有 1 条 `DEEP_DIVE`）、`:281-287`（`arcs_for_format` / `fallback_arc` 二元分派）、`:398-417`（选骨架时按 format 取注册表）。
- **画面/封面按形态分叉**：`stocktalk/modules/hyperframes_builder.py:43`（重复定义 `FORMAT_MONOLOGUE` 字面量）、`:345-347`（归一化）、`:448-452`（脚本自带 format 覆盖 config）、`:745-750`（`_cast()` 硬编码 `("bull","bear")` vs `(narrator,)`）、`:822`/`:1058`/`:1101-1104`/`:1123`/`:1172-1173`；`stocktalk/modules/cover.py:68`（`STYLE_MONOLOGUE`）、`:76`（`COVER_TEMPLATES` 两张模板）、`:218-234`（`style_for()` 四级优先级）；模板实物 `stocktalk/templates/cover/cover.html.j2` 与 `cover-monologue.html.j2:233`（`data-style="monologue"`）。
- **下游按角色取音色**：`stocktalk/pipeline.py:339-366` `_silent_timeline` 注释明确「说话人白名单取 `tts.speakers`，不写死 ("bull","bear")」——说明角色集合已经渗透进配音/字幕链路。

**盘点结论（供 PRD 用）**：今天要加第 3 种类型，可预见要动 `FORMATS` 常量、CLI `choices`、生成器工厂、骨架注册表、builder 的 `_cast` 与多处 `if self.format == ...`、cover 模板表与新模板、默认配置与角色段。这是「把每种类型硬编码进主流程」的具体形态；对比 §3 的 provider 注册表（新增供应商「核心链路零改动」），两者抽象程度不一致。

## 3. provider 抽象现状（统一接入 oneapi 的起点）

### 3.1 已有的注册表/工厂（这部分是资产，不是债）
- 契约与注册表：`stocktalk/modules/providers/base.py:94-95`（`LLM_CLIENTS` / `TTS_PROVIDERS` 两张 dict）、`:98-109`（`register_llm` / `register_tts` 装饰器）、`:34-46`（`LLMClient` Protocol，返回原始响应文本）、`:48-64`（`TTSJob`）、`:67-84`（`TTSProvider` Protocol，`supports_line_prosody` 能力声明）。文件头 `:3-14` 写明开闭原则落点：「新增供应商 = 写实现模块 + 登记 + 在 `__init__` import 一次，核心链路零改动」。
- 配置驱动工厂：`stocktalk/modules/providers/factory.py:22-23`（`DEFAULT_LLM_PROVIDER = "bailian-cli"`、`DEFAULT_TTS_PROVIDER = "bailian-cli"`）、`:26-37`（`create_llm_client` 按 `dialogue.provider` 查表，未知值报出全部可选值）、`:40-51`（`create_tts_provider`，`provider: auto` 按模型名含 `vd-realtime`/`tts-vd` 分流到 WebSocket，`:44-46`）。
- 注册动作：`stocktalk/modules/providers/__init__.py:22-27` import 5 个实现触发注册。已注册：LLM `bailian-cli`（`bailian_chat.py:59`）、`openai-compatible`（`openai_chat.py:69`）；TTS `bailian-cli`（`bailian_tts.py:63`）、`dashscope-ws`（`dashscope_ws_tts.py:161`）、`volcengine-doubao`（`volcengine_tts.py:238`）。
- 编排层用法：`stocktalk/modules/dialogue_generator.py:322`（`create_llm_client(config, ...)`）、`stocktalk/modules/tts_agent.py:57`（`create_tts_provider(self.tts_config)`）——**LLM 与 TTS 是两条独立工厂**。

### 3.2 OpenAI 兼容实现（与 oneapi 最接近的现有件）
`stocktalk/modules/providers/openai_chat.py`：
- `:1-6` 说明「stdlib urllib，不引入新依赖」，适用于任何 `POST {base_url}/chat/completions`、响应 `choices[0].message.content` 的端点。
- `:21-28` 构造参数 `model` / `base_url`（默认 `https://api.openai.com/v1`）/ `api_key_env`（默认 `OPENAI_API_KEY`）/ 可注入 `urlopen`（测试替身入口）。
- `:34-36` key 从环境变量读、缺失抛 `LLMProviderError`（**不落配置文件**）；`:45-51` Bearer 鉴权；`:53-66` 超时/响应信封校验。
- `:69-77` 工厂从 `dialogue.options.base_url` / `options.api_key_env` 读值；配置示例在 `stocktalk/config/default.yaml:10-13`（注释状态，未启用）。
- 测试替身已具备：`tests/test_providers.py:43-50`（从配置构造断言）、`:134`、`:154-185`（注入 `urlopen` 的桩）。

### 3.3 当前并未统一（关键差距）
- LLM 默认走 `bl text chat` 子进程（`stocktalk/config/default.yaml:5-9`，`provider: bailian-cli` / `model: qwen3.6-plus`）。
- TTS 默认直连火山引擎（`stocktalk/config/default.yaml:168-186`，`provider: volcengine-doubao`、`options.api_key_env: VOLCENGINE_TTS_API_KEY`；`stocktalk/modules/providers/volcengine_tts.py:113/125/148` 读同一环境变量），并保留 `bailian-cli` / `dashscope-ws` 作回滚底座。
- 封面 AI 底图是第三条独立 AI 路径：`stocktalk/config/default.yaml:533-558`（`bl image generate`、`DASHSCOPE_API_KEY`、`wan2.6-t2i`），实现 `stocktalk/modules/cover_art.py:230/259` 自行读环境变量，**不在 LLM 注册表内**。
- 全仓 first-party 文件 grep `oneapi|wnbgj` = **0 命中**。

**盘点结论（供 PRD 的「待验证」项）**：「大模型能力统一由 oneapi 提供」目前既未实现，也尚未界定范围——TTS 与图像生成当前都不经过 LLM provider 注册表。是否一并收敛、oneapi 是否具备这些能力，属于必须验证/决策项，不能在本阶段假定。

## 4. publisher 与 third_party 的耦合点

- 发布实现本质：`stocktalk/modules/publisher.py:1-23` 自述「thin wrapper」，shell out `sau <platform> upload-video` / `check`，平台能力来自 social-auto-upload 的浏览器自动化（`:9-10`）。
- 平台规格硬编码：`stocktalk/modules/publisher.py:269-293` `PLATFORM_SPECS` 6 个平台（douyin / bilibili / kuaishou / xiaohongshu / tencent / baijiahao），逐平台写死 title/tags/desc 上限、runtime_flags、tid、covers；`:295-302` 派生 `PLATFORM_LABELS` / `TITLE_LIMITS` / `DEFAULT_SPEC`。这些规格被多处直接引用，例如 `stocktalk/modules/review.py:21`、`stocktalk/cli.py:40/139/202`。
- 工具链耦合：`stocktalk/modules/publisher.py:333-338` `ENVIRONMENT_CHECKS` 把 `sau` 列为 `required=True`（缺失即不发布），另列 `npx`（渲染）、`chrome`（封面）、`cover_ai`。
- **对 vendored 目录的硬引用**：`stocktalk/modules/publisher.py:760` `self.sau_dir = Path(publish.get("sau_dir", "third_party/social-auto-upload"))`；`:761` `self.workdir` 直接落到该目录；`:1455-1472` `_require_sau()` 优先解析 `sau_dir/.venv/bin/sau`（要求 vendored 项目自带 venv），否则 `shutil.which("sau")`，否则报 `PublishError` 并提示 `uv sync`；`:1474-1493` `_run_subprocess` 以 `cwd=sau_dir` 启动子进程。
- **15 个 patch 的维护方式**（即「整包 vendored + 本地 patch」的成本证据）：`third_party/patches/` 下 `0001`–`0015` 共 15 个 `.patch`（无 `0009`）；`third_party/README.md:216-236` 给出按数字顺序 `git apply ../patches/*.patch` 的重放步骤，`:217-218` 声明所有补丁基于上游 SHA `0012d2c` 并用 `git apply --check` 验证，`:238-246` 说明在主仓里改 vendor 代码时路径会错、要改用 `patch -p1`，`:248-249` 要求任何升级后重新核对。
- 体积/规模实测：`du -sh third_party` = 6.1M（`social-auto-upload` 5.9M、`patches` 148K），vendored 项目 27 个顶层条目。
- 配置与文档层面的耦合：`stocktalk/config/default.yaml:270-284`（publish 段以 social-auto-upload 为前提，`:272` 登录命令写 `cd third_party/social-auto-upload && uv run sau ...`）、`stocktalk/cli.py:366-372` 提示直接打印 `third_party/social-auto-upload` 路径。

**盘点结论**：发布能力今天无法脱离 `third_party/social-auto-upload` 目录存在（默认路径、venv、cwd、登录提示、预检都指向它），且升级上游需要重放 15 个 patch。这是「不要整包 vendored 外部 SDK」诉求的直接对象；处置方案（外置/独立进程/保留但可选）属于架构阶段，本阶段只记录耦合面，不定方案。

## 5. 测试覆盖

- 框架与范围：`pyproject.toml:19-22`（dev `pytest>=8.0`）、`:33-34`（`testpaths = ["tests"]`）。
- 规模（本次实测）：`tests/` 下 **32 个** `test_*.py`，**793 个** `def test_`（含类内缩进方法），共 **12327 行**。
- 分布（行数）：`test_publisher.py` 1554、`test_cli.py` 1215、`test_hyperframes_builder.py` 943、`test_dialogue_generator.py` 810、`test_daemon.py` 805、`test_providers.py` 707、`test_episode_planner.py` 573、`test_stock_data.py` 534、`test_notify.py` 463、`test_monologue.py` 427、`test_pipeline.py` 332 等。
- 与本任务相关的现有覆盖：
  - provider 工厂/注册：`tests/test_providers.py:37`（默认 LLM 是 bailian-cli）、`:43`（openai-compatible 从配置构造）、`:53`/`:70`（未知 provider 报出可选值）、`:58-64`（TTS `auto` 分流与显式指定）。
  - 两种形态：`tests/test_monologue.py:37`（默认 debate）、`:41`（未知 format 回退 debate）、`:44`（monologue 可选）、`:66`（骨架注册表分家）、`:101`（工厂按 format 选生成器）、`:255`（builder `_cast` 独白只有 narrator）；`tests/test_default_config.py:134-140`（示例配置只切 format）。
  - 端到端编排：`tests/test_pipeline.py:93`（`test_pipeline_runs_e2e`）、`:116`（结果 JSON）、`:164`/`:184`（TTS 静音降级 / 默认硬失败）、`:198`（渲染开关）。
  - 发布记录：`tests/test_publisher.py`（1554 行）、`tests/test_publish_record_merge.py`。
- **覆盖空白**（对平台化改造直接相关）：
  - 无 Web/桌面 UI 测试——仓库根本没有 UI 层（见 §0）。
  - 无 oneapi 相关测试——仓库 0 处引用（见 §3.3）。
  - **无「新增第 3 种视频类型」测试**：`FORMATS` 只有 2 值，现有形态测试都是 debate/monologue 二元断言，没有一条能证明「加一种类型无需改核心链路」。
  - 无「UI 与核心解耦」的接口测试（核心目前只通过 `Pipeline.run` / CLI 被驱动）。

## 6. 可复现核对命令（本次实际执行，供第三方复核）

```bash
cd hyperframes-stock-debate
git rev-parse --short HEAD                      # e9cddcc
nl -ba stocktalk/modules/dialogue_generator.py | sed -n '54,63p'   # FORMATS 常量
nl -ba stocktalk/modules/providers/base.py     | sed -n '94,109p'   # 注册表
nl -ba stocktalk/modules/publisher.py          | sed -n '760p;1455,1472p'  # sau_dir / _require_sau
nl -ba pyproject.toml                          | sed -n '1,34p'      # 构建与测试配置
ls tests/test_*.py | wc -l                                            # 32
grep -rhcE "^[[:space:]]*def test_" tests/test_*.py | awk '{s+=$1} END{print s}'  # 793
grep -rniE "oneapi|wnbgj" --include="*.py" --include="*.yaml" --include="*.toml" --include="*.md" . | grep -v third_party/social-auto-upload   # 0 命中
du -sh third_party third_party/social-auto-upload third_party/patches   # 6.1M / 5.9M / 148K
ls third_party/patches/*.patch | wc -l                                  # 15
nl -ba third_party/README.md | sed -n '216,249p'                       # 补丁重放步骤
grep -rniE "flask|fastapi|django|uvicorn|electron|tauri|pywebview|tkinter|streamlit|gradio" stocktalk pyproject.toml   # 仅 dashscope_ws_tts.py 的 websockets
```
