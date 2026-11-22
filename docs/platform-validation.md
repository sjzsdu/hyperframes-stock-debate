# 股票视频平台首版验证记录

WAN-208，实现以 `03353d5` 的架构与五篇 ADR 为依据。此文记录本机实施验证；产品验收和独立部署复核仍由审查阶段完成。

## 范围与复跑

Web 发起生成、预检与发布，本机串行服务执行；桌面只读同一任务与结果。对话统一 oneapi，TTS/封面保持原路径。SAU 外置，主仓不再分发其整树。

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[web,dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy
npm ci --ignore-scripts
npm run typecheck
npm run build
.venv/bin/python scripts/verify_platform.py --evidence-dir evidence/offline
.venv/bin/python scripts/verify_errors.py --evidence-dir evidence/errors
.venv/bin/python scripts/verify_types.py --types-dir examples/video_types
```

运行环境：macOS、Python 3.12.12、ffmpeg/ffprobe、Node 22、TypeScript 5.9.3。核心类型检查覆盖新应用/类型/网关边界；旧 Python 核心没有全仓类型化。原仓库未配置 lint/typecheck 或 CI，本次增加 mypy 与 TypeScript strict；未新增 lint 工具或 CI。现有 854 条基线测试未修改；任务描述中的 793 是更早统计。原始测试摘要见 `evidence/pytest-{local,clean}.txt`。

独立检出、独立虚拟环境安装并测试业务提交 `035ac68`。此后类型桩/缓存忽略及证据/README 提交不改变业务代码。独立环境发现缺少 waitress 类型桩，已将 types-waitress 加入 dev 并复跑类型检查。Node 静态 JS 随仓库提交，服务运行不需要 npm；开发者修改 TS 后必须重新构建。生产用 `config.platform.yaml` 与环境注入的 `ONEAPI_API_KEY`。配置明文凭据在建立数据库前拒绝。

## AC 与证据

| AC | 可复核证据 | 验证边界 |
|---|---|---|
| 01 新类型 | `tests/test_video_types.py`；`evidence/type-extension-{tracked,untracked}.txt` 和目录 JSON | 干净检出只增加 `video_types/market-brief/type.yaml` 与 `sample.json`；核心文件 diff 为空。`host` 在 prompt、deep_dive 骨架、脚本、音色请求、字幕、工程中贯通；陌生双角色也有映射测试。声明停用/重复/错误/缺行情/无音色/逃逸模板有测试 |
| 02 Web 路径 | `tests/test_platform.py`、`scripts/verify_platform.py`、`evidence/offline/*.json` | HTTP 入队→生成→真实 MP4 下载→审阅→预检→沙箱发布。固定 1 秒媒体含视频+音轨，只证明应用链路；不冒充真实股票生成/真实发布 |
| 03 桌面 | `evidence/desktop-smoke.txt`、`07-desktop-page.png` | macOS pywebview 窗口真实加载同一任务 URL，自动关闭退出 0，之后服务仍健康。PNG 为浏览器中的共享桌面页面；原生窗口截图工具无法绑定未打包的 Python 进程，未留原生截图 |
| 04 oneapi | `tests/test_oneapi.py`、默认配置、provider 注册 | 固定 HTTPS host、环境变量和协议候选路径。生产工厂查模型目录，清单外模型零 POST；认证、异常信封和跨域跳转均有替身测试。实网成功对话未验证 |
| 05 依赖精简 | `tests/test_external_publisher.py`、`third_party/README.md`、`runtime/sau-requirements.txt` | 主仓 third_party 6.1M→156K，15 个历史 patch 保留、无新增。六平台旧/新 argv、cwd、账户、封面、画幅传参、排期与 AI 声明有替身对照；真实账户未验证 |
| 06 管理 | `tests/test_platform.py`、Web 截图 | 类型/股票/日期/状态筛选、分页、详情、产物 ID 下载、明确失败定向补发；两个渲染输出隔离、并发幂等与快照冻结有测试 |
| 07 错误 | `tests/test_oneapi.py`、`tests/test_platform.py`、`evidence/errors/*.json`、错误/无权限截图 | POST 后读取超时只一次提交，无外层重放；成功响应脚本无效也不重播。发布未知结果不重发；补发仅同账号同平台明确失败/需本机操作。平台任务关闭 Publisher 的外层重试和有头重放 |
| 08 配置 | 缺 key 零网络测试、明文凭据拒绝测试、错误 DTO | 无隐式供应商/模型回退；媒体路径是已确认的保留范围 |
| 09 双端一致 | `01-default.png`、`07-desktop-page.png`、同一任务 `b3f670c0c97c408893377edf2ed46983` | 桌面没有生成/预检/发布表单，界面明确标注差异；两端访问同一服务 |
| 10 约束 | 全量回归、类型边界检查、vendor 统计、秘密扫描 | 原测试未改；核心可在不导入 Flask/pywebview 的情况下单独运行。旧 provider 仅保留内部兼容测试/旧版回退；升级后的 CLI/Web 执行入口校验 oneapi |
| 11 模型与错误 | 模型目录替身、五份 HTTP 错误 JSON；匿名 `/v1/models`=401 | API 把未知能力标为 unknown，不虚构支持矩阵。授权模型清单、Bearer 认证确认、真实成功响应、额度码/费用/时限需维护方补证 |
| 12 第三方复跑 | 上述命令、README、证据目录 | 同机独立检出已复跑；独立第三方审查仍待进行，未声明产品全量验收通过 |

## UI 六态与 diff

`evidence/01-default.png`、`02-empty.png`、`03-loading.png`、`04-error.png`、`05-forbidden.png`、`06-long-narrow.png` 覆盖正常、筛选后空数据、模型加载、过期模型被服务拒绝、CSRF 真实 403 和长文本窄屏。加载态使用延迟 fetch 夹具；长文本使用 DOM 压力夹具。其他状态走真实本机 API。390px 下 `scrollWidth=390`，无横向溢出。

此为新界面，没有已获人工认可的旧视觉基线。屏蔽时间/ID/视频动态区域后，同一构建两次捕获 `08/09` 的结果见 `visual-diff.json` 与 `10-visual-diff.png`：0 / 2,712,960 像素变化。它证明截图重复性，不是跨版本视觉回归通过。不能把这组图当作已批准基线；后续门禁基线需在 CI 相同 OS/浏览器上人工确认。

## 服务、恢复与回退

实际启动：`.venv/bin/python -m stocktalk.platform.server --demo --data-dir .platform/demo --port 18765`。健康响应保存于 `evidence/health.json`：HTTP 200，`{"api_version":1,"status":"ok","version":"0.1.0"}`。测试服务仅用于本轮验证，交付时停止，复跑时自行启动。

SQLite WAL、幂等键和短事务保护任务；版本/类型/模板/模型配置入队冻结。产物先生成于 staging，校验音视频和时长后原子移动，再建索引。离线媒体明确标为夹具，时长不用于真实类型预算验收。真实任务超出声明时长会失败，不能标成功。

服务持有数据根独占锁，正常关闭停止领取并等当前任务结束。强杀可能留下媒体/发布子进程，重启会将 running 标 interrupted、submitting 标 outcome_unknown，写 RECOVERY_REQUIRED 并暂停执行；操作者确认旧子进程全部退出后，才使用 `--ack-recovery`。孤儿 staging/未登记媒体保留隔离，不对用户显示成功。此版不承诺强杀后自动清理整个进程树。

SAU 回退已实际验证：从固定基线重新导出到第二个独立目录，执行 `uv sync --locked`、安装补充 `playwright==1.58.0`，`sau --help` 退出 0。178 个跟踪文件与基线逐文件摘要一致。可复现归档固定 mtime=1795321023，SHA256 为 `251ac4f445ad9a5d627fda2fe03c3a44cd518544d508861ace39ee6a26692811`；初次未固定 mtime 的归档摘要不同，不用于重复校验。保留原锁与 MIT 来源许可，不重放 patch。

应用 schema 回退尚未执行，属于部署复核阶段：先停写、完整备份数据根；恢复旧 schema 备份到独立目录，保留新增媒体、sidecar 和报告后用匹配版本启动。不要在原数据上强行运行不兼容 schema。未知远端发布结果必须人工核对后再决定补发。

## 未覆盖与待补证据

- 未提供 ONEAPI_API_KEY；实际匿名模型目录为 401，授权模型清单、认证方式和成功对话均 unverified。所有成功对话/错误测试都明确使用替身。
- 没有实际调用行情/TTS/AI 封面/HyperFrames 的完整付费或长片链路；核心旧用例和新类型媒体接缝有自动化保护，离线应用流程使用 FFmpeg 固定媒体。
- 六平台真实账号、扫码/SMS/滑块、发布站点现行行为未验证。服务只给本机操作提示，不代替运营者完成这些步骤。
- macOS 桌面启动已测；原生窗口截图、Windows/Linux、安装包、签名/自动更新未验证。首版服务使用 POSIX 进程锁。
- 完整数据备份恢复、磁盘满、强杀孤儿进程回收及跨主机存储未做故障演练。恢复采取暂停和人工确认，不自动重复提交。
- oneapi 原始协议和错误码目前依赖兼容协议假设；未知上游消息不回显。真实费用与网关能力待维护方确认。

预验证通过 ≠ PR 已合并 ≠ 产品已验收。
