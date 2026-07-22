#!/usr/bin/env python3
"""CLI bridge used by the shell pipeline to record generation history."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from scripts.common import absolute_path, load_settings, read_json
from scripts.history.store import GenerationStore


STAGE_FILES = {
    "fetch_weather": [("weather_payload", "payload_file", "snapshot")],
    "fetch_yahoo_weather": [("yahoo_weather", "yahoo_weather_file", "snapshot")],
    "aggregate_weather_sources": [("brief_context", "brief_context_file", "snapshot")],
    "transform_weather": [("transformed_brief", "brief_file", "snapshot")],
    "fetch_day_context": [("day_context", "day_context_file", "snapshot")],
    "generate_brief": [
        ("generated_brief", "brief_file", "snapshot"),
        ("last_good_brief", "last_good_brief_file", "snapshot"),
    ],
    "generate_image": [
        ("image_style_state", "image_style_state_file", "snapshot"),
        ("hero", "hero_file", "artifact"),
    ],
    "compose_board": [("final_display_pre_quantize", "final_file", "artifact")],
    "quantize_palette": [
        ("final_display", "final_file", "artifact"),
        ("preview", "preview_file", "artifact"),
        ("last_success", "stale_file", "snapshot"),
    ],
}


def _runtime_path(settings, key):
    runtime = settings.get("runtime", {})
    value = runtime.get(key)
    if not value and key == "image_style_state_file":
        value = "runtime/image_style_state.json"
    return value


def _capture(store, run_id, stage):
    settings = load_settings()
    captured = []
    for kind, setting_key, record_type in STAGE_FILES.get(stage, []):
        path = _runtime_path(settings, setting_key)
        if not path:
            continue
        if record_type == "snapshot":
            record_id = store.snapshot_file(run_id, kind, path)
        else:
            record_id = store.add_artifact(run_id, kind, path)
        if record_id is not None:
            captured.append({"kind": kind, "type": record_type, "id": record_id})
    return captured


def _summary_from_runtime(settings):
    summary = {
        "image_provider": settings.get("pipeline", {}).get("image_provider"),
    }
    brief_path = _runtime_path(settings, "brief_file")
    if brief_path and absolute_path(brief_path).exists():
        try:
            payload = read_json(brief_path)
            day = payload.get("day_context", {})
            summary.update({
                "target_date": day.get("target_date_iso") or day.get("date_iso"),
                "run_date": day.get("run_date_iso"),
                "daypart_role": day.get("daypart_role"),
                "timezone": payload.get("timezone"),
                "brief_source": payload.get("brief_source"),
                "headline": payload.get("brief", {}).get("headline"),
                "illustration_prompt": payload.get("brief", {}).get("illustration_prompt"),
            })
        except (OSError, json.JSONDecodeError):
            pass
    style_path = _runtime_path(settings, "image_style_state_file")
    if style_path and absolute_path(style_path).exists():
        try:
            state = read_json(style_path)
            summary["selected_style"] = state.get("last_selected")
            summary["style_target_date"] = state.get("target_date")
        except (OSError, json.JSONDecodeError):
            pass
    return summary


def _import_legacy(store):
    settings = load_settings()
    history_path = _runtime_path(settings, "history_file")
    imported = 0
    if history_path and absolute_path(history_path).exists():
        try:
            entries = read_json(history_path)
        except (OSError, json.JSONDecodeError):
            entries = []
        for index, entry in enumerate(entries if isinstance(entries, list) else []):
            digest = __import__("hashlib").sha256(
                json.dumps(entry, sort_keys=True).encode("utf-8")
            ).hexdigest()[:20]
            run_id = f"legacy-{digest}"
            if store.get_run(run_id) is not None:
                continue
            started = f"{entry.get('date')}T00:00:00+00:00" if entry.get("date") else None
            store.start_run(run_id=run_id, started_at=started, status="legacy", settings=settings)
            store.add_snapshot(run_id, "legacy_history_entry", entry, source_path=str(absolute_path(history_path)))
            store.update_run_summary(
                run_id,
                target_date=entry.get("date"),
                daypart_role=entry.get("part_of_day"),
                brief_source="legacy_history",
            )
            store.finish_run(
                run_id,
                status="legacy",
                summary={
                    "target_date": entry.get("date"),
                    "daypart_role": entry.get("part_of_day"),
                    "brief_source": "legacy_history",
                    "headline": entry.get("headline"),
                    "illustration_prompt": entry.get("illustration_prompt"),
                    "legacy_index": index,
                },
            )
            imported += 1

    # Import the current live runtime as a distinct snapshot. Its files may be
    # newer than history.json and include the only surviving hero/board images.
    brief_path = _runtime_path(settings, "brief_file")
    if brief_path and absolute_path(brief_path).exists():
        try:
            brief = read_json(brief_path)
            generated_at = brief.get("generated_at_local")
            identity = json.dumps({"generated_at": generated_at, "brief": brief.get("brief")}, sort_keys=True)
            digest = __import__("hashlib").sha256(identity.encode("utf-8")).hexdigest()[:20]
            run_id = f"legacy-current-{digest}"
            if store.get_run(run_id) is not None:
                return imported
            store.start_run(run_id=run_id, started_at=generated_at, status="legacy", settings=settings)
            for stage in STAGE_FILES:
                _capture(store, run_id, stage)
            summary = _summary_from_runtime(settings)
            store.finish_run(run_id, status="legacy", summary=summary)
            imported += 1
        except (OSError, json.JSONDecodeError):
            pass
    return imported


def main():
    parser = argparse.ArgumentParser(description="Record weather-board generation history")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--forced", action="store_true")
    start.add_argument("--forecast-target")

    stage_start = sub.add_parser("stage-start")
    stage_start.add_argument("run_id")
    stage_start.add_argument("stage")

    stage_end = sub.add_parser("stage-end")
    stage_end.add_argument("stage_id")
    stage_end.add_argument("--status", choices=("succeeded", "failed", "skipped"), required=True)
    stage_end.add_argument("--exit-code", type=int)
    stage_end.add_argument("--message")

    capture = sub.add_parser("capture")
    capture.add_argument("run_id")
    capture.add_argument("stage")

    stage_log = sub.add_parser("stage-log")
    stage_log.add_argument("run_id")
    stage_log.add_argument("stage")
    stage_log.add_argument("log_file")

    finish = sub.add_parser("finish")
    finish.add_argument("run_id")
    finish.add_argument("--status", choices=("succeeded", "failed"), default="succeeded")
    finish.add_argument("--error")

    sub.add_parser("import-legacy")
    sub.add_parser("init")

    args = parser.parse_args()
    store = GenerationStore()

    if args.command == "start":
        print(store.start_run(forced=args.forced, forecast_target=args.forecast_target))
    elif args.command == "stage-start":
        print(store.begin_stage(args.run_id, args.stage))
    elif args.command == "stage-end":
        store.end_stage(
            args.stage_id, status=args.status, exit_code=args.exit_code, message=args.message
        )
    elif args.command == "capture":
        print(json.dumps(_capture(store, args.run_id, args.stage), separators=(",", ":")))
    elif args.command == "stage-log":
        path = Path(args.log_file)
        if path.exists():
            print(store.add_snapshot(
                args.run_id,
                f"stage_log:{args.stage}",
                path.read_text(encoding="utf-8", errors="replace"),
                content_type="text/plain; charset=utf-8",
            ))
    elif args.command == "finish":
        store.finish_run(
            args.run_id,
            status=args.status,
            error_summary=args.error,
            summary=_summary_from_runtime(load_settings()),
        )
    elif args.command == "import-legacy":
        print(f"imported={_import_legacy(store)}")
    elif args.command == "init":
        print(store.db_path)


if __name__ == "__main__":
    main()
