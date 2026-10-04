"""Integration coverage for GPIO isolation of external hardware delivery.

These tests drive the real persistent history server (one long-lived process
handling several ``POST /create_update`` requests) against a fake Waveshare
driver that claims an exclusive OS lock *at import time*. That mirrors the real
driver claiming GPIO lines / ``/dev/gpiochip0`` while the module is imported
(strict preflight) or initialised (display push). The lock can only be released
by the process that owns it exiting, so it is a faithful proxy for the
persistent-process handle leak described in issue #19.
"""
from __future__ import annotations

import copy
import fcntl
import io
import json
import subprocess
import sys
import tempfile
import threading
from base64 import b64encode
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts.common import ROOT, load_settings
from scripts.history.create_update import (
    UpdateService,
    _delivery_child_settings,
    push_image,
)
from scripts.history.server import make_server
from scripts.history.store import GenerationStore

STUB_TEMPLATE = '''"""Fake Waveshare driver that claims an exclusive OS lock at import time.

The lock survives until the owning process exits, exactly like an
in-process lgpio/gpiochip claim that a long-running server never releases.
"""
import fcntl
import os
from pathlib import Path

_LOCK = Path(__LOCK__)
_LOG = Path(__LOG__)
_FAIL_AT = __FAIL_AT__

_lock_handle = _LOCK.open("a+b")
try:
    fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError as error:
    raise OSError("lgpio.error: 'GPIO busy'") from error


def _log(event):
    with _LOG.open("a", encoding="utf-8") as handle:
        handle.write("{}:{}\\n".format(event, os.getpid()))


_log("import")


class EPD:
    def __init__(self):
        if _FAIL_AT == "construct":
            raise RuntimeError("fake driver construct failure")
        _log("construct")

    def init(self):
        if _FAIL_AT == "init":
            raise RuntimeError("fake driver init failure")
        _log("init")

    def getbuffer(self, image):
        return image.tobytes()

    def display(self, buffer):
        if _FAIL_AT == "display":
            raise RuntimeError("fake driver display failure")
        _log("display")

    def sleep(self):
        _log("sleep")
'''


def _write_stub(lib_dir: Path, lock_path: Path, log_path: Path) -> None:
    package = lib_dir / "device_stub"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, fail_at in (("ok", None), ("init_fail", "init"), ("display_fail", "display")):
        source = (
            STUB_TEMPLATE
            .replace("__LOCK__", repr(str(lock_path)))
            .replace("__LOG__", repr(str(log_path)))
            .replace("__FAIL_AT__", repr(fail_at))
        )
        (package / f"{name}.py").write_text(source, encoding="utf-8")


def _lock_is_held(lock_path: Path) -> bool:
    """True when any process (including this one) holds the exclusive lock."""
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def _events(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    return [line.split(":", 1)[0] for line in log_path.read_text(encoding="utf-8").splitlines() if line]


def _board_image(size=(960, 640), color="red") -> str:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return b64encode(out.getvalue()).decode()


def _stub_imported_in_this_process() -> bool:
    return any(name == "device_stub" or name.startswith("device_stub.") for name in sys.modules)


def _run_preflight_module_check(lib_dir: Path, module: str) -> subprocess.CompletedProcess:
    """Exercise preflight's hardware-module import in a fresh process.

    ``_check_waveshare`` is the strict-preflight step that failed with
    ``GPIO busy`` in the field; running it in a short-lived child matches the
    real ``ExecStartPre`` process and keeps the fake driver out of this one.
    """
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from scripts.ops.preflight import _check_waveshare, _inject_waveshare_paths\n"
        f"_inject_waveshare_paths([{str(lib_dir)!r}])\n"
        f"ok, name = _check_waveshare([{module!r}])\n"
        "raise SystemExit(0 if ok else 1)\n"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)


def _run_scheduled_style_push(settings, image_path: Path) -> subprocess.CompletedProcess:
    """Run the scheduled display helper exactly as update_display.sh does."""
    with tempfile.TemporaryDirectory(prefix="scheduled-push-") as tmp:
        settings_file = Path(tmp) / "settings.json"
        settings_file.write_text(json.dumps(settings), encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/display/push_to_epd.py"),
                "--settings-file", str(settings_file),
                "--input", str(image_path),
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )


@pytest.fixture
def delivery(tmp_path):
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    lock_path = tmp_path / "gpiochip.lock"
    lock_path.touch()
    log_path = tmp_path / "driver_calls.log"
    _write_stub(lib_dir, lock_path, log_path)

    settings = copy.deepcopy(load_settings())
    settings["display"]["mode"] = "pi_display"
    settings["display"]["waveshare_python_lib_candidates"] = [str(lib_dir)]
    settings["display"]["waveshare_module_candidates"] = ["device_stub.ok"]
    for key in ("final_file", "preview_file", "stale_file", "brief_file", "hero_file"):
        target = tmp_path / key
        target.write_bytes(b"original")
        settings["runtime"][key] = str(target)
    settings["runtime"]["art_recipe_ledger_file"] = str(tmp_path / "art_recipe_ledger.jsonl")

    store = GenerationStore(tmp_path / "history.jsonl", tmp_path / "artifacts")
    service = UpdateService(store, settings, tmp_path / "update.lock")
    server = make_server("127.0.0.1", 0, store, service, settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(body):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=60)
        connection.request(
            "POST", "/create_update", body=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        status, payload = response.status, json.loads(response.read())
        connection.close()
        return status, payload

    try:
        yield SimpleNamespace(
            post=post, service=service, store=store, settings=settings,
            lock_path=lock_path, log_path=log_path, lib_dir=lib_dir, tmp_path=tmp_path,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_successful_publish_isolates_gpio_and_next_scheduled_steps_succeed(delivery):
    status, result = delivery.post({"board_image": _board_image(), "publish": True})
    assert status == 201, result
    assert result["published"] is True

    # The persistent server process neither imported the driver nor kept any
    # claimed handle after the synchronous request returned.
    assert not _stub_imported_in_this_process()
    assert _lock_is_held(delivery.lock_path) is False
    # The isolated child really ran the full driver sequence and exited.
    assert _events(delivery.log_path) == ["import", "construct", "init", "display", "sleep"]

    # Representative strict preflight (ExecStartPre): importing the module in a
    # fresh process must not hit GPIO busy.
    preflight = _run_preflight_module_check(delivery.lib_dir, "device_stub.ok")
    assert preflight.returncode == 0, preflight.stderr

    # Scheduled-style display push reuses the published final output.
    pushed = _run_scheduled_style_push(delivery.settings, Path(delivery.settings["runtime"]["final_file"]))
    assert pushed.returncode == 0, pushed.stderr

    assert _lock_is_held(delivery.lock_path) is False
    assert not _stub_imported_in_this_process()


def test_publish_false_never_imports_or_claims_the_driver(delivery):
    status, result = delivery.post({"board_image": _board_image(), "publish": False})
    assert status == 201, result
    assert result["published"] is False

    assert not _stub_imported_in_this_process()
    assert _lock_is_held(delivery.lock_path) is False
    # No delivery child was spawned at all.
    assert _events(delivery.log_path) == []
    for key in ("final_file", "preview_file", "stale_file"):
        assert Path(delivery.settings["runtime"][key]).read_bytes() == b"original"


@pytest.mark.parametrize(
    "failing_module,message",
    [
        ("device_stub.init_fail", "fake driver init failure"),
        ("device_stub.display_fail", "fake driver display failure"),
    ],
)


def test_failed_delivery_releases_resources_and_later_publications_succeed(delivery, failing_module, message):
    delivery.service.settings["display"]["waveshare_module_candidates"] = [failing_module]
    status, result = delivery.post({"board_image": _board_image(), "publish": True})
    assert status == 500, result
    assert message in result["error"]

    # Failure happened in a child; nothing is retained here.
    assert not _stub_imported_in_this_process()
    assert _lock_is_held(delivery.lock_path) is False
    assert Path(delivery.settings["runtime"]["final_file"]).read_bytes() == b"original"

    # A later publication succeeds without restarting the history service.
    delivery.service.settings["display"]["waveshare_module_candidates"] = ["device_stub.ok"]
    status, result = delivery.post({"board_image": _board_image(), "publish": True})
    assert status == 201, result
    assert result["published"] is True
    assert _lock_is_held(delivery.lock_path) is False
    assert Path(delivery.settings["runtime"]["final_file"]).read_bytes() != b"original"


def test_repeated_external_publications_do_not_accumulate_claims(delivery):
    for _ in range(3):
        status, result = delivery.post({"board_image": _board_image(), "publish": True})
        assert status == 201, result
        assert result["published"] is True
        assert _lock_is_held(delivery.lock_path) is False
        assert not _stub_imported_in_this_process()

    events = _events(delivery.log_path)
    assert events.count("import") == 3
    assert all(event in {"import", "construct", "init", "display", "sleep"} for event in events)


def _settings_with_secret_sentinels(tmp_path: Path) -> dict:
    """Runtime settings shape carrying sentinel credentials and display config."""
    return {
        "display": {
            "width": 960,
            "height": 640,
            "panel_fraction": 0.25,
            "mode": "pi_display",
            "waveshare_module_candidates": ["device_stub.ok", "custom.module.alt"],
            "waveshare_python_lib_candidates": [str(tmp_path / "lib"), "/opt/custom/waveshare"],
        },
        "runtime": {"final_file": str(tmp_path / "final.png")},
        "openrouter": {
            "api_key": "SENTINEL-OPENROUTER-KEY",
            "api_key_file": "/root/.openrouter.key",
        },
        "fal": {"api_key": "SENTINEL-FAL-KEY", "api_key_file": "/root/.fal.key"},
        "pipeline": {"image_provider": "openrouter"},
        "voice": {"api_key": "SENTINEL-VOICE-KEY"},
    }


def test_delivery_child_settings_allowlists_only_display_fields(tmp_path):
    settings = _settings_with_secret_sentinels(tmp_path)
    child = _delivery_child_settings(settings)

    assert child == {
        "display": {
            "mode": "pi_display",
            "waveshare_module_candidates": ["device_stub.ok", "custom.module.alt"],
            "waveshare_python_lib_candidates": [str(tmp_path / "lib"), "/opt/custom/waveshare"],
        }
    }
    assert set(child) == {"display"}
    assert set(child["display"]) == {
        "mode", "waveshare_module_candidates", "waveshare_python_lib_candidates",
    }
    serialized = json.dumps(child, ensure_ascii=True)
    assert "SENTINEL" not in serialized
    assert "openrouter" not in serialized
    assert "api_key" not in serialized


def test_delivery_child_settings_honours_mode_override(tmp_path):
    settings = _settings_with_secret_sentinels(tmp_path)
    child = _delivery_child_settings(settings, mode="local_preview")
    assert child["display"]["mode"] == "local_preview"
    # The override must not bring unrelated sections along for the ride.
    assert set(child["display"]) == {
        "mode", "waveshare_module_candidates", "waveshare_python_lib_candidates",
    }


def test_push_image_temp_child_config_never_contains_secrets(monkeypatch, tmp_path):
    """The serializer seam must write only the allowlisted display config."""
    settings = _settings_with_secret_sentinels(tmp_path)
    image_path = tmp_path / "board.png"
    image_path.write_bytes(b"board")
    captured = {}

    def fake_run(command, **kwargs):
        settings_file = Path(command[command.index("--settings-file") + 1])
        # Read while the TemporaryDirectory is still alive, like the child does.
        captured["payload"] = json.loads(settings_file.read_text(encoding="utf-8"))
        captured["command"] = list(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("scripts.history.create_update.subprocess.run", fake_run)
    push_image(settings, image_path)

    # The board image path is handed to the child separately, not via settings.
    assert captured["command"][captured["command"].index("--input") + 1] == str(image_path.resolve())

    assert captured["payload"] == {
        "display": {
            "mode": "pi_display",
            "waveshare_module_candidates": ["device_stub.ok", "custom.module.alt"],
            "waveshare_python_lib_candidates": [str(tmp_path / "lib"), "/opt/custom/waveshare"],
        }
    }
    serialized = json.dumps(captured["payload"], ensure_ascii=True)
    assert "SENTINEL" not in serialized
    assert "openrouter" not in serialized
    assert "api_key" not in serialized


def test_local_preview_publishes_without_importing_driver(delivery):
    delivery.service.settings["display"]["mode"] = "local_preview"
    status, result = delivery.post({"board_image": _board_image(), "publish": True})

    assert status == 201, result
    assert result["published"] is True
    assert result["display_mode"] == "local_preview"
    # The delivery child short-circuits before the driver module is imported.
    assert _events(delivery.log_path) == []
    assert not _stub_imported_in_this_process()
    assert _lock_is_held(delivery.lock_path) is False
    # Preview publishes the live files without claiming hardware.
    assert Path(delivery.settings["runtime"]["final_file"]).read_bytes() != b"original"
