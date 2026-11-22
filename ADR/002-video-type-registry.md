# ADR-002 · 用目录声明注册视频类型

状态：提案，V4 真实用例待对照。

## 背景

现有类型选择散在 CLI、生成器、骨架、角色和封面（`stocktalk/cli.py:93`、`stocktalk/modules/dialogue_generator.py:54-63`、`stocktalk/modules/dialogue_generator.py:1660-1668`、`stocktalk/modules/arcs.py:281-287`、`stocktalk/modules/hyperframes_builder.py:745-750`、`stocktalk/modules/cover.py:218-234`、`stocktalk/config/default.yaml:54-68`）。现有 provider 注册表可作为解析与报错范式（`stocktalk/modules/providers/base.py:94-109`）。

## 备选方案与理由

| 方案 | 取舍 |
| --- | --- |
| Python 注册装饰器或 entry points | 行为自由、扩展能力强；每种类型引入可执行代码与分发工作 |
| 目录约定推断类型与资源 | 安装简单；缺少显式的数据能力与规则契约，难以诊断和复跑 |
| 目录发现 + 版本化 YAML 声明 + 本地资产 | 可校验、可离线安装，适用于 PRD 示例类型；数据能力限定到已登记的适配器 |

## 决策

采用第三项。内置声明和运营者配置的 `video_types/` 两个根共享加载接口。外部根由 `video_types_dir` 指向，在验证时设为验收 checkout 根下的 `video_types/`，由既有运行时配置或验证命令参数注入。每个直接子目录恰有一个 `type.yaml`，目录名等于 `id`。重启服务加载，CLI 启动时加载。使用安全 YAML 解析，逐字段报错，稳定排序；重复 ID 拒绝，启用状态与校验诊断可查。无效声明阻止该类型注册，独立已知类型仍可用。显式传入未知或停用类型时失败，旧配置缺省仍解析为辩论以保留兼容。类型版本与引用资产摘要由服务计算，入队时冻结。

| PRD 字段 | 版本 1 契约与使用者 |
| --- | --- |
| `id`, `display_name`, `summary`, `status` | 小写字母开头的 slug、非空名称和简介、`enabled/disabled` 枚举。API 与 CLI 共享目录；历史列表允许过滤停用类型 |
| `cast` | 有序唯一的 `speaker_id`、`name`、`voice_ref` 条目。声明 ID 可自定义。`voice_ref` 引用 `characters.<key>` 的音色与韵律配置，例如 `narrator`。提示词、脚本、配音、字幕和版式使用同一声明 ID，builder 按声明顺序渲染 |
| `duration` | 正整数秒 `min_seconds/max_seconds` 且下限不大于上限。生成预算与成片验收使用该区间 |
| `required_data` | 受支持的数据源 ID 列表，版本 1 首先登记 `quote`。未知 ID 在加载时报错，所需数据缺失在生成前中止 |
| `visual` | `scene`、`cover` 取内置版式键或同目录相对资产引用。禁止绝对路径与逃逸链接，声明的模板只在受限渲染上下文取数据 |
| `sample` | 可解析的相对脚本 JSON 或样例 MP4。示例使用脚本以匹配说话人与校验数据；MP4 检查媒体类型和可解码性 |
| `compliance_defaults` | AI 声明、片尾风险提示开关、标题与标签模板规则。最终发布门禁和平台限制由既有核心校验 |
| 执行元数据 | `schema_version: 1` 与可选的 `script` 声明，可声明 `strategy`、`arc` 及行内 `prompt`。`monologue` 策略接受恰好一个角色，`deep_dive` 沿用既有独白骨架 ID（`stocktalk/modules/arcs.py:242`）。策略、配套版式及数据源的签名须在加载时校验 |

S1 一次性迁移所有现有类型分支，内置声明复用既有辩论、独白策略与模板。对引用 `dialogue.format`、`--format` 和历史 sidecar 的 CLI 保留行为，已有类型的时长、合规、配音、画幅与封面以 793 条基线和代表性 fixture 保护。新 API 仅接受一个类型 ID，同名冲突显式报错。初始版本只允许受控模板语法和有限本地资产，禁止插件任意 Python 导入、网络请求或读取配置。

### S1 的角色绑定

目前独白白名单和归一化绑定 `narrator`，builder 直接分叉阵容（`stocktalk/modules/dialogue_generator.py:293-306`、`stocktalk/modules/dialogue_generator.py:979-994`、`stocktalk/modules/hyperframes_builder.py:745-750`）。S1 将策略与骨架的语义槽位在加载时绑定到声明的角色。例如 `monologue` 的唯一槽位绑定 `cast[0].speaker_id`，默认内置独白仍绑定 `narrator`，行情速览绑定 `host`。同一绑定供提示词、骨架示例、开场门禁、脚本归一化和校验、片尾风险提示及渲染模板使用。骨架/模板中的角色槽位在策略加载时解析。内置独白保留现有把任意原始标签归一到 `narrator` 并记录提示的行为。新声明仅接受其角色和该策略定义的内部槽位别名，统一映射后校验角色集合，未识别或缺失角色给出可读错误。

执行器将声明角色 ID 映射到对应的名称和已解析音色，TTS 用 `host → characters.narrator` 查音色，但 `speaker` 与字幕/时间线仍保留 `host`。单人版式通过角色槽位读 `host` 的元数据。新增声明不需要改动内置角色配置。自动化以陌生 ID `host` 检查提示词、骨架、模型输出、配音请求、字幕索引、成片工程的角色和风险提示，覆盖每处从 `narrator` 到声明 ID 的接缝。

### 第三种类型的文件清单

D-0 验收从已经合入 S1 的干净检出开始。示例装入运营者的外部根，新增 `video_types/market-brief/type.yaml` 与 `video_types/market-brief/sample.json` 两个文件。配置根由验收命令参数或已存在的运行时配置提供。工作树留存 `git diff --name-only HEAD` 和 `git ls-files --others --exclude-standard`，两条合起来应只列两个新增文件。需要自定义资产时，可在同目录附带 `assets/` 中声明引用的文件。

以下为 S1 验收使用的完整声明草稿。版式键 `monologue` 由 S1 从现有独白画面与封面登记。校验时须核对标签数组、模板可用变量、角色与音色、样例和所需数据。

```yaml
schema_version: 1
id: market-brief
display_name: 行情速览
summary: 用固定行情数据讲清当期走势、观察点和风险。
status: enabled
cast:
  - speaker_id: host
    name: 行情主持人
    voice_ref: narrator
duration:
  min_seconds: 30
  max_seconds: 60
required_data: [quote]
visual:
  scene: monologue
  cover: monologue
sample: sample.json
compliance_defaults:
  ai_declaration: true
  closing_risk_notice: true
  title_template: "{{ stock_name }}行情速览"
  tags: [行情速览, 股票]
script:
  strategy: monologue
  arc: deep_dive
  prompt: 先讲观察到的行情，再交代关键观察点，最后给出风险提示。只用输入中的事实。
```

`sample.json` 的最小角色断言输入为 `{"turns":[{"speaker":"host","line":"这次先看行情，再说风险。"}]}`。S1 校验器用样例脚本验证结构和角色，D-5 用完整固定生成夹具验证实际音频和字幕。D-0 到 D-8 覆盖两端列表、成片、发布报告和停用隔离。停用只改该声明的 `status` 并重启，复跑一种既有类型。若真实第三类依赖全新财报数据源，先独立交付通用数据适配器，再按 D-0 检验这类声明的接入。示例的验证范围是现有行情与声明策略。

## 成本、风险与回退

估算 3–5 人日，固定月费增量 0，无新进程。主要成本是梳理两种存量类型的差异及配音等回归。模板受限可降低任意执行风险，超出版本 1 契约的新需求由 V4 走查后再扩展。单个类型回退只需停用并重启，进行中的任务用原快照。核心迁移回退恢复此前 CLI 套件，导出的成片和历史 sidecar 仍可读取。省略注册表会保留新增类型的多处核心修改。
