"""SQLite ledger: short transactions, immutable artifacts, explicit recovery."""
from __future__ import annotations

from contextlib import contextmanager
import builtins
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Conflict(ValueError):
    pass


class Store:
    EXACT_FILTERS = ("type_id", "stock", "status")
    DATE_FILTERS = (("since", ">="), ("until", "<="))
    PAGE_DEFAULTS = {"limit": 50, "offset": 0}

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / "tasks.sqlite3"
        with self.connection() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise Conflict("数据库版本不兼容；请先备份并使用匹配版本启动")
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS task (
              id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
              stock TEXT NOT NULL, type_id TEXT NOT NULL, model TEXT NOT NULL,
              snapshot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
              stage TEXT NOT NULL DEFAULT 'queued', attempt INTEGER NOT NULL DEFAULT 0,
              result TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE INDEX IF NOT EXISTS task_filter ON task(type_id,stock,status,created_at);
            CREATE TABLE IF NOT EXISTS artifact (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES task(id),
              path TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS publication (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES task(id),
              platform TEXT NOT NULL, account TEXT NOT NULL, options TEXT NOT NULL,
              retry_of TEXT REFERENCES publication(id), status TEXT NOT NULL DEFAULT 'pending',
              report TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE UNIQUE INDEX IF NOT EXISTS pub_active ON publication(task_id,platform,account)
              WHERE status IN ('pending','submitting','succeeded','outcome_unknown');
            PRAGMA user_version=1;
            ''')

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, key: str, request: dict[str, Any], snapshot: dict[str, Any]) -> tuple[str, bool]:
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,fingerprint FROM task WHERE idem=?", (key,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise Conflict("幂等键已用于不同输入")
                return row["id"], False
            task_id = uuid4().hex
            db.execute("INSERT INTO task(id,idem,fingerprint,stock,type_id,model,snapshot) VALUES(?,?,?,?,?,?,?)",
                       (task_id, key, fingerprint, request["stock"], request["type_id"], request["model"], json.dumps(snapshot)))
            return task_id, True

    def get(self, task_id: str, *, private: bool = False) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
            for k in ("snapshot", "result", "error"):
                data[k] = json.loads(data[k]) if data[k] else None
            data["artifacts"] = [dict(r) for r in db.execute("SELECT * FROM artifact WHERE task_id=? ORDER BY path", (task_id,))]
            if not private:
                data.pop("snapshot")
                data.pop("fingerprint")
                data.pop("idem")
                result = data.get("result") or {}
                data["result"] = {k: result.get(k) for k in ("stock_name", "approved_script", "canvas", "platform_canvas", "elapsed_seconds")}
                for a in data["artifacts"]:
                    a["name"] = Path(a.pop("path")).name
            return data

    def list(self, filters: dict[str, str]) -> list[dict[str, Any]]:
        supported = {*self.EXACT_FILTERS, *dict(self.DATE_FILTERS), *self.PAGE_DEFAULTS}
        if filters.keys() - supported:
            raise ValueError("任务筛选仅接受 " + "/".join(sorted(supported)))
        where, values = [], []
        for field in self.EXACT_FILTERS:
            if filters.get(field):
                where.append(f"{field}=?")
                values.append(filters[field])
        for key, op in self.DATE_FILTERS:
            if filters.get(key):
                where.append(f"created_at {op} ?")
                values.append(filters[key])
        page = {key: int(filters.get(key, default)) for key, default in self.PAGE_DEFAULTS.items()}
        limit = max(1, min(page["limit"], 100))
        offset = max(0, page["offset"])
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT id,stock,type_id,model,status,stage,created_at FROM task" +
                    (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ? OFFSET ?", (*values, limit, offset))]

    def claim(self) -> dict[str, Any] | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM task WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE task SET status='running',stage='generation',attempt=attempt+1 WHERE id=? AND status='queued'", (row["id"],))
        return self.get(row["id"], private=True)

    def stage(self, task_id: str, stage: str) -> None:
        with self.connection() as db:
            db.execute("UPDATE task SET stage=? WHERE id=? AND status='running'", (stage, task_id))

    def fail(self, task_id: str, error: dict[str, Any]) -> None:
        with self.connection() as db:
            db.execute("UPDATE task SET status='failed',stage='failed',error=? WHERE id=?", (json.dumps(error), task_id))

    def complete(self, task_id: str, result: dict[str, Any], directory: Path) -> None:
        entries = []
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("symlink artifact rejected")
            if path.is_file():
                entries.append((uuid4().hex, task_id, str(path.relative_to(self.root)), path.suffix.lstrip("."),
                                path.stat().st_size, _sha256(path)))
        with self.connection() as db:
            db.executemany("INSERT INTO artifact VALUES(?,?,?,?,?,?)", entries)
            db.execute("UPDATE task SET status='succeeded',stage='review',result=? WHERE id=?", (json.dumps(result), task_id))

    def artifact(self, artifact_id: str, task_id: str) -> Path | None:
        with self.connection() as db:
            row = db.execute("SELECT path FROM artifact WHERE id=? AND task_id=?", (artifact_id, task_id)).fetchone()
        if not row:
            return None
        path = (self.root / row["path"]).resolve()
        if not path.is_relative_to(self.root / "artifacts") or not path.is_file():
            return None
        return path

    def publications(self, task_id: str) -> builtins.list[dict[str, Any]]:
        with self.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM publication WHERE task_id=? ORDER BY created_at", (task_id,))]
        for r in rows:
            r["options"] = json.loads(r["options"])
            r["report"] = json.loads(r["report"]) if r["report"] else None
        return rows

    def publish(self, task_id: str, platform: str, account: str, options: dict[str, Any], retry_of=None) -> str:
        publication_id = uuid4().hex
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if retry_of:
                old = db.execute("SELECT * FROM publication WHERE id=? AND task_id=?", (retry_of, task_id)).fetchone()
                if not old or old["platform"] != platform or old["account"] != account or old["status"] not in ("failed", "action_required"):
                    raise Conflict("只能补发同任务、同账号下明确失败的平台；未知结果须先人工核对")
            elif db.execute("SELECT 1 FROM publication WHERE task_id=? AND platform=? AND account=?", (task_id, platform, account)).fetchone():
                raise Conflict("已有发布记录，请选择失败记录补发")
            try:
                db.execute("INSERT INTO publication(id,task_id,platform,account,options,retry_of) VALUES(?,?,?,?,?,?)",
                           (publication_id, task_id, platform, account, json.dumps(options), retry_of))
            except sqlite3.IntegrityError:
                raise Conflict("已有活动、成功或结果未知的发布记录") from None
        return publication_id

    def claim_publication(self) -> dict[str, Any] | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM publication WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE publication SET status='submitting' WHERE id=? AND status='pending'", (row["id"],))
        data = dict(row)
        data["options"] = json.loads(data["options"])
        return data

    def finish_publication(self, publication_id: str, status: str, report: dict[str, Any]) -> None:
        with self.connection() as db:
            db.execute("UPDATE publication SET status=?,report=? WHERE id=?", (status, json.dumps(report), publication_id))

    def recover(self) -> bool:
        with self.connection() as db:
            interrupted = db.execute("SELECT COUNT(*) FROM task WHERE status='running'").fetchone()[0]
            submitting = db.execute("SELECT COUNT(*) FROM publication WHERE status='submitting'").fetchone()[0]
            db.execute("UPDATE task SET status='interrupted',stage='interrupted',error=? WHERE status='running'",
                       (json.dumps({"code": "INTERRUPTED", "message": "执行中断，请确认旧子进程退出后重试"}),))
            db.execute("UPDATE publication SET status='outcome_unknown',report=? WHERE status='submitting'",
                       (json.dumps({"code": "OUTCOME_UNKNOWN", "message": "提交中断，请人工核对远端结果；禁止自动补发"}),))
        marker = self.root / "RECOVERY_REQUIRED"
        if interrupted or submitting:
            marker.touch(mode=0o600)
        return marker.exists()
