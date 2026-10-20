#!/usr/bin/env python3
"""Five issue #22 hypotheses against saved matched-seed control artwork.

A: concise ink contract. B: style first. C: style-led ink texture.
D: C plus original material/atmosphere descriptions, translating explicit hues.
E: D plus an ink plan only for glass architecture at night.
Palette/text/collage gates are unchanged. Only actual rejections trigger retry.
Art comparisons randomize image order and hide the variant name from the judge.
"""
import argparse
import base64
import copy
import hashlib
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, load_settings, get_openrouter_api_key
from scripts.openrouter import generate_image as gi
from scripts.openrouter import art_guardrail as ag
from scripts.openrouter.network import urlopen_with_context
from scripts.render.palette_metrics import analyze
from scripts.render.palette_quantize import quantize_board
from scripts.eval.eval_palette_prompts import scenarios, SEED_PAIRS, rejection_reasons

VARIANTS = ['concise', 'style_first', 'texture', 'materials', 'targeted']
HYPOTHESES = {
    'concise': 'Remove fixed per-object mappings and all-surfaces-flat instructions; keep the normalized scene.',
    'style_first': 'Move the same style instruction before the concise ink specification.',
    'texture': 'Add permission for style-led detail and tone through separated ink marks.',
    'materials': 'Keep texture treatment but preserve original materials and atmospheric descriptions; translate explicit surface colours only.',
    'targeted': 'Preserve materials, adding a local ink plan only when the scene describes glass architecture at night.',
}


def colour_only(scene):
    replacements = [
        (r'\b(?:blue|cyan|teal|azure)\b(?=\s+(?:sky|water|river|sea|clouds?|panes?|windows?|light|hue))', 'white'),
        (r'\b(?:green|brown|grey|gray|purple|violet|indigo)\b(?=\s+(?:trees?|leaves|foliage|sky|water|river|clouds?|panes?|windows?|light|hue))', 'black'),
        (r'\b(?:pink|magenta)\b(?=\s+(?:sky|clouds?|blossoms?|flowers?|light|hue))', 'red'),
        (r'\borange\b(?=\s+(?:sky|light|glow|clouds?|leaves|hue))', 'yellow'),
    ]
    for pattern, replacement in replacements:
        scene = re.sub(pattern, replacement, scene, flags=re.I)
    return scene


def build_prompt(variant, scene, style, palette):
    is_night_glass = 'glass' in scene.lower() and 'night' in scene.lower()
    template_variant = 'materials' if variant == 'targeted' and not is_night_glass else variant
    template = (ROOT / f'config/prompt_templates/experiments/issue22_{template_variant}.txt').read_text()
    subject = colour_only(scene) if variant in ('materials', 'targeted') else gi._rewrite_palette_scene(scene)
    if variant == 'targeted' and is_night_glass:
        subject = subject.replace('soft cool night air', 'still night air')
        template = template.replace('{{SCENE_INK_PLAN}}',
            'For the glass building at night: sky and window shadows are black ink, office lights yellow ink. '
            'Glass reflections and faint forms use sparse white ink marks. Convey cool air through the still '
            'composition rather than tinted surfaces; preserve the scene details and the selected style.')
    return (template.replace('{{IMAGE_PROMPT}}', subject)
            .replace('{{STYLE_GUIDANCE}}', f"Style: {style['name']}. {style['prompt']}. Apply this style using only the specified inks.")
            .replace('{{PALETTE_GUIDANCE}}', palette['prompt']))


def inspect(image, settings):
    metrics = analyze(image)
    vision = ag.inspect_art(image, settings)
    reasons = rejection_reasons(metrics, vision, settings['pipeline']['image_guardrail_max_off_palette_pct'])
    return {'metrics': metrics, 'vision': vision, 'reasons': reasons,
            'status': 'rejected' if reasons else 'accepted' if vision.get('status') == 'accepted' else 'unverified'}


def compare(candidate, control, scene, settings, order):
    images = [candidate, control] if order == 'candidate_first' else [control, candidate]
    instruction = f'''Compare these two illustrations of the same scene, using the requested {scene['style']} style. Do not prefer simpler art just because it is flatter. Judge craftsmanship, expressive mark-making, texture, composition and atmospheric depth appropriate to that style, plus fidelity to the scene. Do not reward detail that makes the subject unreadable. Ignore colour eligibility and lettering for this comparison; those have separate gates. Do not assume either image is the reference or superior.
Scene: {scene['subject']}
Return JSON only: {{"image_a":{{"style_fidelity":1-5,"expressive_detail":1-5,"scene_fidelity":1-5}},"image_b":{{"style_fidelity":1-5,"expressive_detail":1-5,"scene_fidelity":1-5}},"artistic_preference":"a|b|tie","reason":"short concrete visual evidence"}}.'''
    content = [{'type': 'text', 'text': instruction}]
    content += [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,'+base64.b64encode(img).decode()}} for img in images]
    body = {'model': settings['openrouter']['image_tool_model'],
            'messages': [{'role': 'user', 'content': content}], 'response_format': {'type': 'json_object'}}
    req = urllib.request.Request(settings['openrouter']['base_url']+'/chat/completions',
                                 data=json.dumps(body).encode(), headers={
                                 'Authorization': 'Bearer '+get_openrouter_api_key(settings), 'Content-Type': 'application/json'})
    with urlopen_with_context(req,timeout=30,settings=settings) as resp:
        payload = json.loads(resp.read())
    judged = json.loads(payload['choices'][0]['message']['content'])
    candidate_key = 'image_a' if order == 'candidate_first' else 'image_b'
    control_key = 'image_b' if order == 'candidate_first' else 'image_a'
    winner = judged['artistic_preference']
    preferred = 'tie' if winner == 'tie' else ('candidate' if ('image_'+winner)==candidate_key else 'control')
    return {'order': order, 'candidate': judged[candidate_key], 'control': judged[control_key],
            'preference': preferred, 'reason': judged['reason'], 'usage': payload.get('usage')}


def run(job, settings, out, controls):
    variant, scene, seeds, prompt = job
    directory = out/variant/scene['name']/str(seeds[0]);directory.mkdir(parents=True,exist_ok=True)
    row = {'variant': variant, 'scenario': scene, 'seeds': seeds, 'attempts': []}
    reasons = []
    selected = None
    for index, seed in enumerate(seeds, 1):
        current = prompt if index == 1 else gi._retry_prompt(prompt,reasons)
        s = copy.deepcopy(settings);s['fal']['image_generation_parameters']['seed'] = seed
        (directory/f'attempt-{index}-request.json').write_text(json.dumps({
            'model':s['fal']['image_model'],'request':{'prompt':current,**s['fal']['image_generation_parameters']}},indent=2)+'\n')
        (directory/f'attempt-{index}-prompt.txt').write_text(current)
        t = time.monotonic()
        try:
            image = gi._call_fal_image_api(s,current)
            (directory/f'attempt-{index}.png').write_bytes(image)
            attempt = inspect(image,s)
            attempt.update(attempt=index,seed=seed,image_sha256=hashlib.sha256(image).hexdigest(),elapsed_s=round(time.monotonic()-t,2))
            row['attempts'].append(attempt)
            print(f"{variant} {scene['name']} {seed}: off={attempt['metrics']['off_palette_pct']} {attempt['status']}",flush=True)
            if attempt['status'] != 'rejected':
                selected = image if attempt['status']=='accepted' else None
                break
            reasons = attempt['reasons']
        except Exception as error:
            row['attempts'].append({'attempt':index,'seed':seed,'status':'error','error':str(error)})
            break
    row['operational_status'] = row['attempts'][-1]['status']
    row['fallback'] = row['operational_status']=='rejected'
    control = controls[(scene['name'],seeds[0])]
    if selected and control['selected_path']:
        order = 'candidate_first' if (VARIANTS.index(variant)+int(seeds[0]))%2 else 'control_first'
        try:
            row['art_comparison'] = compare(selected,Path(control['selected_path']).read_bytes(),scene,settings,order)
        except Exception as error:row['art_comparison'] = {'error':str(error)}
    (directory/'result.json').write_text(json.dumps(row,indent=2)+'\n')
    return row


def device_comparison(row, settings, out, controls):
    """Compare actual palette-converted art, excluding the separate text strip."""
    scene, variant, seed = row['scenario'], row['variant'], row['seeds'][0]
    attempt = row['attempts'][-1]['attempt']
    path = out/'device'/variant/scene['name']/str(seed)
    path.mkdir(parents=True, exist_ok=True)
    quantizer_settings = copy.deepcopy(settings)
    quantizer_settings['display']['panel_fraction'] = 0
    quantize_board(quantizer_settings, out/variant/scene['name']/str(seed)/f'attempt-{attempt}.png', path/'candidate.png')
    quantize_board(quantizer_settings, Path(controls[(scene['name'],seed)]['selected_path']), path/'control.png')
    order = 'candidate_first' if (VARIANTS.index(variant)+seed)%2 else 'control_first'
    try:
        result = compare((path/'candidate.png').read_bytes(), (path/'control.png').read_bytes(), scene, settings, order)
    except Exception as error:
        result = {'error': str(error)}
    measured = {'variant': variant, 'scenario': scene['name'], 'seed': seed, 'comparison': result}
    (path/'comparison.json').write_text(json.dumps(measured,indent=2)+'\n')
    return measured


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True)
    parser.add_argument('--control',default='runtime/issue22-final/candidate')
    parser.add_argument('--variants', nargs='+', choices=VARIANTS, default=VARIANTS)
    parser.add_argument('--device', action='store_true', help='Also compare accepted art after device quantization.')
    args = parser.parse_args()
    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):parser.error('Choose an empty output directory.')
    out.mkdir(parents=True,exist_ok=True)
    settings = load_settings()
    settings['pipeline']['image_guardrail_check_text'] = True
    fixture = json.loads((ROOT/'tests/fixtures/issue22_glass_building.json').read_text())
    scenes = scenarios(fixture)
    styles = {s['name']:s for s in gi.ART_STYLE_POOL};palettes={p['name']:p for p in gi.PALETTE_STRATEGY_POOL}
    meta = {'hypotheses':HYPOTHESES,'image_model':settings['fal']['image_model'],
            'vision_model':settings['openrouter']['image_tool_model'],'parameters':settings['fal']['image_generation_parameters'],
            'seed_pairs':SEED_PAIRS,'threshold':settings['pipeline']['image_guardrail_max_off_palette_pct'],
            'vision_text_check_enabled':True,
            'max_generation_calls':len(set(args.variants))*len(scenes)*len(SEED_PAIRS)*2,
            'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'templates':{v:hashlib.sha256((ROOT/f'config/prompt_templates/experiments/issue22_{v}.txt').read_bytes()).hexdigest() for v in args.variants}}
    (out/'meta.json').write_text(json.dumps(meta,indent=2)+'\n')
    controls={}
    # Recheck control images with the new model, avoiding cross-judge comparisons.
    for scene in scenes:
        for seeds in SEED_PAIRS:
            source = Path(args.control)/scene['name']/str(seeds[0])
            row={'scenario':scene,'seeds':seeds,'attempts':[],'selected_path':None}
            for index in (1,2):
                imagepath=source/f'attempt-{index}.png';a=inspect(imagepath.read_bytes(),settings)
                a.update(attempt=index,seed=seeds[index-1]);row['attempts'].append(a)
                if a['status']!='rejected':
                    if a['status']=='accepted':row['selected_path']=str(imagepath.resolve())
                    break
            row['operational_status']=row['attempts'][-1]['status']
            controls[(scene['name'],seeds[0])]=row
    (out/'control-results.json').write_text(json.dumps(list(controls.values()),indent=2)+'\n')
    jobs=[]
    for variant in dict.fromkeys(args.variants):
        for scene in scenes:
            prompt=build_prompt(variant,scene['subject'],styles[scene['style']],palettes[scene['palette']])
            for seeds in SEED_PAIRS:jobs.append((variant,scene,seeds,prompt))
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(lambda job:run(job,settings,out,controls),jobs))
    summary={}
    for variant in dict.fromkeys(args.variants):
        group=[r for r in rows if r['variant']==variant];judged=[r['art_comparison'] for r in group if 'art_comparison' in r and 'error' not in r['art_comparison']]
        summary[variant]={'flows':len(group),'accepted':sum(r['operational_status']=='accepted' for r in group),
            'fallbacks':sum(r['fallback'] for r in group),'first_off_palette_failures':sum(r['attempts'][0].get('metrics',{}).get('off_palette_pct',0)>settings['pipeline']['image_guardrail_max_off_palette_pct'] for r in group),
            'first_mean_off_palette':round(sum(r['attempts'][0].get('metrics',{}).get('off_palette_pct',0) for r in group)/len(group),4),
            'first_explicit_rejections':sum(r['attempts'][0]['status']=='rejected' for r in group),
            'art_comparisons':len(judged),'candidate_wins':sum(j['preference']=='candidate' for j in judged),
            'control_wins':sum(j['preference']=='control' for j in judged),'ties':sum(j['preference']=='tie' for j in judged),
            'mean_expressive_detail_delta':round(sum(j['candidate']['expressive_detail']-j['control']['expressive_detail'] for j in judged)/len(judged),2) if judged else None}
    result={'meta':meta,'summary':summary,'rows':rows,'controls':list(controls.values())}
    if args.device:
        accepted = [r for r in rows if r['operational_status']=='accepted'
                    and controls[(r['scenario']['name'],r['seeds'][0])]['selected_path']]
        with ThreadPoolExecutor(max_workers=2) as pool:
            result['device_comparisons'] = list(pool.map(
                lambda row: device_comparison(row, settings, out, controls), accepted))
    (out/'results.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
