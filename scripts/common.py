#!/usr/bin/env python3
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / "config" / "settings.json"


def load_settings() -> Dict[str, Any]:
    with SETTINGS_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_runtime_dirs(settings: Dict[str, Any]) -> None:
    runtime_dir = ROOT / settings["runtime"]["dir"]
    logs_dir = runtime_dir / "logs"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)


def absolute_path(path_str: str) -> Path:
    path = Path(path_str)
    if path.is_absolute():
        return path
    return ROOT / path


def write_json(path_str: str, payload: Dict[str, Any]) -> None:
    target = absolute_path(path_str)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2)


def read_json(path_str: str) -> Dict[str, Any]:
    target = absolute_path(path_str)
    with target.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_openrouter_api_key(settings: Dict[str, Any]) -> Optional[str]:
    env_key = os.getenv("OPENROUTER_API_KEY")
    if env_key:
        return env_key.strip()

    configured = settings.get("openrouter", {}).get("api_key_file")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            Path("~/.openrouter.key").expanduser(),
            Path("~/.config/openrouter/api_key").expanduser(),
        ]
    )

    for path in candidates:
        if not path.exists():
            continue
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    return None
