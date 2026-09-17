# 谈股论金

全自动 AI 财报对话视频生成工具。输入股票代码，自动完成：数据获取 → 对话脚本生成 → 合规审核 → 双角色语音合成 → HyperFrames 视频工程构建 → 渲染输出 MP4。

## 功能特性

- 零操作出片：不用剪辑、不用配音、不用排版、不用写稿
- 双人设对话：股市新手（乐观冲动） vs 股市老登（理性批判）
- 内容专业：基本面财报 + 技术面 K 线综合解读
- 强合规：无投资建议，纯观点碰撞，自动过滤禁用词
- 风格统一：音色、视觉、人设全部由配置文件锁定

## 环境要求

- Python 3.12+
- tongstock-cli（A股数据源）
- bailian-cli（AI对话 + TTS语音）
- HyperFrames CLI（视频渲染）

## 安装

```bash
# 克隆仓库
git clone git@github.com:sjzsdu/hyperframes-stock-debate.git
cd hyperframes-stock-debate

# 安装 Python 依赖
pip install -e .

# 安装外部依赖
pip install tongstock-cli
npm install -g bailian-cli
npm install -g hyperframes

# 配置 bailian API Key
bl auth login --api-key sk-xxx
```

## 快速开始

```bash
# 一条命令生成视频
tangulunjin 600519 --name 贵州茅台

# 或使用模块方式运行
python -m stocktalk.pipeline 600519 --name 贵州茅台
```

## 一条命令：生成 MP4 + 全平台发布

```bash
tangulunjin 601689 --publish
```

这一条会依次完成：拉行情与个股资讯 → 生成对话脚本 → 合规审核 → TTS 配音 →
构建 HyperFrames 工程 → 渲染桌面版（横版）与手机版（竖版）两支成片 → **自动生成封面图** →
发布前预检 cookie → 逐个平台上传各自那一版（带封面），并在 `output/<代码>_<时间戳>.json` 里
留下完整结果（含每个平台的命令、封面路径与失败原因）。

> 画幅按平台偏好下发（`publish.platform_canvas`）：抖音/视频号/快手/B站拿横版
> 1920×1080，小红书拿竖版 1080×1920。横版是分成门槛（「原创横屏 ≥1 分钟」），
> 竖版只给小红书引流；正片 70–110 秒是为了完播率——分成只算有效播放。
> 画面布局是「常驻舞台 + 内容槽位」：标题、面板边框、字幕框全程不动，
> 话题/关键词/图板只在内容变化时原地溶解更新，连续讲同一话题时画面完全静止。

**前置条件（只做一次）：**

```bash
cd third_party/social-auto-upload
uv sync                                        # 初始化 sau 环境
uv run sau douyin login --account default      # 每个平台各登录一次，cookie 会保存
cd ../..
```

B站特殊：它的上传走外部 `biliup` 二进制（首次使用时自动下载），且**登录必须在交互终端里执行**。

**批量与选择性发布：**

```bash
tangulunjin --check                               # 发布预检：工具链 + 各平台登录态
tangulunjin 601689 600519 000001 --publish        # 依次生成并发布多只票
tangulunjin --watchlist watchlist.txt --publish   # 从文件读代码（每行一个，支持「601689 拓普集团」）
tangulunjin 601689 --platforms douyin,xiaohongshu # 本次只发指定平台
tangulunjin 601689 --republish                    # 补发：只重发上次失败的平台
tangulunjin 601689 --republish --platforms douyin # 强制只补抖音
tangulunjin 601689 --publish-only output/601689_20260916_110907.mp4   # 指定 MP4 重发
```

批量运行时任一环节失败**不会中断后面的票**，结束时打印汇总并以退出码 1 退出，方便接到
cron 或自动化里判断成败。`--publish-only` / `--republish` 会从 MP4 同名的 `.json` 读回
原标题、简介、分平台视频路径与已渲染的封面，不会退化成文件名。

### 发布预检（`--check`）

```bash
tangulunjin --check
```

输出两类结果，任一项失败退出码为 1：

- **环境**：sau CLI（必需）、Node/npx（渲染用）、Chrome（封面用）——后两项缺失只降级
  （渲染不了 MP4 / 没有封面），不算硬失败；
- **平台登录态**：逐个执行 `sau <平台> check`，失败的会直接给出对应登录命令。

### 补发（`--republish`）

某次发布里抖音失败了，不需要重新渲染，也不用抄 MP4 路径：

```bash
tangulunjin 601689 --republish
```

它会自动找到 `output/` 里 601689 最近一次的成片，读回上次的标题、简介和封面，
**只补发上次失败的平台**；上次全部成功时会提示无需补发。要强制重发某平台加
`--platforms douyin`。单个平台失败依旧不影响其他平台，失败退出码为 1。

### 发布中遇到风控怎么办

**抖音短信验证码**：发布瞬间可能弹风控，这时终端会**直接弹出提示**，把手机
收到的验证码粘进去回车，上传就继续往下走：

```
  ⚠️  抖音这次发布要过一道短信验证码风控
      手机应该刚收到验证码，粘到这里回车就继续（35s 内不输入算失败）
      验证码: 654321
  已写入 verify_code.txt，正在提交…
```

跑 `--publish` 时进度条会自动让位，输入过程不会被刷新打断。超时没输入则会给
出明确原因并跳过该平台（其他平台照发），之后 `--republish` 再补一次即可。
无人值守（cron）下不弹提示，保留老的救急方式：`echo -n "123456" >
third_party/social-auto-upload/verify_code.txt`；命令行可加 `--no-interactive`
强制走这条路。

**B站限流**：连着上传会被 biliup 挡回来（`upload rate limit (code: 601)
您上传视频过快`）。它不会被当成普通失败立刻重试（那样只会再失败一次），而是
按 `publish.rate_limit_wait_seconds`（默认 10 分钟）冷却后再试，等待期间进度条
会显示剩余时间。跑批时想更保守就把这个值调大。

**Ctrl-C**：随时中断，已成功的平台不会被回滚；剩下的标为「未执行」并照常
生成结果 JSON，上传用的浏览器进程也会被一起杀掉，不会留在后台偷偷发。

### 封面图

发布前会自动用 headless Chrome 截图生成封面，并按平台支持的能力下发
（`portrait` 1080×1440 给抖音/快手/小红书/视频号，`wide` 1440×810 给 B站封面，
抖音与视频号还会带一张 `landscape` 横版）。封面复用视频的配色与 A股 红涨绿跌
约定，内容为股票名/代码/最新价/涨跌幅/真实收盘曲线/看点一句话。封面失败只会
降级为「不带封面发布」，绝不影响已经渲染好的视频发布。配置见 `cover:` 段。

## CLI 命令

```
tangulunjin <股票代码...> [选项]

参数：
  股票代码              6位A股代码，如 600519、000001；可一次给多个，逐个生成并发布

选项：
  --name NAME           股票名称（可选，会自动获取；仅在单个代码时生效）
  --config PATH         自定义配置文件路径
  --output-dir DIR      输出目录（默认 output/）
  --duration-minutes N  对话目标时长（默认 2-8 分钟，按素材量自然伸缩）
  --publish             渲染后自动发布到已配置的平台（抖音/B站/快手/小红书/视频号）
  --publish-only MP4    跳过生成，直接发布已有 MP4
  --republish           补发：自动找最近一次成片，只重发上次失败的平台
  --check               发布预检：工具链 + 各平台登录态，全过才退出码 0
  --platforms LIST      本次只发布指定平台，逗号分隔
  --watchlist FILE      从文件读取股票代码（每行一个，# 之后为注释）
  --no-interactive      不询问任何问题（验证码等提示走文件方式）
  --help                显示帮助信息
```

**示例：**

```bash
# 基础用法
tangulunjin 600519

# 指定名称和配置
tangulunjin 600519 --name 贵州茅台 --config my_config.yaml

# 指定输出目录和目标时长
tangulunjin 000001 --name 平安银行 --output-dir ./videos --duration-minutes 3

# 生成并自动发布到所有已配置平台
tangulunjin 600519 --publish

# 只发布一个已有视频
tangulunjin 600519 --publish-only output/600519_20260915_120000.mp4
```

## 多平台自动发布

基于开源工具 [social-auto-upload](https://github.com/dreammis/social-auto-upload)（已内置于
`third_party/social-auto-upload`，通过浏览器自动化登录并上传）。支持：抖音、B站、快手、小红书、视频号。

**首次使用：逐平台扫码登录一次（cookie 会保存，之后无需再登录）：**

```bash
cd third_party/social-auto-upload
uv sync                                       # 初始化 sau 环境（首次）
uv run sau douyin login --account default     # 每个平台登录一次
cd ../..
```

登录时会自动弹出浏览器窗口（login 永远有头，即使配置里 headless: true），
在**弹出的浏览器里**扫码并在手机上点「确认登录」即可，等待窗口为 5 分钟。
注意：不要只扫保存的二维码 PNG 截图——抖音截图二维码常因风控校验不生效；
若手机点了确认但终端仍报超时，直接重跑一次登录命令。

**配置**（`stocktalk/config/default.yaml`）：

```yaml
publish:
  platforms: [douyin, bilibili, kuaishou, xiaohongshu, tencent]
  accounts: {douyin: default, bilibili: default, ...}  # 每平台的账号名
  headless: true
  preflight: true            # 上传前用 sau <p> check 验 cookie，失效平台直接跳过
  retries: 1                 # 上传失败的平台在同一轮内自动重试
  retry_delay_seconds: 30    # 普通失败（浏览器崩了、网络抖了）的退避
  verify_code_wait_seconds: 150  # 抖音短信验证码等待窗口，超出即中止该平台
  interactive_verify_code: true  # 交互终端下直接弹提示让你输入验证码
  rate_limit_wait_seconds: 600   # B站限流（601 上传过快）的冷却时长
  platform_canvas:           # 每个平台发哪一版画幅；出现的画幅种类 = 要渲几版
    douyin: horizontal       #   桌面版（横版）——播放分成只认原创横屏 ≥1 分钟
    bilibili: horizontal
    kuaishou: horizontal
    xiaohongshu: vertical    #   手机版（竖版）——小红书无分成，竖版完播更好
    tencent: horizontal
  ai_content_label:          # AI 生成声明的选项文案（不标注会被限流/取消变现资格）
    kuaishou: 内容由AI生成
    xiaohongshu: AI生成
  schedule: ""               # 留空立即发布；如 "2026-09-16 19:30" 定时发布
  extra_tags: []             # 追加自定义话题标签
```

发布内容（标题/简介/话题标签）从审核后的对话脚本自动生成，标题按平台字数限制截断，
简介自动附上「本内容由AI生成，仅供学习交流，不构成投资建议；投资有风险，入市需谨慎」，
B站自动选择财经分区。五平台都会标注 AI 生成：抖音/视频号由上传器勾选，快手/小红书
按上面的 `ai_content_label` 传选项文案，B站的 `biliup` 命令没有声明字段，靠简介里那一句。

**无人值守运行时的三个已知坑：**

1. **抖音短信验证码**：发布瞬间可能触发风控弹窗。交互终端下会直接提示你把验证码
   粘进来（见「发布中遇到风控怎么办」）；无人值守时则在 `verify_code_wait_seconds`
   的窗口内等 `third_party/social-auto-upload/verify_code.txt`，超时即中止该平台
   并给出提示（不再白等到 15 分钟上传超时）。想抢救就在窗口期内写文件：
   `echo -n "123456" > third_party/social-auto-upload/verify_code.txt`（sau 读到后自动提交并删除）。
2. **cookie 失效**：预检会拦下失效平台并在报告里提示 `sau <platform> login`，不会浪费一次渲染。
3. **B站首次上传**：会从 GitHub 下载 `biliup` 二进制，网络不通时 B站失败但**不影响其他平台**。

## 配置

默认配置位于 `stocktalk/config/default.yaml`，可自定义：

```yaml
dialogue:
  model: qwen3.6-plus      # AI 模型
  min_duration_seconds: 180 # 最短目标时长
  max_duration_seconds: 480 # 最长目标时长
  max_line_chars: 130       # 每句最大字数（30-150）
  temperature: 0.8          # 创造性

characters:
  bull:
    name: 股市新手
    persona: 年轻乐观的女生，喜欢从生意角度理解公司
    voice_id: longxiaochun_v3  # 知性积极女
    speed: 1.07
    voice_direction: 年轻、好奇、自然聊天的女生语气
  bear:
    name: 股市老登
    persona: 老练理性，擅长从风险角度分析
    voice_id: longtian_v3      # 磁性理智男
    speed: 0.94
    voice_direction: 成熟、低沉、克制的男性语气

compliance:
  forbidden:
    - 保证
    - 必涨
    - 必跌

output:
  dir: output
```

## 项目结构

```
.
├── stocktalk/
│   ├── __init__.py
│   ├── __main__.py           # python -m stocktalk 入口
│   ├── cli.py                # CLI 命令行入口
│   ├── pipeline.py           # 流水线主逻辑
│   ├── config/
│   │   └── default.yaml      # 默认配置
│   └── modules/
│       ├── stock_data.py     # 股票数据获取
│       ├── dialogue_generator.py  # AI 对话生成
│       ├── compliance.py     # 合规审核
│       ├── tts_agent.py      # TTS 语音合成
│       └── hyperframes_builder.py  # 视频工程构建
├── tests/
├── pyproject.toml
└── README.md
```

## 输出文件

运行后在 `output/` 目录生成：

```
output/
├── 600519_20260909_143000.json      # 完整结果数据
├── 600519_20260909_143000.srt       # 字幕文件
├── 600519_20260909_143000.mp4       # 渲染视频
└── 600519_20260909_143000/          # HyperFrames 工程
    └── index.html
```

## 模块独立使用

```python
# 单独获取股票数据（含个股新闻/研报资讯，来自 tongstock news query）
from stocktalk.modules.stock_data import StockDataClient
data = StockDataClient().get_stock_data('600519')
print(data['news']['items'][:3])  # 真实资讯标题，作为对话创作素材与“近期资讯”画面

# 单独生成对话
from stocktalk.modules.dialogue_generator import DialogueGenerator
script = DialogueGenerator().generate(data)

# 单独运行合规审核
from stocktalk.modules.compliance import ComplianceAgent
approved = ComplianceAgent({}).review(script)

# 单独发布视频
from stocktalk.modules.publisher import SauPublisher
report = SauPublisher({}).publish('output/xxx.mp4', approved, '贵州茅台', '600519')
print(report['succeeded'], report['failed'])
```

## 常见问题

**tongstock 查询超时**
```bash
tongstock sync  # 同步本地数据
```

**bl 命令未找到**
```bash
npm install -g bailian-cli
bl auth login --api-key sk-xxx
```

## License

MIT
