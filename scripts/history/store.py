#!/usr/bin/env python3
"""Append-only storage for weather-board generation history.

Every state change is an immutable, schema-versioned JSON object on one line of
the ledger. Binary artifacts are copied into per-run directories. Query indexes
are derived in memory from the ledger, which is intentionally practical for the
board's low write volume (roughly three generations per day).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from scripts.common import ROOT, absolute_path, load_settings


SCHEMA_VERSION = 1
EVENT_TYPES = {
    "ledger_initialized",
    "run_started",
    "run_updated",
    "run_finished",
    "stage_started",
    "stage_finished",
    "log_recorded",
    "snapshot_added",
    "artifact_added",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _redact_settings(settings: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(settings))
    for section in redacted.values():
        if not isinstance(section, dict):
            continue
        for key in list(section):
            if key.lower() in {"api_key", "token", "password", "secret"}:
                section[key] = "[redacted]"
    return redacted


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class GenerationStore:
    """Append-only ledger with derived indexes and immutable artifact storage."""

    def __init__(
        self,
        record_file: str | Path | None = None,
        artifacts_dir: str | Path | None = None,
        **legacy_kwargs: Any,
    ):
        # Accept db_path during the transition so callers get a useful ledger at
        # the requested test/custom location instead of failing mysteriously.
        record_file = record_file or legacy_kwargs.pop("db_path", None)
        if legacy_kwargs:
            raise TypeError(f"Unexpected arguments: {', '.join(legacy_kwargs)}")
        settings = load_settings()
        runtime = settings.get("runtime", {})
        self.record_file = absolute_path(str(record_file or runtime.get(
            "generation_record_file", "runtime/generations.jsonl")))
        self.artifacts_dir = absolute_path(str(artifacts_dir or runtime.get(
            "generation_artifacts_dir", "runtime/generations")))
        self.record_file.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._cache_lock = threading.RLock()
        self._cache_signature: tuple[int, int] | None = None
        self._cache_events: list[dict[str, Any]] = []
        self._initialize()

    @property
    def db_path(self) -> Path:
        """Backward-compatible display name for older callers."""
        return self.record_file

    def _initialize(self) -> None:
        if self.record_file.exists() and self.record_file.stat().st_size:
            return
        self._append_event("ledger_initialized", None, {
            "description": "Weather Board generation history",
            "event_types": sorted(EVENT_TYPES),
        })

    def _append_event(self, event_type: str, run_id: str | None, data: dict[str, Any]) -> str:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown ledger event type: {event_type}")
        event_id = uuid.uuid4().hex
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "recorded_at": utc_now(),
            "type": event_type,
            "run_id": run_id,
            "data": data,
        }
        line = (_json(event) + "\n").encode("utf-8")
        with self.record_file.open("a+b", buffering=0) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() > 0:
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        # Preserve a partial crash record as an invalid line and
                        # start the next valid event on a clean boundary.
                        handle.write(b"\n")
                handle.write(line)
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with self._cache_lock:
            self._cache_signature = None
        return event_id

    def _load_events(self) -> list[dict[str, Any]]:
        try:
            stat = self.record_file.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            return []
        with self._cache_lock:
            if signature == self._cache_signature:
                return self._cache_events
            events: list[dict[str, Any]] = []
            with self.record_file.open("rb") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                try:
                    for raw_line in handle:
                        try:
                            event = json.loads(raw_line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            # Ignore an incomplete record after power loss.
                            continue
                        if (
                            isinstance(event, dict)
                            and event.get("type") in EVENT_TYPES
                            and isinstance(event.get("schema_version"), int)
                        ):
                            events.append(event)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            self._cache_events = events
            self._cache_signature = signature
            return events

    def _materialize(self) -> dict[str, Any]:
        runs: dict[str, dict[str, Any]] = {}
        stages: dict[str, dict[str, Any]] = {}
        snapshots: dict[str, dict[str, Any]] = {}
        artifacts: dict[str, dict[str, Any]] = {}
        logs: dict[str, dict[str, Any]] = {}

        for event in self._load_events():
            event_type = event["type"]
            run_id = event.get("run_id")
            data = event.get("data", {})
            event_id = event["event_id"]
            if event_type == "run_started":
                runs.setdefault(run_id, {
                    "id": run_id,
                    "started_at": data.get("started_at") or event["recorded_at"],
                    "completed_at": None,
                    "status": data.get("status", "running"),
                    "target_date": None,
                    "run_date": None,
                    "daypart_role": None,
                    "timezone": None,
                    "forced": bool(data.get("forced")),
                    "forecast_target": data.get("forecast_target"),
                    "git_revision": data.get("git_revision"),
                    "brief_source": None,
                    "selected_style": None,
                    "image_provider": None,
                    "error_summary": None,
                    "settings": data.get("settings", {}),
                    "summary": {},
                })
            elif event_type == "run_updated" and run_id in runs:
                runs[run_id].update(data)
            elif event_type == "run_finished" and run_id in runs:
                runs[run_id].update(data)
                runs[run_id]["completed_at"] = data.get("completed_at") or event["recorded_at"]
            elif event_type == "stage_started":
                stages[event_id] = {
                    "id": event_id,
                    "run_id": run_id,
                    "stage_name": data["stage_name"],
                    "sequence": data["sequence"],
                    "started_at": data.get("started_at") or event["recorded_at"],
                    "completed_at": None,
                    "status": "running",
                    "exit_code": None,
                    "duration_ms": None,
                    "message": None,
                    "details": data.get("details", {}),
                }
            elif event_type == "stage_finished":
                stage_id = data.get("stage_id")
                if stage_id in stages:
                    stages[stage_id].update({k: v for k, v in data.items() if k != "stage_id"})
            elif event_type == "log_recorded":
                logs[event_id] = {
                    "id": event_id,
                    "run_id": run_id,
                    "occurred_at": data.get("occurred_at") or event["recorded_at"],
                    "level": data.get("level", "info"),
                    "component": data.get("component", "pipeline"),
                    "event_type": data.get("event_type", "message"),
                    "message": data.get("message"),
                    "data": data.get("data", {}),
                }
            elif event_type == "snapshot_added":
                snapshots[event_id] = {"id": event_id, "run_id": run_id, **data}
            elif event_type == "artifact_added":
                artifacts[event_id] = {"id": event_id, "run_id": run_id, **data}

        return {
            "runs": runs,
            "stages": stages,
            "logs": logs,
            "snapshots": snapshots,
            "artifacts": artifacts,
        }

    def start_run(
        self,
        *,
        run_id: str | None = None,
        forced: bool = False,
        forecast_target: str | None = None,
        started_at: str | None = None,
        status: str = "running",
        settings: dict[str, Any] | None = None,
    ) -> str:
        run_id = run_id or uuid.uuid4().hex
        if self.get_run(run_id) is not None:
            return run_id
        settings = settings or load_settings()
        self._append_event("run_started", run_id, {
            "started_at": started_at or utc_now(),
            "status": status,
            "forced": bool(forced),
            "forecast_target": forecast_target,
            "git_revision": _git_revision(),
            "settings": _redact_settings(settings),
        })
        return run_id

    def begin_stage(self, run_id: str, stage_name: str, details: Any = None) -> str:
        state = self._materialize()
        sequence = 1 + max(
            (item["sequence"] for item in state["stages"].values() if item["run_id"] == run_id),
            default=0,
        )
        return self._append_event("stage_started", run_id, {
            "stage_name": stage_name,
            "sequence": sequence,
            "started_at": utc_now(),
            "details": details or {},
        })

    def end_stage(
        self,
        stage_id: str,
        *,
        status: str,
        exit_code: int | None = None,
        message: str | None = None,
        details: Any = None,
    ) -> None:
        state = self._materialize()
        stage = state["stages"].get(stage_id)
        if stage is None:
            raise KeyError(f"Unknown stage event: {stage_id}")
        completed_at = utc_now()
        try:
            duration_ms = max(0, int((
                datetime.fromisoformat(completed_at) - datetime.fromisoformat(stage["started_at"])
            ).total_seconds() * 1000))
        except (TypeError, ValueError):
            duration_ms = None
        merged = dict(stage.get("details") or {})
        if details:
            merged.update(details if isinstance(details, dict) else {"value": details})
        self._append_event("stage_finished", stage["run_id"], {
            "stage_id": stage_id,
            "completed_at": completed_at,
            "status": status,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "message": message,
            "details": merged,
        })

    def log(
        self,
        run_id: str,
        *,
        component: str,
        event_type: str,
        message: str | None = None,
        level: str = "info",
        data: Any = None,
    ) -> str:
        return self._append_event("log_recorded", run_id, {
            "occurred_at": utc_now(),
            "level": level,
            "component": component,
            "event_type": event_type,
            "message": message,
            "data": data or {},
        })

    def add_snapshot(
        self,
        run_id: str,
        kind: str,
        payload: Any,
        *,
        source_path: str | None = None,
        content_type: str = "application/json",
    ) -> str:
        if isinstance(payload, str):
            text = payload
            if content_type == "application/json":
                content_type = "text/plain; charset=utf-8"
        else:
            text = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
        encoded = text.encode("utf-8")
        return self._append_event("snapshot_added", run_id, {
            "captured_at": utc_now(),
            "kind": kind,
            "source_path": source_path,
            "content_type": content_type,
            "sha256": _sha256(encoded),
            "byte_size": len(encoded),
            "payload_text": text,
        })

    def snapshot_file(self, run_id: str, kind: str, path: str | Path) -> str | None:
        source = absolute_path(str(path))
        if not source.exists() or not source.is_file():
            return None
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
            return self.add_snapshot(run_id, kind, payload, source_path=str(source))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.add_snapshot(
                run_id,
                kind,
                source.read_text(encoding="utf-8", errors="replace"),
                source_path=str(source),
                content_type="text/plain; charset=utf-8",
            )

    def add_artifact(self, run_id: str, kind: str, path: str | Path) -> str | None:
        source = absolute_path(str(path))
        if not source.exists() or not source.is_file():
            return None
        data = source.read_bytes()
        digest = _sha256(data)
        state = self._materialize()
        for artifact in state["artifacts"].values():
            if artifact["run_id"] == run_id and artifact["kind"] == kind and artifact["sha256"] == digest:
                return artifact["id"]
        run_dir = self.artifacts_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower() or ".bin"
        destination = run_dir / f"{kind}-{digest[:12]}{suffix}"
        if not destination.exists():
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            shutil.copyfile(source, temporary)
            os.replace(temporary, destination)
        mime_type = mimetypes.guess_type(destination.name)[0] or "application/octet-stream"
        width = height = None
        if mime_type.startswith("image/"):
            try:
                with Image.open(destination) as image:
                    width, height = image.size
            except OSError:
                pass
        stored_path = str(destination.relative_to(ROOT)) if destination.is_relative_to(ROOT) else str(destination)
        return self._append_event("artifact_added", run_id, {
            "captured_at": utc_now(),
            "kind": kind,
            "source_path": str(source),
            "stored_path": stored_path,
            "mime_type": mime_type,
            "sha256": digest,
            "byte_size": len(data),
            "width": width,
            "height": height,
        })

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "succeeded",
        error_summary: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        summary = dict(summary or {})
        state = self._materialize()
        failed_stages = any(
            stage["run_id"] == run_id and stage["status"] == "failed"
            for stage in state["stages"].values()
        )
        error_logs = any(
            log["run_id"] == run_id and log["level"] == "error"
            for log in state["logs"].values()
        )
        if status == "succeeded" and (failed_stages or error_logs):
            status = "degraded"
        finished = {
            "completed_at": utc_now(),
            "status": status,
            "error_summary": error_summary,
            "summary": summary,
        }
        for key in (
            "target_date", "run_date", "daypart_role", "timezone", "brief_source",
            "selected_style", "image_provider",
        ):
            if summary.get(key) is not None:
                finished[key] = summary[key]
        self._append_event("run_finished", run_id, finished)

    def update_run_summary(self, run_id: str, **values: Any) -> None:
        allowed = {
            "target_date", "run_date", "daypart_role", "timezone", "brief_source",
            "selected_style", "image_provider", "error_summary",
        }
        self._append_event("run_updated", run_id, {
            key: value for key, value in values.items() if key in allowed
        })

    def list_runs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        target_date: str | None = None,
        status: str | None = None,
        style: str | None = None,
    ) -> dict[str, Any]:
        state = self._materialize()
        stage_counts: dict[str, int] = {}
        artifact_counts: dict[str, int] = {}
        for item in state["stages"].values():
            stage_counts[item["run_id"]] = stage_counts.get(item["run_id"], 0) + 1
        for item in state["artifacts"].values():
            artifact_counts[item["run_id"]] = artifact_counts.get(item["run_id"], 0) + 1
        items = list(state["runs"].values())
        if target_date:
            items = [item for item in items if item.get("target_date") == target_date]
        if status:
            items = [item for item in items if item.get("status") == status]
        if style:
            items = [item for item in items if item.get("selected_style") == style]
        items.sort(key=lambda item: item.get("started_at") or "", reverse=True)
        total = len(items)
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        page = []
        for item in items[offset:offset + limit]:
            row = dict(item)
            # Full settings are available on run detail; omit them from list
            # responses so a long history stays compact.
            row.pop("settings", None)
            row["stage_count"] = stage_counts.get(item["id"], 0)
            row["artifact_count"] = artifact_counts.get(item["id"], 0)
            page.append(row)
        return {"items": page, "total": total, "limit": limit, "offset": offset}

    def get_run(self, run_id: str, include_payloads: bool = False) -> dict[str, Any] | None:
        state = self._materialize()
        run = state["runs"].get(run_id)
        if run is None:
            return None
        result = dict(run)
        result["stages"] = sorted(
            (dict(item) for item in state["stages"].values() if item["run_id"] == run_id),
            key=lambda item: (item["sequence"], item["id"]),
        )
        result["logs"] = sorted(
            (dict(item) for item in state["logs"].values() if item["run_id"] == run_id),
            key=lambda item: (item["occurred_at"], item["id"]),
        )
        snapshots = sorted(
            (dict(item) for item in state["snapshots"].values() if item["run_id"] == run_id),
            key=lambda item: (item["captured_at"], item["id"]),
        )
        if not include_payloads:
            for item in snapshots:
                item.pop("payload_text", None)
        result["snapshots"] = snapshots
        result["artifacts"] = sorted(
            (dict(item) for item in state["artifacts"].values() if item["run_id"] == run_id),
            key=lambda item: (item["captured_at"], item["id"]),
        )
        return result

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        item = self._materialize()["snapshots"].get(str(snapshot_id))
        if item is None:
            return None
        result = dict(item)
        text = result.pop("payload_text")
        if result.get("content_type") == "application/json":
            result["payload"] = _decode_json(text, text)
        else:
            result["payload"] = text
        return result

    def get_artifact(self, artifact_id: str) -> tuple[dict[str, Any], Path] | None:
        item = self._materialize()["artifacts"].get(str(artifact_id))
        if item is None:
            return None
        metadata = dict(item)
        path = absolute_path(metadata["stored_path"])
        try:
            path.resolve().relative_to(self.artifacts_dir.resolve())
        except ValueError:
            return None
        return metadata, path

    def stats(self) -> dict[str, Any]:
        state = self._materialize()
        runs = list(state["runs"].values())
        style_counts: dict[str, int] = {}
        failed_stages: dict[str, int] = {}
        for run in runs:
            if run.get("selected_style"):
                style_counts[run["selected_style"]] = style_counts.get(run["selected_style"], 0) + 1
        for stage in state["stages"].values():
            if stage["status"] == "failed":
                failed_stages[stage["stage_name"]] = failed_stages.get(stage["stage_name"], 0) + 1
        started = [run["started_at"] for run in runs if run.get("started_at")]
        return {
            "runs": len(runs),
            "succeeded": sum(run["status"] == "succeeded" for run in runs),
            "degraded": sum(run["status"] == "degraded" for run in runs),
            "failed": sum(run["status"] == "failed" for run in runs),
            "first_run": min(started) if started else None,
            "latest_run": max(started) if started else None,
            "styles": [
                {"name": name, "count": count}
                for name, count in sorted(style_counts.items(), key=lambda pair: (-pair[1], pair[0]))
            ],
            "failed_stages": [
                {"stage_name": name, "count": count}
                for name, count in sorted(failed_stages.items(), key=lambda pair: (-pair[1], pair[0]))
            ],
        }


def current_run_id() -> str | None:
    return os.environ.get("GENERATION_RUN_ID") or None


def record_current_snapshot(kind: str, payload: Any, **kwargs: Any) -> str | None:
    run_id = current_run_id()
    if not run_id:
        return None
    try:
        return GenerationStore().add_snapshot(run_id, kind, payload, **kwargs)
    except Exception as error:  # noqa: BLE001 - observability must not break the display
        print(f"[history] snapshot failed: {error}")
        return None


def record_current_log(
    component: str,
    event_type: str,
    message: str | None = None,
    *,
    level: str = "info",
    data: Any = None,
) -> str | None:
    run_id = current_run_id()
    if not run_id:
        return None
    try:
        return GenerationStore().log(
            run_id, component=component, event_type=event_type,
            message=message, level=level, data=data,
        )
    except Exception as error:  # noqa: BLE001
        print(f"[history] log failed: {error}")
        return None
