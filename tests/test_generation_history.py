import io
import json

from PIL import Image

from scripts.history.server import HistoryRequestHandler
from scripts.history.store import GenerationStore, SCHEMA_VERSION


def _store(tmp_path):
    return GenerationStore(
        record_file=tmp_path / "history.jsonl",
        artifacts_dir=tmp_path / "artifacts",
    )


def test_ledger_is_schema_versioned_append_only(tmp_path):
    store = _store(tmp_path)
    initial = store.record_file.read_bytes()
    first = json.loads(initial.splitlines()[0])
    assert first["schema_version"] == SCHEMA_VERSION
    assert first["type"] == "ledger_initialized"

    run_id = store.start_run()
    after_start = store.record_file.read_bytes()
    store.finish_run(run_id)
    after_finish = store.record_file.read_bytes()
    assert after_start.startswith(initial)
    assert after_finish.startswith(after_start)
    assert [json.loads(line)["type"] for line in after_finish.splitlines()] == [
        "ledger_initialized", "run_started", "run_finished",
    ]


def test_recovery_skips_partial_record_and_keeps_future_appends(tmp_path):
    store = _store(tmp_path)
    with store.record_file.open("ab") as handle:
        handle.write(b'{"schema_version":1,"type":"run_started"')
    run_id = store.start_run()
    store.finish_run(run_id)
    assert store.get_run(run_id)["status"] == "succeeded"
    assert store.stats()["runs"] == 1


def test_records_and_fetches_complete_generation(tmp_path):
    store = _store(tmp_path)
    run_id = store.start_run(forced=True, forecast_target="tomorrow")
    store.update_run_summary(
        run_id,
        target_date="2026-07-12",
        daypart_role="primary",
        selected_style="Linocut",
        image_provider="fal",
    )
    stage_id = store.begin_stage(run_id, "generate_image", {"attempt": 1})
    store.end_stage(stage_id, status="succeeded", exit_code=0)
    snapshot_id = store.add_snapshot(run_id, "weather_payload", {"temperature": 31})
    store.log(
        run_id,
        component="generate_image",
        event_type="style_selected",
        message="Selected Linocut",
        data={"style": "Linocut"},
    )

    image_path = tmp_path / "hero.png"
    Image.new("RGB", (12, 8), "red").save(image_path)
    artifact_id = store.add_artifact(run_id, "hero", image_path)
    store.finish_run(
        run_id,
        summary={
            "target_date": "2026-07-12",
            "selected_style": "Linocut",
            "headline": "Hot afternoon",
        },
    )

    listed = store.list_runs(target_date="2026-07-12", style="Linocut")
    assert listed["total"] == 1
    assert listed["items"][0]["forced"] is True
    assert listed["items"][0]["summary"]["headline"] == "Hot afternoon"

    detail = store.get_run(run_id)
    assert detail["status"] == "succeeded"
    assert detail["stages"][0]["duration_ms"] >= 0
    assert detail["logs"][0]["data"] == {"style": "Linocut"}
    assert detail["snapshots"][0]["id"] == snapshot_id
    assert "payload" not in detail["snapshots"][0]
    assert store.get_snapshot(snapshot_id)["payload"] == {"temperature": 31}

    metadata, stored_path = store.get_artifact(artifact_id)
    assert metadata["width"] == 12 and metadata["height"] == 8
    assert stored_path.exists()


def test_error_log_marks_successful_pipeline_degraded(tmp_path):
    store = _store(tmp_path)
    run_id = store.start_run()
    store.log(
        run_id,
        component="generate_image",
        event_type="image_generation_failed",
        message="timeout",
        level="error",
    )
    store.finish_run(run_id, status="succeeded")
    assert store.get_run(run_id)["status"] == "degraded"


def test_http_api_dashboard_and_artifact(tmp_path):
    store = _store(tmp_path)
    run_id = store.start_run()
    snapshot_id = store.add_snapshot(run_id, "brief_prompt", "hello", content_type="text/plain")
    image_path = tmp_path / "board.png"
    Image.new("RGB", (10, 10), "yellow").save(image_path)
    artifact_id = store.add_artifact(run_id, "final_display", image_path)
    store.finish_run(run_id, summary={"headline": "Clear and warm"})

    class TestHandler(HistoryRequestHandler):
        pass

    TestHandler.store = store

    def request(path):
        handler = TestHandler.__new__(TestHandler)
        handler.path = path
        handler.wfile = io.BytesIO()
        handler.response_status = None
        handler.response_headers = {}
        handler.send_response = lambda status: setattr(handler, "response_status", status)
        handler.send_header = lambda key, value: handler.response_headers.__setitem__(key, value)
        handler.end_headers = lambda: None
        handler.do_GET()
        return handler.response_status, handler.response_headers, handler.wfile.getvalue()

    status, _, body = request("/")
    assert status == 200 and b"Generation History" in body
    status, _, body = request("/api/health")
    assert status == 200 and json.loads(body)["ok"] is True
    status, _, body = request("/api/runs")
    assert status == 200 and json.loads(body)["items"][0]["id"] == run_id
    status, _, body = request(f"/api/runs/{run_id}")
    assert status == 200 and json.loads(body)["summary"]["headline"] == "Clear and warm"
    status, _, body = request(f"/api/snapshots/{snapshot_id}")
    assert status == 200 and json.loads(body)["payload"] == "hello"
    status, headers, body = request(f"/api/artifacts/{artifact_id}")
    assert status == 200 and headers["Content-Type"] == "image/png"
    assert body.startswith(b"\x89PNG")
