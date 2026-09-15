# StockTalk · 股会说

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
stocktalk 600519 --name 贵州茅台

# 或使用模块方式运行
python -m stocktalk.pipeline 600519 --name 贵州茅台
```

## CLI 命令

```
stocktalk <股票代码> [选项]

参数：
  股票代码              6位A股代码，如 600519、000001

选项：
  --name NAME           股票名称（可选，会自动获取）
  --config PATH         自定义配置文件路径
  --output-dir DIR      输出目录（默认 output/）
  --duration-minutes N  对话目标时长（默认 2-8 分钟，按素材量自然伸缩）
  --publish             渲染后自动发布到已配置的平台（抖音/B站/快手/小红书/视频号）
  --publish-only MP4    跳过生成，直接发布已有 MP4
  --help                显示帮助信息
```

**示例：**

```bash
# 基础用法
stocktalk 600519

# 指定名称和配置
stocktalk 600519 --name 贵州茅台 --config my_config.yaml

# 指定输出目录和目标时长
stocktalk 000001 --name 平安银行 --output-dir ./videos --duration-minutes 3

# 生成并自动发布到所有已配置平台
stocktalk 600519 --publish

# 只发布一个已有视频
stocktalk 600519 --publish-only output/600519_20260915_120000.mp4
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

**配置**（`stocktalk/config/default.yaml`）：

```yaml
publish:
  platforms: [douyin, bilibili, kuaishou, xiaohongshu, tencent]
  accounts: {douyin: default, bilibili: default, ...}  # 每平台的账号名
  headless: true
  schedule: ""               # 留空立即发布；如 "2026-09-16 19:30" 定时发布
  extra_tags: []             # 追加自定义话题标签
```

发布内容（标题/简介/话题标签）从审核后的对话脚本自动生成，标题按平台字数限制截断，
简介自动附上“不构成投资建议”免责声明，B站自动选择财经分区。

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
