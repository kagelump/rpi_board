import base64
import copy
import io
import json
import subprocess
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest
from PIL import Image

from scripts.common import ROOT, load_settings
from scripts.history.create_update import MAX_BODY_BYTES, UpdateService, update_lock
from scripts.history.server import make_server
from scripts.history.store import GenerationStore


def encoded_image(size=(960, 640), color="blue", format="PNG"):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format=format)
    return base64.b64encode(out.getvalue()).decode()


@pytest.fixture
def payload():
    return {
        "brief": {"headline": "A dry afternoon", "subtitle": "Leave the umbrella at home.", "accent": "yellow",
                  "bullets": ["An externally authored update"], "custom_field": "preserve me"},
        "today": {"daily_summary": {"date": "2026-10-01", "temp_min_c": 18, "temp_max_c": 29,
                                     "weather_code": 0, "condition": "Clear"}},
        "day_context": {"date_pretty": "Thursday, October 1", "daypart_role": "afternoon"},
        "custom_context": {"author": "Codex"},
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    settings = copy.deepcopy(load_settings())
    settings["display"]["mode"] = "pi_display"
    for key in ("final_file", "preview_file", "stale_file", "brief_file", "hero_file"):
        path = tmp_path / key
        path.write_bytes(b"original")
        settings["runtime"][key] = str(path)
    settings["runtime"]["art_recipe_ledger_file"] = str(tmp_path / "art_recipe_ledger.jsonl")
    store = GenerationStore(tmp_path / "history.jsonl", tmp_path / "artifacts")
    service = UpdateService(store, settings, tmp_path / "update.lock")
    pushed = []
    monkeypatch.setattr("scripts.history.create_update.push_image", lambda s, p: pushed.append(p.read_bytes()))
    server = make_server("127.0.0.1", 0, store, service, settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(body=None, method="POST", path="/create_update", headers=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=20)
        connection.request(method, path, body=data if method == "POST" else None,
                           headers=headers if headers is not None else {"Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    yield request, store, service, pushed
    server.shutdown()
    server.server_close()
    thread.join()


def test_compose_preview_preserves_fields_and_live_files(api, payload):
    request, store, service, pushed = api
    status, raw = request({"payload": payload, "hero_image": encoded_image((1024, 512), "red"),
                           "metadata": {"source": "Codex"}, "publish": False})
    assert status == 201, raw
    result = json.loads(raw)
    assert result["published"] is False
    assert not pushed
    for key in ("final_file", "preview_file", "stale_file", "brief_file", "hero_file"):
        assert Path(service.settings["runtime"][key]).read_bytes() == b"original"
    run = store.get_run(result["run_id"])
    assert run["status"] == "succeeded"
    snapshots = {s["kind"]: store.get_snapshot(s["id"])["payload"] for s in run["snapshots"]}
    assert snapshots["external_update"]["payload"] == payload
    assert snapshots["external_update"]["metadata"] == {"source": "Codex"}
    assert snapshots["generated_brief"]["brief_source"] == "external"
    status, png = request(method="GET", path=result["artifacts"]["final_display"])
    assert status == 200
    image = Image.open(io.BytesIO(png))
    assert image.size == (960, 640)
    assert image.getpixel((480, 150)) == (220, 0, 0)
    assert set(image.getdata()) <= {(255, 255, 255), (0, 0, 0), (220, 0, 0), (220, 200, 0)}


def test_board_upload_publishes_quantized_output_without_composition(api):
    request, store, service, pushed = api
    status, raw = request({"board_image": encoded_image(color="red", format="JPEG")})
    assert status == 201, raw
    result = json.loads(raw)
    assert result["published"] is True
    assert len(pushed) == 1
    assert Path(service.settings["runtime"]["final_file"]).read_bytes() == pushed[0]
    assert Path(service.settings["runtime"]["brief_file"]).read_bytes() == b"original"
    assert Path(service.settings["runtime"]["hero_file"]).read_bytes() == b"original"
    assert Image.open(io.BytesIO(pushed[0])).getpixel((0, 0)) == (220, 0, 0)
    run = store.get_run(result["run_id"])
    assert run["stages"][-1]["stage_name"] == "push_to_display"


def test_no_hero_uses_pictogram_and_schema_is_discoverable(api, payload):
    request, store, service, pushed = api
    status, raw = request({"payload": payload, "publish": False})
    assert status == 201, raw
    status, raw = request(method="GET")
    schema = json.loads(raw)
    assert status == 200 and "payload" in schema["properties"]
    assert schema["properties"]["publish"]["default"] is True


def test_unfittable_copy_fails_without_publishing(api, payload):
    request, store, service, pushed = api
    payload["brief"]["subtitle"] = "Carry an umbrella and a light layer. " * 50
    status, raw = request({"payload": payload})
    assert status == 500, raw
    result = json.loads(raw)
    run = store.get_run(result["run_id"])
    assert run["status"] == "failed"
    assert "rewrite more concisely" in run["error_summary"]
    assert run["stages"][-1]["stage_name"] == "compose_board"
    assert run["stages"][-1]["status"] == "failed"
    assert not pushed
    for key in ("final_file", "preview_file", "stale_file", "brief_file", "hero_file"):
        assert Path(service.settings["runtime"][key]).read_bytes() == b"original"


@pytest.mark.parametrize("body", [
    [], {}, {"payload": {}}, {"publish": "false"}, {"settings": {}},
    {"board_image": "not base64"}, {"board_image": base64.b64encode(b"not an image").decode()},
    {"board_image": encoded_image((20, 20))},
    {"hero_image": "a", "board_image": "b"},
])
def test_invalid_requests_do_not_start_runs(api, body):
    request, store, _, pushed = api
    status, _ = request(body)
    assert status == 400
    assert store.stats()["runs"] == 0
    assert not pushed


@pytest.mark.parametrize("field,value", [("temp_min_c", True), ("temp_max_c", 101), ("date", "wrong")])
def test_invalid_render_fields(api, payload, field, value):
    request, store, _, _ = api
    payload["today"]["daily_summary"][field] = value
    assert request({"payload": payload})[0] == 400
    assert store.stats()["runs"] == 0


def test_http_input_limits_and_origin(api):
    request, store, _, _ = api
    assert request(raw=b"{oops")[0] == 400
    assert request(raw=b'{"publish": NaN}')[0] == 400
    assert request({}, headers={"Content-Type": "text/plain"})[0] == 415
    assert request({}, headers={"Content-Type": "application/json", "Origin": "http://example.com"})[0] == 403
    assert request({}, headers={"Content-Type": "application/json", "Content-Length": str(MAX_BODY_BYTES + 1)})[0] == 413
    assert request({}, path="/unknown")[0] == 404
    assert store.stats()["runs"] == 0


def test_shared_lock_returns_conflict_without_creating_run(api, payload):
    request, store, service, pushed = api
    with update_lock(service.lock_path):
        assert request({"payload": payload})[0] == 409
    assert store.stats()["runs"] == 0
    assert not pushed


def test_publication_failure_keeps_live_outputs_and_records_failure(api, payload, monkeypatch):
    request, store, service, _ = api
    def fail(*args):
        raise RuntimeError("hardware offline")
    monkeypatch.setattr("scripts.history.create_update.push_image", fail)
    status, raw = request({"payload": payload})
    assert status == 500, raw
    result = json.loads(raw)
    run = store.get_run(result["run_id"])
    assert run["status"] == "failed"
    assert run["error_summary"] == "hardware offline"
    assert run["stages"][-1]["status"] == "failed"
    assert any(a["kind"] == "final_display" for a in run["artifacts"])
    assert Path(service.settings["runtime"]["final_file"]).read_bytes() == b"original"


def test_upload_helper_over_real_http(api, payload, tmp_path):
    request, store, service, pushed = api
    # Exercise the CLI against another listener backed by the same service.
    server = make_server("127.0.0.1", 0, store, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(payload))
    hero_file = tmp_path / "hero.png"
    Image.new("RGB", (512, 256), "yellow").save(hero_file)
    try:
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts/ops/upload_update.py"),
            "--url", f"http://127.0.0.1:{server.server_port}",
            "--payload", str(payload_file), "--hero", str(hero_file), "--preview",
        ], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["published"] is False
        assert not pushed
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_scheduled_shell_and_api_share_process_lock(tmp_path):
    lock = tmp_path / "update.lock"
    with update_lock(lock):
        assert subprocess.run(["flock", "-n", str(lock), "true"], check=False).returncode == 1
    assert subprocess.run(["flock", "-n", str(lock), "true"], check=False).returncode == 0


def test_large_image_rejected_before_run(api, monkeypatch):
    monkeypatch.setattr("scripts.history.create_update.MAX_IMAGE_PIXELS", 100)
    request, store, _, _ = api
    assert request({"board_image": encoded_image((20, 20))})[0] == 400
    assert store.stats()["runs"] == 0


def test_unknown_timezone_rejected(api, payload):
    request, store, _, _ = api
    payload["timezone"] = "not/a/timezone"
    assert request({"payload": payload})[0] == 400
    assert store.stats()["runs"] == 0


# ---------------------------------------------------------------------------
# Issue #18: external publications feed the shared artwork recipe ledger
# ---------------------------------------------------------------------------


RECIPE_METADATA = {
    "recipe": {
        "subject": "cyclist",
        "motifs": ["bicycle"],
        "setting": "riverside_path",
        "viewpoint": "high_angle",
        "composition": "layered_depth",
    },
    "style": "Linocut",
    "dominant_ink": "Red Signal",
}


def _ledger_events(service):
    path = Path(service.settings["runtime"]["art_recipe_ledger_file"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_external_publication_records_shared_recipe(api, payload):
    request, store, service, pushed = api
    status, raw = request({
        "payload": payload,
        "hero_image": encoded_image((1024, 512), "red"),
        "metadata": RECIPE_METADATA,
    })
    assert status == 201, raw
    result = json.loads(raw)
    assert result["art_recipe"]["status"] == "published"
    published = [event for event in _ledger_events(service) if event["event"] == "recipe_published"]
    assert len(published) == 1
    assert published[0]["source"] == "external"
    assert published[0]["tags"]["subject"] == "cyclist"
    assert published[0]["tags"]["style"] == "linocut"
    assert published[0]["tags"]["dominant_ink"] == "red signal"


def test_preview_and_failed_delivery_do_not_record_recipe(api, payload, monkeypatch):
    request, store, service, pushed = api
    status, _ = request({
        "payload": payload,
        "hero_image": encoded_image((1024, 512), "red"),
        "publish": False,
        "metadata": RECIPE_METADATA,
    })
    assert status == 201
    assert _ledger_events(service) == []

    def fail(*args):
        raise RuntimeError("hardware offline")

    monkeypatch.setattr("scripts.history.create_update.push_image", fail)
    status, _ = request({
        "payload": payload,
        "hero_image": encoded_image((1024, 512), "blue"),
        "metadata": RECIPE_METADATA,
    })
    assert status == 500
    assert _ledger_events(service) == []


def test_reused_external_artwork_is_booked_as_reuse(api, payload):
    request, store, service, pushed = api
    body = {
        "payload": payload,
        "hero_image": encoded_image((1024, 512), "green"),
        "metadata": RECIPE_METADATA,
    }
    assert request(body)[0] == 201
    assert request(body)[0] == 201
    events = _ledger_events(service)
    assert sum(event["event"] == "recipe_published" for event in events) == 1
    assert sum(event["event"] == "recipe_reused" for event in events) == 1


def test_payload_only_publish_does_not_record_recipe(api, payload):
    """A native payload-only ``publish:true`` request still composes, previews,
    and delivers the deterministic weather pictogram, but that local board was
    never authored or shown as uploaded artwork. It must not enter the shared
    published-artwork ledger or consume a motif cooldown -- even though a recipe
    could be derived from ``payload.brief.illustration_prompt`` or supplied via
    ``metadata.recipe``.
    """
    request, store, service, pushed = api
    for _ in range(2):
        status, raw = request({"payload": payload, "metadata": RECIPE_METADATA})
        assert status == 201, raw
        result = json.loads(raw)
        assert result["published"] is True
        assert result["art_recipe"]["status"] == "skipped_no_uploaded_artwork"
        assert result["artifacts"]["final_display"]

    # The board was delivered to the panel and the live files updated as before.
    assert len(pushed) == 2
    assert Path(service.settings["runtime"]["final_file"]).read_bytes() == pushed[-1]

    # No recipe_published and no recipe_reused events: the pictogram is not art.
    assert _ledger_events(service) == []


def test_board_upload_records_shared_recipe(api):
    """An uploaded full board (not just a hero) still feeds the ledger: the
    correction only excludes the locally composited pictogram, not authored art."""
    request, store, service, pushed = api
    status, raw = request({
        "board_image": encoded_image(color="red", format="JPEG"),
        "metadata": RECIPE_METADATA,
    })
    assert status == 201, raw
    assert json.loads(raw)["art_recipe"]["status"] == "published"
    published = [event for event in _ledger_events(service) if event["event"] == "recipe_published"]
    assert len(published) == 1
    assert published[0]["source"] == "external"
    assert published[0]["tags"]["subject"] == "cyclist"
