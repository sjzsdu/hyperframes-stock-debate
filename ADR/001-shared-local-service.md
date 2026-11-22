# ADR-001 · 本机 Python 服务与桌面 WebView 共享界面

状态：提案，V3/V7 待产品负责人确认。

## 背景

现有入口为 Python CLI（`pyproject.toml:11`、`pyproject.toml:24-25`），生成纳入 Pipeline（`stocktalk/pipeline.py:368`、`stocktalk/pipeline.py:441-475`），发布具有本机子进程和登录态依赖（`stocktalk/modules/publisher.py:760-761`、`stocktalk/modules/publisher.py:1455-1493`）。

## 备选方案与理由

| 方案 | 取舍 |
| --- | --- |
| 本机 Python HTTP + 浏览器 | 交付快，调试集中；桌面窗口需要另补 |
| 本机 Python HTTP + 浏览器 + pywebview | 复用 Python 工具链和同一套界面；系统 WebView 差异需在目标 OS 检验 |
| Electron + Python sidecar | Chromium 行为统一，桌面集成成熟；引入第二套发布和更新链，Node 渲染依赖仍需管理 |
| Tauri + Python sidecar | 系统 WebView 和桌面安装包；引入 Rust 构建、sidecar 打包和进程治理 |

## 决策

选第二项，建议 Flask/Jinja2 + 生产 WSGI 服务，桌面打包为可选项。Python 服务单独常驻，绑定环回并提供健康检查。浏览器与 pywebview 加载同一站点；桌面启动器在主线程运行 GUI，只连接服务，关闭窗口时后台任务继续。启动指引使用同一个服务地址和数据根。启动失败、API 不兼容或运行时缺失在界面显式显示。标题/日志输出服务版本与连接地址。pywebview 的本机服务与主线程契约见官方[应用结构](https://pywebview.flowrl.com/guide/architecture)和[主线程说明](https://pywebview.flowrl.com/guide/faq)。

本地请求校验 Host/Origin，同源会话和 CSRF 防护保护状态写入，拒绝跨站调用。桌面锁定为同源导航，外链用系统浏览器，无任意文件或命令桥接。首轮在现有 macOS 工作站验证壳；Windows/Linux 签名、安装包及远程暴露属于后续验收范围。若未来提供远程访问，另行以 Casdoor/OIDC 和 TLS 接入同一服务。

| 能力 | 浏览器 | 桌面首版 |
| --- | --- | --- |
| 类型、任务、状态、产物、报告 | 同源读写流程 | 同源读取、下载与展示 |
| 发起生成、预检、发布和失败补发 | 表单委托本机服务执行，AC-02/07 使用 C6 固定夹具 | 共享状态和报告。界面注明「生成与发布请使用 Web」 |
| 系统文件路径、渲染工具、SAU、平台登录态 | 由服务主机处理；缺登录态或需扫码时标「需要在桌面端执行」并提供本机预检/登录指引 | 运营者在服务主机按指引运行工具。桌面窗口继续显示状态 |

W3 的「桌面侧」和 W4 的 Web 端到端对「执行」有歧义。按上表区分提交和本机执行可同时走查两条路径；若 W3 要求 Web 完全不能发起这些任务，需由 WAN-205 负责人调整 W3 或 AC-02/07 后才有统一验收口径。

## 成本、风险与回退

估算 2–4 人日，固定月费增量 0，新增一个本机服务与可选窗口进程。风险是服务关闭后两端同时不可用及系统 WebView 差异。浏览器 URL 可在壳不启动时继续使用，数据留在同一服务；必要时停服务并以现有 CLI 和 sidecar 操作。省略壳会缺少独立桌面入口的验收证据。
