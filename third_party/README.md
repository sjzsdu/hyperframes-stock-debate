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

Re-apply after an upgrade (both patches are diffed against `0012d2c` and
verified with `git apply --check`; 0001 must land first):

```bash
cd third_party/social-auto-upload
git apply ../patches/0001-sau-douyin-local-fixes.patch
git apply ../patches/0002-sau-bilibili-runtime-resilience.patch
```

Both patches together reproduce this directory exactly apart from the
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
