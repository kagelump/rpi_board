import json

from scripts.eval import run_eval


def test_calibrated_judge_formats_weather_and_title_without_rating_data(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "get_openrouter_api_key", lambda settings: "test-key")
    board = tmp_path / "board.png"
    board.write_bytes(b"board")
    captured = []

    def capture(request, **kwargs):
        captured.append(json.loads(request.data)["messages"][0]["content"][0]["text"])
        raise OSError("offline test")

    monkeypatch.setattr(run_eval, "urlopen_with_context", capture)
    settings = {"openrouter": {"base_url": "https://example.invalid"}}
    result = {
        "board": str(board), "headline": "Sunny", "subtitle": "Walk outside",
        "weather": {"condition": "clear", "temp_max_c": 23, "temp_min_c": 16},
    }
    assert run_eval.judge(result, settings, "judge", 1) == {"error": "offline test"}
    assert "owner's taste" in captured[0]
    assert "clear, high 23C / low 16C" in captured[0]
    assert "headline: Sunny" in captured[0] and "subtitle: Walk outside" in captured[0]
    assert "holistic owner preference" in captured[0]
