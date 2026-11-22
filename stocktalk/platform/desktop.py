"""Optional desktop viewer connects to an independently managed local service."""
from __future__ import annotations
import argparse
import json
from urllib.parse import urlsplit
from urllib.request import urlopen


def viewer_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("桌面窗口仅连接 http://127.0.0.1:<端口> 本机服务")
    return url.rstrip("/") + "/desktop"


def main(argv=None):
    parser = argparse.ArgumentParser(description="谈股论金桌面查看器（生成与发布请使用 Web）")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--task", help="打开共享任务 ID")
    parser.add_argument("--smoke-seconds", type=float, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        url = viewer_url(args.url)
        with urlopen(args.url.rstrip("/") + "/health", timeout=5) as response:
            health = json.load(response)
        if health.get("api_version") != 1:
            parser.error("共享服务 API 版本不兼容，请升级服务和窗口到同一版本")
    except (ValueError, OSError):
        parser.error("本机服务不可用，请先启动 tangulunjin-web")
    try:
        import webview
    except ImportError:
        parser.error("请安装桌面可选依赖：pip install '.[web,desktop]'")
    if args.task:
        import re
        if not re.fullmatch(r"[a-f0-9]{32}", args.task):
            parser.error("任务 ID 格式错误")
        url += "#" + args.task
    print(f"Stocktalk desktop 0.1.0 API 1: {url} · read-only", flush=True)
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    # No js_api, file access or command bridge is exposed to page content.
    window = webview.create_window("谈股论金 · 桌面任务查看器", url=url, width=1180, height=820)
    assert window is not None
    def enforce_origin():
        current = window.get_current_url()
        if current and not current.startswith(args.url.rstrip("/") + "/") and current != "about:blank":
            import webbrowser
            if urlsplit(current).scheme in ("http", "https"):
                webbrowser.open(current)
            window.load_url(url)
    def loaded():
        enforce_origin()
        print(f"Desktop loaded: {window.get_current_url()}", flush=True)
    window.events.loaded += loaded
    if args.smoke_seconds:
        import time
        def close_after_load():
            time.sleep(min(max(args.smoke_seconds, 1), 30))
            window.destroy()
        webview.start(close_after_load)
    else:
        webview.start()


if __name__ == "__main__":
    main()
