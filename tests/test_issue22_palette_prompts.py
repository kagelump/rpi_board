"""Regression coverage for the exact rejected October 11 night scene."""
import json
from pathlib import Path

from scripts.common import ROOT
from scripts.eval import eval_palette_prompts as ev
from scripts.openrouter import generate_image as gi

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/issue22_glass_building.json').read_text())


def _prompt():
    template = (ROOT / 'config/prompt_templates/weather_image.txt').read_text()
    return gi._inject_style_prompt(template, FIXTURE['illustration_prompt'],
                                   FIXTURE['selected_style'], FIXTURE['selected_palette'])


def test_first_attempt_preserves_materials_while_retaining_ink_palette():
    prompt = _prompt()
    scene = gi._rewrite_palette_colors(FIXTURE['illustration_prompt'])
    assert scene in prompt
    assert 'glass office building' in scene and 'faint ghost' in scene
    assert 'only white, black, red and yellow inks' in prompt.lower()
    assert 'Retro Pixel Art' in prompt
    assert FIXTURE['selected_palette']['prompt'] in prompt


def test_archived_control_is_preserved_but_live_prompt_allows_texture():
    selected = (ROOT / 'config/prompt_templates/weather_image.txt').read_text()
    control = (ROOT / 'config/prompt_templates/experiments/issue22_control.txt').read_text()
    assert 'All surfaces are opaque flat ink shapes' in control
    assert 'All surfaces are opaque flat ink shapes' not in selected
    assert 'hatching, stippling, rough edges' in selected


def test_palette_retry_rewrites_scene_and_preserves_composition():
    # Exercise a cached/legacy subject that has not yet been translated.
    prompt = _prompt().replace(gi._rewrite_palette_colors(FIXTURE['illustration_prompt']),
                               FIXTURE['illustration_prompt'])
    retry = gi._retry_prompt(prompt, ['off_palette=19%', 'has_text', 'is_collage'])
    scene = retry.split(gi._SCENE_HEADING, 1)[1].split('\n\n', 1)[0]
    for cue in ('soft cool night air', 'barely visible', 'faint ghost', 'glass office building'):
        assert cue not in scene
    for subject in ('extreme close-up', 'building facade', 'thin new moon', 'stars', 'pedestrian', 'cropped close'):
        assert subject in scene
    assert 'white or yellow panes' in scene
    assert 'white silhouette' in scene
    assert 'NO text' in retry and 'ONE unified illustration' in retry
    assert not any(char.isdigit() for char in retry.split(gi._SCENE_HEADING, 1)[1].split('\n\n', 1)[0])


def test_non_palette_rejection_does_not_rewrite_scene():
    prompt = _prompt()
    scene = gi._rewrite_palette_colors(FIXTURE['illustration_prompt'])
    assert scene in gi._retry_prompt(prompt, ['has_text'])
    assert gi._retry_prompt(prompt, []) == prompt


def test_translation_handles_foliage_without_changing_object_names():
    scene = gi._rewrite_palette_scene(
        'Green trees under a blue sky, soft golden sunlight through translucent leaves. '
        'A blue jay beside green tea and an orange on a bench in cool night air.')
    assert 'black trees' in scene and 'white sky' in scene
    assert 'soft' not in scene and 'translucent' not in scene
    assert 'blue jay' in scene and 'green tea' in scene and 'an orange' in scene
    assert 'cool night air' in scene
    assert gi._rewrite_palette_scene(scene) == scene
    assert gi._rewrite_palette_scene('cool light breeze and soft light rain') == (
        'cool light breeze and soft light rain')


def test_later_retry_keeps_prior_palette_and_text_corrections(monkeypatch):
    prompts = []
    images = iter([b'blue', b'lettering', b'good'])
    monkeypatch.setattr(gi, '_call_image_api', lambda s, p, pr: prompts.append(p) or next(images))
    monkeypatch.setattr(gi, '_off_palette_pct', lambda b: 0.20 if b == b'blue' else 0.0)
    monkeypatch.setattr(gi, 'inspect_art', lambda b, s: {
        'status': 'rejected' if b == b'lettering' else 'accepted',
        'has_text': b == b'lettering', 'is_collage': False})
    out = gi._generate_with_guardrail({'pipeline': {
        'enable_image_guardrail': True, 'image_guardrail_max_retries': 2,
        'image_guardrail_max_off_palette_pct': 0.15}}, _prompt(), 'fal')
    assert out['status'] == 'accepted'
    assert out['image_bytes'] == b'good'
    assert 'soft cool night air' not in prompts[2]
    assert 'NO text' in prompts[2]
    assert out['attempts'][0]['off_palette_pct'] == 0.20


def test_eval_baseline_is_exact_archived_prompt_and_retry():
    prompt = ev.baseline_prompt(FIXTURE, FIXTURE['illustration_prompt'],
                               FIXTURE['selected_style'], FIXTURE['selected_palette'])
    assert prompt == FIXTURE['archived_prompt']
    assert ev.baseline_retry(prompt, ['off_palette=19%']) == FIXTURE['archived_retry_prompt']


def test_eval_diagnostic_retry_does_not_count_as_operational_fallback():
    verdict = {
        'status': 'accepted', 'has_text': False,
        'has_watermark_or_signature': False, 'is_collage': False,
    }
    assert ev.rejection_reasons({'off_palette_pct': 0.15}, verdict, 0.15) == []
    assert ev.rejection_reasons({'off_palette_pct': 0.1501}, verdict, 0.15)
    marked = dict(verdict, has_watermark_or_signature=True)
    assert ev.rejection_reasons(
        {'off_palette_pct': 0.0}, marked, 0.15) == ['has_watermark_or_signature']
    row = {'variant': 'candidate', 'attempts': [
        {'metrics': {'off_palette_pct': 0.0}}, {'metrics': {'off_palette_pct': 0.3}}],
        'operational_status': 'accepted', 'fallback': False}
    result = ev.summarize([row], 0.15)['candidate']
    assert result['fallbacks'] == 0 and result['accepted_flows'] == 1
    assert result['retry_diagnostic']['over_threshold'] == 1
