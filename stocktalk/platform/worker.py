"""Serial application executor; all costly work occurs outside database transactions."""
from __future__ import annotations

from copy import deepcopy
import json
import subprocess
from pathlib import Path
import threading
from typing import Any

from stocktalk.modules.providers.oneapi import OneAPIRequestError
from stocktalk.modules.publisher import SauPublisher
from stocktalk.pipeline import Pipeline
from .store import Store


def relocate(value: Any, before: str, after: str) -> Any:
    if isinstance(value, str):
        return after + value[len(before):] if value.startswith(before + "/") else value
    if isinstance(value, dict):
        return {k: relocate(v, before, after) for k, v in value.items()}
    if isinstance(value, list):
        return [relocate(v, before, after) for v in value]
    return value


class Worker:
    def __init__(self, store: Store, *, pipeline_factory=Pipeline, publisher_factory=SauPublisher):
        self.store = store
        self.pipeline_factory, self.publisher_factory = pipeline_factory, publisher_factory
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def once(self) -> bool:
        task = self.store.claim()
        if task:
            task_id = task["id"]
            staging = self.store.root / "staging" / task_id / str(task["attempt"])
            config = deepcopy(task["snapshot"]["config"])
            config.setdefault("output", {})["dir"] = str(staging)
            try:
                staging.mkdir(parents=True, exist_ok=False)
                pipeline = self.pipeline_factory(config)
                pipeline.progress_callback = lambda stage: self.store.stage(task_id, stage)
                result = pipeline.run(task["stock"], render=True, publish=False)
                video = Path(result.get("video_path") or "")
                if not video.is_file() or video.stat().st_size == 0 or not video.resolve().is_relative_to(staging.resolve()):
                    raise ValueError("missing rendered media")
                measured = json.loads(subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)], timeout=10))
                if not {"audio", "video"}.issubset({s["codec_type"] for s in measured["streams"]}):
                    raise ValueError("rendered media must contain audio and video")
                if not result.get("acceptance_fixture"):
                    bounds = task["snapshot"]["type"]["duration"]
                    duration = float(measured["format"]["duration"])
                    if not bounds["min_seconds"] <= duration <= bounds["max_seconds"]:
                        raise ValueError("rendered duration outside type contract")
                final = self.store.root / "artifacts" / task_id / str(task["attempt"])
                final.parent.mkdir(parents=True, exist_ok=True)
                result = relocate(result, str(staging), str(final))
                # Rewrite sidecars before atomic publication, including their embedded output references.
                for sidecar in staging.glob("*.json"):
                    sidecar.write_text(json.dumps(relocate(json.loads(sidecar.read_text()), str(staging), str(final)), ensure_ascii=False))
                staging.rename(final)
                self.store.complete(task_id, result, final)
            except OneAPIRequestError as exc:
                self.store.fail(task_id, exc.public())
            except Exception:
                self.store.fail(task_id, {"code": "GENERATION_FAILED", "message": "生成失败，请检查本机媒体工具与输入数据后重新创建任务"})
            return True
        publication = self.store.claim_publication()
        if not publication:
            return False
        task = self.store.get(publication["task_id"], private=True)
        assert task is not None
        config = deepcopy(task["snapshot"]["config"])
        options = publication["options"]
        platform = publication["platform"]
        publish_config = config.setdefault("publish", {})
        labels = publish_config.get("ai_content_label", {})
        configured_label = labels.get(platform, "AI生成") if isinstance(labels, dict) else labels
        publish_config["ai_content_label"] = {platform: configured_label if options["ai_declaration"] else ""}
        publish_config["interactive_verify_code"] = False
        # Platform attempts are ledger-controlled: no hidden replay or headed retry.
        publish_config["retries"] = 0
        publish_config["no_replay"] = True
        result = task["result"]
        try:
            publisher = self.publisher_factory(config)
            checks = publisher.check_platforms([platform])
            if not checks[platform]["ok"]:
                self.store.finish_publication(publication["id"], "action_required", {
                    "code": "LOCAL_ACTION_REQUIRED", "message": "请在服务主机执行发布工具预检/登录，再补发"})
                return True
        except Exception:
            self.store.finish_publication(publication["id"], "action_required", {
                "code": "DEPENDENCY_MISSING", "message": "发布工具预检失败，请在服务主机检查外置工具与登录态后补发"})
            return True
        try:
            report = publisher.publish(result["video_path"], result["approved_script"], result["stock_name"], task["stock"],
                    platforms=[platform], schedule=options["schedule"], platform_videos=result.get("platform_videos"), covers=result.get("covers"))
            rows = report.get("platforms", [])
            ok = any(r.get("platform") == platform and r.get("ok") for r in rows)
            # Tool output may contain cookies/stack traces; expose only normalized status.
            self.store.finish_publication(publication["id"], "succeeded" if ok else "outcome_unknown", {
                "task_id": task["id"], "platform": platform, "ok": ok,
                "code": "PUBLISHED" if ok else "OUTCOME_UNKNOWN",
                "message": "发布完成" if ok else "上传未确认成功，请核对远端结果"})
        except Exception:
            self.store.finish_publication(publication["id"], "outcome_unknown", {
                "code": "OUTCOME_UNKNOWN", "message": "提交结果未知，请人工核对远端；不会自动重发"})
        return True

    def start(self) -> None:
        def run():
            while not self.stop.is_set():
                try:
                    if not self.once():
                        self.stop.wait(0.25)
                except Exception:
                    (self.store.root / "RECOVERY_REQUIRED").touch(mode=0o600)
                    self.stop.set()
        self.thread = threading.Thread(target=run, name="stocktalk-worker")
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join()
