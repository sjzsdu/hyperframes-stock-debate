# third_party/

Vendored upstream dependencies. Source code here is committed to this repository
directly (no submodules), so a fresh `git clone` gives a working tree with no
extra steps beyond installing each dependency's own environment.

## social-auto-upload

支撑 `tangulunjin` `--publish` 的多平台上传器（抖音/B站/快手/小红书/视频号）。

- Upstream: https://github.com/dreammis/social-auto-upload
- Vendored from commit: `0012d2c` (shallow clone, so upstream history was dropped)
- License: see `social-auto-upload/LICENSE`

### Environment setup

The environment is **not** in git. Rebuild it once per machine:

```bash
cd third_party/social-auto-upload
uv sync                    # creates .venv with Playwright
uv run playwright install chromium
sau douyin login --account default    # one-time scan per platform, writes cookies/
```

### Local patches

This fork carries changes that upstream does not have. They are archived as
patches here so they survive an upgrade and can be sent upstream later:

- `patches/0001-sau-douyin-local-fixes.patch`
  - `login` always opens a visible browser (scanning a PNG screenshot under
    headless routinely timed out)
  - 抖音 login: treat a valid `sessionid` cookie as success, friendlier timeout
    message, and widen the scan wait from ~2 min to ~5 min
- `patches/0002-sau-bilibili-runtime-resilience.patch`
  - B站's first upload downloads the `biliup` binary through the GitHub Releases
    API; an anonymous rate limit (HTTP 403) or a network blip killed the publish
    outright with no way to recover. Now falls back to the release web page
    (whose 302 carries the latest tag), HEAD-checks the URL derived from
    biliup's asset naming rules, and reuses an already-installed local `biliup`
    when the network is unavailable.
- `patches/0003-sau-ai-content-declaration.patch`
  - 快手/小红书的上传器原本不会标注 AI 生成内容。财经内容不标注会被限流乃至取消
    变现资格（小红书《社区金融生态公约》明确要求），所以给两家都加了声明步骤：
    快手在「作者声明」下拉里选，小红书在「添加内容类型声明」弹窗里选。
  - 新增 `sau kuaishou|xiaohongshu upload-video --ai-content-label <文案>`：选项
    文案随站点改版会变，做成参数后改配置即可，不必改代码。上层通过
    `publish.ai_content_label` 传入（见 `stocktalk/config/default.yaml`）。
  - 找不到入口或选项时只记 warning 继续发布——描述里另有「本内容由AI生成」兜底，
    不因为一个下拉框把整次发布打断。
- `patches/0004-sau-baijiahao-navigation-race.patch`
  - 百家号后台是 SPA：进入发布页、选完视频文件都会触发整页导航（跳到带
    `productId` 的新编辑页 URL）。导航瞬间 Playwright 执行上下文被销毁，
    所有 `locator().count()`/`wait_for` 抛 "Execution context was destroyed"，
    2026-09-19 两次发布均死于该竞态（一次死在选完文件后，一次死在刚进页面）。
  - 修复：goto 后等 URL 连续采样稳定（`_wait_nav_settled`）；选文件/等标题编辑器/
    填标题等步骤包 `_nav_retry`（命中上下文销毁自动重试）；点发布后的成功轮询
    探测改为容错——页面跳向成功页时不再把竞态误判为发布失败。
- `patches/0005-sau-baijiahao-system-chrome-title-panel.patch` (2026-09-20)
  - 百家号发布页的富文本编辑器（FeEditorApp）在 Playwright 内置 Chromium
    headless 下根本不渲染——表单的 contentEditable 永远不出现，等到 180s 超时
    （09-18/09-19 全部发布失败均死于其后，0004 修的导航竞态只是前奏）；同一
    页面换系统 Chrome 立即正常。`_build_launch_kwargs` 现按平台探测系统 Chrome，
    找不到才回退内置 Chromium（conf 的 `LOCAL_CHROME_PATH` 优先级不变）。
  - 新版发布页把标题挪进右下角「封面/标题板」弹层，主表单只有作品描述；老的
    `div[class*="contentEditable"]` 现匹配到的是描述编辑器，`_fill_title` 实际
    填的是描述（保留该行为：描述=标题，利于推荐）。新增 `_set_title_via_panel`
    尽力而为步骤：入口 enabled 才点开填写，失败只记 warning——百家号会预填
    文件名当标题，发布不被阻塞。
  - 点发布后百度会弹「百度安全验证」滑块（2026-09-20 两次实发均触发）：
    headless 下立即失败；headed 下提示人工拖滑块并把等待窗口从 30s 延长到
    5 分钟。stocktalk 侧 `PLATFORM_SPECS["baijiahao"]["headed"]=True` 强制
    有头（无视 `publish.headless`）。
  - 已用 DryRun（`_submit_publish` 置空）端到端走通：选文件→编辑器→标题/描述→
    上传→AI声明→停在发布前。
- `patches/0006-sau-tencent-cover-dialog-visible.patch` (2026-09-20)
  - 视频号发布页把封面编辑弹窗**常驻在 DOM 里（hidden）**。`open_thumbnail_dialog`
    点完入口只等 500ms 就按标题数 count，拿到隐藏弹窗交给后续
    `wait_for(visible)`，5s 必超时——2026-09-20 15:49 国轩高科实发：4:3 横版封面
    设置失败被跳过，视频号随即回退到**视频首帧**当封面，而首帧是入场动画未完成
    的半空画面，聊天分享卡很难看（3:4 竖版那次入口点击恰好真弹了窗所以成功）。
  - 修复：点入口后轮询最多 8s 等 `div.weui-desktop-dialog` **真正可见**
    （`is_visible()`），等不到就换下一个入口选择器重试；全部失败才返回 None。
  - 配套修复在 stocktalk 侧：`templates/hyperframes/stock-debate.js` 把开场场景
    改为第 0 帧完成态（海报帧）——即使封面再被跳过，首帧也是完整排版。
- `patches/0007-sau-tencent-keepalive.patch` (2026-09-23)
  - 视频号会话无法长期保活，是「持续定时运行」的死结。实测：cookie 文件里
    `sessionid` 的 expires 是 **2027**，本地根本不过期；真正会死的是**服务端会话**。
  - **口径修正（2026-09-23 晚，用户澄清）**：过去每天都由人工在发布前跑一次
    `sau tencent login`，把时钟重置了，所以日志里那些「失效」多数只是登录命令自己的
    前置检查。可用的干净读数只有：
    * 每次扫码后**确认可用**的最长记录 = **8.0h**（最后一次真实上传距登录）；
    * 次日首次检查（扫码后 16.6 / 16.7 / 23.0 / 23.9 / 25.6 / 32.5h）**全部判失效**；
    * 一次矛盾读数：09-19 08:30 判「有效」（距上次登录 41.3h，中间无登录），但当时
      没有任何上传动作去证实它 → 更像 `cookie_auth` 的假阳性（它只等 8 秒跳转）。
    结论：**会话能否撑过两次发布之间的间隔（08:30↔19:30 = 11h、19:30↔08:30 = 13h），
    在人工每日登录的前提下根本测不出来**。故 0007 的 keepalive 现在连「会话年龄」
    一起报（见下），先把这段空白测出来，再决定是否需要改成平台侧定时发表。
  - 新增 `tencent_keepalive(account_file, retries=1)` 与 `sau tencent keepalive`：
    无头真实访问一次视频号后台，仍然有效就**回写 `storage_state`**，输出
    `alive|dead: <detail>` 并以 0/1 退出，供定时任务判断；`--retries` 默认 1，用于压
    掉抖动假阳性（2026-09-18 23:46 报过一次失效、次日 08:30 又是有效，中间没有任何
    登录动作）。stocktalk 侧还会从 `logs/tencent.log` 读最后一次「扫码成功」，
    把**会话年龄**写进 `output/.session_state.json` 与心跳输出。
  - 与 `check` 的分工：`check` 只回答能不能用、失败即 1，适合发布前预检；
    `keepalive` 面向无人值守，负责保温 + 提前发现失效。
  - 判定逻辑抽成 `_tencent_page_is_logged_in()`（跳转登录页 / 扫码 iframe /
    URL 含 login 三重判定）；`cookie_auth` 保持原实现不动，避免回归风险。
  - 失效后的补救不需要新命令：`sau tencent login` 本身就是无头 + 把二维码存成
    `cookies/<account>_tencent_login_qrcode_<ts>.png` 并等待扫码约 5 分钟，定时任务
    可以把这个 PNG 推给手机、扫码即恢复。
  - 若心跳证明「活动不能续期」，退路是**平台侧定时发表**：`sau tencent upload-video
    --schedule "YYYY-MM-DD HH:MM"`（至少提前 2 小时；stocktalk 侧对应
    `tangulunjin --publish-at 19:30`，见 `publisher.parse_publish_at`）。把发布时刻搬进
    「会话还活着的那一刻」，一次登录就能排好后面几条，不必让会话撑到发布点。
- `patches/0008-sau-tencent-quick-login.patch` (2026-09-24)
  - 让 `sau tencent login` **免扫码**：打开登录页后先找 qrconnect iframe 里的
    「微信快捷登录」按钮（`.js_quick_login_btn`；页面自己会探测本机微信
    `localhost.weixin.qq.com`，探测通过才渲染它），点一下并等跳转；成功就跳过
    二维码，失败（微信没跑 / 点了没反应）自动 reload 回落扫码流程。
  - **2026-09-24 15:19 实测通过**：点击后 **33 秒**静默完成授权并跳转，**微信客户端
    全程无需点「允许」**（此前一直存疑的关键问题）。登录后 `cookie_auth` 复核有效。
    这意味着只要 Mac 上微信已登录，视频号重新登录就是**零人工**的。
  - 两个实现坑（已写进代码注释）：①按钮要等页面 JS 探测完本机微信才渲染、iframe
    也是异步挂载 → 找按钮必须轮询等待（第一版 goto 完立刻扫，永远扫不到）；
    ②iframe 里有**两个**同名按钮，`.first` 撞上的那个是隐藏副本（`visible=False`），
    必须逐个挑可见的那个。
  - 快捷登录成功后仍走原收尾路径（`storage_state` 回写 + `cookie_auth` 校验），
    不另起一套登录代码。

Re-apply after an upgrade (all patches are diffed against `0012d2c` and
verified with `git apply --check`; apply in numeric order):

```bash
cd third_party/social-auto-upload
git apply ../patches/0001-sau-douyin-local-fixes.patch
git apply ../patches/0002-sau-bilibili-runtime-resilience.patch
git apply ../patches/0003-sau-ai-content-declaration.patch
git apply ../patches/0004-sau-baijiahao-navigation-race.patch
git apply ../patches/0005-sau-baijiahao-system-chrome-title-panel.patch
git apply ../patches/0006-sau-tencent-cover-dialog-visible.patch
git apply ../patches/0007-sau-tencent-keepalive.patch
git apply ../patches/0008-sau-tencent-quick-login.patch
```

注意补丁里的路径是 **vendored 目录相对**（`sau_cli.py`、`uploader/...`），所以上面这套
只在「重新克隆的上游检出」里成立——那里该目录就是仓库根。在本仓里改 vendor 代码时，
`third_party/social-auto-upload` 只是主仓的一个子目录，`git apply` 会把补丁路径当成
**主仓根相对**、直接报 `Skipped patch 'sau_cli.py'` 且静默不落任何改动。这种场景用：

```bash
cd third_party/social-auto-upload
patch -p1 < ../patches/0007-sau-tencent-keepalive.patch   # 先加 --dry-run 校验
```

All patches together reproduce this directory exactly apart from the
force-added `conf.py` — re-verify after any upgrade with:

```bash
diff -rq --exclude=.venv --exclude=cookies --exclude=logs \
  --exclude=__pycache__ --exclude='*.egg-info' \
  /path/to/clean-0012d2c third_party/social-auto-upload
# expected output: only "Only in ...: conf.py"
```

### Upgrading

```bash
cd /tmp && git clone https://github.com/dreammis/social-auto-upload sau-new
cd sau-new
rsync -a --delete \
  --exclude '.venv/' --exclude 'cookies/' --exclude 'logs/' --exclude 'conf.py' \
  ./ /path/to/hyperframes-stock-debate/third_party/social-auto-upload/
```

Then re-apply the patches above, rebuild the venv, and re-run a publish to
confirm nothing broke. Diff before committing — upstream selector changes are
frequent, and the publish flow depends on exact DOM class names.

### Gotcha: conf.py

Upstream's `.gitignore` excludes `conf.py`, but the package imports it for
`BASE_DIR`. It is force-added to this repo:

```bash
git add -f third_party/social-auto-upload/conf.py
```

If a future `git add .` seems to silently drop it, that rule is why.
