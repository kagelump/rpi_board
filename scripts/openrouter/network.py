#!/usr/bin/env python3
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


# Message fragments used only as a last resort when an error arrives without a
# typed reason (for example a bare RuntimeError string from a proxy).
_DNS_MARKERS = (
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "no address associated with hostname",
    "getaddrinfo failed",
    "name resolution",
)
_TLS_MARKERS = ("certificate verify failed", "certificate", "ssl", "tls")
_READ_TIMEOUT_MARKERS = ("the read operation timed out", "timed out", "timeout")
_CONNECT_MARKERS = (
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection closed",
    "network is unreachable",
    "no route to host",
)

# Cap the diagnostic HTTP error body read so an error response can never pull in
# an unbounded amount of data.
_HTTP_ERROR_BODY_LIMIT = 4096


class DeadlineExceeded(TimeoutError):
    """A request did not finish before its wall-clock deadline.

    ``scope`` records which budget actually expired so callers can tell a
    retryable per-attempt timeout (``"attempt"``) apart from exhaustion of the
    whole stage budget (``"stage"``).
    """

    def __init__(self, message, *, scope="request"):
        super().__init__(message)
        self.scope = scope


class NetworkRequestError(RuntimeError):
    """A categorized network failure that is safe to log and retry on."""

    def __init__(self, message, *, category="unknown", retryable=True, original=None):
        super().__init__(message)
        self.category = category
        self.retryable = bool(retryable)
        self.original = original


class WallClockDeadline:
    """A start-anchored monotonic budget shared by every attempt in a stage."""

    def __init__(self, total_seconds, *, clock=time.monotonic):
        self._clock = clock
        self.total_seconds = max(0.0, float(total_seconds))
        self._start = clock()
        self._deadline_at = self._start + self.total_seconds

    @property
    def deadline_at(self):
        return self._deadline_at

    @property
    def elapsed_seconds(self):
        return self._clock() - self._start

    @property
    def remaining_seconds(self):
        return self._deadline_at - self._clock()

    @property
    def expired(self):
        return self.remaining_seconds <= 0.0


def classify_network_error(error):
    """Return a stable failure category (dns/connect/tls/read_timeout/...).

    Categories are deliberately coarse: callers only use them to pick backoff
    and to describe the failure in logs.
    """
    if isinstance(error, NetworkRequestError):
        return error.category
    if isinstance(error, urllib.error.HTTPError):
        return "http"

    reason = getattr(error, "reason", None)
    for candidate in (reason, error):
        if candidate is None:
            continue
        if isinstance(candidate, ssl.SSLError):  # covers SSLCertVerificationError
            return "tls"
        if isinstance(candidate, socket.gaierror):
            return "dns"
        if isinstance(candidate, (TimeoutError, socket.timeout)):
            return "read_timeout"
        if isinstance(candidate, ConnectionError):
            return "connect"

    message = str(error).lower()
    if any(marker in message for marker in _DNS_MARKERS):
        return "dns"
    if any(marker in message for marker in _TLS_MARKERS):
        return "tls"
    if any(marker in message for marker in _READ_TIMEOUT_MARKERS):
        return "read_timeout"
    if any(marker in message for marker in _CONNECT_MARKERS):
        return "connect"
    return "unknown"


def failure_category(error):
    """Category for any exception raised by an attempt (not just network ones)."""
    category = getattr(error, "category", None)
    if category:
        return category
    if isinstance(error, DeadlineExceeded):
        if error.scope == "attempt":
            return "attempt_timeout"
        if error.scope == "stage":
            return "budget_exhausted"
        return "deadline"
    if isinstance(error, (urllib.error.URLError, OSError, TimeoutError)):
        return classify_network_error(error)
    if isinstance(error, ValueError):  # JSON/Unicode decode of a malformed body
        return "invalid_response"
    return "unknown"


def _http_error_status(error):
    """Format an HTTPError without ever reading its response body."""
    return f"HTTP {error.code} {error.reason}"


def _invoke_cancel(cancel):
    """Best-effort teardown that never raises into the deadline path."""
    try:
        cancel()
    except Exception:
        pass


def run_with_deadline(func, deadline, *, clock=time.monotonic, cancel=None, scope="request"):
    """Run ``func`` and return its value, aborting at a monotonic ``deadline``.

    ``func`` runs on a daemon worker so a peer that keeps trickling bytes can
    never hold the caller past the deadline, even though the socket timeout only
    bounds individual reads. The worker is a daemon and is never joined without a
    timeout: the caller raises at the deadline and the pipeline proceeds, so an
    abandoned request cannot keep production work alive. When ``cancel`` is
    provided it is invoked best-effort on a separate daemon thread on expiry, to
    tear the abandoned response down sooner (for example by closing its socket)
    without ever blocking the caller. Any exception from ``func`` is re-raised in
    the caller.
    """
    if deadline is None:
        return func()

    remaining = deadline - clock()
    if remaining <= 0:
        raise DeadlineExceeded("request deadline already expired", scope=scope)

    outcome = {}

    def _run():
        try:
            outcome["value"] = func()
        except BaseException as error:  # noqa: BLE001 - re-raised on caller thread
            outcome["error"] = error

    thread = threading.Thread(target=_run, daemon=True, name="openrouter-deadline")
    thread.start()
    thread.join(remaining)
    if "error" in outcome:
        raise outcome["error"]
    if "value" in outcome:
        return outcome["value"]

    if cancel is not None:
        # Fire-and-forget: ``cancel`` may close a buffered response whose lock
        # the abandoned worker still holds, so it must never run on (or block)
        # the caller thread. The hard deadline still wins.
        try:
            threading.Thread(
                target=_invoke_cancel, args=(cancel,), daemon=True, name="openrouter-cancel"
            ).start()
        except Exception:
            pass
    raise DeadlineExceeded("request exceeded its wall-clock deadline", scope=scope)


def fetch_bytes_with_deadline(
    request_or_url, *, timeout, settings, deadline=None, clock=time.monotonic, scope="request"
):
    """Open a request and read the full body under an optional wall-clock bound.

    ``timeout`` still bounds each socket operation; ``deadline`` (monotonic
    absolute) bounds the whole open+read sequence. HTTP error bodies are read
    (capped) inside the same bounded worker rather than by the caller, so no
    response body can be consumed after the stage deadline.
    """
    holder = {}

    def _fetch():
        try:
            with urlopen_with_context(request_or_url, timeout=timeout, settings=settings) as response:
                holder["response"] = response
                try:
                    return response.read()
                finally:
                    holder.pop("response", None)
        except urllib.error.HTTPError as error:
            message = _http_error_status(error)
            try:
                body = error.read(_HTTP_ERROR_BODY_LIMIT).decode("utf-8", errors="replace").strip()
            except Exception:
                body = ""
            finally:
                try:
                    error.close()
                except Exception:
                    pass
            if body:
                message = f"{message}: {body}"
            raise NetworkRequestError(
                message, category="http", retryable=True, original=error
            ) from error

    def _cancel():
        response = holder.get("response")
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    return run_with_deadline(_fetch, deadline, clock=clock, cancel=_cancel, scope=scope)


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


def describe_network_error(error, *, read_body=True):
    # HTTPError carries a response body that usually names the offending field
    # (e.g. fal 422 validation detail); surface it instead of a bare status.
    # Callers on a hard deadline pass ``read_body=False`` so formatting an error
    # can never block on a slow/trickling response body.
    if isinstance(error, urllib.error.HTTPError):
        base = _http_error_status(error)
        if not read_body:
            return base
        try:
            body = error.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        return f"{base}: {body}" if body else base

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
