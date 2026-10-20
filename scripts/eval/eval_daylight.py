#!/usr/bin/env python3
"""Twenty-image, five-city before/after evaluation of the daylight draft.

Before is the committed deployment (HEAD); after is the working-tree draft.
Each gets two independent briefs and two matched image seeds per location.
No image retries or hardware publication. Resume reuses saved outputs.
"""
import argparse
import base64
import copy
import hashlib
import html
import io
import json
import os
import shutil
import subprocess
import sys
import time
import types
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
import certifi

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, load_settings
from scripts.openrouter import generate_brief as gb
from scripts.openrouter import generate_image as gi
from scripts.openrouter.network import urlopen_with_context
from scripts.render.compose_board import render_board
from scripts.render.palette_metrics import analyze
from scripts.render.palette_quantize import quantize_board
from scripts.weather.fetch_weather import fetch_forecast
from scripts.weather.aggregate_weather_sources import build_aggregated_context
from scripts.weather.transform_weather import build_payload

SEEDS = [910921837, 424242]
PALETTES = ['Golden Light', 'Red Signal', 'Balanced Four-Ink', 'Night Signal', 'Ink Drawing']


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + '\n')


def read(path):
    return json.loads(path.read_text())


def snapshot_modules(out):
    sources = out / 'sources'
    paths = ['scripts/openrouter/generate_brief.py', 'scripts/openrouter/generate_image.py',
             'config/prompt_templates/weather_brief.txt', 'config/prompt_templates/weather_image.txt']
    for variant in ('before', 'after'):
        for path in paths:
            dest = sources / variant / path
            if not dest.exists():
                data = (subprocess.check_output(['git', 'show', 'HEAD:' + path], cwd=ROOT)
                        if variant == 'before' else (ROOT / path).read_bytes())
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
    helper = sources / 'after/scripts/openrouter/art_direction.py'
    if not helper.exists():
        helper.write_bytes((ROOT / 'scripts/openrouter/art_direction.py').read_bytes())
    for name in ('art_guardrail.py', 'art_recipes.py'):
        for variant in ('before', 'after'):
            dest = sources / variant / 'scripts/openrouter' / name
            if not dest.exists():
                path = 'scripts/openrouter/' + name
                dest.write_bytes(subprocess.check_output(['git', 'show', 'HEAD:' + path], cwd=ROOT)
                                 if variant == 'before' else (ROOT / path).read_bytes())
    modules = {}
    for variant in ('before', 'after'):
        modules[variant] = []
        for name in ('generate_brief', 'generate_image'):
            source = sources / variant / f'scripts/openrouter/{name}.py'
            module = types.ModuleType(f'eval_{variant}_{name}')
            module.__file__ = str(ROOT / f'scripts/openrouter/{name}.py')
            exec(compile(source.read_text(), module.__file__, 'exec'), module.__dict__)
            modules[variant].append(module)
    return modules


def api_json(settings, body, timeout=90):
    # The user explicitly requested these local files, ignoring environment keys.
    key = Path('~/.openrouter.key').expanduser().read_text().strip()
    request = urllib.request.Request(
        settings['openrouter']['base_url'] + '/chat/completions',
        data=json.dumps(body).encode(), headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urlopen_with_context(request, timeout=timeout, settings=settings) as response:
        result = json.loads(response.read())
    if 'choices' not in result:
        raise RuntimeError('OpenRouter response missing choices: ' + str(result.get('error')))
    return result


def parsed(response):
    content = response['choices'][0]['message']['content'].strip()
    if content.startswith('```'):
        content = content.split('```', 2)[1].removeprefix('json').strip()
    return json.loads(content)


def city_settings(base, city, directory):
    settings = copy.deepcopy(base)
    settings['location'] = {k: city[k] for k in ('label', 'latitude', 'longitude', 'timezone')}
    settings['context'].update(events_mode='off', location_descriptor=city['location_descriptor'])
    settings['runtime'] = {key: str(directory / name) for key, name in {
        'hero_file': 'hero.png', 'stale_file': 'status.json', 'history_file': 'history.json',
        'day_context_file': 'day_context.json', 'art_recipe_ledger_file': 'recipes.jsonl',
    }.items()}
    settings['display']['mode'] = 'local_preview'
    settings['pipeline']['image_timeout_seconds'] = 90
    return settings


def run_one(job, base, modules, out):
    city, palette_name, variant, repeat = job
    directory = out / city['id'] / variant / str(repeat + 1)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / 'result.json').exists():
        return read(directory / 'result.json')
    settings = city_settings(base, city, directory)
    brief_module, image_module = modules[variant]
    payload = read(out / city['id'] / 'payload.json')
    seed = SEEDS[repeat]
    row = {'city': city['id'], 'variant': variant, 'repeat': repeat + 1, 'seed': seed,
           'style': city['force_style'], 'palette': palette_name}
    started = time.monotonic()
    try:
        if (directory / 'brief.json').exists():
            payload['brief'] = read(directory / 'brief.json')
        else:
            enriched = brief_module._enrich_payload(payload, settings)
            save(directory / 'brief-input.json', enriched)
            template = (out / 'sources' / variant / 'config/prompt_templates/weather_brief.txt').read_text()
            prompt = brief_module._render_prompt(template, enriched)
            (directory / 'brief-prompt.txt').write_text(prompt)
            for attempt in range(1, 4):
                body = {'model': settings['openrouter']['text_model'],
                        'messages': [{'role': 'user', 'content': prompt}],
                        **gb._brief_model_parameters(settings, settings['openrouter']['text_model'])}
                save(directory / f'brief-request-{attempt}.json', body)
                response = api_json(settings, body)
                save(directory / f'brief-response-{attempt}.json', response)
                brief = gb._normalize_brief_punct(parsed(response))
                violations = gb._brief_violations(brief, settings)
                if not violations:
                    payload['brief'] = brief
                    save(directory / 'brief.json', brief)
                    break
                prompt += '\n\nCorrect these contract violations: ' + '; '.join(violations)
            else:
                raise RuntimeError('Brief failed validation after three attempts')
        payload['brief_source'] = 'openrouter'
        save(directory / 'composed-payload.json', payload)
        row.update(headline=payload['brief']['headline'], scene=payload['brief']['illustration_prompt'])
        style = next(s for s in image_module.ART_STYLE_POOL if s['name'] == city['force_style'])
        palette = next(p for p in image_module.PALETTE_STRATEGY_POOL if p['name'] == palette_name)
        template = (out / 'sources' / variant / 'config/prompt_templates/weather_image.txt').read_text()
        prompt = image_module._inject_style_prompt(
            template, row['scene'], style, palette, forecast=payload)
        (directory / 'image-prompt.txt').write_text(prompt)
        settings['fal']['image_generation_parameters']['seed'] = seed
        save(directory / 'image-request.json', {
            'model': settings['fal']['image_model'],
            'request': {'prompt': prompt, **settings['fal']['image_generation_parameters']}})
        hero = directory / 'hero.png'
        if not hero.exists():
            # Always one image per cell. Explicitly rejected images are retained
            # as diagnostic references, never published or silently regenerated.
            hero.write_bytes(image_module._call_fal_image_api(settings, prompt))
        image_bytes = hero.read_bytes()
        row['image_sha256'] = hashlib.sha256(image_bytes).hexdigest()
        row['image_size'] = list(Image.open(io.BytesIO(image_bytes)).size)
        row['palette_metrics'] = analyze(image_bytes)
        row['vision'] = gi.inspect_art(image_bytes, settings)
        palette_ok = row['palette_metrics']['off_palette_pct'] <= settings['pipeline']['image_guardrail_max_off_palette_pct']
        row['status'] = ('rejected' if not palette_ok or row['vision']['status'] == 'rejected'
                         else row['vision']['status'])
        art_settings = copy.deepcopy(settings)
        art_settings['display']['panel_fraction'] = 0
        resized = Image.open(hero).convert('RGB').resize((960, 480), Image.Resampling.LANCZOS)
        resized.save(directory / 'art-resized.png')
        quantize_board(art_settings, directory / 'art-resized.png', directory / 'art-eink.png')
        render_board(settings, payload, directory / 'board-raw.png', directory / 'preview-raw.png')
        quantize_board(settings, directory / 'board-raw.png', directory / 'board-eink.png')
    except Exception as error:
        row.update(status='error', error=f'{type(error).__name__}: {error}')
    row['elapsed_seconds'] = round(time.monotonic() - started, 2)
    save(directory / 'result.json', row)
    off = row.get('palette_metrics', {}).get('off_palette_pct')
    print(f"DONE {city['id']} {variant} {repeat + 1}: {row['status']}, off-palette={off}", flush=True)
    return row


def judge_pair(job, settings, out):
    city, repeat = job
    dest = out / city['id'] / f'comparison-{repeat + 1}.json'
    if dest.exists():
        return read(dest)
    order = ['before', 'after'] if repeat == 0 else ['after', 'before']
    directories = [out / city['id'] / variant / str(repeat + 1) for variant in order]
    results = [read(d / 'result.json') for d in directories]
    row = {'city': city['id'], 'repeat': repeat + 1, 'order': order}
    try:
        instruction = (
            f"Compare two four-ink illustrations for {city['label']}, requested style {city['force_style']}. "
            "The goal is expressive craftsmanship in the selected style, atmospheric depth and readable "
            "material detail, using white/black/red/yellow inks. Do not automatically favor flatness or "
            "reward detail that hurts readability. Separately classify the actual depicted time of day; "
            "a black printed area alone is not evidence of night. Look for celestial objects, artificial "
            "illumination and daylight cues. Judge the pictures, not just their prompts. Different scene "
            "directions are intentional in this full-pipeline comparison. "
            "Return JSON only: {\"image_a\":{\"style_fidelity\":1-5,\"expressive_detail\":1-5,"
            "\"atmosphere\":1-5,\"readability\":1-5,\"time_of_day\":\"day|night|ambiguous\","
            "\"time_evidence\":\"short visual evidence\"},\"image_b\":{same keys},"
            "\"artistic_preference\":\"a|b|tie\",\"reason\":\"concrete visual evidence\"}.\n"
            + '\n'.join(f"Image {'AB'[i]} scene: {r.get('scene')}" for i, r in enumerate(results)))
        content = [{'type': 'text', 'text': instruction}]
        for directory in directories:
            encoded = base64.b64encode((directory / 'art-eink.png').read_bytes()).decode()
            content.append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + encoded}})
        body = {'model': settings['openrouter']['image_tool_model'],
                'messages': [{'role': 'user', 'content': content}],
                'response_format': {'type': 'json_object'}}
        response = api_json(settings, body)
        save(dest.with_name(dest.stem + '-response.json'), response)
        verdict = parsed(response)
        row['judgment'] = verdict
        winner = verdict['artistic_preference']
        row['preferred'] = 'tie' if winner == 'tie' else order[0 if winner == 'a' else 1]
        row.update({variant: verdict[f'image_{"ab"[i]}'] for i, variant in enumerate(order)})
    except Exception as error:
        row['error'] = f'{type(error).__name__}: {error}'
    save(dest, row)
    print(f"JUDGED {city['id']} {repeat + 1}: {row.get('preferred', row.get('error'))}", flush=True)
    return row


def report(out, cities, rows, comparisons, meta):
    summary = {}
    for variant in ('before', 'after'):
        group = [r for r in rows if r['variant'] == variant]
        values = [r['palette_metrics']['off_palette_pct'] for r in group if 'palette_metrics' in r]
        judged = [r[variant] for r in comparisons if variant in r]
        summary[variant] = {
            'images': len(values), 'accepted': sum(r['status'] == 'accepted' for r in group),
            'rejected': sum(r['status'] == 'rejected' for r in group),
            'unverified': sum(r['status'] == 'unverified' for r in group),
            'errors': sum(r['status'] == 'error' for r in group),
            'palette_failures': sum(v > meta['palette_limit'] for v in values),
            'max_off_palette_fraction': max(values) if values else None,
            'mean_off_palette_fraction': sum(values)/len(values) if values else None,
            'daylight': sum(r['time_of_day'] == 'day' for r in judged),
            'night': sum(r['time_of_day'] == 'night' for r in judged),
            'ambiguous': sum(r['time_of_day'] == 'ambiguous' for r in judged),
            'artistic_wins': sum(r.get('preferred') == variant for r in comparisons),
        }
    save(out / 'results.json', {'meta': meta, 'summary': summary, 'rows': rows, 'comparisons': comparisons})
    font_path = '/System/Library/Fonts/Supplemental/Arial.ttf'
    font = ImageFont.truetype(font_path, 18) if Path(font_path).exists() else ImageFont.load_default()
    label_font = ImageFont.truetype(font_path, 14) if Path(font_path).exists() else ImageFont.load_default()
    montage = Image.new('RGB', (1440, 5 * 320), '#eeece6')
    draw = ImageDraw.Draw(montage)
    page = ['<!doctype html><meta charset="utf-8"><title>Daylight prompt comparison</title>',
            '<style>body{font:16px system-ui;background:#eeece6;color:#171717;margin:24px}img{width:100%}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}article{background:white;padding:12px}pre{white-space:pre-wrap}h2{margin-top:40px}</style>',
            '<h1>Deployed prompt vs daylight draft</h1><p>Two matched image seeds per city. Each cell has an independently generated brief. All runs simulate 21:00 local time. Unpublished diagnostic references; rejected samples are labeled.</p>',
            '<p>Each pair: Before on left, After on right. <a href="results.json">Full results</a></p>']
    for index, city in enumerate(cities):
        draw.text((12, index * 320 + 8), f"{city['label']} | {city['force_style']} | BEFORE 1 / AFTER 1 / BEFORE 2 / AFTER 2", font=font, fill='black')
        page.append(f'<h2>{html.escape(city["label"])} — {html.escape(city["force_style"])}</h2>')
        for repeat in range(2):
            page.append(f'<h3>Seed {SEEDS[repeat]}</h3><div class="grid">')
            for vi, variant in enumerate(('before', 'after')):
                directory = out / city['id'] / variant / str(repeat + 1)
                r = read(directory / 'result.json')
                relative = directory.relative_to(out).as_posix()
                off = r.get('palette_metrics', {}).get('off_palette_pct')
                label = f'{variant.upper()} · {r["status"]} · off-palette {off*100:.2f}%' if off is not None else f'{variant}: {r.get("error")}'
                draw.text(((repeat * 2 + vi) * 360 + 6, index * 320 + 33), label,
                          font=label_font, fill='#b00000' if r['status'] == 'rejected' else '#222222')
                image_path = directory / 'board-eink.png'
                if image_path.exists():
                    thumb = Image.open(image_path).convert('RGB').resize((360, 240))
                    montage.paste(thumb, ((repeat * 2 + vi) * 360, index * 320 + 58))
                page.append(f'<article><b>{html.escape(label)}</b><p>{html.escape(r.get("headline", ""))}</p>'
                            f'<a href="{relative}/board-eink.png"><img src="{relative}/board-eink.png"></a>'
                            f'<p><a href="{relative}/hero.png">Raw artwork</a> · <a href="{relative}/image-prompt.txt">Image prompt</a> · <a href="{relative}/brief-prompt.txt">Brief prompt</a></p>'
                            f'<details><summary>Scene</summary><pre>{html.escape(r.get("scene", ""))}</pre></details></article>')
            page.append('</div>')
    montage.save(out / 'comparison.png')
    (out / 'index.html').write_text('\n'.join(page))
    lines = ['# Daylight draft evaluation', '', '20 requested images, no image retries, no publication.',
             'Before = committed deployed prompts. After = working-tree daylight draft.',
             'Two independent briefs and matched image seeds per location; weather/style/palette/model held constant.',
             'This tests the combined brief/image changes, not rendering in isolation.', '',
             '| Measure | Before | After |', '|---|---:|---:|']
    for key in ('images', 'accepted', 'rejected', 'unverified', 'errors', 'palette_failures', 'daylight', 'night', 'ambiguous', 'artistic_wins'):
        lines.append(f'| {key} | {summary["before"][key]} | {summary["after"][key]} |')
    lines += ['', '| Location | Before accepted | After accepted | After palette failures | Artistic preference |',
              '|---|---:|---:|---|---|']
    for city in cities:
        group = [r for r in rows if r['city'] == city['id']]
        accepted = {v: sum(r['status'] == 'accepted' for r in group if r['variant'] == v)
                    for v in ('before', 'after')}
        failures = [f"{r['palette_metrics']['off_palette_pct']*100:.2f}%" for r in group
                    if r['variant'] == 'after' and r.get('palette_metrics', {}).get('off_palette_pct', 0) > meta['palette_limit']]
        preferences = [r.get('preferred', 'error') for r in comparisons if r['city'] == city['id']]
        lines.append(f"| {city['label']} | {accepted['before']}/2 | {accepted['after']}/2 | {', '.join(failures) or 'none'} | {', '.join(preferences)} |")
    lines += ['', f"The reviewer classified {summary['after']['daylight']}/10 draft scenes as daylight and preferred the draft in {summary['after']['artistic_wins']}/10 artistic comparisons, including rejected diagnostic references."]
    if summary['after']['rejected']:
        lines.append('Samples flagged by the shared current guardrail (diagnostic references):')
        for r in rows:
            if r['variant'] == 'after' and r['status'] == 'rejected':
                lines.append(f"- {r['city']} repeat {r['repeat']}: off-palette {r['palette_metrics']['off_palette_pct']*100:.2f}%; {r['vision'].get('note')}")
    lines += ['', 'Time-of-day classifications and artistic preferences are subjective model judgments on device-quantized artwork.',
              'The reviewer sees images in balanced order with no before/after labels. Raw palette measurements use unquantized images.',
              'The production text-check opt-out remains in force for guardrail verdicts.',
              'Both variants use the same working-tree vision checker; generation prompts use their respective source snapshots.',
              f"Weather inputs: {meta['weather_source']}",
              'Rejected samples remain visible as diagnostic references and were never published.',
              'No image-provider billing information was returned; OpenRouter usage/cost is retained in response files.',
              '', '[Full-size comparison gallery](index.html)', '', '![Comparison](comparison.png)']
    (out / 'report.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps(summary, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--workers', type=int, default=2, choices=(1, 2, 3, 4))
    parser.add_argument('--weather-from', help='Reuse weather/payload snapshots from a previous run, while generating all images anew')
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ.pop('GENERATION_RUN_ID', None)
    os.environ.pop('FORECAST_TARGET', None)
    # Set only the provider-specific variables, using the user-authorized files.
    os.environ['OPENROUTER_API_KEY'] = Path('~/.openrouter.key').expanduser().read_text().strip()
    os.environ['FAL_KEY'] = Path('~/.fai.key').expanduser().read_text().strip()
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
    settings = load_settings()
    modules = snapshot_modules(out)
    cities = read(ROOT / 'scripts/eval/cities.json')
    meta_path = out / 'meta.json'
    if meta_path.exists():
        meta = read(meta_path)
    else:
        meta = {
            'created_at_utc': datetime.now(timezone.utc).isoformat(),
            'baseline_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'simulated_local_run': '2026-10-20T21:00:00', 'image_seeds': SEEDS,
            'image_model': settings['fal']['image_model'], 'brief_model': settings['openrouter']['text_model'],
            'vision_model': settings['openrouter']['image_tool_model'],
            'parameters': settings['fal']['image_generation_parameters'],
            'palette_limit': settings['pipeline']['image_guardrail_max_off_palette_pct'],
            'text_check_enabled': settings['pipeline']['image_guardrail_check_text'],
            'image_requests': 20, 'image_retries': 0,
            'sources_sha256': {str(p.relative_to(out / 'sources')): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (out / 'sources').rglob('*') if p.is_file()},
            'weather_source': (f"Archived Open-Meteo snapshots reused from {Path(args.weather_from).resolve()}"
                               if args.weather_from else 'live Open-Meteo, archived once per city; no fallback'),
            'shared_validation': 'Current working-tree vision-check prompt and unchanged raw palette metric for both variants',
            'events': 'off', 'history': 'empty and isolated',
            'recipe_selection': 'disabled in eval; identical fixed style/palette per city',
            'brief_sampling': 'independent per cell; provider text seed not specified',
        }
        save(meta_path, meta)
    for city in cities:
        payload_path = out / city['id'] / 'payload.json'
        if payload_path.exists():
            continue
        if args.weather_from:
            source = Path(args.weather_from).resolve() / city['id']
            payload_path.parent.mkdir(parents=True, exist_ok=True)
            for name in ('payload.json', 'weather.json'):
                shutil.copyfile(source / name, payload_path.parent / name)
            print(f"WEATHER {city['id']}: reused archived inputs", flush=True)
            continue
        s = city_settings(settings, city, out / city['id'])
        raw = fetch_forecast(s)
        save(out / city['id'] / 'weather.json', raw)
        wrapper = {'fetched_at': datetime.now(timezone.utc).isoformat(), 'location': s['location'],
                   'source': 'open-meteo', 'raw': raw}
        context = build_aggregated_context(s, wrapper, {}, {})
        now = datetime.fromisoformat(meta['simulated_local_run']).replace(tzinfo=ZoneInfo(city['timezone']))
        payload = build_payload(context, now_local=now)
        if payload['today']['daily_summary']['date'] != '2026-10-21':
            raise RuntimeError('Live weather does not include the required forecast day')
        save(payload_path, payload)
        print(f"WEATHER {city['id']}: {payload['today']['daily_summary']}", flush=True)
    jobs = [(city, PALETTES[i], variant, repeat) for i, city in enumerate(cities)
            for repeat in range(2) for variant in ('before', 'after')]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda job: run_one(job, settings, modules, out), jobs))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        comparisons = list(pool.map(lambda job: judge_pair(job, settings, out),
                                    [(city, r) for city in cities for r in range(2)]))
    report(out, cities, rows, comparisons, meta)


if __name__ == '__main__':
    main()
