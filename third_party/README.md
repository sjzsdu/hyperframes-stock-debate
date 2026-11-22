# 可选发布运行时

SAU 已移出主仓，生成、管理、Web 与桌面查看无需安装它。保留的 15 个历史 patch 仅供来源审计，后续不在主仓增加 SAU 补丁。原有效树固定于 `e9cddcc:third_party/social-auto-upload`，含 `conf.py` 和当时已应用的补丁；不要再把全部 patch 重放到该快照。

从仓库根运行：

```bash
python scripts/export_sau_runtime.py --destination "$HOME/.local/share/stocktalk/sau"
uv sync --locked --project "$HOME/.local/share/stocktalk/sau"
uv pip install --python "$HOME/.local/share/stocktalk/sau/.venv/bin/python" -r runtime/sau-requirements.txt
"$HOME/.local/share/stocktalk/sau/.venv/bin/sau" --help
```

Python 3.12，快照本身要求 `<3.13`。导出只读取 Git 跟踪树，不打包当前账号 cookie、venv 或日志。上游 MIT 许可见 `SAU-LICENSE`，完整许可随归档保留。固定 mtime `1795321023` 的可复现归档 SHA256：`251ac4f445ad9a5d627fda2fe03c3a44cd518544d508861ace39ee6a26692811`。浅克隆缺少基线提交时先补全历史。

原锁文件可用 `uv sync --locked` 安装，但基线代码额外导入了未列入锁文件的 `playwright`。`runtime/sau-requirements.txt` 固定该补充依赖，不修改上游源码。浏览器安装和六平台实际帐号行为仍由运营者在服务主机验证；依赖可启动不等于平台登录或发布通过。

配置使用同一个根和可执行文件（YAML 必须写展开后的绝对路径，`$HOME` 不会自动展开）：

```yaml
publish:
  sau_dir: /Users/your-name/.local/share/stocktalk/sau
  sau_executable: /Users/your-name/.local/share/stocktalk/sau/.venv/bin/sau
```

在该工具根执行 `sau <platform> login --account <alias>`，账号沿用 `publish.accounts`。首次安装按 SAU 的浏览器指引初始化。登录数据只留在私有目录，目录权限 0700；重新登录优先于跨版本复制 cookie。不要把账号数据纳入归档或问题附件。

兼容顺序：显式 `sau_executable` → `sau_dir/.venv/bin/sau` → PATH。cwd 和账号根使用同一 `sau_dir`。GUI 不接受任意命令/路径。CLI、守护和 Web 发布通过共享账号锁互斥。六平台参数、画幅、封面、定时和 AI 声明由 `tests/test_external_publisher.py` 对比；真实账号行为未验证。

升级/回退：先停同账号的守护发布并排空队列；未知结果必须先核对远端。把 `sau_dir` 和 `sau_executable` 一起切回保留的版本根，运行 `sau --help`、本机登录预检和沙箱测试，再恢复队列。也可从同一 Git 基线重新导出原树并用旧 CLI；保留 SQLite 与媒体备份，禁止为回退删除成片。回退无需修改或复制账号文件。
