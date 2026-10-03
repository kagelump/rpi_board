#!/usr/bin/env python3
"""Environment preflight for the weather board.

Checks are split into two classes:

* ``required`` readiness checks gate ``--strict`` success (systemd's
  ``ExecStartPre`` uses ``--strict``).
* ``optional`` diagnostics are reported for operators but never fail a strict
  preflight.

The human-readable summary prints ``required=``, ``optional=`` and ``overall=``;
``overall`` mirrors required readiness so it always agrees with the process
exit status under ``--strict``. JSON keeps the historical ``ok``/``strict_ok``
keys and adds ``optional_ok`` for the same distinction.
"""
import argparse
import importlib
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import get_openrouter_api_key, load_settings
from scripts.openrouter.network import build_ssl_context


def validate_config(settings):
    """Static sanity checks on settings.json. Returns a list of issue strings.

    Pure (no I/O) so it is easy to test and catches config drift like an image
    size that no longer matches the art region after a panel_fraction change.
    """
    issues = []
    display = settings.get("display", {})
    width = display.get("width")
    height = display.get("height")
    if not isinstance(width, int) or width <= 0:
        issues.append("display.width must be a positive integer")
    if not isinstance(height, int) or height <= 0:
        issues.append("display.height must be a positive integer")

    panel_fraction = display.get("panel_fraction", 0.25)
    if not isinstance(panel_fraction, (int, float)) or not (0 < panel_fraction < 1):
        issues.append("display.panel_fraction must be between 0 and 1")
    elif isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        art_h = height - round(height * panel_fraction)
        size = settings.get("fal", {}).get("image_generation_parameters", {}).get("image_size")
        if isinstance(size, dict):
            if size.get("width") != width:
                issues.append(f"fal image_size.width ({size.get('width')}) != display.width ({width})")
            if size.get("height") != art_h:
                issues.append(
                    f"fal image_size.height ({size.get('height')}) != art region ({art_h}); "
                    "art will be cropped or letterboxed"
                )

    provider = settings.get("pipeline", {}).get("image_provider", "openrouter")
    if provider not in ("openrouter", "fal", "fai"):
        issues.append(f"pipeline.image_provider '{provider}' is not recognised")

    events_mode = settings.get("context", {}).get("events_mode", "off")
    if events_mode not in ("online_model", "off"):
        issues.append(f"context.events_mode '{events_mode}' is not recognised")

    return issues


def _check_spi():
    return Path("/dev/spidev0.0").exists()


def _inject_waveshare_paths(path_candidates):
    os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
    existing = []
    for raw in path_candidates:
        candidate = Path(raw).expanduser()
        if not candidate.exists():
            continue
        candidate_s = str(candidate)
        if candidate_s not in sys.path:
            sys.path.append(candidate_s)
        existing.append(candidate_s)
    return existing


def _check_waveshare(candidates):
    for name in candidates:
        try:
            importlib.import_module(name)
            return True, name
        except ImportError:
            continue
    return False, None


def _check_dns():
    try:
        socket.getaddrinfo("openrouter.ai", 443, proto=socket.IPPROTO_TCP)
        return True, "Resolved openrouter.ai"
    except OSError as error:
        return False, f"DNS resolution failed: {error}"


def _check_openrouter_https_reachability(timeout, settings):
    """Non-mutating HTTPS reachability probe for openrouter.ai.

    Issues an unauthenticated GET (``urlopen`` defaults to GET and no API key
    or other credential is attached). Any HTTP status -- including the 403 that
    openrouter.ai currently returns for its root page -- proves that DNS
    resolution, the TCP connection, and the TLS handshake succeeded, so it is
    reported as reachable. ``urlopen`` raises ``HTTPError`` for non-2xx
    responses and ``HTTPError`` subclasses ``URLError``, so it must be handled
    first. This probe deliberately reports transport reachability only; it does
    not authenticate or assert API usability.

    The TLS context comes from the same runtime builder used by production
    OpenRouter requests (:func:`scripts.openrouter.network.build_ssl_context`),
    so a configured ``openrouter.ca_bundle_file`` (and the certifi fallback) is
    honored here too.
    """
    ctx = build_ssl_context(settings)
    try:
        with urllib.request.urlopen("https://openrouter.ai", timeout=timeout, context=ctx) as response:
            return True, f"HTTPS reachability probe: reachable (HTTP status={response.status})"
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            error.close()
        except Exception:
            pass
        return True, f"HTTPS reachability probe: reachable (HTTP status={status})"
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None) or error
        return False, f"HTTPS reachability probe: unreachable (no HTTP response: {reason})"
    except OSError as error:
        return False, f"HTTPS reachability probe: unreachable (no HTTP response: {error})"


# Backwards-compatible private alias for callers/tests that used the old name.
_check_openrouter_https = _check_openrouter_https_reachability


def _run_checks(settings):
    checks = []
    config_issues = validate_config(settings)
    checks.append(
        {
            "name": "config_valid",
            "ok": not config_issues,
            "required": True,
            "detail": "settings.json consistent" if not config_issues else "; ".join(config_issues),
        }
    )

    spi_ok = _check_spi()
    checks.append(
        {
            "name": "spi_device",
            "ok": spi_ok,
            "required": True,
            "detail": "Found /dev/spidev0.0" if spi_ok else "Missing /dev/spidev0.0 (enable SPI via raspi-config)",
        }
    )

    path_hits = _inject_waveshare_paths(settings["display"].get("waveshare_python_lib_candidates", []))
    waveshare_ok, waveshare_name = _check_waveshare(settings["display"]["waveshare_module_candidates"])
    checks.append(
        {
            "name": "waveshare_module",
            "ok": waveshare_ok,
            "required": True,
            "detail": (
                f"Imported {waveshare_name}"
                if waveshare_ok
                else (
                    "No module found in "
                    f"{settings['display']['waveshare_module_candidates']} "
                    f"(python/lib hits: {path_hits or 'none'})"
                )
            ),
        }
    )

    has_key = bool(get_openrouter_api_key(settings))
    checks.append(
        {
            "name": "openrouter_api_key",
            "ok": has_key,
            "required": False,
            "detail": "API key found" if has_key else "No OpenRouter key found",
        }
    )

    dns_ok, dns_detail = _check_dns()
    checks.append({"name": "openrouter_dns", "ok": dns_ok, "required": False, "detail": dns_detail})

    https_ok, https_detail = _check_openrouter_https_reachability(
        settings["pipeline"]["image_timeout_seconds"], settings
    )
    checks.append(
        {
            "name": "openrouter_https_reachability",
            "ok": https_ok,
            "required": False,
            "detail": https_detail,
        }
    )
    return checks


def _summarize(checks):
    """Split checks into required readiness and optional diagnostics.

    ``strict_ok`` (a.k.a. required readiness) is what gates ``--strict``
    success. ``optional_ok`` is diagnostic health only and must never fail a
    strict preflight. ``ok`` remains true only when every check passes, matching
    the historical JSON contract.
    """
    required_ok = all(item["ok"] for item in checks if item.get("required", True))
    optional_ok = all(item["ok"] for item in checks if not item.get("required", True))
    return {"ok": required_ok and optional_ok, "strict_ok": required_ok, "optional_ok": optional_ok}


def _format_human(checks, summary):
    lines = []
    for item in checks:
        status = "PASS" if item["ok"] else "FAIL"
        scope = "required" if item.get("required", True) else "optional"
        lines.append(f"[{status}] {item['name']} ({scope}): {item['detail']}")
    # ``overall`` tracks required readiness so it always agrees with the exit
    # status of ``--strict``. Optional diagnostics are reported separately and
    # never produce an unexplained overall FAIL alongside a zero exit status.
    lines.append(f"required={'PASS' if summary['strict_ok'] else 'FAIL'}")
    lines.append(f"optional={'PASS' if summary['optional_ok'] else 'FAIL'}")
    lines.append(f"overall={'PASS' if summary['strict_ok'] else 'FAIL'}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Run Raspberry Pi weather board preflight checks.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any required check fails (optional diagnostics do not gate success).",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args()

    settings = load_settings()
    checks = _run_checks(settings)
    summary = _summarize(checks)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": summary["ok"],
                    "strict_ok": summary["strict_ok"],
                    "optional_ok": summary["optional_ok"],
                    "checks": checks,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
    else:
        print(_format_human(checks, summary))

    if args.strict and not summary["strict_ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
