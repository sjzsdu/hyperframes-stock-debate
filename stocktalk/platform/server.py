"""Loopback-only HTTP application shared by browser and desktop viewer."""
from __future__ import annotations

import argparse
import atexit
from copy import deepcopy
import fcntl
import json
from pathlib import Path
import re
import secrets
import sys

from flask import Flask, abort, jsonify, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException

from stocktalk.modules.providers.oneapi import OneAPIClient, OneAPIRequestError, validate_dialogue
from stocktalk.modules.publisher import PLATFORM_LABELS, SauPublisher
from stocktalk.pipeline import load_config
from stocktalk.video_types import TypeCatalog, TypeContractError, bind_type
from .store import Conflict, Store
from .worker import Worker

API_VERSION = 1


def reject_inline_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"api_key", "access_token", "refresh_token", "token", "password", "cookie", "cookies", "secret", "client_secret", "authorization"} and item:
                raise ValueError("凭据只能通过服务进程环境或私有账号目录注入，禁止在配置中存储明文")
            reject_inline_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_inline_credentials(item)


def create_app(config=None, *, root=Path(".platform"), origin="http://127.0.0.1:8765", models=None,
               pipeline_factory=None, publisher_factory=SauPublisher, demo=False):
    config = deepcopy(config if config is not None else load_config())
    reject_inline_credentials(config)
    validate_dialogue(config)
    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)
    app.config.update(MAX_CONTENT_LENGTH=16384, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")
    store = Store(Path(root))
    catalog = TypeCatalog(config.get("video_types_dir"))
    provider = OneAPIClient()
    get_models = models or provider.models
    worker = Worker(store, publisher_factory=publisher_factory, **({"pipeline_factory": pipeline_factory} if pipeline_factory else {}))
    app.extensions.update(store=store, worker=worker, catalog=catalog)

    @app.before_request
    def protect():
        if request.host != origin.split("://", 1)[1] or request.headers.get("Origin", origin) != origin:
            abort(403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            expected = session.get("csrf")
            if not expected or not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), expected):
                abort(403)
            if (Path(root) / "RECOVERY_REQUIRED").exists():
                raise Conflict("执行器恢复待确认；请先确认旧子进程已退出，再用 --ack-recovery 启动")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, OneAPIRequestError):
            return jsonify(error=exc.public()), 503
        if isinstance(exc, Conflict):
            return jsonify(error={"code": "CONFLICT", "message": str(exc)}), 409
        if isinstance(exc, (TypeContractError, ValueError)):
            return jsonify(error={"code": "INVALID_INPUT", "message": str(exc)}), 400
        if isinstance(exc, HTTPException):
            return jsonify(error={"code": "FORBIDDEN" if exc.code == 403 else "HTTP_ERROR",
                                  "message": "无权限，请从本机同源页面重新进入" if exc.code == 403 else "请求无效或资源不存在"}), exc.code
        return jsonify(error={"code": "INTERNAL_ERROR", "message": "服务未完成请求，请检查本机配置"}), 500

    @app.get("/")
    @app.get("/desktop")
    def home():
        session.setdefault("csrf", secrets.token_urlsafe(32))
        return render_template("index.html", csrf=session["csrf"], desktop=request.path == "/desktop", platforms=PLATFORM_LABELS, demo=demo)

    @app.get("/health")
    def health():
        return jsonify(status="recovery_required" if (Path(root) / "RECOVERY_REQUIRED").exists() else "ok", api_version=API_VERSION, version="0.1.0")

    @app.get("/api/types")
    def types():
        return jsonify(types=[t.public() for t in catalog.types.values()], diagnostics=catalog.diagnostics)

    @app.get("/api/models")
    def model_list():
        return jsonify(models=[{"id": m, "capability": "unknown"} for m in get_models()], source="oneapi")

    @app.route("/api/tasks", methods=["GET", "POST"])
    def tasks():
        if request.method == "GET":
            return jsonify(tasks=store.list(request.args.to_dict()))
        data = request.get_json()
        if not isinstance(data, dict) or set(data) - {"stock", "type_id", "model"}:
            raise ValueError("仅接受 stock/type_id/model")
        if not re.fullmatch(r"[0-9]{6}", str(data.get("stock", ""))):
            raise ValueError("stock: 请输入六位股票代码")
        t = catalog.get(str(data.get("type_id", "")))
        if data.get("model") not in get_models():
            raise OneAPIRequestError("MODEL_UNAVAILABLE")
        key = request.headers.get("Idempotency-Key", "")
        if not key or len(key) > 128:
            raise ValueError("Idempotency-Key: 需要 1–128 字符")
        snapshot_config = deepcopy(config)
        snapshot_config.setdefault("dialogue", {}).update(type_id=t.id, model=data["model"])
        bind_type(snapshot_config)
        snapshot_config["_frozen_type"] = t.public()
        snapshot_config["output"] = {}
        task_id, created = store.create(key, data, {"config": snapshot_config, "type": t.public(), "api_version": API_VERSION})
        return jsonify(id=task_id, status="queued" if created else (store.get(task_id) or {})["status"]), 202 if created else 200

    def require_task(task_id):
        task = store.get(task_id)
        if task is None:
            abort(404)
        return task

    @app.get("/api/tasks/<task_id>")
    def detail(task_id):
        return jsonify(require_task(task_id))

    @app.get("/api/tasks/<task_id>/artifacts/<artifact_id>")
    def artifact(task_id, artifact_id):
        path = store.artifact(artifact_id, task_id)
        if path is None:
            abort(404)
        # Never execute generated project/review HTML as same-origin application code.
        return send_file(path, as_attachment=path.suffix.lower() not in (".mp4", ".png", ".jpg", ".wav", ".mp3"))

    @app.post("/api/preflight")
    def preflight():
        data = request.get_json()
        selected = data.get("platforms") if isinstance(data, dict) else None
        if not isinstance(selected, list) or not selected or any(p not in PLATFORM_LABELS for p in selected):
            raise ValueError("请选择支持的平台")
        publisher = publisher_factory(deepcopy(config))
        try:
            publisher._require_sau()
            checks = publisher.check_platforms(selected)
            return jsonify(platforms={p: {"ok": bool(r["ok"]), "code": "READY" if r["ok"] else "LOCAL_ACTION_REQUIRED",
                                 "message": "可发布" if r["ok"] else "请在服务主机执行 sau 平台 login/check"} for p, r in checks.items()})
        except Exception:
            return jsonify(error={"code": "DEPENDENCY_MISSING", "message": "请在服务主机安装外置 SAU，配置 publish.sau_dir/sau_executable"}), 503

    @app.route("/api/tasks/<task_id>/publications", methods=["GET", "POST"])
    def publications(task_id):
        task = require_task(task_id)
        if request.method == "GET":
            return jsonify(task_id=task_id, publications=store.publications(task_id))
        if task["status"] != "succeeded":
            raise Conflict("请先生成并审阅成片")
        data = request.get_json()
        if not isinstance(data, dict) or set(data) - {"platform", "schedule", "ai_declaration", "reviewed", "retry_of"}:
            raise ValueError("发布字段不合法")
        platform = data.get("platform")
        if platform not in PLATFORM_LABELS or data.get("reviewed") is not True or type(data.get("ai_declaration")) is not bool:
            raise ValueError("请选择平台，并确认已审阅成片及 AI 声明")
        if task["result"].get("approved_script", {}).get("type_compliance", {}).get("ai_declaration") and not data["ai_declaration"]:
            raise ValueError("此类型要求声明 AI 生成内容")
        schedule = data.get("schedule") or None
        if schedule is not None:
            from stocktalk.modules.publisher import parse_publish_at
            schedule = parse_publish_at(str(schedule).replace("T", " "))
        account = str(config.get("publish", {}).get("accounts", {}).get(platform, "default"))
        options = {"schedule": schedule, "ai_declaration": data["ai_declaration"],
                   "canvas": config.get("publish", {}).get("platform_canvas", {}).get(platform, task["result"]["canvas"])}
        publication_id = store.publish(task_id, platform, account, options, data.get("retry_of"))
        return jsonify(id=publication_id, task_id=task_id, status="pending"), 202

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="股票视频工作台 · 本机共享服务")
    parser.add_argument("--config")
    parser.add_argument("--data-dir", default=".platform")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="固定行情/媒体与沙箱发布；不调用真实服务")
    parser.add_argument("--ack-recovery", action="store_true", help="仅在人工确认旧执行器及子进程退出后使用")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    root = Path(args.data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (root / "service.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("数据目录已被另一个服务占用")
    origin = f"http://127.0.0.1:{args.port}"
    extra = {}
    if args.demo:
        from .demo import DemoPipeline, DemoPublisher
        extra = dict(pipeline_factory=DemoPipeline, publisher_factory=DemoPublisher, models=lambda: ["demo-fixture"], demo=True)
        config["video_types_dir"] = str(Path(__file__).resolve().parents[2] / "examples/video_types")
    app = create_app(config, root=root, origin=origin, **extra)
    recovery = app.extensions["store"].recover()
    if args.ack_recovery:
        (root / "RECOVERY_REQUIRED").unlink(missing_ok=True)
        recovery = False
    worker = app.extensions["worker"]
    if not recovery:
        worker.start()
    atexit.register(worker.close)
    print(f"Stocktalk 0.1.0 API {API_VERSION}: {origin} · data={root} · demo={args.demo}", flush=True)
    from waitress import serve
    import signal
    def shutdown(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, shutdown)
    try:
        serve(app, host="127.0.0.1", port=args.port, threads=4)
    finally:
        worker.close()
        lock.close()


if __name__ == "__main__":
    main()
