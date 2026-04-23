#!/usr/bin/env python3
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path


def build_ssl_context(settings):
    ctx = ssl.create_default_context()
    ca_bundle = settings.get("openrouter", {}).get("ca_bundle_file")
    if ca_bundle:
        ca_path = Path(ca_bundle).expanduser()
        if ca_path.exists():
            ctx.load_verify_locations(cafile=str(ca_path))
            return ctx

    # Python installs on macOS can miss system trust linkage; certifi is a solid default.
    try:
        import certifi  # type: ignore

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


def proxy_hint():
    keys = [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]
    present = [f"{k}={os.getenv(k)}" for k in keys if os.getenv(k)]
    if not present:
        return "none"
    return ", ".join(present)


def describe_network_error(error):
    reason = getattr(error, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (
            "TLS certificate verification failed. If you are behind a proxy with custom "
            "root certificates, configure openrouter.ca_bundle_file in config/settings.json "
            "to point to that CA bundle. Active proxy env: "
            + proxy_hint()
        )
    message = str(error)
    if "Tunnel connection failed: 403" in message or "CONNECT tunnel failed" in message:
        return (
            "Proxy tunnel rejected OpenRouter (HTTP 403). Check proxy allowlist/policy "
            "for openrouter.ai. Active proxy env: "
            + proxy_hint()
        )
    return message


def urlopen_with_context(request_or_url, timeout, settings):
    return urllib.request.urlopen(request_or_url, timeout=timeout, context=build_ssl_context(settings))
