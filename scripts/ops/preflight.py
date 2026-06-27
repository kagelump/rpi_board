#!/usr/bin/env python3
import argparse
import importlib
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import get_openrouter_api_key, load_settings


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


def _check_openrouter_https(timeout):
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen("https://openrouter.ai", timeout=timeout, context=ctx) as response:
            return 200 <= response.status < 500, f"HTTPS status={response.status}"
    except urllib.error.URLError as error:
        return False, f"HTTPS request failed: {error}"


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

    https_ok, https_detail = _check_openrouter_https(settings["pipeline"]["image_timeout_seconds"])
    checks.append({"name": "openrouter_https", "ok": https_ok, "required": False, "detail": https_detail})
    return checks


def main():
    parser = argparse.ArgumentParser(description="Run Raspberry Pi weather board preflight checks.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any check fails.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args()

    settings = load_settings()
    checks = _run_checks(settings)
    ok = all(item["ok"] for item in checks)
    strict_ok = all(item["ok"] for item in checks if item.get("required", True))

    if args.json:
        print(json.dumps({"ok": ok, "strict_ok": strict_ok, "checks": checks}, ensure_ascii=True, indent=2))
    else:
        for item in checks:
            status = "PASS" if item["ok"] else "FAIL"
            print(f"[{status}] {item['name']}: {item['detail']}")
        print(f"overall={'PASS' if ok else 'FAIL'}")

    if args.strict and not strict_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
