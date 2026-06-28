#!/usr/bin/env python3
"""Autorating harness for the e-ink weather board.

Drives the REAL pipeline (Open-Meteo -> aggregate -> transform -> LLM brief ->
LLM image -> compose board) for one or more prompt VARIANTS across several
cities, then scores every rendered board with a vision LLM judge and writes a
side-by-side report.

It is built to A/B prompt changes: edit the candidate templates / style pool,
re-run, and read the delta. Variants:
  baseline  -> config/prompt_templates/weather_brief.txt + weather_image.txt
               + the live ART_STYLE_POOL in generate_image.py
  candidate -> *.candidate.txt templates + the reworded candidate style pool
               (all the proposed fixes applied)

Usage:
  python3 scripts/eval/run_eval.py                      # both variants, all cities
  python3 scripts/eval/run_eval.py --variants candidate # candidate only
  python3 scripts/eval/run_eval.py --cities tokyo,london
  python3 scripts/eval/run_eval.py --no-image           # reuse/skip image gen
  python3 scripts/eval/run_eval.py --no-judge           # render only, no scoring

Outputs land under runtime/eval/<timestamp>/ with per-city artifacts and a
report.md.
"""
import argparse
import base64
import copy
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, get_openrouter_api_key, load_settings, write_json
from scripts.openrouter.network import urlopen_with_context
import scripts.openrouter.generate_brief as gb
import scripts.openrouter.generate_image as gi
import scripts.render.compose_board as cb
from scripts.weather.fetch_weather import fetch_forecast
from scripts.weather.aggregate_weather_sources import build_aggregated_context
from scripts.weather.transform_weather import build_payload

import urllib.request


# --- Variant definitions -----------------------------------------------------

PROMPT_DIR = ROOT / "config" / "prompt_templates"

# Per-variant style-pool overrides for the next experiment. Empty means the
# candidate uses the live pool (the De Stijl reword + coherence-focused negative
# constraints were locked into generate_image.py). To A/B a new style tweak,
# add an entry here (same name as a live pool style) and/or set CANDIDATE_NEGATIVE.
CANDIDATE_STYLE_OVERRIDES = {}

CANDIDATE_NEGATIVE = gi.NEGATIVE_STYLE_CONSTRAINTS

VARIANTS = {
    "baseline": {
        "brief_template": PROMPT_DIR / "weather_brief.txt",
        "image_template": PROMPT_DIR / "weather_image.txt",
        "style_pool": {s["name"]: s for s in gi.ART_STYLE_POOL},
        "negative": gi.NEGATIVE_STYLE_CONSTRAINTS,
    },
    "candidate": {
        "brief_template": PROMPT_DIR / "weather_brief.candidate.txt",
        "image_template": PROMPT_DIR / "weather_image.candidate.txt",
        "style_pool": {
            **{s["name"]: s for s in gi.ART_STYLE_POOL},
            **CANDIDATE_STYLE_OVERRIDES,
        },
        "negative": CANDIDATE_NEGATIVE,
    },
}


# --- Pipeline stages ---------------------------------------------------------

def _city_settings(base, city, city_dir):
    """A per-city/variant deep copy with isolated, absolute runtime paths."""
    s = copy.deepcopy(base)
    s["location"] = {
        "label": city["label"],
        "latitude": city["latitude"],
        "longitude": city["longitude"],
        "timezone": city["timezone"],
    }
    s.setdefault("context", {})["location_descriptor"] = city.get("location_descriptor", city["label"])
    # No live web grounding during eval, for a clean and repeatable A/B.
    s["context"]["events_mode"] = "off"
    # The Pi runs a tight brief timeout (8s) to protect the refresh cadence; an
    # eval should give the model room so timeouts don't masquerade as prompt
    # quality (a timeout silently falls back to deterministic text).
    s["pipeline"]["brief_timeout_seconds"] = max(s["pipeline"].get("brief_timeout_seconds", 8), 45)
    s["pipeline"]["image_timeout_seconds"] = max(s["pipeline"].get("image_timeout_seconds", 20), 60)
    rt = {}
    for key in (
        "payload_file", "brief_context_file", "brief_file", "hero_file",
        "final_file", "preview_file", "stale_file", "log_file",
    ):
        suffix = {
            "payload_file": "payload.json",
            "brief_context_file": "brief_context.json",
            "brief_file": "brief.json",
            "hero_file": "hero.png",
            "final_file": "board.png",
            "preview_file": "preview.png",
            "stale_file": "status.json",
            "log_file": "log.txt",
        }[key]
        rt[key] = str(city_dir / suffix)
    rt["dir"] = str(city_dir)
    # Isolate from the live board's history / caches.
    rt["history_file"] = None
    rt["day_context_file"] = None
    rt["last_good_brief_file"] = None
    s["runtime"] = rt
    return s


def _inject_style(template, illustration_prompt, style, negative):
    style_block = (
        f"Selected art style: {style['name']}\n"
        f"Style direction: {style['prompt']}\n"
        f"{negative}"
    )
    prompt = template.replace("{{IMAGE_PROMPT}}", illustration_prompt.strip())
    if "{{STYLE_GUIDANCE}}" in prompt:
        return prompt.replace("{{STYLE_GUIDANCE}}", style_block)
    return prompt + "\n\nStyle guidance:\n" + style_block


def _compose(city_settings, brief_path, board_path):
    """Reuse compose_board.main with this city's settings + IO."""
    orig_load = cb.load_settings
    orig_argv = sys.argv
    cb.load_settings = lambda: city_settings
    sys.argv = ["compose_board", "--input", brief_path, "--output", board_path]
    try:
        cb.main()
    finally:
        cb.load_settings = orig_load
        sys.argv = orig_argv


def run_pipeline(variant_name, variant, base_settings, city, city_dir, do_image):
    """Run one variant for one city. Returns a result dict (never raises)."""
    city_dir.mkdir(parents=True, exist_ok=True)
    result = {"variant": variant_name, "city": city["id"], "errors": []}
    s = _city_settings(base_settings, city, city_dir)
    text_model = s["openrouter"]["text_model"]

    # 1. Real weather.
    try:
        raw = fetch_forecast(s)
        om_payload = {
            "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "location": s["location"],
            "source": "open-meteo",
            "raw": raw,
        }
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"fetch: {exc}")
        return result

    # 2-3. Aggregate + transform (deterministic baseline brief + day_context).
    context = build_aggregated_context(s, om_payload, {}, {})
    transformed = build_payload(context)
    deterministic = transformed["brief"]
    daily = transformed["today"]["daily_summary"]
    result["weather"] = {
        "condition": daily["condition"],
        "temp_max_c": daily["temp_max_c"],
        "temp_min_c": daily["temp_min_c"],
        "rain_prob_max_pct": daily["rain_prob_max_pct"],
        "rain_window": deterministic.get("rain_window"),
        "part_of_day": transformed.get("day_context", {}).get("part_of_day"),
    }

    # 4. LLM brief with the variant's template.
    brief_template = variant["brief_template"].read_text(encoding="utf-8")
    try:
        enriched = gb._enrich_payload(transformed, s)
        prompt = gb._render_prompt(brief_template, enriched)
        candidate_brief = gb._call_openrouter(s, prompt, model_override=text_model)
        if gb._is_valid_brief(candidate_brief):
            transformed["brief"] = candidate_brief
            transformed["brief_source"] = "openrouter"
        else:
            transformed["brief_source"] = "deterministic_fallback_invalid_schema"
            result["errors"].append("brief: invalid schema, used deterministic")
    except Exception as exc:  # noqa: BLE001
        transformed["brief_source"] = "deterministic_fallback_error"
        result["errors"].append(f"brief: {exc}")

    brief = transformed["brief"]
    result["headline"] = brief.get("headline", "")
    result["subtitle"] = brief.get("subtitle", "")
    result["illustration_prompt"] = brief.get("illustration_prompt", "")
    result["brief_source"] = transformed.get("brief_source")
    write_json(s["runtime"]["brief_file"], transformed)

    # 5. LLM image with the variant's template + forced style.
    style_name = city.get("force_style")
    style = variant["style_pool"].get(style_name)
    result["style"] = style_name
    hero_path = Path(s["runtime"]["hero_file"])
    if do_image and style and brief.get("illustration_prompt"):
        image_template = variant["image_template"].read_text(encoding="utf-8")
        img_prompt = _inject_style(
            image_template, brief["illustration_prompt"], style, variant["negative"]
        )
        (city_dir / "image_prompt.txt").write_text(img_prompt, encoding="utf-8")
        provider = s["pipeline"].get("image_provider", "fal")
        try:
            image_bytes = gi._call_image_api(s, img_prompt, provider)
            hero_path.write_bytes(image_bytes)
            result["hero_ok"] = True
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"image: {exc}")
            result["hero_ok"] = False
    else:
        result["hero_ok"] = hero_path.exists()
        if not do_image:
            result["errors"].append("image: skipped (--no-image)")

    # 6. Compose the actual board.
    try:
        _compose(s, s["runtime"]["brief_file"], s["runtime"]["final_file"])
        result["board"] = s["runtime"]["final_file"]
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"compose: {exc}")
    return result


# --- Judge -------------------------------------------------------------------

JUDGE_INSTRUCTIONS = """You are rating ONE rendered e-ink weather board (the image) plus its title text.

BOARD LAYOUT -- read this so you judge the right region:
- The bottom strip (roughly the lower 25%) holds the big black HEADLINE and a
  smaller subtitle. The system adds this. Do NOT count it as part of the artwork.
- Small rounded chips in the top-left (the date) and top-right (temperature plus
  a tiny weather icon) are also added by the system. IGNORE them entirely.
- Everything else -- the large background illustration above/behind those -- is
  the AI-generated ART you are evaluating. Judge ONLY that region for the art
  scores below.

THE GOALS:
- Title (headline + subtitle): ~80% INFORMATIVE / ~20% whimsy. A reader should
  know today's ACTUAL weather from the words alone, with a light touch of voice.
  Pure poetry with no facts is bad; a dry numbers dump is also bad.
- Background art: ONE single, cohesive illustration in ONE flat graphic poster
  style, filling the whole canvas as a single image.

HARD RULES -- apply strictly:
- COLLAGE / FRAME / PHOTO-IN-BLOCK: if the background art is a collage, OR places
  a realistic or photographic image inside a graphic frame, border, grid, or
  block (i.e. two competing aesthetics in one image), set
  art_collage_or_frame=true and art_coherence MUST be <= 3.
- BAKED-IN TEXT: if the background ARTWORK contains ANY letters, words, numbers,
  or fake/garbled lettering (this does NOT include the system headline strip or
  the corner chips described above), set art_text_detected=true and quote what
  you see in art_text_sample. Real or fake text inside the artwork is a defect;
  knock art_flat_graphic and overall down for it.

GROUND-TRUTH WEATHER for this board:
{weather}

TITLE SHOWN ON THE BOARD:
  headline: {headline}
  subtitle: {subtitle}

Score each 0-10 (higher is better) and return STRICT JSON only, no markdown:
{{
  "title_informativeness": <0-10, can a reader tell today's real weather from the words?>,
  "title_whimsy": <0-10, is there voice/character?>,
  "title_pct_informative": <0-100, your estimate of the info-vs-whimsy split; target ~80>,
  "art_coherence": <0-10, single unified piece? MUST be <=3 if art_collage_or_frame>,
  "art_collage_or_frame": <true|false>,
  "art_text_detected": <true|false, any letters/words/numbers baked into the artwork>,
  "art_text_sample": <quote the text you see in the artwork, or "">,
  "art_flat_graphic": <0-10, flat poster, no photorealism/3D/gradient>,
  "art_palette_fit": <0-10, reads cleanly in black/white/red/yellow>,
  "art_matches_weather": <0-10, art reflects the real conditions>,
  "overall": <0-10>,
  "issues": [<short concrete problems, especially any collage/photo/frame/text>],
  "one_line": <one sentence verdict>
}}"""


def _weather_str(w):
    return (
        f"{w.get('condition')}, high {w.get('temp_max_c')}C / low {w.get('temp_min_c')}C, "
        f"rain chance {w.get('rain_prob_max_pct')}%, {w.get('rain_window') or 'no rain window'}, "
        f"time of day: {w.get('part_of_day')}"
    )


def judge(result, settings, judge_model, timeout):
    board_path = result.get("board")
    if not board_path or not Path(board_path).exists():
        return {"error": "no board image to judge"}
    img_b64 = base64.b64encode(Path(board_path).read_bytes()).decode()
    instructions = JUDGE_INSTRUCTIONS.format(
        weather=_weather_str(result.get("weather", {})),
        headline=result.get("headline", ""),
        subtitle=result.get("subtitle", ""),
    )
    body = {
        "model": judge_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instructions},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ],
            }
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    url = settings["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
    key = get_openrouter_api_key(settings)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urlopen_with_context(req, timeout=timeout, settings=settings) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```", 2)[1].lstrip("json").strip()
        return json.loads(content)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# --- Reporting ---------------------------------------------------------------

NUMERIC_METRICS = [
    "title_informativeness", "title_whimsy", "title_pct_informative",
    "art_coherence", "art_flat_graphic", "art_palette_fit",
    "art_matches_weather", "overall",
]


def _avg(values):
    nums = [v for v in values if isinstance(v, (int, float))]
    return round(sum(nums) / len(nums), 1) if nums else None


def build_report(run_dir, variants, cities, results, scores, meta):
    lines = []
    lines.append("# Weather board prompt eval\n")
    lines.append(f"- Run: `{meta['timestamp']}`")
    lines.append(f"- Text model: `{meta['text_model']}`  |  Image: `{meta['image_provider']}/{meta['image_model']}`  |  Judge: `{meta['judge_model']}`")
    lines.append(f"- Variants: {', '.join(variants)}  |  Cities: {', '.join(c['id'] for c in cities)}\n")

    # Aggregate table.
    lines.append("## Aggregate scores (avg across cities)\n")
    header = "| metric | " + " | ".join(variants) + " | delta |"
    sep = "|" + "---|" * (len(variants) + 2)
    lines.append(header)
    lines.append(sep)
    for metric in NUMERIC_METRICS:
        row = [metric]
        per_variant = {}
        for v in variants:
            vals = [scores.get((v, c["id"]), {}).get(metric) for c in cities]
            per_variant[v] = _avg(vals)
            row.append("-" if per_variant[v] is None else f"{per_variant[v]}")
        if len(variants) == 2 and all(per_variant[v] is not None for v in variants):
            delta = round(per_variant[variants[1]] - per_variant[variants[0]], 1)
            row.append(f"{'+' if delta >= 0 else ''}{delta}")
        else:
            row.append("")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("Title-length compliance (headline <=52, subtitle <=72) + ASCII:\n")
    for v in variants:
        oks, ascii_oks = [], []
        for c in cities:
            r = results.get((v, c["id"]), {})
            h, sub = r.get("headline", ""), r.get("subtitle", "")
            oks.append(len(h) <= 52 and len(sub) <= 72)
            ascii_oks.append(h.isascii() and sub.isascii())
        lines.append(f"- {v}: {sum(oks)}/{len(cities)} within length, {sum(ascii_oks)}/{len(cities)} pure ASCII")
    lines.append("")

    # Guardrail / trust flags: the harness is only trustworthy if it CATCHES the
    # known failure modes. Count them per variant so a glance says how clean it is.
    lines.append("## Guardrail flags (lower is better)\n")
    lines.append("| flag | " + " | ".join(variants) + " |")
    lines.append("|" + "---|" * (len(variants) + 1))
    for flag, label in (
        ("art_collage_or_frame", "collage / photo-in-frame"),
        ("art_text_detected", "baked-in text in art"),
    ):
        row = [label]
        for v in variants:
            hits = [c["id"] for c in cities if scores.get((v, c["id"]), {}).get(flag) is True]
            row.append(f"{len(hits)}/{len(cities)}" + (f" ({', '.join(hits)})" if hits else ""))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    # Laundry watch: count how often the chore motif shows up in prose or art.
    lines.append("Laundry-motif watch (headline+subtitle+art prompt mentions):\n")
    for v in variants:
        hits = []
        for c in cities:
            r = results.get((v, c["id"]), {})
            blob = " ".join([r.get("headline", ""), r.get("subtitle", ""), r.get("illustration_prompt", "")]).lower()
            if any(w in blob for w in ("laundry", "clotheslin", "clothes line", "hanging clothes", "washing line")):
                hits.append(c["id"])
        lines.append(f"- {v}: {len(hits)}/{len(cities)}" + (f" ({', '.join(hits)})" if hits else ""))
    lines.append("")

    # Per-city.
    lines.append("## Per-city detail\n")
    for c in cities:
        w = None
        for v in variants:
            r = results.get((v, c["id"]), {})
            if r.get("weather"):
                w = r["weather"]
                break
        lines.append(f"### {c['label']}  (style: {c.get('force_style')})\n")
        if w:
            lines.append(f"Weather: {_weather_str(w)}\n")
        for v in variants:
            r = results.get((v, c["id"]), {})
            sc = scores.get((v, c["id"]), {})
            lines.append(f"**{v}**")
            lines.append(f"- headline ({len(r.get('headline',''))}): `{r.get('headline','')}`")
            lines.append(f"- subtitle ({len(r.get('subtitle',''))}): `{r.get('subtitle','')}`")
            if r.get("illustration_prompt"):
                lines.append(f"- art prompt: {r['illustration_prompt']}")
            if r.get("board"):
                lines.append(f"- board: `{r['board']}`")
            if "error" in sc:
                lines.append(f"- judge: ERROR {sc['error']}")
            else:
                score_bits = ", ".join(f"{m}={sc.get(m)}" for m in NUMERIC_METRICS)
                lines.append(f"- scores: {score_bits}")
                flags = []
                if sc.get("art_collage_or_frame") is True:
                    flags.append("COLLAGE/FRAME")
                if sc.get("art_text_detected") is True:
                    flags.append(f"TEXT-IN-ART: {sc.get('art_text_sample','')!r}")
                if flags:
                    lines.append(f"- guardrail: {'; '.join(flags)}")
                if sc.get("issues"):
                    lines.append(f"- issues: {'; '.join(sc['issues'])}")
                if sc.get("one_line"):
                    lines.append(f"- verdict: {sc['one_line']}")
            if r.get("errors"):
                lines.append(f"- pipeline notes: {'; '.join(r['errors'])}")
            lines.append("")
    return "\n".join(lines)


# --- Main --------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variants", default="baseline,candidate")
    parser.add_argument("--cities", default=None, help="comma list of city ids; default all")
    parser.add_argument("--out", default="runtime/eval")
    parser.add_argument("--text-model", default=None)
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--no-image", action="store_true")
    parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    if args.text_model:
        settings["openrouter"]["text_model"] = args.text_model
    judge_model = args.judge_model or settings["openrouter"].get("image_tool_model", settings["openrouter"]["text_model"])

    all_cities = json.loads((Path(__file__).parent / "cities.json").read_text())
    if args.cities:
        wanted = {x.strip() for x in args.cities.split(",")}
        cities = [c for c in all_cities if c["id"] in wanted]
    else:
        cities = all_cities
    variants = [v.strip() for v in args.variants.split(",") if v.strip() in VARIANTS]

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = (ROOT / args.out / timestamp).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    results, scores = {}, {}
    judge_timeout = settings["pipeline"].get("image_timeout_seconds", 20) * 2
    for v in variants:
        for c in cities:
            city_dir = run_dir / v / c["id"]
            print(f"[{v}/{c['id']}] running pipeline...", flush=True)
            t0 = time.time()
            r = run_pipeline(v, VARIANTS[v], settings, c, city_dir, do_image=not args.no_image)
            results[(v, c["id"])] = r
            print(f"[{v}/{c['id']}] headline: {r.get('headline','')!r} ({time.time()-t0:.1f}s)"
                  + (f"  errors: {r['errors']}" if r.get("errors") else ""), flush=True)
            if not args.no_judge:
                sc = judge(r, settings, judge_model, judge_timeout)
                scores[(v, c["id"])] = sc
                write_json(str(city_dir / "judge.json"), sc if isinstance(sc, dict) else {"raw": sc})
                if "error" not in sc:
                    print(f"[{v}/{c['id']}] judge overall={sc.get('overall')} "
                          f"info={sc.get('title_informativeness')} coherence={sc.get('art_coherence')}", flush=True)
                else:
                    print(f"[{v}/{c['id']}] judge error: {sc['error']}", flush=True)

    meta = {
        "timestamp": timestamp,
        "text_model": settings["openrouter"]["text_model"],
        "image_provider": settings["pipeline"].get("image_provider"),
        "image_model": settings.get("fal", {}).get("image_model") if settings["pipeline"].get("image_provider") in ("fal", "fai") else settings["openrouter"].get("image_model"),
        "judge_model": judge_model,
    }
    write_json(str(run_dir / "results.json"),
               {"meta": meta,
                "results": {f"{v}/{c}": results[(v, c)] for (v, c) in results},
                "scores": {f"{v}/{c}": scores.get((v, c)) for (v, c) in results}})
    report = build_report(run_dir, variants, cities, results, scores, meta)
    report_path = run_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport: {report_path}")
    print(f"Artifacts: {run_dir}")


if __name__ == "__main__":
    main()
