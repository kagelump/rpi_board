"""Tests for scripts/ops/preflight."""
import json
import socket
import ssl
import sys
import urllib.error
import urllib.request

import pytest

from scripts.common import load_settings
from scripts.ops import preflight
from scripts.ops.preflight import (
    _check_openrouter_https_reachability,
    _format_human,
    _run_checks,
    _summarize,
    validate_config,
)


def _base():
    return {
        "display": {"width": 960, "height": 640, "panel_fraction": 0.25},
        "fal": {"image_generation_parameters": {"image_size": {"width": 960, "height": 480}}},
        "pipeline": {"image_provider": "fal"},
        "context": {"events_mode": "online_model"},
    }


class TestValidateConfig:
    def test_consistent_config_has_no_issues(self):
        assert validate_config(_base()) == []

    def test_real_settings_file_is_valid(self):
        # Guards against shipping a settings.json that fails its own checks.
        assert validate_config(load_settings()) == []

    def test_image_size_height_mismatch_flagged(self):
        s = _base()
        s["fal"]["image_generation_parameters"]["image_size"]["height"] = 640
        issues = validate_config(s)
        assert any("art region" in i for i in issues)

    def test_image_size_width_mismatch_flagged(self):
        s = _base()
        s["fal"]["image_generation_parameters"]["image_size"]["width"] = 1024
        assert any("image_size.width" in i for i in validate_config(s))

    def test_bad_panel_fraction(self):
        s = _base()
        s["display"]["panel_fraction"] = 1.5
        assert any("panel_fraction" in i for i in validate_config(s))

    def test_bad_dimensions(self):
        s = _base()
        s["display"]["width"] = 0
        assert any("display.width" in i for i in validate_config(s))

    def test_unknown_provider(self):
        s = _base()
        s["pipeline"]["image_provider"] = "midjourney"
        assert any("image_provider" in i for i in validate_config(s))

    def test_unknown_events_mode(self):
        s = _base()
        s["context"]["events_mode"] = "telepathy"
        assert any("events_mode" in i for i in validate_config(s))

    def test_enum_string_image_size_not_flagged(self):
        # Non-dict image_size (a fal preset enum) is left to the API to validate.
        s = _base()
        s["fal"]["image_generation_parameters"]["image_size"] = "landscape_4_3"
        assert validate_config(s) == []


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _patch_urlopen(monkeypatch, outcome):
    """Patch ``urllib.request.urlopen`` and record how the probe called it."""
    calls = {}

    def fake_urlopen(url, *args, **kwargs):
        calls["url"] = url
        calls["args"] = args
        calls["kwargs"] = kwargs
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


class TestOpenrouterHttpsReachability:
    def test_successful_response_reports_reachability(self, monkeypatch):
        calls = _patch_urlopen(monkeypatch, _FakeResponse(200))
        ok, detail = _check_openrouter_https_reachability(20)
        assert ok is True
        assert "reachability probe" in detail
        assert "reachable" in detail
        assert "HTTP status=200" in detail
        assert calls["url"] == "https://openrouter.ai"
        assert calls["args"] == ()
        assert calls["kwargs"]["timeout"] == 20
        # Non-mutating, unauthenticated probe: plain GET with no body and no
        # headers/credentials attached.
        assert "data" not in calls["kwargs"]
        assert "headers" not in calls["kwargs"]

    def test_http_403_is_reachable_not_transport_failure(self, monkeypatch):
        error = urllib.error.HTTPError("https://openrouter.ai", 403, "Forbidden", None, None)
        _patch_urlopen(monkeypatch, error)
        ok, detail = _check_openrouter_https_reachability(20)
        assert ok is True
        assert "reachability probe" in detail
        assert "reachable" in detail
        assert "HTTP status=403" in detail
        # The recorded bug rendered this as "HTTPS request failed".
        assert "failed" not in detail.lower()
        assert "unreachable" not in detail.lower()

    def test_http_error_status_other_than_403_is_reachable(self, monkeypatch):
        error = urllib.error.HTTPError("https://openrouter.ai", 503, "Service Unavailable", None, None)
        _patch_urlopen(monkeypatch, error)
        ok, detail = _check_openrouter_https_reachability(20)
        assert ok is True
        assert "HTTP status=503" in detail

    @pytest.mark.parametrize(
        "reason",
        [
            socket.gaierror(-2, "Name or service not known"),
            ssl.SSLCertVerificationError("certificate verify failed"),
            ConnectionRefusedError(111, "Connection refused"),
            TimeoutError("timed out"),
        ],
    )
    def test_transport_failures_are_unreachable(self, monkeypatch, reason):
        _patch_urlopen(monkeypatch, urllib.error.URLError(reason))
        ok, detail = _check_openrouter_https_reachability(20)
        assert ok is False
        assert "reachability probe" in detail
        assert "unreachable" in detail
        assert "no HTTP response" in detail

    def test_bare_oserror_during_send_is_unreachable(self, monkeypatch):
        # urlopen usually wraps OSError in URLError, but tolerate a direct one.
        _patch_urlopen(monkeypatch, socket.gaierror(-2, "Name or service not known"))
        ok, detail = _check_openrouter_https_reachability(20)
        assert ok is False
        assert "unreachable" in detail
        assert "no HTTP response" in detail

    def test_legacy_helper_alias_still_works(self, monkeypatch):
        _patch_urlopen(monkeypatch, _FakeResponse(204))
        ok, detail = preflight._check_openrouter_https(20)
        assert ok is True
        assert "HTTP status=204" in detail


class TestRunChecks:
    def _patch_dependencies(self, monkeypatch):
        monkeypatch.setattr(preflight, "_check_spi", lambda: True)
        monkeypatch.setattr(preflight, "_check_waveshare", lambda candidates: (True, "fake_module"))
        monkeypatch.setattr(preflight, "_check_dns", lambda: (True, "Resolved openrouter.ai"))

    def test_reachability_probe_is_optional_and_named_for_reachability(self, monkeypatch):
        self._patch_dependencies(monkeypatch)
        monkeypatch.setattr(
            preflight,
            "_check_openrouter_https_reachability",
            lambda timeout: (True, "HTTPS reachability probe: reachable (HTTP status=200)"),
        )
        checks = _run_checks(load_settings())
        by_name = {item["name"]: item for item in checks}
        assert "openrouter_https_reachability" in by_name
        probe = by_name["openrouter_https_reachability"]
        assert probe["required"] is False
        assert "reachability probe" in probe["detail"]

    def test_http_403_flows_through_run_checks_as_pass(self, monkeypatch):
        self._patch_dependencies(monkeypatch)
        error = urllib.error.HTTPError("https://openrouter.ai", 403, "Forbidden", None, None)
        _patch_urlopen(monkeypatch, error)
        checks = _run_checks(load_settings())
        probe = next(item for item in checks if item["name"] == "openrouter_https_reachability")
        assert probe["ok"] is True
        assert "HTTP status=403" in probe["detail"]


def _optional_fail_checks():
    return [
        {"name": "config_valid", "ok": True, "required": True, "detail": "settings.json consistent"},
        {
            "name": "openrouter_https_reachability",
            "ok": False,
            "required": False,
            "detail": "HTTPS reachability probe: unreachable (no HTTP response: dns)",
        },
    ]


class TestSummaryAndOutput:
    def test_optional_failure_does_not_change_strict_ok(self):
        summary = _summarize(_optional_fail_checks())
        assert summary["strict_ok"] is True
        assert summary["optional_ok"] is False
        # Historical "ok" still means every check passed.
        assert summary["ok"] is False

    def test_required_failure_changes_strict_ok(self):
        checks = _optional_fail_checks()
        checks[0]["ok"] = False
        checks[1]["ok"] = True
        summary = _summarize(checks)
        assert summary["strict_ok"] is False
        assert summary["optional_ok"] is True
        assert summary["ok"] is False

    def test_human_output_labels_required_and_optional(self):
        checks = _optional_fail_checks()
        out = _format_human(checks, _summarize(checks))
        assert "[PASS] config_valid (required)" in out
        assert "[FAIL] openrouter_https_reachability (optional)" in out
        assert "required=PASS" in out
        assert "optional=FAIL" in out
        # overall agrees with the required class that gates --strict.
        assert "overall=PASS" in out

    def test_human_output_overall_fails_when_required_fails(self):
        checks = _optional_fail_checks()
        checks[0]["ok"] = False
        out = _format_human(checks, _summarize(checks))
        assert "required=FAIL" in out
        assert "overall=FAIL" in out


class TestMainCli:
    def _stub(self, monkeypatch, checks):
        monkeypatch.setattr(preflight, "load_settings", lambda: {})
        monkeypatch.setattr(preflight, "_run_checks", lambda settings: checks)

    def test_strict_exit_zero_when_only_optional_fails(self, monkeypatch, capsys):
        self._stub(monkeypatch, _optional_fail_checks())
        monkeypatch.setattr(sys, "argv", ["preflight.py", "--strict"])
        preflight.main()  # must not raise for an optional-only failure
        out = capsys.readouterr().out
        assert "optional=FAIL" in out
        assert "overall=PASS" in out

    def test_strict_exit_nonzero_when_required_fails(self, monkeypatch, capsys):
        checks = _optional_fail_checks()
        checks[0]["ok"] = False
        self._stub(monkeypatch, checks)
        monkeypatch.setattr(sys, "argv", ["preflight.py", "--strict"])
        with pytest.raises(SystemExit) as excinfo:
            preflight.main()
        assert excinfo.value.code == 1
        assert "overall=FAIL" in capsys.readouterr().out

    def test_json_reports_optional_ok_separately(self, monkeypatch, capsys):
        self._stub(monkeypatch, _optional_fail_checks())
        monkeypatch.setattr(sys, "argv", ["preflight.py", "--json"])
        preflight.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["strict_ok"] is True
        assert payload["optional_ok"] is False
        assert payload["ok"] is False
        assert payload["checks"][1]["name"] == "openrouter_https_reachability"

    def test_json_403_reachability_reports_all_ok(self, monkeypatch, capsys):
        checks = _optional_fail_checks()
        checks[1]["ok"] = True
        checks[1]["detail"] = "HTTPS reachability probe: reachable (HTTP status=403)"
        self._stub(monkeypatch, checks)
        monkeypatch.setattr(sys, "argv", ["preflight.py", "--json"])
        preflight.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["strict_ok"] is True
        assert payload["optional_ok"] is True

    def test_end_to_end_http_403_is_not_reported_as_failure(self, monkeypatch, capsys):
        # Exercise the real CLI/summary path with only the hardware/host probes
        # stubbed to a healthy state; the HTTPS probe must see the 403.
        monkeypatch.setattr(preflight, "_check_spi", lambda: True)
        monkeypatch.setattr(preflight, "_check_waveshare", lambda candidates: (True, "fake_module"))
        error = urllib.error.HTTPError("https://openrouter.ai", 403, "Forbidden", None, None)
        _patch_urlopen(monkeypatch, error)
        monkeypatch.setattr(sys, "argv", ["preflight.py", "--strict"])
        preflight.main()  # real settings + real _run_checks, must exit 0
        out = capsys.readouterr().out
        assert "[PASS] openrouter_https_reachability (optional)" in out
        assert "HTTP status=403" in out
        assert "failed" not in out.lower()
        assert "overall=PASS" in out
