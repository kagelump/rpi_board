#!/usr/bin/env python3
"""Compare image-generation MODELS on the weather board (not prompt variants).

Feeds ONE fixed illustration prompt + style (identical to every model, built with
the live image template) to each model N times, composes the real board, scores
it with the vision judge, and records cost + latency + a link to each final
board. Use it to pick an image model.

Cost notes:
- fal does NOT return a price in its response, so fal costs are CONFIGURED
  ESTIMATES from PRICE_USD below -- edit them to match your billing.
- OpenRouter returns usage.cost; we record it as the API-reported cost (it covers
  the orchestration call; the upstream image charge may bill separately).
Latency is always measured wall-clock.

Usage:
  python3 scripts/eval/compare_models.py                 # all 6 models, 3 runs
  python3 scripts/eval/compare_models.py --runs 2 --models fal-ai/flux/schnell
  python3 scripts/eval/compare_models.py --style "Pictogram Minimalism"
"""
import argparse
import base64
import copy
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, get_fal_api_key, get_openrouter_api_key, load_settings, write_json
import scripts.openrouter.generate_image as gi
import scripts.eval.run_eval as ev
import scripts.render.palette_metrics as pm


MODELS = [
    {"id": "fal-ai/krea-2/turbo", "provider": "fal"},
    {"id": "fal-ai/flux-2/turbo", "provider": "fal", "label": "flux-2/turbo (current prod)"},
    {"id": "fal-ai/flux-2/flash", "provider": "fal"},
    {"id": "fal-ai/flux-2/klein/9b", "provider": "fal"},
    {"id": "fal-ai/z-image/turbo", "provider": "fal"},
    {"id": "fal-ai/flux/schnell", "provider": "fal"},
    {"id": "openai/gpt-image-2", "provider": "openrouter",
     "label": "gpt-image-2 (low)", "params": {"quality": "low", "size": "1536x1024"}},
]

# Best-effort per-image USD estimates as of 2026-06. fal returns no cost in its
# response, so these are CONFIGURED guesses -- edit to match your fal billing.
# gpt-image-2's cost is taken from the OpenRouter response, not this table.
PRICE_USD = {
    "fal-ai/krea-2/turbo": 0.010,
    "fal-ai/flux-2/turbo": 0.006,
    "fal-ai/flux-2/flash": 0.005,
    "fal-ai/flux-2/klein/9b": 0.004,
    "fal-ai/z-image/turbo": 0.004,
    "fal-ai/flux/schnell": 0.003,
    "openai/gpt-image-2": 0.011,  # gpt-image-2 low-quality estimate (fallback)
}

# Fixed, reproducible scenario so the only variable is the model.
SCENARIO = {
    "style": "Minimal Ukiyo-e",
    "illustration_prompt": (
        "A lone red umbrella on a rain-swept city street at dusk, puddles "
        "reflecting cool streetlight, a low heavy grey sky pressing down, distant "
        "buildings as flat silhouettes. Bold, minimal, generous negative space."
    ),
    "headline": "Heavy rain through midday, 23C",
    "subtitle": "23C and soaking through lunch; skip the riverside walk.",
    "weather": {
        "condition": "Heavy rain", "temp_max_c": 23.0, "temp_min_c": 21.0,
        "rain_prob_max_pct": 90, "rain_window": "Heavy rain likely 09:00-11:00",
        "part_of_day": "midday",
    },
    "weather_code": 65,
    "date_pretty": "Sunday, June 28",
}


def build_prompt():
    template = (ROOT / "config" / "prompt_templates" / "weather_image.txt").read_text(encoding="utf-8")
    style = next((s for s in gi.ART_STYLE_POOL if s["name"] == SCENARIO["style"]), gi.ART_STYLE_POOL[0])
    return gi._inject_style_prompt(template, SCENARIO["illustration_prompt"], style)


def _download(url, timeout=120):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def gen_fal(settings, model_id, prompt):
    """Returns (image_bytes, meta). Raises on failure."""
    key = get_fal_api_key(settings)
    body = {"prompt": prompt, "image_size": {"width": 1280, "height": 640},
            "num_images": 1, "output_format": "png"}
    req = urllib.request.Request(
        f"https://fal.run/{model_id}", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Key {key}", "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    url = payload["images"][0]["url"]
    image_bytes = _download(url)
    latency = time.time() - t0
    return image_bytes, {
        "latency_s": round(latency, 2),
        "inference_s": round(payload.get("timings", {}).get("inference", 0), 2),
        "seed": payload.get("seed"),
        "api_cost_usd": None,
        "est_cost_usd": PRICE_USD.get(model_id),
    }


def gen_openrouter_image(settings, model, prompt):
    """gpt-image-style via the OpenRouter image-generation tool. (bytes, meta)."""
    key = get_openrouter_api_key(settings)
    tool_model = settings["openrouter"].get("image_tool_model", settings["openrouter"]["text_model"])
    params = {"model": model["id"], **model.get("params", {})}
    body = {"model": tool_model, "input": prompt,
            "tools": [{"type": "openrouter:image_generation", "parameters": params}]}
    url = settings["openrouter"]["base_url"].rstrip("/") + "/responses"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    latency = time.time() - t0
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    image_bytes = None
    img_url = gi._extract_image_url(payload)
    if img_url:
        image_bytes = _download(img_url)
    else:
        data_img = gi._extract_data_image(payload)
        if data_img:
            image_bytes = base64.b64decode(data_img.split(";base64,", 1)[1])
    if image_bytes is None:
        raise RuntimeError("no image in response")
    api_cost = payload.get("usage", {}).get("cost")
    return image_bytes, {
        "latency_s": round(latency, 2),
        "inference_s": None,
        "seed": None,
        "api_cost_usd": round(api_cost, 5) if isinstance(api_cost, (int, float)) else None,
        "est_cost_usd": PRICE_USD.get(model["id"]),
    }


def _synthetic_payload():
    return {
        "generated_at_local": datetime.now().replace(microsecond=0).isoformat(),
        "brief": {"headline": SCENARIO["headline"], "subtitle": SCENARIO["subtitle"], "accent": "red"},
        "today": {"daily_summary": {
            "condition": SCENARIO["weather"]["condition"], "weather_code": SCENARIO["weather_code"],
            "temp_max_c": SCENARIO["weather"]["temp_max_c"], "temp_min_c": SCENARIO["weather"]["temp_min_c"],
            "date": "2026-06-28"}},
        "day_context": {"date_pretty": SCENARIO["date_pretty"]},
        "brief_source": "openrouter",
    }


def _run_settings(base, run_dir):
    s = copy.deepcopy(base)
    rt = {k: str(run_dir / v) for k, v in {
        "hero_file": "hero.png", "final_file": "board.png", "preview_file": "preview.png",
        "stale_file": "status.json", "brief_file": "brief.json"}.items()}
    rt["dir"] = str(run_dir)
    s["runtime"] = rt
    return s


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--models", default=None, help="comma list of model ids; default all")
    parser.add_argument("--style", default=None, help="override the fixed art style")
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--out", default="runtime/eval_models")
    parser.add_argument("--append-to", default=None,
                        help="path to an existing run dir; run only new/selected models and "
                             "merge them into that run's results + report")
    args = parser.parse_args()

    settings = load_settings()
    judge_model = args.judge_model or settings["openrouter"].get("image_tool_model")

    rows = {}  # model_id -> list of per-run dicts
    if args.append_to:
        # Reuse the existing run's prompt + style so the appended model gets the
        # identical input. The scenario is deterministic, so the data is comparable.
        run_dir = Path(args.append_to).resolve()
        existing = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
        rows = existing.get("rows", {})
        meta = existing["meta"]
        SCENARIO["style"] = meta["style"]
        judge_model = args.judge_model or meta.get("judge_model", judge_model)
        prompt = (run_dir / "prompt.txt").read_text(encoding="utf-8")
        if args.models:
            wanted = {m.strip() for m in args.models.split(",")}
            models = [m for m in MODELS if m["id"] in wanted]
        else:
            models = [m for m in MODELS if m["id"] not in rows]
        print(f"Appending models to {run_dir}: {[m['id'] for m in models]}")
    else:
        if args.style:
            SCENARIO["style"] = args.style
        models = MODELS
        if args.models:
            wanted = {m.strip() for m in args.models.split(",")}
            models = [m for m in MODELS if m["id"] in wanted]
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = (ROOT / args.out / timestamp).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt = build_prompt()
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        meta = {"timestamp": timestamp, "runs": args.runs, "style": SCENARIO["style"], "judge_model": judge_model}

    for model in models:
        rows[model["id"]] = []
        for i in range(args.runs):
            tag = f"{model.get('label', model['id'])} run{i + 1}"
            mdir = run_dir / model["id"].replace("/", "_") / f"run{i + 1}"
            mdir.mkdir(parents=True, exist_ok=True)
            entry = {"run": i + 1, "model": model["id"]}
            try:
                if model["provider"] == "fal":
                    image_bytes, gmeta = gen_fal(settings, model["id"], prompt)
                else:
                    image_bytes, gmeta = gen_openrouter_image(settings, model, prompt)
                entry.update(gmeta)
                # Deterministic colour metrics on the raw art (not the board).
                entry["palette"] = pm.analyze(image_bytes)
                s = _run_settings(settings, mdir)
                Path(s["runtime"]["hero_file"]).write_bytes(image_bytes)
                write_json(s["runtime"]["brief_file"], _synthetic_payload())
                ev._compose(s, s["runtime"]["brief_file"], s["runtime"]["final_file"])
                entry["board"] = s["runtime"]["final_file"]
                result = {"board": entry["board"], "weather": SCENARIO["weather"],
                          "headline": SCENARIO["headline"], "subtitle": SCENARIO["subtitle"]}
                sc = ev.judge(result, settings, judge_model, 60)
                entry["scores"] = sc
                pal = entry["palette"]
                print(f"[{tag}] {gmeta['latency_s']}s overall={sc.get('overall')} "
                      f"coherence={sc.get('art_coherence')} balance={pal['palette_balance']} "
                      f"off-pal={pal['off_palette_pct']*100:.0f}% text={sc.get('art_text_detected')}", flush=True)
            except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as exc:
                entry["error"] = str(exc)
                print(f"[{tag}] ERROR {exc}", flush=True)
            rows[model["id"]].append(entry)

    write_json(str(run_dir / "results.json"), {"meta": meta, "scenario": SCENARIO, "rows": rows})
    # Report covers every model present (existing + appended), in MODELS order.
    models_for_report = [m for m in MODELS if m["id"] in rows]
    report = build_report(run_dir, models_for_report, rows, meta)
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    print(f"\nReport: {run_dir / 'report.md'}")


def _avg(vals, nd=2):
    nums = [v for v in vals if isinstance(v, (int, float))]
    return round(sum(nums) / len(nums), nd) if nums else None


def build_report(run_dir, models, rows, meta):
    L = []
    L.append("# Image-model comparison\n")
    L.append(f"- Run: `{meta['timestamp']}`  |  runs/model: {meta['runs']}  |  fixed style: `{meta['style']}`  |  judge: `{meta['judge_model']}`")
    L.append(f"- Same prompt + style fed to every model (see prompt.txt). Identical board layout/text.\n")
    L.append("> Cost: fal returns no price -> **est. $/img is a configured guess** (edit PRICE_USD). "
             "gpt-image-2 cost is OpenRouter-reported. Latency is measured wall-clock.\n")

    L.append("## Summary (averaged over runs)\n")
    L.append("> `balance` 0-10 = uses all four inks without looking monochrome. "
             "`off-pal%` = share of art in hues the panel can't show (blue/green/etc). "
             "`W/K/R/Y` = avg ink coverage %.\n")
    L.append("| model | overall | coherence | flat | balance | off-pal% | W/K/R/Y | text? | latency s | est $/img |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    summary = []
    for model in models:
        rs = rows[model["id"]]
        oks = [r for r in rs if "scores" in r and "error" not in (r.get("scores") or {})]
        pals = [r["palette"] for r in rs if isinstance(r.get("palette"), dict)]
        def sc_avg(k):
            return _avg([r["scores"].get(k) for r in oks])
        text_hits = sum(1 for r in oks if r["scores"].get("art_text_detected") is True)
        balance = _avg([p["palette_balance"] for p in pals], nd=1)
        offpal = _avg([p["off_palette_pct"] * 100 for p in pals], nd=1)
        cov = {name: _avg([p["coverage"][name] * 100 for p in pals], nd=0) for name, _ in pm.DEVICE_PALETTE} if pals else {}
        cov_str = "/".join(str(int(cov[name])) for name, _ in pm.DEVICE_PALETTE) if cov else "-"
        lat = _avg([r.get("latency_s") for r in rs])
        est = _avg([r.get("est_cost_usd") for r in rs], nd=4)
        overall = sc_avg("overall")
        summary.append((model, overall))
        label = model.get("label", model["id"])
        n = len(rs)
        L.append(f"| `{label}` | {overall} | {sc_avg('art_coherence')} | {sc_avg('art_flat_graphic')} | "
                 f"{balance} | {offpal} | {cov_str} | {text_hits}/{n} | {lat} | "
                 f"{est if est is not None else '-'} |")
    L.append("")
    ranked = sorted([s for s in summary if s[1] is not None], key=lambda x: x[1], reverse=True)
    if ranked:
        L.append("Ranked by avg overall: " + " > ".join(f"`{m.get('label', m['id'])}` ({o})" for m, o in ranked) + "\n")

    L.append("## Per-run detail (with board links)\n")
    for model in models:
        L.append(f"### {model.get('label', model['id'])}\n")
        for r in rows[model["id"]]:
            if "error" in r:
                L.append(f"- run {r['run']}: ERROR {r['error']}")
                continue
            sc = r.get("scores", {})
            flags = []
            if sc.get("art_text_detected"): flags.append("TEXT")
            if sc.get("art_collage_or_frame"): flags.append("COLLAGE")
            board = r.get("board", "")
            pal = r.get("palette", {})
            pal_str = ""
            if pal:
                pal_str = (f" | balance={pal['palette_balance']} off-pal={pal['off_palette_pct']*100:.0f}%"
                           + (f" ({pal['off_palette_hue']})" if pal.get("off_palette_hue") else "")
                           + f" W/K/R/Y={pm.coverage_str(pal['coverage'])}")
            L.append(f"- run {r['run']}: overall={sc.get('overall')} coherence={sc.get('art_coherence')} "
                     f"flat={sc.get('art_flat_graphic')}{pal_str} | {r.get('latency_s')}s | est ${r.get('est_cost_usd')}"
                     + (f" | api ${r.get('api_cost_usd')}" if r.get("api_cost_usd") is not None else "")
                     + (f" | {'/'.join(flags)}" if flags else "")
                     + (f"\n  - verdict: {sc.get('one_line')}" if sc.get("one_line") else "")
                     + f"\n  - board: [{Path(board).name}]({board})  \n    ![run]({board})")
        L.append("")
    L.append("## Fixed prompt\n```\n" + (run_dir / "prompt.txt").read_text(encoding="utf-8") + "\n```")
    return "\n".join(L)


if __name__ == "__main__":
    main()
