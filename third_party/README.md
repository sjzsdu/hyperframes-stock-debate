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

Re-apply after an upgrade (all patches are diffed against `0012d2c` and
verified with `git apply --check`; apply in numeric order):

```bash
cd third_party/social-auto-upload
git apply ../patches/0001-sau-douyin-local-fixes.patch
git apply ../patches/0002-sau-bilibili-runtime-resilience.patch
git apply ../patches/0003-sau-ai-content-declaration.patch
git apply ../patches/0004-sau-baijiahao-navigation-race.patch
git apply ../patches/0005-sau-baijiahao-system-chrome-title-panel.patch
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
