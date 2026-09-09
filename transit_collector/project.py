from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class TaskState:
    NEW = "NEW"
    READY = "READY"
    DISCOVERING = "DISCOVERING"
    CRAWLING = "CRAWLING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    POSTPROCESSING = "POSTPROCESSING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"

    RUNNING = {DISCOVERING, CRAWLING, PAUSING, POSTPROCESSING}

    LABELS = {
        NEW: "未配置",
        READY: "可以开始",
        DISCOVERING: "正在发现公交线路",
        CRAWLING: "正在采集线路与站点",
        PAUSING: "正在安全停止",
        PAUSED: "已暂停",
        POSTPROCESSING: "正在整理公交站点",
        COMPLETED: "已完成",
        PARTIAL: "部分数据",
        FAILED: "发生错误",
    }


@dataclass
class ProjectManifest:
    name: str
    database: str
    city: str = ""
    source: str = ""
    state: str = TaskState.NEW
    data_status: str = "unknown"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    app_version: str = "0.4.0"
    schema_version: int = 1
    merge_radius_m: float | None = None
    last_run: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "database": self.database,
            "city": self.city,
            "source": self.source,
            "state": self.state,
            "data_status": self.data_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "app_version": self.app_version,
            "schema_version": self.schema_version,
            "merge_radius_m": self.merge_radius_m,
            "last_run": self.last_run,
            "artifacts": self.artifacts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectManifest":
        known = {
            "name", "database", "city", "source", "state", "data_status",
            "created_at", "updated_at", "app_version", "schema_version",
            "merge_radius_m", "last_run", "artifacts",
        }
        values = {k: v for k, v in data.items() if k in known}
        return cls(**values)


class ProjectManager:
    """Small project/state layer around an existing SQLite transit database.

    v0.4 keeps the existing database location for backward compatibility and
    creates a side project directory next to it. That means old v0.3 databases
    can be opened directly without moving or copying large files.
    """

    def __init__(self, manifest_path: Path, manifest: ProjectManifest):
        self.manifest_path = Path(manifest_path)
        self.root = self.manifest_path.parent
        self.manifest = manifest
        self.logs_dir = self.root / "logs"
        self.exports_dir = self.root / "exports"
        self.gis_dir = self.root / "gis"
        self.backups_dir = self.root / "backups"
        for p in (self.logs_dir, self.exports_dir, self.gis_dir, self.backups_dir):
            p.mkdir(parents=True, exist_ok=True)

    @property
    def database_path(self) -> Path:
        return Path(self.manifest.database)

    @classmethod
    def for_database(
        cls,
        database: str | Path,
        *,
        name: str = "",
        city: str = "",
        source: str = "",
    ) -> "ProjectManager":
        db = Path(database).expanduser().resolve()
        root = db.parent / f"{db.stem}_project"
        manifest_path = root / "project.json"
        if manifest_path.exists():
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = ProjectManifest.from_dict(data)
            # The database may have been moved together with its project folder.
            manifest.database = str(db)
            if name:
                manifest.name = name
            if city:
                manifest.city = city
            if source:
                manifest.source = source
        else:
            manifest = ProjectManifest(
                name=name or db.stem,
                database=str(db),
                city=city,
                source=source,
            )
        manager = cls(manifest_path, manifest)
        manager.save()
        return manager

    @classmethod
    def open_manifest(cls, path: str | Path) -> "ProjectManager":
        manifest_path = Path(path).expanduser().resolve()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manager = cls(manifest_path, ProjectManifest.from_dict(data))
        if not manager.database_path.exists():
            raise FileNotFoundError(f"项目数据库不存在：{manager.database_path}")
        return manager

    def save(self) -> None:
        self.manifest.updated_at = _now()
        _write_json_atomic(self.manifest_path, self.manifest.to_dict())

    def update_identity(self, *, name: str | None = None, city: str | None = None, source: str | None = None) -> None:
        if name:
            self.manifest.name = name
        if city is not None:
            self.manifest.city = city
        if source is not None:
            self.manifest.source = source
        self.save()

    def mark_state(self, state: str, *, data_status: str | None = None) -> None:
        self.manifest.state = state
        if data_status is not None:
            self.manifest.data_status = data_status
        self.save()

    def start_run(self, task_type: str, parameters: dict[str, Any]) -> tuple[str, Path]:
        run_id = uuid.uuid4().hex[:12]
        log_path = self.logs_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{task_type}_{run_id}.log"
        run = {
            "run_id": run_id,
            "task_type": task_type,
            "started_at": _now(),
            "finished_at": None,
            "status": "running",
            "parameters": parameters,
            "log": str(log_path),
        }
        self.manifest.last_run = run
        self.save()
        log_path.write_text(
            "公交线路与站点 GIS 采集分析器\n"
            f"run_id={run_id}\n"
            f"task_type={task_type}\n"
            f"started_at={run['started_at']}\n"
            f"parameters={json.dumps(parameters, ensure_ascii=False)}\n\n",
            encoding="utf-8",
        )
        return run_id, log_path

    def append_log(self, text: str) -> None:
        run = self.manifest.last_run or {}
        log = run.get("log")
        if not log:
            return
        with Path(log).open("a", encoding="utf-8") as fh:
            fh.write(str(text).rstrip() + "\n")

    def finish_run(self, status: str, summary: str = "") -> None:
        run = self.manifest.last_run
        if not run:
            return
        run["finished_at"] = _now()
        run["status"] = status
        if summary:
            run["summary"] = summary
        self.append_log(f"\nfinished_at={run['finished_at']}\nstatus={status}\nsummary={summary}")
        self.save()

    def register_artifact(
        self,
        kind: str,
        path: str | Path,
        *,
        parameters: dict[str, Any] | None = None,
        source_signature: str = "",
        status: str = "current",
    ) -> None:
        p = Path(path).resolve()
        record = {
            "artifact_id": uuid.uuid4().hex[:12],
            "kind": kind,
            "path": str(p),
            "created_at": _now(),
            "parameters": parameters or {},
            "source_signature": source_signature,
            "status": status,
        }
        self.manifest.artifacts.append(record)
        # Keep the manifest bounded while retaining a useful history.
        self.manifest.artifacts = self.manifest.artifacts[-200:]
        self.save()

    def backup_database(self, note: str = "backup", keep: int = 5) -> Path | None:
        db = self.database_path
        if not db.exists():
            return None
        safe_note = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in note)[:40] or "backup"
        target = self.backups_dir / f"{db.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_note}{db.suffix}"
        shutil.copy2(db, target)
        backups = sorted(self.backups_dir.glob(f"{db.stem}_*{db.suffix}"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[max(1, keep):]:
            try:
                old.unlink()
            except OSError:
                pass
        return target
