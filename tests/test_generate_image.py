"""Tests for image style and palette direction rotation."""

from scripts.openrouter.generate_image import (
    ART_STYLE_POOL,
    PALETTE_STRATEGY_POOL,
    _inject_style_prompt,
    _pick_art_style,
    _pick_palette_strategy,
)


def test_style_pool_has_broad_rotation():
    names = [style["name"] for style in ART_STYLE_POOL]
    assert len(names) >= 20
    assert len(names) == len(set(names))


def test_style_rotation_does_not_repeat_before_pool_exhaustion():
    state = {}
    selected = []
    for index in range(len(ART_STYLE_POOL)):
        selected.append(_pick_art_style({}, state, f"day-{index}")["name"])
    assert len(set(selected)) == len(ART_STYLE_POOL)


def test_palette_is_locked_for_day_and_advances_on_new_day():
    state = {}
    first = _pick_palette_strategy(state, new_target_day=True)
    same_day = _pick_palette_strategy(state, new_target_day=False)
    next_day = _pick_palette_strategy(state, new_target_day=True)

    assert same_day == first
    assert next_day["name"] != first["name"]
    assert len(PALETTE_STRATEGY_POOL) >= 6


def test_prompt_injects_subject_style_and_palette():
    template = "SUBJECT={{IMAGE_PROMPT}}\nPALETTE={{PALETTE_GUIDANCE}}\nSTYLE={{STYLE_GUIDANCE}}"
    style = {"name": "Linocut", "prompt": "carved marks"}
    palette = {"name": "Red Signal", "prompt": "red focal accent"}

    prompt = _inject_style_prompt(template, "wind in trees", style, palette)

    assert "wind in trees" in prompt
    assert "Linocut" in prompt and "carved marks" in prompt
    assert "Red Signal" in prompt and "red focal accent" in prompt
    assert "{{" not in prompt
