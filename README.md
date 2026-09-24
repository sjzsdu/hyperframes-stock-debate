# 谈股论金

全自动 AI 财报对话视频生成工具。输入股票代码，自动完成：数据获取 → 对话脚本生成 → 合规审核 → 双角色语音合成 → HyperFrames 视频工程构建 → 渲染输出 MP4。

## 功能特性

- 零操作出片：不用剪辑、不用配音、不用排版、不用写稿
- 双人设对话：股市新手（乐观冲动） vs 股市老登（理性批判）
- 内容专业：基本面财报 + 技术面 K 线综合解读
- 结构不重样：剧本骨架按个股实际拿得到的数据自动挑（裂痕式 / 客户视角 / 年报逐条 / 同行对比 / 六层主线），有数据才讲、没数据不讲（见[剧本骨架](#剧本骨架arc让每期的结构不一样)）
- 片尾有互动：每期给出一个可核对的下期变量、一个生意层面的二选一问题，多空分歧打在片尾图板上（见[片尾互动](#片尾互动内容层--画面层)）
- 强合规：无投资建议，纯观点碰撞，自动过滤禁用词；标题与上屏文案同样过审，逐字改写留痕
- 全平台自动发布：抖音 / B站 / 快手 / 视频号 / 百家号，各自下发对应画幅与封面，失败只影响该平台
- 无人值守：常驻守护进程在发布窗口内**随机挑时刻**自动选题出片发布，扛得住睡眠与关机（见[无人值守](#无人值守守护进程--daemon)）
- 风格统一：音色、视觉、人设全部由配置文件锁定

## 环境要求

**必需**（缺任一项都会在开跑时就报错停下）：

| 依赖 | 用途 | 自检 |
|---|---|---|
| Python 3.12+ | 本体 | `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | 依赖管理（仓库带 `uv.lock`） | `uv --version` |
| `tongstock` | A股行情 / 财务 / F10 / 资讯，**唯一数据真源** | `tongstock --help` |
| `bl`（bailian-cli） | AI 对话生成 + TTS 配音 | `bl --version` |
| Node.js（`npx`） | HyperFrames 渲染 MP4 | `node --version` |

**可选**（缺了会降级，不会中断出片）：

| 依赖 | 缺失后果 |
|---|---|
| `ffmpeg` / `ffprobe` | 不做句首句尾静音裁剪与淡入淡出，相邻台词衔接略生硬 |
| Chrome | 不生成封面图，降级为「不带封面发布」 |
| `sau`（已内置在 `third_party/`） | 不能发布，但出片不受影响 |

一条命令全查：`tangulunjin --check`，输出里会分别标注必需项与降级项。

## 安装

```bash
# 1. 克隆
git clone git@github.com:sjzsdu/hyperframes-stock-debate.git
cd hyperframes-stock-debate

# 2. Python 依赖（版本已由 uv.lock 锁定）
uv sync

# 3. 外部依赖
#    tongstock：A股数据源，不在 PyPI 上，拿到二进制后放进 PATH 即可
bl auth login --api-key sk-xxx      # bl 来自 bailian-cli：npm install -g bailian-cli
#    Node.js ≥18 就够；hyperframes 由 npx 按需获取，首次渲染会先下载

# 4. 自检
uv run tangulunjin --check
```

> 本文档其余示例统一简写为 `tangulunjin`。如果没有把脚本装进 PATH，前面加 `uv run` 即可（`uv run tangulunjin ...`）。

## 新手起步：先出片，再发布

**别一上来就 `--publish`。** 先确认「数据 → 脚本 → 配音 → 渲染」这条链路在你的机器上是通的，再碰发布——发布是唯一对外产生后果、也最容易撞平台风控的一步。四步走：

### 第 1 步：跑通链路，先不渲染

```bash
tangulunjin 601689 --no-render
```

只生成数据、对话脚本、配音和 HyperFrames 工程（HTML），跳过最慢的 MP4 渲染。跑完 `output/` 下会有：

```
601689_20260924_143000.json          # 完整结果：脚本、合规结论、数据来源
601689_20260924_143000/index.html    # HyperFrames 工程，浏览器打开即可预览成片效果
601689_20260924_143000/audio/        # 逐句配音
```

用浏览器打开那个 `index.html` 核对台词、标题和上屏文案。**这一步用最快的速度暴露环境问题**（数据源没配好、`bl` 没登录、缺依赖），比渲染十几分钟后才失败划算得多。

> 封面、审查页（`.review.html`）和 MP4 都在渲染那一步一起产出，`--no-render` 不会生成它们。

### 第 2 步：渲染成片

```bash
tangulunjin 601689
```

去掉 `--no-render` 就会渲染，产出**两支**不同画幅的成片，外加封面和审查页：

- `601689_....mp4` —— 横版 1920×1080，给抖音 / 视频号 / B站 / 百家号
- `601689_....vertical.mp4` —— 竖版 1080×1920，给快手
- `601689_....review.html` —— **拿这个当验收页**：文案 + 合规自检 + 两支成片 + 封面同屏看全
- `601689_....cover-*.png` —— 各平台封面

哪几支要渲由 `publish.platform_canvas` 决定（出现的画幅种类 = 要渲几版）。

渲染是最慢的一步（首次还要下载 hyperframes），`video.render_timeout_seconds` 默认给到 3600 秒。

### 第 3 步：发布（登录只做一次）

```bash
tangulunjin --check             # 看各平台登录态，失败的会直接给出该执行的登录命令
tangulunjin 601689 --publish
```

首次发布前需要逐平台扫码登录一次，见[多平台自动发布](#多平台自动发布)。

### 第 4 步：交给守护进程

跑通一次之后就不必再手敲了：

```bash
tangulunjin --daemon-status     # 看今天/明天的计划时刻
tangulunjin --daemon-install    # 装成 launchd 服务：开机自启、崩溃自动拉起
tangulunjin --notify-test       # 确认「跑完的结果」真的能推到你手机上
```

之后每天在配置的发布窗口内**随机挑时刻**自动选题、生成、发布，**每次跑完都会推一条结果
给你**（成功、部分成功、失败、错过窗口都会推）。详见[无人值守](#无人值守守护进程--daemon)
与[结果反馈](#跑完给你回话结果反馈notify)。

> `--daemon-install` 要在 macOS 自带的「终端.app」里跑。编辑器内置终端 / 沙箱里
> `launchctl` 会被拒绝，装完服务其实没注册 —— 所以这条命令装完会自己查一遍，
> 没查到时直接告诉你该在终端里补哪一行。

## 典型命令速查

| 我想… | 命令 |
|---|---|
| 先出片，不发布 | `tangulunjin 601689` |
| 最快确认环境通不通 | `tangulunjin 601689 --no-render` |
| 出片并发布到所有平台 | `tangulunjin 601689 --publish` |
| 让程序自己挑票（热门榜） | `tangulunjin --auto-pick --publish` |
| 批量跑一批票 | `tangulunjin --watchlist watchlist.txt --publish` |
| 多只票一次给 | `tangulunjin 601689 600519 000001 --publish` |
| 只发某几个平台 | `tangulunjin 601689 --platforms douyin,tencent` |
| 只补发上次失败的平台 | `tangulunjin 601689 --republish` |
| 用已有 MP4 重发（不重新渲染） | `tangulunjin 601689 --publish-only output/xxx.mp4` |
| 排到平台定时发表 | `tangulunjin 601689 --publish --publish-at 19:30` |
| 以后都不用管 | `tangulunjin --daemon-install` |
| 看今天的排期与上一次的结果 | `tangulunjin --daemon-status` |
| 确认反馈真的收得到 | `tangulunjin --notify-test` |
| 查某平台为什么发不出去 | `tangulunjin --check` |
| 无人值守时别因一个平台掉线整条不发 | `--continue-anyway`（配合上面任意发布命令） |

## 一条命令：生成 MP4 + 全平台发布

```bash
tangulunjin 601689 --publish
```

这一条会依次完成：拉行情与个股资讯 → 生成对话脚本 → 合规审核 → TTS 配音 →
构建 HyperFrames 工程 → 渲染桌面版（横版）与手机版（竖版）两支成片 → **自动生成封面图** →
发布前预检 cookie → 逐个平台上传各自那一版（带封面），并在 `output/<代码>_<时间戳>.json` 里
留下完整结果（含每个平台的命令、封面路径与失败原因）。

> 画幅按平台偏好下发（`publish.platform_canvas`）：抖音 / 视频号 / B站 / 百家号拿横版
> 1920×1080，快手拿竖版 1080×1920。横版是分成门槛（「原创横屏 ≥1 分钟」），
> 竖版走快手的信息流形态；正片 70–110 秒是为了完播率——分成只算有效播放。
> 画面布局是「常驻舞台 + 内容槽位」：标题、面板边框、字幕框全程不动，
> 话题/关键词/图板只在内容变化时原地溶解更新，连续讲同一话题时画面完全静止。

**前置条件（只做一次）：**

```bash
cd third_party/social-auto-upload
uv sync                                        # 初始化 sau 环境
uv run sau douyin login --account default      # 每个平台各登录一次，cookie 会保存
cd ../..
```

完整平台清单与登录命令见[多平台自动发布](#多平台自动发布)；不确定谁掉线就直接跑 `tangulunjin --check`。

B站特殊：它的上传走外部 `biliup` 二进制（首次使用时自动下载），且**登录必须在交互终端里执行**。

**批量与选择性发布：**

```bash
tangulunjin --check                               # 发布预检：工具链 + 各平台登录态
tangulunjin 601689 600519 000001 --publish        # 依次生成并发布多只票
tangulunjin --watchlist watchlist.txt --publish   # 从文件读代码（每行一个，支持「601689 拓普集团」）
tangulunjin 601689 --platforms douyin,tencent     # 本次只发指定平台
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

### 无人值守：守护进程（`--daemon`）

每天两条（早班/晚班）交给一个常驻进程，**不用 cron**：

```bash
tangulunjin --daemon-status     # 看今天几点发、昨天发成没发、下次睁眼是几时
tangulunjin --daemon-install    # 装成 launchd 服务：开机自启、退出自动拉起
tangulunjin --daemon            # 前台跑（调试时直接看日志）
tangulunjin --daemon-uninstall  # 卸掉
```

它跑的就是 `tangulunjin --auto-pick --publish --no-interactive --continue-anyway`，
所以「守护进程发了什么」和你手敲的那条命令永远是同一件事，行为不会分叉。

**它闲着的时候几乎不存在：**

- **不轮询**：算出下一个发布时刻，一觉睡到那儿——可能是九个小时后。不是分钟一次地问
  「到点了吗」。
- **不写盘**：状态只在真的变了才落盘，闲置一整夜磁盘一动不动（`--daemon-status` 这类
  查看也不会触发写）。
- **不常驻渲染**：发布是拉起子进程干的，跑完就退，十几分钟的重活不会一直挂在后台。

**为什么不直接挂 cron：**

- **发布时刻**：cron 每天精确同一秒，规律一眼可见；守护进程在窗口内随机取时刻，
  今天的 08:37、明天的 09:17、后天的 08:46。
- **崩了以后**：cron 静默死掉；launchd 的 `KeepAlive` 会拉起来，日志里每几小时还有
  一行心跳证明它在看表。

**发布窗口**在 `daemon.windows` 里配，写的是「什么时候发都合理」的范围，不是精确时刻：

```yaml
daemon:
  windows: ["07:30-09:30", "18:30-21:00"]   # 每天两条，各自在窗口内随机挑
  max_sleep_seconds: 900                    # 待机时最长睡 15 分钟（见下）
  max_attempts: 3                           # 失败退避重试，到上限才认输
```

**窗口是硬边界：过了就不发**。睡眠、关机导致错过窗口，那一档直接记为「漏了」，不做
事后补发——早班拖到下午才补出去，那个时间点反而更假，还会搅乱当天的节奏。可靠性靠
的是「窗口有两个小时宽，机器只要在其中任意一刻醒着就行」，不是事后追认。

（`max_sleep_seconds` 为什么不是「一觉睡到下一个时刻」：macOS 的单调时钟在系统睡眠期间
**不走字**，一觉 13 小时睡下去、中途合盖两小时，醒来时计时器还剩 11 小时，那一档就被
无声地睡过去了。分片睡觉把计时误差压在 15 分钟以内，而每次醒来只做一次内存里的时间
比较，一天不到 100 次。）

`--daemon-status` 报出来的计划时刻**就是真会用的那一刻**（看一眼就固化，随后启动
守护进程不会重掷），所以可以放心先看再决定装不装：

```
守护进程: 运行中 pid=86081
发布窗口: 07:30-09:30, 18:30-21:00
重试: 失败后 45 分钟重试，最多 3 次
下一次: 09-24 20:12
待机: 不轮询，只在上面那个时刻睁眼；错过窗口不补发
反馈: macos（webhook(wecom)、smtp、command 未配置）
日志: /path/to/output/daemon.log
最近: ✅ 9-24 早档发布完成 · 振华重工(600320)  成功 抖音、快手、视频号

今天 07:30-09:30  计划 08:37  [漏了]  错过 07:30-09:30，窗口已过
今天 18:30-21:00  计划 20:12  [待发]
明天 07:30-09:30  计划 09:17  [待发]
明天 18:30-21:00  计划 19:42  [待发]
累计: 成功 1 / 放弃 0 / 漏掉 1
  launchd: state=running pid=86081
```

装之前这里会显示 `守护进程: 未运行` / `launchd: 未加载`——两者都空就说明服务从没装过，
排期表里的「待发」只是算出来等人执行的计划，**不会有任何东西真的跑起来**。

日志与状态都在 `output/` 下：`daemon.log` 是每次运行的完整输出（含子进程 stdout），
`.daemon_state.json` 是排期、退出码与累计成败。

**重试也受窗口约束**：失败后 45 分钟退避重试，但如果那时窗口已经关了，就不再试了——
不会为了重试而把发布越界到窗口外。

**排错三件套：**

```bash
launchctl print "gui/$(id -u)/com.tangulunjin.daemon"        # 服务状态（认准 state = running）
launchctl kickstart -k "gui/$(id -u)/com.tangulunjin.daemon" # 改了代码后重启服务
tail -f output/daemon.log                                    # 实时看它在干什么
```

> 别用 `launchctl list | grep tangulunjin` 判断：`launchctl list` 在某些 shell / 沙箱 / 非交互
> 会话里会返回**空**（连系统服务都列不出来），会把「正在跑」误报成「未加载」。`--daemon-status`
> 已经改用 `launchctl print`，所以它的结论是可信的。

> **重装服务必须在「终端.app」里做。** `launchctl load` 在没有 GUI 会话权限的 shell（编辑器
> 内置终端、沙箱、SSH）里，对**任何** plist 都返回 `Input/output error`，而 legacy `load`
> 连非零退出码都不给。`--daemon-install` 因此不信任返回值，装完会自己查一遍；没查到时它会
> 把该在终端里跑的那一行打出来并以非零退出，不会假装装好了。

### 跑完给你回话：结果反馈（`notify`）

无人值守最糟的失败不是报错，而是**安静**：片子没发出去，你三天后才发现。日志和状态文件
都在 `output/` 下，但没人会主动去看一个「应该没事」的目录。所以每次跑完都推一条：

| 什么时候 | 推什么 |
|---|---|
| 全平台成功 | `✅ 9-24 晚档发布完成 · 振华重工(600320)` |
| 只发出去一部分 | `⚠️ 9-24 早档只发出去 3/5 · 拓普集团(601689)` |
| 一个都没发出去 | `❌ 9-24 早档没发出去`（附原因 + 什么时候重试） |
| 错过窗口 | `⏰ 3 档漏了`（连续漏掉几档会**合并成一条**，不刷屏） |
| 异常重启 | `🔁 守护进程重启`（pid 文件还在、进程却没了） |

**成功也推是有意的**：只在失败时说话，你会把通知和「出事」画等号，而没收到通知时又
分不清「今天没事」和「它昨晚就死了」。

一条完整的通知长这样 —— 说清哪几个平台成了、为什么没成，并给出**能直接执行的下一步**：

```
⚠️ 9-24 早档只发出去 3/5 · 拓普集团(601689)
拓普集团（601689）：把零件卖给所有新势力
成功: 抖音、快手、视频号
失败: B站 —— 601 风控冷却中，还需 23 小时
失败: 百家号 —— 凭证过期
用时: 11.2 分钟（第 1/3 次尝试）
补发: tangulunjin 601689 --republish
```

日志、退出码、完整输出留在 `output/daemon.log` 里，不往手机上搬。

#### 通道

按 `notify.channels` 顺序逐个发，互不影响。**任何一条发不出去只写一行日志，绝不会影响
发布本身** —— 通知是旁路，不是链路的一环。

| `kind` | 送到哪 | 要配什么 |
|---|---|---|
| `macos`（默认开） | 本机通知中心 | 什么都不用配 |
| `webhook` | 手机：企业微信 / 钉钉 / 飞书 / Bark / 裸 JSON | `type` + `url` |
| `smtp` | 邮件（能归档、能搜索） | `host`/`port`/`user`/`to`，密码走环境变量 |
| `command` | 任意推送工具（ntfy / pushover / 自己的脚本） | `command`，可带 `{report}` |

没填 `url`/`host`/`command` 的通道＝**还没启用**，会被静默跳过。默认配置里四个都留着，
填哪个哪个就活。

```yaml
notify:
  enabled: true
  # 什么时候推。on_success 建议留着 true，理由见上。
  on_success: true
  on_partial: true
  on_failure: true
  on_missed: true
  on_restart: true
  channels:
    - kind: macos                       # 本机横幅：零配置，人在电脑前立刻到
    - kind: webhook                     # 手机：建一个只有自己的群 → 群机器人 → 复制地址
      type: wecom                       # wecom | dingtalk | feishu | bark | json
      url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
    - kind: smtp
      host: smtp.qq.com
      port: 465
      user: you@qq.com
      password_env: TANGULUNJIN_SMTP_PASSWORD   # 密码只从环境变量读
      to: you@qq.com
```

本机通知有个坑：launchd 起的进程没有 bundle，系统会把通知算在「脚本编辑器」名下。
第一次没收到就去**系统设置 → 通知 → 脚本编辑器**把它打开。

#### 别等出事那天才发现收不到

```bash
tangulunjin --notify-test
```

它按真实配置发一条，并列出每个通道的状态：

```
反馈通道自检
  ✓ macos: 已发出
  · webhook: 跳过（url 为空 —— 还没启用）
  · smtp: 跳过（host/to 为空 —— 还没启用）
```

这条命令专门堵三个坑：通道填了但没填全（`url` 空着，看起来配了其实一次都不会发）、
macOS 通知权限没给（脚本不报错但横幅根本不出现）、webhook 被群里踢掉了
（返回 HTTP 200 + `errcode`，只看状态码会误报「送达」）。

想关掉全部反馈：`notify.enabled: false`。结果仍然完整留在日志和 `.daemon_state.json` 里，
`--daemon-status` 的「最近」一行也照常显示。

### 某个平台掉线时：`--continue-anyway`

默认是「有平台掉线就整条不发」——宁可一个都不发，也不能让你以为 5 个都发了：

```
⚠ 发布预检：视频号(tencent) 登录态失效，直接发布会跳过它们：
    uv run sau tencent login --account default
已退出。请先重新登录再重跑本命令，或用 --platforms 只发布健康平台。
```

无人值守时加 `--continue-anyway`（守护进程默认带），就会跳过掉线的平台继续发其余
的，并明确报出**发了几个**：

```
⚠ 发布预检：视频号(tencent) 登录态失效 —— 按 --continue-anyway 跳过它们，只发健康平台（4/5）。
```

被跳过的平台会记进 sidecar，登录好之后 `tangulunjin <code> --republish` 补上。

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
  --duration-minutes N  对话目标时长（分钟，默认 2-5）
  --no-render           只生成 HyperFrames 工程，跳过 MP4 渲染（最快跑通链路）
  --preview             同 --no-render
  --publish             渲染后自动发布到已配置的平台（抖音/B站/快手/视频号/百家号）
  --publish-only MP4    跳过生成，直接发布已有 MP4
  --republish           补发：自动找最近一次成片，只重发上次失败的平台
  --auto-pick           选题交给 tongstock 热门榜（取榜首未发过的那只）
  --topic-top N         选题时看榜单前 N 名（默认 10）
  --publish-at WHEN     走平台定时发表：19:30 / +11h / '2026-09-24 19:30'（至少提前 2h）
  --continue-anyway     有平台登录失效时跳过它继续发其余平台（无人值守用）
  --check               发布预检：工具链 + 各平台登录态，全过才退出码 0
  --keepalive           登录态心跳：真实活动一次并记录会话年龄，防发布时才发现失效
  --daemon              守护进程：窗口内随机挑时刻自动选题+生成+发布（推荐）
  --daemon-install      把守护进程装成 macOS launchd 服务（开机自启、崩溃自拉起）
  --daemon-status       看今天的排期、计划时刻、下次睁眼时刻与累计成败
  --daemon-uninstall    卸载 launchd 服务
  --notify-test         反馈通道自检：按 notify 配置发一条测试通知，确认收得到
  --platforms LIST      本次只发布指定平台，逗号分隔，可选：douyin、bilibili、
                        kuaishou、tencent、baijiahao、xiaohongshu
  --watchlist FILE      从文件读取股票代码（每行一个，可写「601689 拓普集团」，# 之后为注释）
  --no-preflight        跳过开跑前的平台登录态探测
  --no-interactive      不询问任何问题（抖音要短信验证码时只等手工写入 verify_code.txt）
  --help                显示帮助信息
```

**示例：**

```bash
# 基础用法
tangulunjin 600519

# 先跑通链路，不渲染 MP4
tangulunjin 600519 --no-render

# 指定名称和配置
tangulunjin 600519 --name 贵州茅台 --config my_config.yaml

# 指定输出目录和目标时长
tangulunjin 000001 --name 平安银行 --output-dir ./videos --duration-minutes 3

# 生成并自动发布到所有已配置平台
tangulunjin 600519 --publish

# 只发布一个已有视频
tangulunjin 600519 --publish-only output/600519_20260915_120000.mp4

# 无人值守：装好守护进程就不用管了
tangulunjin --daemon-status && tangulunjin --daemon-install
```

## 多平台自动发布

基于开源工具 [social-auto-upload](https://github.com/dreammis/social-auto-upload)（已内置于
`third_party/social-auto-upload`，通过浏览器自动化登录并上传）。默认发布平台：抖音、B站、快手、视频号、
百家号（小红书登录/上传能力保留，默认不在发布列表里）。

**首次使用：逐平台扫码登录一次（cookie 会保存，之后无需再登录）：**

```bash
cd third_party/social-auto-upload
uv sync                                       # 初始化 sau 环境（首次）

uv run sau douyin    login --account default  # 每个平台各登录一次
uv run sau bilibili  login --account default
uv run sau kuaishou  login --account default
uv run sau tencent   login --account default  # 视频号
uv run sau baijiahao login --account default
cd ../..
```

不确定哪个平台掉线时，直接跑 `tangulunjin --check`，它会把需要执行的登录命令逐条打出来。

登录时会自动弹出浏览器窗口（login 永远有头，即使配置里 headless: true），
在**弹出的浏览器里**扫码并在手机上点「确认登录」即可，等待窗口为 5 分钟。
注意：不要只扫保存的二维码 PNG 截图——抖音截图二维码常因风控校验不生效；
若手机点了确认但终端仍报超时，直接重跑一次登录命令。

**配置**（`stocktalk/config/default.yaml`）：

```yaml
publish:
  platforms: [douyin, bilibili, kuaishou, tencent, baijiahao]
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
    kuaishou: vertical       #   手机版（竖版）——快手激励不要求横屏，流量集中在 9:16
    tencent: horizontal
    baijiahao: horizontal
  ai_content_label:          # AI 生成声明的选项文案（不标注会被限流/取消变现资格）
    kuaishou: 内容由AI生成
  schedule: ""               # 留空立即发布；如 "2026-09-16 19:30" 定时发布
  extra_tags: []             # 追加自定义话题标签
```

发布内容（标题/简介/话题标签）从审核后的对话脚本自动生成，标题按平台字数限制截断，
简介自动附上「本内容由AI生成，仅供学习交流，不构成投资建议；投资有风险，入市需谨慎」，
B站自动选择财经分区。五个平台的 AI 生成声明都到位：抖音、视频号由上传器自行勾选，
快手按上面的 `ai_content_label` 传选项文案，B站与百家号没有声明字段、靠简介里那一句。

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
  min_duration_seconds: 120 # 最短目标时长
  max_duration_seconds: 300 # 最长目标时长
  max_line_chars: 130       # 每句最大字数（30-150）
  temperature: 0.8          # 创造性
  arc: auto                 # 剧本骨架：auto 按个股数据自动挑，也可写死
                            # mainline / crack / customer / annual / peers
  short_line_chars: 20      # 短回应（"嗯"）的字数上限；命中的轮次不重画画面图板

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

### 剧本骨架（`arc`）：让每期的结构不一样

视频结构不再写死在 prompt 字符串里，而是由 `stocktalk/modules/arcs.py` 的**骨架注册表**
决定。一条骨架（`Arc`）就是若干**节拍**（`Beat`）的顺序；每个节拍声明自己需要哪份数据
（`needs`），缺数据的节拍会被整段剪掉——"这只票讲什么"由它实际拿得到的数据决定，不硬凑。

| id | 结构 | 定位 |
| --- | --- | --- |
| `mainline` | 生意本质→行业→财务→风险→估值→小结 | 回退骨架，数据太薄时用 |
| `crack` | 反常识数字开场→追问代价→数据核对→极短认可→留下分歧 | 强冲突开场，抢前 3 秒 |
| `customer` | 谁在买→为什么买→会不会续买→谁能替代→小结 | 贴近日常认知，全程不碰行情 |
| `annual` | 收入结构→毛利来源→现金流质量→钱压在哪→隐患 | 最硬核，适合长片 / B站 |
| `peers` | 同行家数→规模位置→毛利差距→排名证据→小结 | 用 F10 行业排名做锚 |

- `auto`（默认）：只有**依赖全都能满足**的骨架才够格（不是"剪完还剩几拍"）；够格的有
  好几条时按 `(代码, 日期)` 稳定分散——同一只票同一天结果可复现（跑两次一样），不同票
  之间天然不撞形，且不依赖任何跨运行状态。
- 固定风格：`arc: annual`。数据不够时照旧按数据剪枝，剪掉了哪些节拍记录在
  `script["arc"]["pruned"]` 里，审查页看得到。
- **加一条新骨架**：在 `arcs.py` 里加一个 `Arc(...)` 并登记进 `ARCS` 字典，不用动 prompt
  字符串。节拍的 `needs` 用点路径指向压平后的 payload（如 `f10.主营构成.明细`、
  `financials.经营现金流(亿元)`），单条内用 `|` 表示"任一满足"。

### 片尾互动：内容层 + 画面层

每期脚本会多出三个字段，用于把观众留下来继续互动：`hook`（下期要核对的一个**可验证**
的具体变量）、`question`（抛给观众的生意层面二选一问题）、`sides`（多空各一句立场，
只讲生意分歧）。

它们落在片尾的**互动图板**上：沿用 visual 槽的最后一块图板，不新增槽位、不改模板/CSS/JS。
三样都没有时就不画这块图板，片尾退回原样。图板上的文案比字幕多过一道合规检查——
"看多/看空"这类立场标签与免责声明不允许出现在帧内，改写成中性说法（改写前后的对照
留在审查页上），买卖动作/点位/收益暗示则直接记为问题。

每期用了哪条骨架、被剪掉了哪些节拍、模型哪里没照骨架走，都记进 `script["arc"]` /
`script["arc_notes"]`，并在审查页的"本期结构与互动"一节里显示。

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
│       ├── arcs.py           # 剧本骨架注册表（结构不再写死在 prompt 里）
│       ├── dialogue_generator.py  # AI 对话生成
│       ├── compliance.py     # 合规审核
│       ├── tts_agent.py      # TTS 语音合成
│       ├── hyperframes_builder.py  # 视频工程构建（含片尾互动图板）
│       ├── cover.py          # 封面图（headless Chrome 截图）
│       ├── review.py         # 审查页（成片 + 封面 + 文案 + 合规自检一页看全）
│       ├── topic_picker.py   # 定时任务的选题（tongstock 热门榜 + 轮转记忆）
│       ├── scheduler.py      # 排期引擎（发布窗口、窗口内随机、错过即认）
│       ├── daemon.py         # 守护进程（常驻调度 + launchd 安装 + 每次跑完报结果）
│       ├── notify.py         # 结果反馈（本机通知 / 手机 webhook / 邮件 / 任意命令）
│       └── publisher.py      # 多平台发布
├── third_party/
│   └── social-auto-upload/   # 内嵌的发布工具（sau CLI，各平台登录 cookie 在此）
├── tests/                    # pytest 全量回归
├── pyproject.toml
└── README.md
```

## 输出文件

一次运行在 `output/` 下产出一组同名前缀的文件（`<tag>` = `<代码>_<时间戳>`）：

```
output/
├── 600519_20260924_143000.json            # 完整结果：脚本、合规结论、各平台上传结果
├── 600519_20260924_143000.mp4             # 成片·横版 1920×1080
├── 600519_20260924_143000.vertical.mp4    # 成片·竖版 1080×1920
├── 600519_20260924_143000.review.html     # 审查页：文案 + 合规自检 + 成片 + 封面，一页看全
├── 600519_20260924_143000.cover-portrait.png   # 竖版封面
├── 600519_20260924_143000.cover-landscape.png  # 横版封面（4:3）
├── 600519_20260924_143000.cover-wide.png       # 横版封面（16:9）
├── 600519_20260924_143000/                # HyperFrames 工程
│   ├── index.html                         #   浏览器打开即可预览
│   ├── subtitles.srt                      #   字幕
│   ├── audio/                             #   逐句配音
│   └── vertical/                          #   竖版工程
├── daemon.log                             # 守护进程：每次运行的完整输出
├── .daemon_state.json                     # 守护进程：排期、退出码、累计成败、上一次的平台级结果
├── .session_state.json                    # 登录态心跳：各平台最后有效时间
├── .publish_state.json                    # B站限流冷却等跨运行状态
└── .topic_rotation.json                   # 选题轮转：哪些票已发过
```

封面与审查页在渲染那一步一起产出（`--no-render` 不产），生成失败会降级为「不带封面发布」、
不影响成片。`output/` 里的 `*.json` 是 `--republish` / `--publish-only` 找回标题、简介、
封面与各平台结果的依据，别单独删。

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

先跑 `tangulunjin --check`。它把「工具链 + 各平台登录态」一次查完，缺什么会直接给出该执行的命令，
比对着报错猜快得多。下面是几个最常撞到的：

**`tongstock` 找不到 / `--auto-pick` 选不出题**
`tongstock` 不在 PATH 里。它通常装在 `~/.local/bin`，确认该目录在 PATH 中：
```bash
export PATH="$HOME/.local/bin:$PATH"   # 建议写进 ~/.zshrc
tongstock --help                       # 有输出即正常
```

**`tongstock` 查询超时**
```bash
tongstock sync   # 同步本地数据后重试
```

**报「公司数据缺失」直接中断**
这是**故意的**：F10 财务/公司信息是内容的唯一真源，拿不到就中断，不会降级成一条只复述行情的空壳脚本
（`stock_data.require_company_data`）。换个代码，或先把 tongstock 的数据补全。

**`bl` 未找到**
```bash
npm install -g bailian-cli
bl auth login --api-key sk-xxx
```

**`未找到 npx` / 渲染阶段失败**
缺 Node.js。`npx` 在首次渲染时还会下载 hyperframes，网络不通也会卡在这里；也可以先
`npm install -g hyperframes` 把这一步提前做掉。

**`sau CLI 未找到` 或发布报环境错**
内嵌的发布工具还没初始化：
```bash
cd third_party/social-auto-upload && uv sync && cd ../..
```

**抖音发到一半要短信验证码**
平台风控。交互终端下会直接提示你输入；无人值守时把验证码写进 `verify_code.txt`，
超时 `publish.verify_code_wait_seconds`（默认 150 秒）后放弃该平台、不影响其余平台。

**视频号昨天还好，今天就失效了**
它的服务端会话是**扫码后固定约 24 小时绝对过期**，与活跃度无关，保活无法续期。
每天登录一次即可覆盖当天所有发布班次（见[无人值守](#无人值守守护进程--daemon)）。

**渲染很久没动静**
正常。首次要下载 hyperframes，之后一次全链路渲染也常在 15 分钟量级；
上限由 `video.render_timeout_seconds`（默认 3600 秒）控制。

**重装守护进程后它不见了**
`--daemon-install` 要在 macOS 自带的「终端.app」里跑。编辑器内置终端 / 沙箱 / SSH 里，
`launchctl` 对任何 plist 都返回 `Input/output error`（legacy `load` 连退出码都给 0），
服务其实没注册。装完命令会自己查一遍，没查到时退非零并给出要补的那一行：
```bash
launchctl load -w ~/Library/LaunchAgents/com.tangulunjin.daemon.plist
launchctl print "gui/$(id -u)/com.tangulunjin.daemon"   # 认准 state = running
```

**跑完了但没收到任何通知**
先 `tangulunjin --notify-test`，它按真实配置发一条并报每条通道的状态。最常见的三种：
`url` 空着（通道没启用，会被静默跳过）、macOS 通知权限没给（launchd 起的进程没有 bundle，
通知算在「脚本编辑器」名下，去**系统设置 → 通知 → 脚本编辑器**打开）、webhook 地址被
群里踢掉了（返回 200 + `errcode`）。通知本身失败只写 `output/daemon.log`，
不影响发布，也不影响 `--daemon-status` 里的「最近」一行。

## License

MIT
