"""Tests for scripts/openrouter/network.py"""
import os
import ssl
import urllib.error

import pytest

from scripts.openrouter.network import describe_network_error, proxy_hint


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

    def test_includes_proxy_hint_when_proxy_set(self, monkeypatch):
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")

        ssl_err = ssl.SSLCertVerificationError("cert failed")
        err = self._url_error(reason=ssl_err)
        result = describe_network_error(err)
        assert "corp-proxy" in result
