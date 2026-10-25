import io
import json
import threading
from http.client import HTTPConnection

import pytest
from PIL import Image

from scripts.history.server import HistoryRequestHandler, make_server
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
    for asset, content_type in (("review.js", "text/javascript"), ("review.css", "text/css")):
        status, headers, body = request("/static/" + asset)
        assert status == 200 and headers["Content-Type"].startswith(content_type)
        assert body
    assert request("/static/../store.py")[0] == 404
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


def test_overall_run_rating_tracks_board_version_and_survives_resync(tmp_path, monkeypatch):
    # All saves share a timestamp: latest means append order, never UUID order.
    monkeypatch.setattr("scripts.history.store.utc_now", lambda: "2026-10-25T00:00:00+00:00")
    store = _store(tmp_path)
    run_id = store.start_run()
    missing_id = store.start_run()
    image = tmp_path / "board.png"
    Image.new("RGB", (12, 8), "red").save(image)
    before_id = store.add_artifact(run_id, "final_display_pre_quantize", image)
    store.add_artifact(run_id, "preview", image)
    store.add_artifact(run_id, "final_display", image)
    # Even tied artifact timestamps select the last version in the ledger.
    Image.new("RGB", (12, 8), "yellow").save(image)
    final_id = store.add_artifact(run_id, "final_display", image)
    ledger_before = store.record_file.read_bytes()

    first = store.add_rating("run", run_id, 4)
    second = store.add_rating("run", run_id, 4, "  Palette loses detail  ")
    assert first["final_display_id"] == second["final_display_id"] == final_id
    assert second["target_sha256"] == store.get_artifact(final_id)[0]["sha256"]
    assert second["comment"] == "Palette loses detail"
    assert store.get_run(run_id)["latest_rating"] == second
    assert store.get_run(run_id)["reference"]["id"] == before_id
    row = store.list_runs(rating="4", has_final=True)["items"][0]
    assert row["latest_rating"] == second and row["final_display"]["id"] == final_id
    assert "settings" not in row and "stored_path" not in row["final_display"]
    assert store.list_runs(rating="unrated")["total"] == 1
    assert store.list_runs(rating="unrated", has_final=True)["total"] == 0
    assert store.list_runs(rating="rated", limit=1, offset=1)["items"] == []
    assert store.list_runs(has_final=False)["items"][0]["id"] == missing_id
    assert store.stats()["rated"] == 1
    assert store.stats()["unrated_boards"] == 0
    assert store.record_file.read_bytes() == ledger_before

    # Re-copying the generation ledger leaves local feedback untouched.
    store.record_file.write_bytes(ledger_before)
    reloaded = GenerationStore(store.record_file, store.artifacts_dir)
    assert reloaded.get_run(run_id)["latest_rating"] == second
    assert reloaded.list_ratings(run_id=run_id)["total"] == 2
    # Failed/missing-board runs remain selectable and can have overall feedback.
    assert store.add_rating("run", missing_id, 1)["final_display_id"] is None


def test_review_filters_ignore_old_output_ratings_and_paginate(tmp_path):
    store = _store(tmp_path)
    older = store.start_run(started_at="2026-10-24T00:00:00+00:00")
    newer = store.start_run(started_at="2026-10-25T00:00:00+00:00")
    image = tmp_path / "board.png"
    Image.new("RGB", (12, 8), "red").save(image)
    for run_id in (older, newer):
        store.add_artifact(run_id, "final_display", image)
    preview = store.add_artifact(older, "preview", image)
    hero = store.add_artifact(newer, "hero", image)
    store.add_rating("artifact", hero, 5)
    assert store.list_runs(rating="unrated")["total"] == 2
    assert store.stats()["unrated_boards"] == 2
    assert store.get_run(older)["reference"]["id"] == preview
    assert store.list_runs(limit=1)["items"][0]["id"] == newer
    assert store.list_runs(limit=1, offset=1)["items"][0]["id"] == older
    with pytest.raises(ValueError, match="rating must be"):
        store.list_runs(rating="6")
    with pytest.raises(KeyError, match="Unknown run"):
        store.add_rating("run", "missing", 3)
    for invalid in (True, 0, 6, 2.5, "4", None):
        with pytest.raises(ValueError, match="integer from 1 to 5"):
            store.add_rating("run", newer, invalid)
    with pytest.raises(ValueError, match="4000"):
        store.add_rating("run", newer, 3, "a" * 4001)
    with pytest.raises(ValueError, match="string"):
        store.add_rating("run", newer, 3, None)


def test_run_rating_http_contract_and_review_filters(tmp_path):
    store = _store(tmp_path)
    run_id = store.start_run()
    server = make_server("127.0.0.1", 0, store, updates=object())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request("POST", "/api/ratings", json.dumps({
            "target_type": "run", "target_id": run_id, "rating": 2,
            "comment": "Board was not produced",
        }), {"Content-Type": "application/json"})
        response = connection.getresponse()
        saved = json.loads(response.read())
        assert response.status == 201 and saved["target_type"] == "run"
        for query, total in (("rating=2&has_final=false", 1), ("rating=unrated", 0)):
            connection.request("GET", "/api/runs?" + query)
            response = connection.getresponse()
            result = json.loads(response.read())
            assert response.status == 200 and result["total"] == total
            if total:
                assert result["items"][0]["latest_rating"] == saved
        for query in ("rating=banana", "has_final=banana"):
            connection.request("GET", "/api/runs?" + query)
            response = connection.getresponse()
            assert response.status == 400
            assert json.loads(response.read())["error"]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_art_recipe_endpoint_exposes_policy_and_history(tmp_path):
    from scripts.openrouter import art_recipes

    settings = {
        "runtime": {"art_recipe_ledger_file": str(tmp_path / "recipes.jsonl")},
        "art_variety": {"lookback_days": 14, "motif_cooldown_days": 7, "min_dimension_differences": 2},
    }
    art_recipes.record_publication(
        settings,
        recipe={"subject": "lone_walker", "motifs": ["red bag"], "setting": "residential lane",
                "viewpoint": "eye level", "composition": "cropped edge"},
        artwork_sha256="a" * 64,
        target_date="2026-10-01",
        mode="pi_display",
    )
    store = _store(tmp_path)

    class TestHandler(HistoryRequestHandler):
        pass

    TestHandler.store = store
    TestHandler.settings = settings

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
        return handler.response_status, handler.wfile.getvalue()

    status, body = request("/api/art-recipes")
    assert status == 200
    payload = json.loads(body)
    assert payload["policy"]["motif_cooldown_days"] == 7
    assert payload["required_dimensions"] == ["subject/motif", "viewpoint/composition"]
    assert payload["recent_recipes"][0]["tags"]["subject"] == "lone walker"
    text = body.decode().lower()
    for secret in ("api_key", "bearer", "authorization", "password", "secret"):
        assert secret not in text


def test_human_ratings_are_append_only_and_available_as_training_rows(tmp_path):
    store = _store(tmp_path)
    run_id = store.start_run()
    image_path = tmp_path / "hero.png"
    Image.new("RGB", (12, 8), "red").save(image_path)
    hero_id = store.add_artifact(run_id, "hero", image_path)
    final_id = store.add_artifact(run_id, "final_display", image_path)
    brief_id = store.add_snapshot(run_id, "generated_brief", {"headline": "A bright morning"})
    generation_ledger_before_ratings = store.record_file.read_bytes()

    server = make_server("127.0.0.1", 0, store, updates=object())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)

        def post(body, headers=None):
            request_headers = {"Content-Type": "application/json", **(headers or {})}
            connection.request("POST", "/api/ratings", json.dumps(body), request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())

        status, first = post({
            "target_type": "artifact", "target_id": hero_id, "rating": 5,
            "comment": "Strong composition",
        })
        assert status == 201
        assert first["run_id"] == run_id
        assert first["target_sha256"]

        status, second = post({
            "target_type": "snapshot", "target_id": brief_id, "rating": 3,
            "comment": "Clear but generic",
        })
        assert status == 201 and second["target_kind"] == "generated_brief"

        status, _ = post({"target_type": "artifact", "target_id": hero_id, "rating": 4})
        assert status == 201
        status, error = post({"target_type": "artifact", "target_id": hero_id, "rating": 6})
        assert status == 400 and "1 to 5" in error["error"]
        status, error = post({"target_type": "artifact", "target_id": final_id, "rating": 4})
        assert status == 400 and "not rateable" in error["error"]
        status, _ = post(
            {"target_type": "artifact", "target_id": hero_id, "rating": 2},
            {"Origin": "https://untrusted.example"},
        )
        assert status == 403

        connection.request("GET", f"/api/runs/{run_id}")
        response = connection.getresponse()
        run = json.loads(response.read())
        assert response.status == 200
        assert [row["rating"] for row in run["ratings"]] == [5, 3, 4]
        assert first["rated_at"] == run["ratings"][0]["rated_at"]

        connection.request("GET", f"/api/ratings?run_id={run_id}")
        response = connection.getresponse()
        rows = json.loads(response.read())
        assert response.status == 200 and rows["total"] == 3
        assert rows["items"][-1]["target_id"] == hero_id
        connection.close()

        assert store.record_file.read_bytes() == generation_ledger_before_ratings
        rating_events = [json.loads(line) for line in store.ratings_file.read_text().splitlines()]
        assert len(rating_events) == 3
        assert all(event["type"] == "rating_added" for event in rating_events)
        reloaded = GenerationStore(store.record_file, store.artifacts_dir)
        assert reloaded.list_ratings(run_id=run_id)["total"] == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
