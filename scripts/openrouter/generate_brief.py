#!/usr/bin/env python3
import argparse
import json
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, get_openrouter_api_key, load_settings, read_json, write_json
from scripts.history.store import record_current_log, record_current_snapshot
from scripts.openrouter.network import (
    DeadlineExceeded,
    NetworkRequestError,
    WallClockDeadline,
    classify_network_error,
    describe_network_error,
    failure_category,
    fetch_bytes_with_deadline,
)
from scripts.ops.render_gate import compute_signature, should_regenerate
from scripts.render.compose_board import fit_panel_copy


_PUNCT_MAP = {
    "–": "-", "—": "-", "‒": "-", "−": "-",  # dashes
    "‘": "'", "’": "'", "“": '"', "”": '"',  # smart quotes
    "…": "...",  # ellipsis
    " ": " ",  # nbsp
}


def _normalize_brief_punct(brief):
    """Fold common non-ASCII punctuation to ASCII so the board never renders a
    dropped glyph. The model is told ASCII-only, but it slips occasionally."""
    if not isinstance(brief, dict):
        return brief
    for key in ("headline", "subtitle", "illustration_prompt", "event_ref"):
        value = brief.get(key)
        if isinstance(value, str):
            for bad, good in _PUNCT_MAP.items():
                value = value.replace(bad, good)
            brief[key] = value
    return brief


# The strict output contract advertised in config/prompt_templates/weather_brief.txt.
# Runtime validation mirrors it here so an accepted response can never violate the
# schema the model was given (prompt/schema drift is what let issue #6 through).
_BRIEF_REQUIRED_KEYS = (
    "headline",
    "subtitle",
    "illustration_prompt",
    "mood",
    "accent",
    "event_ref",
)
_BRIEF_NONBLANK_KEYS = ("headline", "subtitle", "illustration_prompt")
_BRIEF_ASCII_KEYS = ("headline", "subtitle", "illustration_prompt", "event_ref")
_BRIEF_MAX_LENGTHS = {"headline": 52, "subtitle": 72}
_BRIEF_ENUMS = {
    "mood": frozenset({"calm", "alert", "cozy", "stormy", "festive", "crisp", "muggy"}),
    # The prompt schema offers red|yellow|none; "black" is the external-payload
    # spelling and is intentionally not part of the model contract.
    "accent": frozenset({"red", "yellow", "none"}),
}


def _brief_schema_violations(brief):
    """Return the contract violations in ``brief``; an empty list means valid.

    Covers the deterministic, non-render constraints: exactly the six advertised
    keys, required presence, string types, non-blank copy, ASCII-only text,
    headline/subtitle length caps, and the mood/accent enums.
    """
    if not isinstance(brief, dict):
        return ["response is not a JSON object"]

    violations = []
    for key in sorted(brief):
        if key not in _BRIEF_REQUIRED_KEYS:
            violations.append(
                f"unexpected field {key!r}; schema allows only "
                f"{', '.join(_BRIEF_REQUIRED_KEYS)}"
            )
    for key in _BRIEF_REQUIRED_KEYS:
        if key not in brief:
            violations.append(f"missing required field {key!r}")

    for key in _BRIEF_ASCII_KEYS:
        if key not in brief:
            continue
        value = brief[key]
        if not isinstance(value, str):
            violations.append(f"{key} must be a string (got {type(value).__name__})")
            continue
        if key in _BRIEF_NONBLANK_KEYS and not value.strip():
            violations.append(f"{key} must not be blank")
        if not value.isascii():
            bad = "".join(sorted({ch for ch in value if not ch.isascii()}))
            violations.append(f"{key} contains non-ASCII characters {bad!r}")
        limit = _BRIEF_MAX_LENGTHS.get(key)
        if limit is not None and len(value) > limit:
            violations.append(
                f"{key} is {len(value)} characters, over the {limit}-character limit"
            )

    for key, allowed in _BRIEF_ENUMS.items():
        if key not in brief:
            continue
        value = brief[key]
        if not isinstance(value, str):
            violations.append(f"{key} must be a string enum (got {type(value).__name__})")
        elif value not in allowed:
            violations.append(f"{key}={value!r} is not one of {sorted(allowed)}")

    return violations


def _has_display_settings(settings):
    """True when settings carry enough display geometry to measure panel copy."""
    display = settings.get("display") if isinstance(settings, dict) else None
    if not isinstance(display, dict):
        return False
    return all(isinstance(display.get(key), (int, float)) for key in ("width", "height"))


def _brief_violations(brief, settings=None):
    """Return schema violations plus, when geometry is available, renderability.

    The renderability half reuses compose_board's own font-fitting logic so a
    response that would later raise the readable-fit ``ValueError`` is rejected
    before artwork generation instead. Callers without display geometry still
    get the deterministic schema checks.
    """
    violations = _brief_schema_violations(brief)
    if violations or not _has_display_settings(settings):
        return violations
    try:
        fit_panel_copy(settings, brief["headline"], brief["subtitle"])
    except ValueError as error:
        violations.append(str(error))
    return violations


def _is_valid_brief(brief, settings=None):
    """Return True when ``brief`` satisfies the output contract.

    With no ``settings`` this is the schema-only check used by callers such as
    the eval harness. The live pipeline passes settings so the compositor's own
    renderability check is included before a response is accepted.
    """
    return not _brief_violations(brief, settings)


# Last-resort copy proven to fit at the smallest readable font sizes. Used only
# if the deterministic brief itself cannot be drawn (for example a long Yahoo
# alert or index line), so the scheduled update still publishes rather than
# aborting in compose_board.
_SAFE_FALLBACK_HEADLINE = "Weather update"
_SAFE_FALLBACK_SUBTITLE = "Check the forecast before heading out."


def _renderable_deterministic_brief(settings, deterministic):
    """Return a deterministic brief guaranteed to fit the text panel.

    The transform-derived fallback normally renders, but Yahoo alert/index text
    can occasionally run long. Re-check it with compose_board's own fit logic
    and downgrade only the headline/subtitle to known-safe copy when needed.
    """
    if isinstance(deterministic, dict):
        headline = deterministic.get("headline")
        subtitle = deterministic.get("subtitle")
        if (isinstance(headline, str) and isinstance(subtitle, str)
                and headline.strip() and subtitle.strip()):
            if not _has_display_settings(settings):
                # No geometry to measure against; keep the deterministic copy.
                return deterministic
            try:
                fit_panel_copy(settings, headline, subtitle)
                return deterministic
            except ValueError:
                pass
    safe = dict(deterministic) if isinstance(deterministic, dict) else {}
    safe["headline"] = _SAFE_FALLBACK_HEADLINE
    safe["subtitle"] = _SAFE_FALLBACK_SUBTITLE
    return safe


def _load_recent_history(settings):
    """Return the last N briefs so the model can actively avoid repeating itself."""
    history_path = settings["runtime"].get("history_file")
    if not history_path:
        return []
    window = settings.get("voice", {}).get("history_window", 6)
    try:
        entries = read_json(history_path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(entries, list):
        return []
    return entries[-window:]


def _append_history(settings, day_context, brief):
    history_path = settings["runtime"].get("history_file")
    if not history_path:
        return
    try:
        entries = read_json(history_path)
        if not isinstance(entries, list):
            entries = []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        entries = []
    entries.append(
        {
            "date": day_context.get("date_iso", ""),
            "part_of_day": day_context.get("part_of_day", ""),
            "headline": brief.get("headline", ""),
            "subtitle": brief.get("subtitle", ""),
            "illustration_prompt": brief.get("illustration_prompt", ""),
        }
    )
    # Keep the file bounded; 60 entries is plenty for anti-repetition + debugging.
    write_json(history_path, entries[-60:])


def _load_last_good(settings):
    path = settings["runtime"].get("last_good_brief_file")
    if not path:
        return {}
    try:
        data = read_json(path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_last_good(settings, signature, generated_at, brief):
    path = settings["runtime"].get("last_good_brief_file")
    if not path:
        return
    write_json(path, {"signature": signature, "generated_at": generated_at, "brief": brief})


def _load_day_context_extra(settings):
    """Load holidays/moon/calendar produced by fetch_context.py (optional)."""
    path = settings["runtime"].get("day_context_file")
    if not path:
        return {}
    try:
        extra = read_json(path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return extra if isinstance(extra, dict) else {}


def _select_text_model(settings, override=None):
    """Append :online for live web grounding when events_mode requests it."""
    base = override or settings["openrouter"]["text_model"]
    events_mode = settings.get("context", {}).get("events_mode", "off")
    if events_mode == "online_model" and not base.endswith(":online"):
        return base + ":online"
    return base


def _offline_text_model(model):
    """Return the underlying model name without OpenRouter's online variant."""
    suffix = ":online"
    return model[:-len(suffix)] if model.endswith(suffix) else model


def _brief_model_attempts(settings, override=None):
    """Build the ordered brief-model attempt list.

    The configured online model gets one attempt. Any failure then falls back to
    the same base model without ``:online``. The non-online model may retry the
    configured number of times; when online mode is disabled, those retries
    apply after the initial non-online attempt.
    """
    primary = _select_text_model(settings, override=override)
    offline = _offline_text_model(primary)
    retry_count = max(0, int(settings.get("pipeline", {}).get("brief_offline_retry_count", 2)))
    if primary.endswith(":online"):
        return [primary, *([offline] * retry_count)]
    return [offline] * (retry_count + 1)


_TIME_FRAMES = {
    "morning": "Morning briefing: set up the day ahead and what to wear heading out.",
    "midday": "Midday check-in: how the rest of today actually unfolds from here.",
    "evening": "Evening wind-down: tonight, plus a short look at tomorrow.",
    "night": "Late update: overnight conditions and the shape of tomorrow.",
}


_CREATIVE_ANGLES = [
    "what to wear walking out the door",
    "the commute and getting around",
    "time outdoors: a walk, the park, the riverside",
    "an evening or after-dark beat",
    "a small seasonal observation in the sky or streets",
    "plans with other people",
    "food or a warm/cold drink that fits",
]


_VISUAL_ANGLES = [
    "a wide urban landscape where weather changes the whole scene",
    "an intimate street vignette with a small human story",
    "an expressive human moment outdoors in the weather",
    "an animal or plant reacting to the season",
    "architecture transformed by light, rain, heat, or wind",
    "a close-up seasonal detail that implies the larger weather",
    "an abstract visual metaphor grounded in today's real conditions",
    "a dramatic study of the sky and its relationship to the city",
    "a quiet after-dark scene with one revealing detail",
    "motion: gusts, rain, shadows, people, or objects crossing the frame",
    "a pattern or rhythm found in weather and the built environment",
    "an unexpected outdoor object made expressive by the conditions",
]


_COMPOSITIONS = [
    "extreme close-up with the subject cropped by the frame",
    "wide panorama with a very low horizon",
    "strong diagonal movement from one corner to the other",
    "layered foreground, middle distance, and far weather",
    "tiny focal subject surrounded by purposeful negative space",
    "asymmetrical editorial composition weighted to one edge",
    "bird's-eye view looking down on streets, umbrellas, or shadows",
    "worm's-eye view looking up through architecture, trees, or sky",
    "repeating forms that create a bold visual rhythm",
    "near-symmetrical emblem with one deliberate disruption",
    "split composition contrasting two moments or weather states",
    "edge-to-edge landscape with no isolated central icon",
    "off-center foreground silhouette against an expansive background",
]


def _rotating_choice(options, seed_basis, offset=0):
    """Choose a deterministic daily rotation entry, advancing on real dates."""
    try:
        seed = date.fromisoformat(seed_basis).toordinal()
    except (TypeError, ValueError):
        seed = sum(ord(char) for char in (seed_basis or ""))
    return options[(seed + offset) % len(options)]


def _time_frame(part_of_day):
    """A framing directive so the same weather reads differently across the day."""
    return _TIME_FRAMES.get(part_of_day, _TIME_FRAMES["midday"])


def _enrich_payload(payload, settings):
    """Attach voice, recent history, day context, and a creative angle.

    These ride inside INPUT_JSON (serialised by _render_prompt), so the prompt
    structure and its tests are untouched while the model gets much richer input.
    """
    enriched = dict(payload)
    enriched["voice"] = settings.get("voice", {})
    enriched["board_context"] = settings.get("context", {})
    enriched["recent_history"] = _load_recent_history(settings)
    # Merge the holidays/moon/calendar extras onto the weather-derived day_context.
    extra = _load_day_context_extra(settings)
    if extra:
        merged = dict(payload.get("day_context", {}))
        merged.update({k: v for k, v in extra.items() if k != "fetched_at"})
        enriched["day_context"] = merged
    # Seed on the forecast day only so all refreshes share a coherent daily
    # direction. The pools have different lengths, producing many combinations
    # before a visual angle/composition pairing repeats.
    day_context = payload.get("day_context", {})
    seed_basis = day_context.get("target_date_iso") or day_context.get("date_iso", "")
    enriched["creative_angle"] = _rotating_choice(_CREATIVE_ANGLES, seed_basis)
    enriched["visual_angle"] = _rotating_choice(_VISUAL_ANGLES, seed_basis, offset=3)
    enriched["composition"] = _rotating_choice(_COMPOSITIONS, seed_basis, offset=7)
    enriched["time_frame"] = _time_frame(day_context.get("part_of_day"))
    return enriched


def _render_prompt(template, payload):
    ordered_facts = payload.get("brief_context", {}).get("ordered_facts", [])
    # ORDERED_FACTS is rendered separately below, so drop the duplicate copy
    # from INPUT_JSON. This trims the repeated payload the model has to read
    # without removing anything the template references.
    input_payload = payload
    brief_context = payload.get("brief_context")
    if isinstance(brief_context, dict) and "ordered_facts" in brief_context:
        input_payload = dict(payload)
        input_payload["brief_context"] = {
            key: value for key, value in brief_context.items() if key != "ordered_facts"
        }
    return (
        template
        + "\n\nORDERED_FACTS:\n"
        + json.dumps(ordered_facts, ensure_ascii=True)
        + "\n\nINPUT_JSON:\n"
        + json.dumps(input_payload, ensure_ascii=True)
    )


def _call_openrouter(
    settings, prompt, model_override=None, *, timeout=None, deadline=None, deadline_scope="attempt"
):
    api_key = get_openrouter_api_key(settings)
    if not api_key:
        raise RuntimeError(
            "OpenRouter key not found. Set OPENROUTER_API_KEY or place a key in "
            "~/.openrouter.key or ~/.config/openrouter/api_key"
        )
    if timeout is None:
        timeout = settings["pipeline"]["brief_timeout_seconds"]
    url = settings["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
    model = model_override or settings["openrouter"]["text_model"]
    temperature = settings["openrouter"].get("brief_temperature", 0.85)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    # ``timeout`` bounds each socket operation; ``deadline`` bounds the whole
    # open+read even when the peer trickles bytes to dodge the read timeout.
    # ``deadline_scope`` records whether that deadline is the shared stage
    # budget ("stage") or just this attempt's slice of it ("attempt"), so the
    # failure text/category and retry policy match what actually expired.
    try:
        raw = fetch_bytes_with_deadline(
            request,
            timeout=timeout,
            settings=settings,
            deadline=deadline,
            scope=deadline_scope,
        )
    except DeadlineExceeded as error:
        scope = getattr(error, "scope", deadline_scope)
        if scope == "stage" or deadline_scope == "stage":
            raise NetworkRequestError(
                "openrouter brief request exceeded the brief stage budget",
                category="budget_exhausted",
                retryable=False,
                original=error,
            ) from error
        raise NetworkRequestError(
            "openrouter brief attempt exceeded its per-attempt timeout"
            + (f" ({timeout:.2f}s)" if isinstance(timeout, (int, float)) else ""),
            category="attempt_timeout",
            retryable=True,
            original=error,
        ) from error
    except NetworkRequestError:
        raise
    except OSError as error:
        # Never read an HTTP error body on the caller thread. ``read_body=False``
        # formats just the status, so a slow/trickling error body can never run
        # past the stage deadline while an error is being described.
        raise NetworkRequestError(
            f"openrouter brief request failed: {describe_network_error(error, read_body=False)}",
            category=classify_network_error(error),
            retryable=True,
            original=error,
        ) from error

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"openrouter brief response was not valid JSON: {error}") from error

    content = payload["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("OpenRouter brief response contained no message content")
    return json.loads(content)


# Wall-clock budget for one complete brief stage: every model attempt, every
# response-body read, and every retry delay must fit inside this window. The
# deterministic fallback is effectively instant and runs after the budget.
DEFAULT_BRIEF_TOTAL_BUDGET_SECONDS = 200.0
DEFAULT_BRIEF_RETRY_BACKOFF_SECONDS = 1.5
DEFAULT_BRIEF_RETRY_BACKOFF_MAX_SECONDS = 6.0
# Per-attempt wall-clock caps. The non-online model is the reliable useful
# path and gets a cap above the measured useful-response latency; the
# optional ":online" web-search attempt gets a shorter cap so it can never
# starve the non-online attempts inside the shared stage budget.
DEFAULT_BRIEF_TIMEOUT_SECONDS = 70.0
DEFAULT_BRIEF_ONLINE_TIMEOUT_SECONDS = 40.0

# Network failures get bounded backoff so a dead resolver/route is not hammered
# immediately. Schema/response failures are cheap to re-sample and stay
# immediate, matching the existing retry semantics.
_BACKOFF_FAILURE_CATEGORIES = frozenset(
    {"dns", "connect", "tls", "read_timeout", "timeout", "attempt_timeout", "http", "network"}
)


def _coerce_seconds(value, default):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    return seconds if seconds >= 0 else default


def _brief_total_budget_seconds(settings):
    return _coerce_seconds(
        settings.get("pipeline", {}).get("brief_total_budget_seconds"),
        DEFAULT_BRIEF_TOTAL_BUDGET_SECONDS,
    )


def _brief_configured_timeout_seconds(settings):
    return _coerce_seconds(
        settings.get("pipeline", {}).get("brief_timeout_seconds"),
        DEFAULT_BRIEF_TIMEOUT_SECONDS,
    )


def _brief_retry_backoff_seconds(settings, attempt_index):
    """Exponential, capped backoff for the next retry (attempt_index is 1-based)."""
    pipeline = settings.get("pipeline", {})
    base = _coerce_seconds(
        pipeline.get("brief_retry_backoff_seconds"), DEFAULT_BRIEF_RETRY_BACKOFF_SECONDS
    )
    cap = _coerce_seconds(
        pipeline.get("brief_retry_backoff_max_seconds"), DEFAULT_BRIEF_RETRY_BACKOFF_MAX_SECONDS
    )
    if base <= 0 or cap <= 0:
        return 0.0
    return min(cap, base * (2 ** max(0, attempt_index - 1)))


def _brief_online_timeout_seconds(settings):
    return _coerce_seconds(
        settings.get("pipeline", {}).get("brief_online_timeout_seconds"),
        DEFAULT_BRIEF_ONLINE_TIMEOUT_SECONDS,
    )


def _is_online_model(model_name):
    return isinstance(model_name, str) and model_name.endswith(":online")


def _brief_attempt_timeout_seconds(settings, model_name):
    """Per-attempt wall-clock cap for one scheduled brief model.

    The optional ``:online`` web-search attempt is capped shorter than the
    non-online model and may never exceed the general per-attempt timeout, so
    it cannot consume the window the useful non-online attempts need.
    """
    general = _brief_configured_timeout_seconds(settings)
    if _is_online_model(model_name):
        return min(general, _brief_online_timeout_seconds(settings))
    return general


def _brief_stage_worst_case_seconds(settings, override=None):
    """Worst-case stage duration: every attempt's cap plus capped backoffs.

    Retries only back off after transient network failures, so this is the
    upper bound ``brief_total_budget_seconds`` must cover for every scheduled
    attempt to keep its full cap.
    """
    models = _brief_model_attempts(settings, override=override)
    total = sum(_brief_attempt_timeout_seconds(settings, name) for name in models)
    for index in range(1, len(models)):
        total += _brief_retry_backoff_seconds(settings, index)
    return total


def _request_brief_with_fallback(
    settings,
    prompt,
    signature,
    model_override=None,
    *,
    deadline=None,
    clock=time.monotonic,
    sleep_fn=time.sleep,
):
    """Request a valid brief, falling back from online to non-online attempts.

    The whole stage -- every attempt, body read, and retry delay -- is bounded by
    a configurable wall-clock budget (``pipeline.brief_total_budget_seconds``).
    Each attempt is bounded by its own per-attempt cap (a shorter one for the
    optional ``:online`` attempt), but never by more than what is left of that
    shared budget. Intermediate failures are warnings so a later successful
    fallback does not mark the run degraded; only exhaustion records an error.
    Returns ``(candidate, metadata)``; candidate is ``None`` on exhaustion or
    an expired budget.
    """
    models = _brief_model_attempts(settings, override=model_override)
    configured_timeout = _brief_configured_timeout_seconds(settings)
    if deadline is None:
        deadline = WallClockDeadline(_brief_total_budget_seconds(settings), clock=clock)

    last_failure = {
        "kind": "error",
        "error_type": "RuntimeError",
        "message": "No model attempts configured",
        "category": "config",
        "budget_exhausted": False,
    }

    for index, model_name in enumerate(models, start=1):
        is_fallback = index > 1
        remaining = deadline.remaining_seconds
        if remaining <= 0:
            if index == 1:
                # The budget expired before anything ran; there is no more
                # specific failure to report.
                last_failure = {
                    "kind": "error",
                    "error_type": "BudgetExhausted",
                    "message": (
                        "brief generation exceeded its total wall-clock budget "
                        "before an attempt could start"
                    ),
                    "category": "budget_exhausted",
                    "budget_exhausted": True,
                }
            else:
                # Keep the underlying failure category; only flag the budget.
                last_failure["budget_exhausted"] = True
            break

        # Bound this attempt by both its per-attempt cap and the shared stage
        # deadline. ``stage_limited`` records which budget is binding so a
        # DeadlineExceeded is described -- and retried -- accurately.
        attempt_timeout = _brief_attempt_timeout_seconds(settings, model_name)
        stage_limited = remaining <= attempt_timeout
        attempt_budget = min(attempt_timeout, remaining)
        attempt_deadline = clock() + attempt_budget
        deadline_scope = "stage" if stage_limited else "attempt"
        request_data = {
            "model": model_name,
            "online_model": _is_online_model(model_name),
            "attempt": index,
            "attempt_count": len(models),
            "fallback": is_fallback,
            "temperature": settings.get("openrouter", {}).get("brief_temperature", 0.85),
            "signature": signature,
            "timeout_seconds": round(attempt_budget, 3),
            "configured_timeout_seconds": attempt_timeout,
            "budget_remaining_seconds": round(remaining, 3),
            "budget_seconds": deadline.total_seconds,
        }
        record_current_log(
            "generate_brief", "model_request", f"Requesting brief from {model_name}",
            data=request_data,
        )
        print(
            f"[brief] requesting OpenRouter model={model_name} attempt={index}/{len(models)} "
            f"timeout={attempt_budget:.2f}s budget_left={remaining:.2f}s"
        )

        started = clock()
        try:
            candidate = _normalize_brief_punct(
                _call_openrouter(
                    settings,
                    prompt,
                    model_override=model_name,
                    timeout=attempt_budget,
                    deadline=attempt_deadline,
                    deadline_scope=deadline_scope,
                )
            )
            record_current_snapshot("brief_model_response", candidate)
            elapsed = clock() - started
            record_current_log(
                "generate_brief", "brief_attempt_succeeded",
                f"Model {model_name} returned a brief",
                data={**request_data, "attempt_elapsed_seconds": round(elapsed, 3)},
            )
        except Exception as error:  # noqa: BLE001 - retry model/network/response failures
            elapsed = clock() - started
            category = failure_category(error)
            retryable = bool(getattr(error, "retryable", True))
            # The stage budget is exhausted when the shared deadline has passed
            # or the failure itself was a stage-budget timeout. A per-attempt
            # timeout that still leaves stage budget stays retryable.
            budget_exhausted = category == "budget_exhausted" or deadline.expired
            if budget_exhausted:
                # Retryability metadata must match the policy: no retry happens
                # once the shared stage budget is gone, even if the underlying
                # attempt timeout was otherwise retryable.
                retryable = False
            will_retry = (index < len(models)) and not budget_exhausted and retryable
            last_failure = {
                "kind": "error",
                "error_type": type(error).__name__,
                "message": str(error),
                "category": category,
                "elapsed_seconds": round(elapsed, 3),
                "budget_exhausted": budget_exhausted,
                "retryable": retryable,
            }
            event_type = "brief_attempt_failed" if will_retry else "brief_request_failed"
            record_current_log(
                "generate_brief", event_type, str(error),
                level="warning" if will_retry else "error",
                data={
                    **request_data,
                    "error_type": type(error).__name__,
                    "failure_category": category,
                    "attempt_elapsed_seconds": round(elapsed, 3),
                    "will_retry": will_retry,
                    "retryable": retryable,
                    "budget_exhausted": budget_exhausted,
                },
            )
            if will_retry:
                print(
                    f"[brief] attempt {index} failed "
                    f"({category}: {error}); backing off/retrying"
                )
                backoff = _brief_retry_backoff_seconds(settings, index)
                if category in _BACKOFF_FAILURE_CATEGORIES and backoff > 0:
                    backoff = min(backoff, max(0.0, deadline.remaining_seconds))
                    if backoff > 0:
                        record_current_log(
                            "generate_brief", "brief_retry_backoff",
                            f"Backing off {backoff:.2f}s before retrying after {category}",
                            data={
                                **request_data,
                                "failure_category": category,
                                "backoff_seconds": round(backoff, 3),
                            },
                        )
                        sleep_fn(backoff)
                continue
            break

        violations = _brief_violations(candidate, settings)
        if not violations:
            return candidate, {
                "kind": "accepted",
                "model": model_name,
                "attempt": index,
                "attempt_count": len(models),
                "fallback": is_fallback,
            }

        elapsed = clock() - started
        detail = "; ".join(violations)
        budget_exhausted = deadline.expired
        will_retry = (index < len(models)) and not budget_exhausted
        last_failure = {
            "kind": "invalid",
            "candidate": candidate,
            "violations": violations,
            "elapsed_seconds": round(elapsed, 3),
            "budget_exhausted": budget_exhausted,
        }
        event_type = "brief_attempt_rejected" if will_retry else "brief_rejected"
        record_current_log(
            "generate_brief", event_type,
            f"Model brief violated the output contract: {detail}",
            level="warning" if will_retry else "error",
            data={
                **request_data,
                "candidate": candidate,
                "violations": violations,
                "attempt_elapsed_seconds": round(elapsed, 3),
                "failure_category": "invalid_response",
                "will_retry": will_retry,
                "budget_exhausted": budget_exhausted,
            },
        )
        if will_retry:
            print(f"[brief] attempt {index} rejected ({detail}); retrying")
        else:
            break

    if deadline.expired or last_failure.get("budget_exhausted"):
        last_failure["budget_exhausted"] = True
        record_current_log(
            "generate_brief", "brief_budget_exhausted",
            "Brief stage exhausted its total wall-clock budget",
            level="warning",
            data={
                "total_elapsed_seconds": round(deadline.elapsed_seconds, 3),
                "budget_seconds": deadline.total_seconds,
                "configured_timeout_seconds": configured_timeout,
                "online_timeout_seconds": _brief_online_timeout_seconds(settings),
                "attempt_count": len(models),
                "error_type": last_failure.get("error_type"),
                "failure_category": last_failure.get("category"),
            },
        )

    return None, last_failure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--force-openrouter", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the regen cache (interval/skip-unchanged) and regenerate now")
    parser.add_argument("--model", default=None, help="Override OpenRouter text model for this run")
    args = parser.parse_args()

    settings = load_settings()
    input_path = args.input or settings["runtime"]["brief_file"]
    output_path = args.output or settings["runtime"]["brief_file"]
    transformed = read_json(input_path)
    deterministic = transformed["brief"]

    use_openrouter = settings["pipeline"]["enable_openrouter_brief"] or args.force_openrouter
    if not use_openrouter:
        # Issue #8: deterministic-only mode bypasses the model-failure recovery
        # below, so run the transform-derived brief through the same
        # compositor-fitting check before it can reach compose_board.
        transformed["brief"] = _renderable_deterministic_brief(settings, deterministic)
        transformed["brief_source"] = "deterministic"
        record_current_log(
            "generate_brief", "deterministic_mode",
            "OpenRouter brief generation is disabled",
            data={"brief": transformed.get("brief", {})},
        )
        write_json(output_path, transformed)
        print(output_path)
        return

    # Cost guardrail: reuse the last good brief when the material inputs are
    # unchanged and we generated recently, so a frequent timer doesn't pay for
    # near-identical output. A new daypart or forecast change forces a refresh.
    signature = compute_signature(transformed)
    last_good = _load_last_good(settings)
    now_iso = transformed.get("generated_at_local")
    regenerate = should_regenerate(
        last_good,
        signature,
        now_iso,
        settings["pipeline"].get("regen_min_interval_seconds", 0),
        force=args.force_openrouter or args.force,
        skip_enabled=settings["pipeline"].get("skip_unchanged", False),
    )
    if not regenerate and isinstance(last_good.get("brief"), dict):
        cached_violations = _brief_violations(last_good["brief"], settings)
        if not cached_violations:
            transformed["brief"] = last_good["brief"]
            transformed["brief_source"] = "cached"
            record_current_log(
                "generate_brief", "brief_reused", "Reused cached brief",
                data={"signature": signature, "generated_at": last_good.get("generated_at")},
            )
            write_json(output_path, transformed)
            print(f"[brief] reusing cached brief (signature {signature} unchanged within interval)")
            print(output_path)
            return
        # A brief cached by an older, weaker validator must not bypass the new
        # contract and fail late in the compositor; regenerate instead.
        record_current_log(
            "generate_brief", "brief_cache_rejected",
            "Cached brief violated the output contract; regenerating",
            level="warning",
            data={"signature": signature, "violations": cached_violations},
        )
        print("[brief] cached brief violated the output contract; regenerating")

    template_path = ROOT / "config" / "prompt_templates" / "weather_brief.txt"
    template = template_path.read_text(encoding="utf-8")
    enriched = _enrich_payload(transformed, settings)
    prompt = _render_prompt(template, enriched)
    record_current_snapshot("brief_generation_input", enriched)
    record_current_snapshot("brief_prompt", prompt, content_type="text/plain; charset=utf-8")

    print(f"[brief] use_openrouter={use_openrouter}")
    candidate, attempt = _request_brief_with_fallback(
        settings, prompt, signature, model_override=args.model
    )
    if candidate is not None:
        transformed["brief"] = candidate
        transformed["brief_source"] = "openrouter"
        _save_last_good(settings, signature, now_iso, candidate)
        _append_history(settings, transformed.get("day_context", {}), candidate)
        record_current_log(
            "generate_brief", "brief_accepted", "Model brief passed validation",
            data={"signature": signature, "brief": candidate, **attempt},
        )
        print("[brief] OpenRouter response accepted: ")
        print(json.dumps(candidate, indent=2, ensure_ascii=True))
    elif attempt.get("kind") == "invalid":
        transformed["brief"] = _renderable_deterministic_brief(settings, deterministic)
        transformed["brief_source"] = "deterministic_fallback_invalid_schema"
        record_current_log(
            "generate_brief", "brief_fallback_deterministic",
            "All model briefs violated the output contract; using a renderable deterministic brief",
            level="warning",
            data={
                "violations": attempt.get("violations", []),
                "failure_category": "invalid_response",
                "budget_exhausted": attempt.get("budget_exhausted", False),
                "headline": transformed["brief"].get("headline", ""),
                "subtitle": transformed["brief"].get("subtitle", ""),
            },
        )
        print("[brief] OpenRouter attempts returned invalid briefs; using deterministic fallback.")
    else:
        transformed["brief"] = _renderable_deterministic_brief(settings, deterministic)
        transformed["brief_source"] = "deterministic_fallback_error"
        record_current_log(
            "generate_brief", "brief_fallback_deterministic",
            "OpenRouter brief attempts failed; using a renderable deterministic brief",
            level="warning",
            data={
                "error_type": attempt.get("error_type"),
                "message": attempt.get("message"),
                "failure_category": attempt.get("category"),
                "budget_exhausted": attempt.get("budget_exhausted", False),
                "attempt_elapsed_seconds": attempt.get("elapsed_seconds"),
            },
        )
        print(
            "[brief] OpenRouter attempts failed "
            f"({attempt.get('category') or attempt.get('error_type')}: "
            f"{attempt.get('message')}); using deterministic fallback."
        )

    write_json(output_path, transformed)
    print(output_path)


if __name__ == "__main__":
    main()
