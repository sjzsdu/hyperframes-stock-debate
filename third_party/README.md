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

Re-apply after an upgrade (all patches are diffed against `0012d2c` and
verified with `git apply --check`; apply in numeric order):

```bash
cd third_party/social-auto-upload
git apply ../patches/0001-sau-douyin-local-fixes.patch
git apply ../patches/0002-sau-bilibili-runtime-resilience.patch
git apply ../patches/0003-sau-ai-content-declaration.patch
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
