#!/usr/bin/env python3
"""Bounded issue #22 A/B: raw fal output, production vision and palette checks.

Both attempts are always sampled for matched-seed retry diagnostics. Operational
fallback uses the retry only when the first candidate is explicitly rejected.
No production runtime files or hardware are touched. Artifacts/prompts are
immutable within an output directory; use a new directory after changing code.
"""
import argparse
import copy
import hashlib
import io
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, load_settings
from scripts.openrouter import generate_image as gi
from scripts.render.palette_metrics import analyze

FIXTURE = ROOT / 'tests/fixtures/issue22_glass_building.json'
SEED_PAIRS = [(910921837, 1664093912), (424242, 424243)]


def scenarios(fixture):
    return [
        {'name': 'glass-pixel', 'subject': fixture['illustration_prompt'],
         'style': 'Retro Pixel Art', 'palette': 'Golden Light'},
        {'name': 'glass-stained', 'subject': fixture['illustration_prompt'],
         'style': 'Stained Glass', 'palette': 'Night Signal'},
        {'name': 'rain-ukiyo', 'subject': (
            'A lone red umbrella on a rain-swept city street at dusk, puddles '
            'reflecting cool streetlight, a low heavy grey sky pressing down, '
            'distant buildings as flat silhouettes. No text.'),
         'style': 'Minimal Ukiyo-e', 'palette': 'Red Signal'},
        {'name': 'trees-linocut', 'subject': (
            'A low viewpoint under green trees beside a river on a clear '
            'afternoon, blue sky reflected in the water, soft golden sunlight '
            'through translucent leaves and a distant walker. No text.'),
         'style': 'Linocut', 'palette': 'Yellow Field'},
    ]


def baseline_prompt(fixture, scene, style, palette):
    # Preserve the archived prompt byte for byte for its original scenario.
    prompt = fixture['archived_prompt'].replace(fixture['illustration_prompt'], scene)
    prompt = prompt.replace(fixture['selected_style']['name'], style['name'])
    prompt = prompt.replace(fixture['selected_style']['prompt'], style['prompt'])
    old = fixture['selected_palette']
    return prompt.replace(f"{old['name']}: {old['prompt']}",
                          f"{palette['name']}: {palette['prompt']}")


def baseline_retry(prompt, reasons):
    clauses = [gi._RETRY_PROMPT_CLAUSES[r] for r in reasons if r in gi._RETRY_PROMPT_CLAUSES]
    if any(r.startswith('off_palette=') for r in reasons):
        clauses.append('The previous attempt used colours the panel cannot show. Use only white, '
                       'black, red, and yellow, with no grey, blue, green, or other hues.')
    return prompt + '\n\nRetry correction:\n- ' + '\n- '.join(clauses) if clauses else prompt


def rejection_reasons(metrics, verdict, threshold):
    reasons = [r for r in ('has_text', 'is_collage') if verdict.get(r)]
    if metrics['off_palette_pct'] > threshold:
        reasons.append(f"off_palette={metrics['off_palette_pct'] * 100:.0f}%")
    return reasons


def evaluate(job, settings, out, threshold):
    variant, scene, seeds, prompt = job
    directory = out / variant / scene['name'] / str(seeds[0])
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / 'result.json').exists():
        raise RuntimeError(f'Output already exists: {directory}; choose a new --out.')
    row = {'variant': variant, 'scenario': scene, 'seeds': seeds, 'attempts': []}
    first_reasons = []
    for attempt, seed in enumerate(seeds, 1):
        current = prompt
        if attempt == 2:
            # Even passing first attempts get a forced palette retry diagnostic.
            correction = first_reasons or ['off_palette=diagnostic']
            current = (baseline_retry if variant == 'baseline' else gi._retry_prompt)(prompt, correction)
        request_settings = copy.deepcopy(settings)
        params = request_settings['fal']['image_generation_parameters']
        params['seed'] = seed
        request = {'model': settings['fal']['image_model'], 'request': {'prompt': current, **params}}
        (directory / f'attempt-{attempt}-request.json').write_text(json.dumps(request, indent=2)+'\n')
        (directory / f'attempt-{attempt}-prompt.txt').write_text(current)
        started = time.monotonic()
        result = {'attempt': attempt, 'seed': seed, 'diagnostic_only': attempt == 2 and not first_reasons}
        try:
            image = gi._call_fal_image_api(request_settings, current)
            (directory / f'attempt-{attempt}.png').write_bytes(image)
            result['image_sha256'] = hashlib.sha256(image).hexdigest()
            result['image_size'] = list(Image.open(io.BytesIO(image)).size)
            result['metrics'] = analyze(image)
            result['vision'] = gi.inspect_art(image, request_settings)
            result['reasons'] = rejection_reasons(result['metrics'], result['vision'], threshold)
            result['status'] = ('rejected' if result['reasons'] else
                                'accepted' if result['vision'].get('status') == 'accepted' else 'unverified')
            if attempt == 1:
                first_reasons = result['reasons']
        except Exception as error:
            result['status'] = 'error'
            result['error'] = f'{type(error).__name__}: {error}'
        result['elapsed_s'] = round(time.monotonic() - started, 2)
        row['attempts'].append(result)
        print(f"{variant} {scene['name']} seed={seed}: {result.get('metrics', {}).get('off_palette_pct')} {result['status']}", flush=True)
    first, retry = row['attempts']
    row['operational_status'] = retry['status'] if first['status'] == 'rejected' else first['status']
    row['fallback'] = first['status'] == retry['status'] == 'rejected'
    (directory / 'result.json').write_text(json.dumps(row, indent=2)+'\n')
    return row


def summarize(rows, threshold):
    summary = {}
    for variant in sorted({r['variant'] for r in rows}):
        group = [r for r in rows if r['variant'] == variant]
        item = {'flows': len(group), 'fallbacks': sum(r['fallback'] for r in group),
                'accepted_flows': sum(r['operational_status'] == 'accepted' for r in group),
                'unverified_flows': sum(r['operational_status'] == 'unverified' for r in group),
                'error_flows': sum(r['operational_status'] == 'error' for r in group)}
        for i, name in enumerate(('first', 'retry_diagnostic')):
            values = [r['attempts'][i]['metrics']['off_palette_pct'] for r in group if 'metrics' in r['attempts'][i]]
            item[name] = {'measured': len(values), 'over_threshold': sum(v > threshold for v in values),
                          'mean_off_palette_pct': round(sum(values)/len(values), 4) if values else None,
                          'max_off_palette_pct': max(values) if values else None}
        summary[variant] = item
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--seed-pairs', type=int, choices=(1, 2), default=2)
    parser.add_argument('--workers', type=int, choices=(1, 2), default=2)
    parser.add_argument('--variants', nargs='+', choices=('baseline', 'candidate'),
                        default=['baseline', 'candidate'])
    args = parser.parse_args()
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        parser.error('Use an empty output directory to preserve experiment provenance.')
    out.mkdir(parents=True, exist_ok=True)
    fixture = json.loads(FIXTURE.read_text())
    settings = load_settings()
    # The benchmark includes text detection even when production opts out.
    settings['pipeline']['image_guardrail_check_text'] = True
    threshold = settings['pipeline']['image_guardrail_max_off_palette_pct']
    template = (ROOT / 'config/prompt_templates/weather_image.txt').read_text()
    styles = {s['name']: s for s in gi.ART_STYLE_POOL}
    palettes = {p['name']: p for p in gi.PALETTE_STRATEGY_POOL}
    jobs = []
    for scene in scenarios(fixture):
        style, palette = styles[scene['style']], palettes[scene['palette']]
        for variant in dict.fromkeys(args.variants):
            prompt = (baseline_prompt(fixture, scene['subject'], style, palette) if variant == 'baseline'
                      else gi._inject_style_prompt(template, scene['subject'], style, palette))
            for pair in SEED_PAIRS[:args.seed_pairs]:
                jobs.append((variant, scene, pair, prompt))
    # Key-file locations are omitted; secrets are never copied into artifacts.
    meta = {'model': settings['fal']['image_model'], 'parameters': settings['fal']['image_generation_parameters'],
            'threshold': threshold, 'seed_pairs': SEED_PAIRS[:args.seed_pairs],
            'vision_text_check_enabled': True,
            'max_generation_calls': len(jobs)*2, 'template_sha256': hashlib.sha256(template.encode()).hexdigest(),
            'variants': args.variants,
            'generator_sha256': hashlib.sha256(Path(gi.__file__).read_bytes()).hexdigest(),
            'cost': 'Provider billing unavailable; no cost estimate asserted.'}
    (out / 'meta.json').write_text(json.dumps(meta, indent=2)+'\n')
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda job: evaluate(job, settings, out, threshold), jobs))
    result = {'meta': meta, 'summary': summarize(rows, threshold), 'rows': rows}
    (out / 'results.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result['summary'], indent=2))


if __name__ == '__main__':
    main()
