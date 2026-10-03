"""Tests for scripts/openrouter/network.py"""
import os
import socket
import ssl
import time
import urllib.error

import pytest

from scripts.openrouter.network import (
    DeadlineExceeded,
    NetworkRequestError,
    WallClockDeadline,
    build_ssl_context,
    classify_network_error,
    describe_network_error,
    failure_category,
    fetch_bytes_with_deadline,
    proxy_hint,
    run_with_deadline,
)


# ---------------------------------------------------------------------------
# proxy_hint
# ---------------------------------------------------------------------------

class TestProxyHint:
    def test_no_proxy_vars(self, monkeypatch):
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(key, raising=False)
        assert proxy_hint() == "none"

    def test_single_proxy_var(self, monkeypatch):
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:8080")
        result = proxy_hint()
        assert "HTTPS_PROXY" in result
        assert "http://proxy.example.com:8080" in result

    def test_multiple_proxy_vars(self, monkeypatch):
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HTTP_PROXY", "http://a:3128")
        monkeypatch.setenv("HTTPS_PROXY", "http://b:3128")
        result = proxy_hint()
        assert "HTTP_PROXY" in result
        assert "HTTPS_PROXY" in result


# ---------------------------------------------------------------------------
# build_ssl_context
# ---------------------------------------------------------------------------

class TestBuildSslContext:
    def test_configured_ca_bundle_file_is_loaded(self, monkeypatch, tmp_path):
        ca_bundle = tmp_path / "custom-ca.pem"
        ca_bundle.write_text("dummy CA bundle contents")
        loaded = []

        class FakeContext:
            def load_verify_locations(self, **kwargs):
                loaded.append(kwargs)

        monkeypatch.setattr(ssl, "create_default_context", lambda: FakeContext())
        ctx = build_ssl_context({"openrouter": {"ca_bundle_file": str(ca_bundle)}})

        assert isinstance(ctx, FakeContext)
        assert loaded == [{"cafile": str(ca_bundle)}]

    def test_missing_ca_bundle_falls_back_to_certifi(self, monkeypatch, tmp_path):
        # A configured but nonexistent path must not be handed to OpenSSL; the
        # builder falls back to certifi (a dev/test requirement) or the default
        # trust store.
        missing = tmp_path / "nope.pem"
        loaded = []

        class FakeContext:
            def load_verify_locations(self, **kwargs):
                loaded.append(kwargs)

        monkeypatch.setattr(ssl, "create_default_context", lambda: FakeContext())
        ctx = build_ssl_context({"openrouter": {"ca_bundle_file": str(missing)}})

        assert isinstance(ctx, FakeContext)
        assert loaded, "expected a certifi/default trust store load"
        assert loaded[0].get("cafile")
        assert loaded[0]["cafile"] != str(missing)


# ---------------------------------------------------------------------------
# describe_network_error
# ---------------------------------------------------------------------------

class TestDescribeNetworkError:
    def _url_error(self, reason=None, message=""):
        err = urllib.error.URLError(reason or message)
        if reason is not None:
            err.reason = reason
        return err

    def test_ssl_cert_verification_error(self, monkeypatch):
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("ALL_PROXY", raising=False)
        monkeypatch.delenv("http_proxy", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.delenv("all_proxy", raising=False)

        ssl_err = ssl.SSLCertVerificationError("certificate verify failed")
        err = self._url_error(reason=ssl_err)
        result = describe_network_error(err)
        assert "TLS certificate verification failed" in result
        assert "ca_bundle_file" in result
        assert "proxy env: none" in result

    def test_proxy_tunnel_403(self):
        err = urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
        result = describe_network_error(err)
        assert "403" in result
        assert "proxy" in result.lower()

    def test_proxy_connect_tunnel_failed(self):
        err = urllib.error.URLError("CONNECT tunnel failed: 407")
        result = describe_network_error(err)
        assert "proxy" in result.lower()

    def test_generic_error_returns_string(self):
        err = urllib.error.URLError("Connection refused")
        result = describe_network_error(err)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_no_reason_attribute(self):
        err = urllib.error.URLError("some generic message")
        # reason will be the string, not an SSLCertVerificationError
        result = describe_network_error(err)
        assert isinstance(result, str)

    def test_http_error_surfaces_body(self):
        import io

        body = b'{"detail":[{"loc":["body","num_inference_steps"],"msg":"too big"}]}'
        err = urllib.error.HTTPError(
            url="https://fal.run/x", code=422, msg="Unprocessable Entity",
            hdrs=None, fp=io.BytesIO(body),
        )
        result = describe_network_error(err)
        assert "422" in result
        assert "num_inference_steps" in result

    def test_http_error_status_without_body_read_is_instant(self):
        import io

        reads = []

        class StallingBody(io.RawIOBase):
            def readable(self):
                return True

            def read(self, *args, **kwargs):
                reads.append(1)
                time.sleep(2.0)
                return b"late"

        err = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1",
            code=503,
            msg="Service Unavailable",
            hdrs=None,
            fp=StallingBody(),
        )
        start = time.monotonic()
        result = describe_network_error(err, read_body=False)
        assert time.monotonic() - start < 1.0
        assert reads == []  # body was never touched
        assert "503" in result

    def test_includes_proxy_hint_when_proxy_set(self, monkeypatch):
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")

        ssl_err = ssl.SSLCertVerificationError("cert failed")
        err = self._url_error(reason=ssl_err)
        result = describe_network_error(err)
        assert "corp-proxy" in result


# ---------------------------------------------------------------------------
# classify_network_error / failure_category
# ---------------------------------------------------------------------------

class TestClassifyNetworkError:
    def test_dns_gaierror_reason(self):
        err = urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution"))
        assert classify_network_error(err) == "dns"

    def test_dns_gaierror_direct(self):
        assert classify_network_error(socket.gaierror(-2, "Name or service not known")) == "dns"

    def test_connect_refused(self):
        err = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        assert classify_network_error(err) == "connect"

    def test_read_timeout_reason(self):
        err = urllib.error.URLError(TimeoutError("The read operation timed out"))
        assert classify_network_error(err) == "read_timeout"

    def test_read_timeout_direct(self):
        assert classify_network_error(TimeoutError("timed out")) == "read_timeout"

    def test_tls_certificate_error(self):
        err = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
        assert classify_network_error(err) == "tls"

    def test_http_error(self):
        import io

        err = urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1", code=429, msg="Too Many Requests",
            hdrs=None, fp=io.BytesIO(b"slow down"),
        )
        assert classify_network_error(err) == "http"

    def test_network_request_error_uses_its_category(self):
        err = NetworkRequestError("boom", category="dns")
        assert classify_network_error(err) == "dns"

    def test_unknown_error(self):
        assert classify_network_error(RuntimeError("something odd")) == "unknown"


class TestFailureCategory:
    def test_deadline_exceeded(self):
        assert failure_category(DeadlineExceeded("late")) == "deadline"

    def test_network_request_error_category(self):
        assert failure_category(NetworkRequestError("x", category="read_timeout")) == "read_timeout"

    def test_os_error_classified(self):
        assert failure_category(TimeoutError("timed out")) == "read_timeout"

    def test_malformed_response_value_error(self):
        assert failure_category(ValueError("bad json")) == "invalid_response"

    def test_generic_runtime_error_is_unknown(self):
        assert failure_category(RuntimeError("nope")) == "unknown"


# ---------------------------------------------------------------------------
# WallClockDeadline / run_with_deadline / fetch_bytes_with_deadline
# ---------------------------------------------------------------------------

class TestWallClockDeadline:
    def test_remaining_and_expiry(self):
        now = [100.0]
        deadline = WallClockDeadline(10.0, clock=lambda: now[0])
        assert deadline.remaining_seconds == 10.0
        now[0] = 106.0
        assert deadline.remaining_seconds == 4.0
        assert deadline.elapsed_seconds == 6.0
        assert deadline.deadline_at == 110.0
        now[0] = 111.0
        assert deadline.expired is True


class TestRunWithDeadline:
    def test_returns_value_when_fast(self):
        assert run_with_deadline(lambda: 42, time.monotonic() + 1.0) == 42

    def test_propagates_errors(self):
        def boom():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            run_with_deadline(boom, time.monotonic() + 1.0)

    def test_none_deadline_runs_directly(self):
        assert run_with_deadline(lambda: "ok", None) == "ok"

    def test_slow_call_is_cut_off_within_bound(self):
        start = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            run_with_deadline(lambda: time.sleep(2.0), time.monotonic() + 0.15)
        assert time.monotonic() - start < 1.0

    def test_already_expired_does_not_invoke(self):
        calls = []
        with pytest.raises(DeadlineExceeded):
            run_with_deadline(lambda: calls.append(1), time.monotonic() - 1.0)
        assert calls == []

    def test_timeout_invokes_cancel_and_records_scope(self):
        cancelled = []
        start = time.monotonic()
        with pytest.raises(DeadlineExceeded) as excinfo:
            run_with_deadline(
                lambda: time.sleep(2.0),
                time.monotonic() + 0.1,
                cancel=lambda: cancelled.append(1),
                scope="stage",
            )
        assert time.monotonic() - start < 1.0
        deadline = time.monotonic() + 1.0
        while not cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cancelled == [1]
        assert excinfo.value.scope == "stage"

    def test_blocking_cancel_cannot_extend_the_deadline(self):
        def blocking_cancel():
            time.sleep(2.0)

        start = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            run_with_deadline(
                lambda: time.sleep(2.0),
                time.monotonic() + 0.1,
                cancel=blocking_cancel,
            )
        assert time.monotonic() - start < 1.0


class TestFetchBytesWithDeadline:
    def test_http_error_body_is_deadline_bounded(self, monkeypatch):
        import io

        class StallingBody(io.RawIOBase):
            def readable(self):
                return True

            def read(self, *args, **kwargs):
                time.sleep(2.0)
                return b"late body"

        def raises_http_error(*args, **kwargs):
            raise urllib.error.HTTPError(
                url="https://example.test",
                code=502,
                msg="Bad Gateway",
                hdrs=None,
                fp=StallingBody(),
            )

        monkeypatch.setattr(
            "scripts.openrouter.network.urlopen_with_context", raises_http_error
        )
        start = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            fetch_bytes_with_deadline(
                "https://example.test", timeout=5.0, settings={}, deadline=time.monotonic() + 0.1
            )
        assert time.monotonic() - start < 1.0

    def test_http_error_body_reads_inside_worker_when_fast(self, monkeypatch):
        import io

        def raises_http_error(*args, **kwargs):
            raise urllib.error.HTTPError(
                url="https://example.test",
                code=422,
                msg="Unprocessable Entity",
                hdrs=None,
                fp=io.BytesIO(b'{"detail": "bad model"}'),
            )

        monkeypatch.setattr(
            "scripts.openrouter.network.urlopen_with_context", raises_http_error
        )
        with pytest.raises(NetworkRequestError) as excinfo:
            fetch_bytes_with_deadline("https://example.test", timeout=1.0, settings={})
        assert excinfo.value.category == "http"
        assert excinfo.value.retryable is True
        assert "422" in str(excinfo.value)
        assert "bad model" in str(excinfo.value)

    def test_expired_fetch_closes_the_response(self, monkeypatch):
        closed = []

        class TrickleResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                end = time.monotonic() + 2.0
                while time.monotonic() < end:
                    time.sleep(0.01)
                return b"late body"

            def close(self):
                closed.append(1)

        monkeypatch.setattr(
            "scripts.openrouter.network.urlopen_with_context", lambda *a, **k: TrickleResponse()
        )
        start = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            fetch_bytes_with_deadline(
                "https://example.test", timeout=5.0, settings={}, deadline=time.monotonic() + 0.3
            )
        assert time.monotonic() - start < 1.0
        # Cancellation runs on a separate daemon thread so it can never extend
        # the hard caller deadline; wait briefly for it to complete.
        wait_until = time.monotonic() + 1.0
        while not closed and time.monotonic() < wait_until:
            time.sleep(0.01)
        assert closed == [1]  # best-effort cancellation tore the request down

    def test_reads_body(self, monkeypatch):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"ok": true}'

        monkeypatch.setattr("scripts.openrouter.network.urlopen_with_context", lambda *a, **k: Response())
        body = fetch_bytes_with_deadline("https://example.test", timeout=1.0, settings={})
        assert body == b'{"ok": true}'

    def test_trickling_body_cannot_defeat_deadline(self, monkeypatch):
        class TrickleResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                # Never returns within the deadline: keeps dribbling bytes.
                end = time.monotonic() + 2.0
                while time.monotonic() < end:
                    time.sleep(0.01)
                return b"late body"

        monkeypatch.setattr(
            "scripts.openrouter.network.urlopen_with_context", lambda *a, **k: TrickleResponse()
        )
        start = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            fetch_bytes_with_deadline(
                "https://example.test", timeout=5.0, settings={}, deadline=time.monotonic() + 0.15
            )
        assert time.monotonic() - start < 1.0
