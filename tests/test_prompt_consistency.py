"""Cross-stage regressions from the October 21 prompt-conflict audit."""
import re

import pytest

from scripts.common import ROOT
from scripts.openrouter import art_guardrail as ag
from scripts.openrouter import art_recipes
from scripts.openrouter import generate_image as gi


TEMPLATE = (ROOT / 'config/prompt_templates/weather_image.txt').read_text()
INK_RULE = 'Use only white, black, red and yellow inks.'
FORECAST = {'day_context': {'target_date_iso': '2026-10-21', 'daypart_role': 'primary'}}
RECIPE = {'tags': {
    'subject': 'tree', 'subject_text': 'a ginkgo tree',
    'setting': 'park', 'viewpoint': 'eye_level', 'composition': 'diagonal',
}}


@pytest.mark.parametrize('style_name,palette_name', [
    ('Paper Cutout', 'Red Signal'), ('De Stijl', 'Night Signal'),
    ('Minimal Ukiyo-e', 'Balanced Four-Ink'),
])
def test_palette_reminders_survive_recipe_and_retries(style_name, palette_name):
    style = next(s for s in gi.ART_STYLE_POOL if s['name'] == style_name)
    palette = next(p for p in gi.PALETTE_STRATEGY_POOL if p['name'] == palette_name)
    prompt = gi._inject_style_prompt(
        TEMPLATE, 'A wet leaf above a softly blurred street in daylight.',
        style, palette, recipe=RECIPE, forecast=FORECAST)
    for candidate in (prompt, gi._retry_prompt(prompt, ['off_palette=20%', 'is_collage'])):
        positions = [m.start() / len(candidate) for m in re.finditer(re.escape(INK_RULE), candidate)]
        assert len(positions) == 3
        assert positions[0] < .15 and .35 < positions[1] < .8 and positions[2] > .8
        assert '{{' not in candidate
        scene = candidate.split(gi._SCENE_HEADING, 1)[1].split('\n\n', 1)[0]
        assert 'softly blurred' not in scene
        assert 'wet leaf' in scene and 'daylight' in scene
        assert 'Artwork recipe' in candidate


@pytest.mark.parametrize('scene', [
    'A leaf in sharp focus before softly blurred rooftops and rain.',
    'A rainy street out of focus behind a blue jay beside green tea.',
    'A stone wall with shallow depth of field and bokeh behind a leaf.',
])
def test_cached_camera_requests_become_drawn_depth_without_losing_subject(scene):
    rewritten = gi._rewrite_scene_direction(scene)
    assert not re.search(r'blurred|out of focus|sharp focus|depth of field|bokeh', rewritten)
    for subject in ('leaf', 'rain', 'rooftops', 'blue jay', 'green tea', 'stone wall'):
        if subject in scene:
            assert subject in rewritten
    assert gi._rewrite_scene_direction(rewritten) == rewritten


def test_scene_normalization_keeps_negative_camera_constraints_and_materials():
    scene = 'Translucent leaves in soft golden sunlight; no blurred background, avoid bokeh.'
    assert gi._rewrite_scene_direction(scene) == scene


def test_recipe_has_no_obsolete_subject_or_position_dependent_priority():
    directive = art_recipes.recipe_directive(RECIPE)
    assert 'SUBJECT' not in directive and 'constraints below' not in directive
    assert 'explicit scene-lighting direction' in directive
    assert 'staging and framing only' in directive


def test_style_and_qa_wording_allow_rich_continuous_illustration():
    styles = {s['name']: s['prompt'] for s in gi.ART_STYLE_POOL}
    assert 'primary color' not in styles['De Stijl']
    assert 'red, yellow and white' in styles['De Stijl']
    assert 'collage' not in styles['Paper Cutout']
    for prompt in (ag.INSPECT_PROMPT, ag.COLLAGE_ONLY_PROMPT):
        assert 'flat, minimal' not in prompt
        assert 'Detailed illustrated backgrounds' in prompt
        assert 'realistic/photographic image' in prompt
