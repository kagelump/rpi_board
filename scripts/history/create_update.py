"""Validate, render, archive, and optionally publish externally authored updates."""
from __future__ import annotations

import base64
import binascii
import copy
import fcntl
import io
import json
import math
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError

from scripts.common import ROOT, absolute_path, load_settings, write_json
from scripts.render.compose_board import render_board
from scripts.render.palette_quantize import quantize_board

MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
DELIVERY_SCRIPT = ROOT / "scripts" / "display" / "push_to_epd.py"
DELIVERY_TIMEOUT_SECONDS = 300

TEXT = {"type": "string", "maxLength": 4000}
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Externally authored board update",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "publish": {"type": "boolean", "default": True},
        "payload": {
            "type": "object", "additionalProperties": True,
            "description": "Full native board payload; additional fields are preserved in history.",
            "properties": {
                "brief": {"type": "object", "additionalProperties": True, "properties": {
                    **{k: TEXT for k in ("headline", "subtitle", "tomorrow_preview", "illustration_prompt",
                                        "mood", "event_ref", "rain_level", "rain_window", "temp_range")},
                    "accent": {"type": "string", "enum": ["black", "none", "red", "yellow"]},
                    "bullets": {"type": "array", "items": TEXT},
                    "layout_emphasis": {"type": "object"},
                }},
                "day_context": {"type": "object", "additionalProperties": True, "properties": {
                    **{k: TEXT for k in ("date_pretty", "date_iso", "target_date_iso", "run_date_iso", "daypart_role")},
                }},
                "today": {"type": "object", "additionalProperties": True, "properties": {
                    "daily_summary": {"type": "object", "additionalProperties": True, "properties": {
                        "date": {"type": "string", "format": "date"},
                        "condition": TEXT,
                        "weather_code": {"type": ["integer", "null"]},
                        "temp_min_c": {"type": "number", "minimum": -100, "maximum": 100},
                        "temp_max_c": {"type": "number", "minimum": -100, "maximum": 100},
                    }},
                }},
                "tomorrow": {"type": "object"},
                "brief_context": {"type": "object"},
                "timezone": {"type": "string", "description": "IANA timezone; defaults to device timezone."},
                "generated_at_local": {"type": "string", "format": "date-time"},
                "brief_source": TEXT,
                "location": {"type": "object"},
            },
        },
        "hero_image": {"type": "string", "contentEncoding": "base64",
                       "description": "PNG/JPEG artwork; omitted artwork uses the built-in weather pictogram."},
        "board_image": {"type": "string", "contentEncoding": "base64",
                        "description": "Complete PNG/JPEG board at device dimensions; bypasses composition."},
        "metadata": {"type": "object", "description": "Author, source, style, notes, or other provenance."},
    },
    "not": {"required": ["hero_image", "board_image"]},
    "if": {"not": {"required": ["board_image"]}},
    "then": {"required": ["payload"], "properties": {"payload": {
        "required": ["brief", "today"], "properties": {
            "brief": {"required": ["headline"]},
            "today": {"required": ["daily_summary"], "properties": {
                "daily_summary": {"required": ["date", "temp_min_c", "temp_max_c"]},
            }},
        },
    }}},
}


class UpdateBusy(Exception):
    pass


class UpdateFailed(Exception):
    def __init__(self, run_id, message):
        super().__init__(message)
        self.run_id = run_id


def _validate(value, schema, path="request"):
    """Validate the structural constraints used by our published schema."""
    types = schema.get("type")
    types = [types] if isinstance(types, str) else types
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "boolean": type(value) is bool,
               "integer": type(value) is int, "number": type(value) in (int, float),
               "null": value is None}
    if types and not any(matches[t] for t in types):
        raise ValueError(f"{path} must be {' or '.join(types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if key not in properties and schema.get("additionalProperties") is False:
                raise ValueError(f"unknown field: {path}.{key}")
            if key in properties:
                _validate(child, properties[key], f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for child in value:
            _validate(child, schema["items"], path + "[]")
    if isinstance(value, str) and len(value) > schema.get("maxLength", MAX_BODY_BYTES):
        raise ValueError(f"{path} is too long")
    if type(value) in (int, float):
        if not math.isfinite(value) or value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError(f"{path} is outside the allowed numeric range")


def _decode_image(encoded, name):
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ("PNG", "JPEG"):
                raise ValueError(f"{name} must contain PNG or JPEG data")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError(f"{name} exceeds {MAX_IMAGE_PIXELS} pixels")
            image.load()
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", image.size, "white")
            background.alpha_composite(rgba)
            return background.convert("RGB")
    except (binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError(f"invalid {name}: expected base64 PNG/JPEG") from error


def validate_request(request, settings):
    _validate(request, SCHEMA)
    if "hero_image" in request and "board_image" in request:
        raise ValueError("hero_image and board_image are mutually exclusive")
    if "board_image" not in request:
        _validate(request, SCHEMA["then"])
        if not request["payload"]["brief"]["headline"].strip():
            raise ValueError("payload.brief.headline must not be blank")
    payload = copy.deepcopy(request.get("payload", {}))
    timezone = payload.setdefault("timezone", settings["location"]["timezone"])
    try:
        now = datetime.now(ZoneInfo(timezone))
    except (KeyError, ValueError) as error:
        raise ValueError("payload.timezone must be an IANA timezone") from error
    payload.setdefault("generated_at_local", now.isoformat())
    try:
        generated = datetime.fromisoformat(payload["generated_at_local"])
        if generated.tzinfo is None:
            raise ValueError("timezone required")
        daily = payload.get("today", {}).get("daily_summary", {})
        if "date" in daily:
            date.fromisoformat(daily["date"])
        if "temp_min_c" in daily and "temp_max_c" in daily and daily["temp_min_c"] > daily["temp_max_c"]:
            raise ValueError("temp_min_c must not exceed temp_max_c")
    except (ValueError, TypeError) as error:
        raise ValueError(f"invalid payload date or temperature: {error}") from error
    payload.setdefault("brief_source", "external")
    image_name = "board_image" if "board_image" in request else "hero_image"
    image = _decode_image(request[image_name], image_name) if image_name in request else None
    if image_name == "board_image" and image.size != (settings["display"]["width"], settings["display"]["height"]):
        raise ValueError("board_image must match the configured display width and height")
    return payload, image, image_name


@contextmanager
def update_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UpdateBusy("another update is in progress; retry later") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _delivery_child_settings(settings, mode=None):
    """Build the strict allowlisted config for the isolated delivery child.

    Only the display mode and the two Waveshare import candidate lists are read
    by :mod:`scripts.display.push_to_epd`. The shape is constructed explicitly
    so the full runtime settings object -- which may hold OpenRouter/FAL
    credentials or other auth configuration -- is never copied into the
    temporary child file. Do not replace this with a full or recursively
    sanitised settings object.
    """
    display = settings.get("display", {})
    return {
        "display": {
            "mode": mode or display.get("mode"),
            "waveshare_module_candidates": list(display.get("waveshare_module_candidates", [])),
            "waveshare_python_lib_candidates": list(display.get("waveshare_python_lib_candidates", [])),
        }
    }


def push_image(settings, image_path, mode=None):
    """Deliver the final board from an isolated, short-lived child process.

    The Waveshare/lgpio stack claims GPIO lines (and ``/dev/gpiochip0``) while
    the driver module is imported or initialised. ``POST /create_update`` runs
    inside the long-lived history server, so initialising the driver in-process
    would pin those resources for the server's lifetime and make the next
    scheduled preflight/display fail with ``GPIO busy``. A dedicated
    interpreter guarantees the kernel releases every handle when the child
    exits, including after import, initialisation, or display failures.

    The child receives only the non-secret display config built by
    :func:`_delivery_child_settings`; the board image path is passed
    separately on the command line. This remains the module-level
    ``push_image`` seam so the rest of the pipeline (and tests) can substitute
    delivery without touching the render, archive, or locking behaviour.
    """
    target = Path(image_path).resolve()
    child_settings = _delivery_child_settings(settings, mode)
    with tempfile.TemporaryDirectory(prefix="board-delivery-") as tmp:
        settings_file = Path(tmp) / "settings.json"
        settings_file.write_text(json.dumps(child_settings, ensure_ascii=True), encoding="utf-8")
        command = [
            sys.executable or "python3",
            str(DELIVERY_SCRIPT),
            "--settings-file", str(settings_file),
            "--input", str(target),
        ]
        if mode:
            command.extend(["--mode", mode])
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=DELIVERY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"display delivery timed out after {DELIVERY_TIMEOUT_SECONDS}s"
            ) from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = " | ".join(line for line in detail[-5:] if line)
        raise RuntimeError(f"display delivery failed (exit {completed.returncode}): {tail}")
    return completed


class UpdateService:
    def __init__(self, store, settings=None, lock_path=None):
        self.store = store
        self.settings = settings
        self.lock_path = lock_path or ROOT / "runtime/update.lock"

    def create(self, request):
        settings = copy.deepcopy(self.settings or load_settings())
        payload, image, image_name = validate_request(request, settings)
        with update_lock(self.lock_path):
            return self._create(request, payload, image, image_name, settings)

    def _create(self, request, payload, image, image_name, settings):
        store = self.store
        run_id = store.start_run(settings=settings)
        day = payload.get("day_context", {})
        summary = {"brief_source": "external", "image_provider": "external",
                   "target_date": day.get("target_date_iso") or payload.get("today", {}).get("daily_summary", {}).get("date"),
                   "daypart_role": day.get("daypart_role", "external"), "timezone": payload["timezone"],
                   "headline": payload.get("brief", {}).get("headline"), "published": False}
        stage_id = None
        try:
            store.add_snapshot(run_id, "external_update", {
                "payload": request.get("payload", {}), "metadata": request.get("metadata", {}),
                "publish": request.get("publish", True), "image_kind": image_name if image else None,
            })
            store.add_snapshot(run_id, "generated_brief", payload)
            with tempfile.TemporaryDirectory(prefix="board-update-") as tmp:
                work = Path(tmp)
                isolated = copy.deepcopy(settings)
                isolated["runtime"].update({"hero_file": str(work / "hero.png"),
                                            "stale_file": str(work / "last_success.json")})
                final, preview = work / "final.png", work / "preview.png"
                stage_id = store.begin_stage(run_id, "compose_board")
                if image is not None:
                    upload_path = work / ("board.png" if image_name == "board_image" else "hero.png")
                    image.save(upload_path)
                    store.add_artifact(run_id, "uploaded_board" if image_name == "board_image" else "hero", upload_path)
                if image_name == "board_image":
                    image.save(final)
                else:
                    render_board(isolated, payload, final, preview)
                store.add_artifact(run_id, "final_display_pre_quantize", final)
                store.end_stage(stage_id, status="succeeded", exit_code=0)
                stage_id = store.begin_stage(run_id, "quantize_palette")
                quantize_board(isolated, final, final)
                with Image.open(final) as board:
                    board.resize((board.width // 2, board.height // 2)).save(preview)
                artifacts = {}
                for kind, path in (("final_display", final), ("preview", preview)):
                    artifact_id = store.add_artifact(run_id, kind, path)
                    artifacts[kind] = f"/api/artifacts/{artifact_id}"
                store.end_stage(stage_id, status="succeeded", exit_code=0)
                stage_id = None
                if request.get("publish", True):
                    stage_id = store.begin_stage(run_id, "push_to_display")
                    push_image(settings, final)
                    # Only update live outputs after hardware delivery succeeds.
                    # Scheduled brief/hero caches remain independent of external submissions.
                    for key, path in (("final_file", final), ("preview_file", preview)):
                        destination = absolute_path(settings["runtime"][key])
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        temporary = destination.with_name(destination.name + ".new")
                        temporary.write_bytes(path.read_bytes())
                        temporary.replace(destination)
                    write_json(settings["runtime"]["stale_file"], {
                        "last_success_at": payload["generated_at_local"], "brief_source": "external", "run_id": run_id,
                    })
                    summary["published"] = True
                    store.end_stage(stage_id, status="succeeded", exit_code=0)
                    stage_id = None
            store.finish_run(run_id, summary=summary)
            return {"run_id": run_id, "status": "succeeded", "published": summary["published"],
                    "display_mode": settings["display"]["mode"], "run_url": f"/api/runs/{run_id}",
                    "artifacts": artifacts}
        except Exception as error:
            if stage_id:
                store.end_stage(stage_id, status="failed", exit_code=1, message=str(error))
            store.finish_run(run_id, status="failed", error_summary=str(error), summary=summary)
            raise UpdateFailed(run_id, str(error)) from error
