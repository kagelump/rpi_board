# Weather Display — Improvement Plan

Goal: make the board feel **alive and different every day** by replacing the
static, template-driven brief with a single LLM "creative director" that fuses
weather **and** what's actually happening today (events, holidays, season,
day-of-week) into both the on-screen copy and the image prompt — then render it
well on a 4-colour (black/white/red/yellow) 960×640 e-ink panel.

Hardware reality to respect throughout: Waveshare `epd10in2g`, 960×640, only
**4 inks**. Every visual decision is constrained by that palette.

---

## 0. Current architecture (baseline)

```
fetch_weather (Open-Meteo) ─┐
fetch_yahoo_weather ────────┴─► aggregate_weather_sources ─► brief_context.json
                                                                │
transform_weather (DETERMINISTIC brief + payload) ◄────────────┘
        │ produces brief.headline/subtitle/illustration_prompt from templates
        ▼
generate_brief (LLM, DISABLED → passes deterministic through)
        ▼
generate_image (illustration_prompt + rotating art style)
        ▼
compose_board (paste hero + opaque white text panel + date + temp chip)
        ▼
palette_quantize (nearest-colour to 4 inks)  ─►  push_to_epd / local_preview
```

### Root causes of the three reported symptoms
1. **Dry, identical text** — `enable_openrouter_brief: false`; the board always
   renders the ~7 canned strings in `transform_weather._headline/_subtitle`.
   Even when enabled, `temperature: 0.2` kills variation and the prompt has no
   temporal anchor.
2. **Samey background** — image subject = `brief.illustration_prompt`, which is
   the static template `"Minimal weather poster for {condition} with rain
   hint={pct}%"`. Style rotates; subject doesn't.
3. **Aspect-ratio ignores text panel** — fal renders `landscape_4_3` (4:3) but
   the display is 3:2; then an opaque white panel covers the bottom 24%, so that
   slice of the generated art is cropped/discarded and the model was never told.

---

## Design principle: one "creative director" call, no static brief

Collapse `transform_weather`'s brief + `generate_brief` into a single
LLM call that is the *only* normal path to on-screen text and the image prompt.
The deterministic generator survives **only** as an emergency fallback when the
LLM/network is unavailable (otherwise the board would be blank) — it is no
longer the default and is clearly labelled `brief_source: deterministic_fallback`.

`transform_weather` keeps doing the **mechanical** work (parsing Open-Meteo,
rain windows, temp ranges, hourly rows) and emits a clean *facts* object. It no
longer authors prose. The creative director consumes facts + events and authors
everything human-facing.

### Creative-director output contract (`runtime/brief.json` → `brief`)
```jsonc
{
  "headline": "string  (<=52 chars, voice-driven, not a weather recap)",
  "subtitle": "string  (<=72 chars, one practical or witty beat)",
  "mood": "calm|alert|cozy|stormy|festive|crisp|muggy",
  "accent": "red|yellow|none",          // which non-mono ink leads the board
  "image_prompt": "string  (rich, event-aware art direction, NO TEXT)",
  "art_style": "string  (style name from pool OR model's pick)",
  "event_ref": "string|null  (the event/holiday it leaned on, for logging)",
  "confidence": "low|medium|high"
}
```
Enforced with `response_format` JSON schema + a validator. Invalid → one retry →
deterministic fallback.

---

## 1. New data: "what's happening today"

Add a `fetch_context.py` step that writes `runtime/day_context.json` and is
folded into `brief_context` (and thus the creative-director prompt). Layered so
each source is independently optional and degrades gracefully:

| Layer | Source | Notes |
|---|---|---|
| Calendar / temporal | computed locally | day-of-week, ISO week, season, is-weekend, days-to-next-holiday |
| Japanese holidays | `holidays` lib or Nager.Date API | drives `festive` mood, red accent |
| Astronomy | Open-Meteo daily already has sunrise/sunset; add moon phase (computed) | golden-hour copy, night framing |
| Personal calendar | configurable ICS URL(s) | "you have 3 things today", first event title |
| Local events / news | **OpenRouter `:online` web plugin** on the creative-director model, or a configurable RSS/events feed | the cleanest way to get "events going on today" without a bespoke scraper |
| On-this-day | optional Wikipedia "On this day" API | low-signal flavour, gate behind a flag |

Recommended primary mechanism for *events*: call the creative director with an
**`:online`-enabled** model (e.g. `openai/gpt-4o:online` or a Perplexity model)
and instruct it to weave in *one* genuinely-today local happening (festival,
holiday, notable event in the location) **only if** it is real and high-signal —
otherwise ignore. This avoids building/maintaining an events scraper while still
grounding the copy in reality. Keep a non-online fallback model for cost/offline.

Config additions (`config/settings.json`):
```jsonc
"context": {
  "enable_events": true,
  "calendar_ics_urls": [],
  "events_mode": "online_model | rss | off",
  "rss_feeds": [],
  "include_on_this_day": false,
  "location_descriptor": "Komaba, Meguro, Tokyo"   // for the LLM's local sense
}
```

---

## 2. Text / creative-director (fixes #1)

- **Enable** the LLM path; make it the default. Delete the static brief as the
  primary route (keep as fallback only).
- **Model**: upgrade `text_model` from `gpt-4o-mini` to a stronger model for
  wit; use the `:online` variant when `events_mode=online_model`.
- **Temperature** ~0.8 for the brief (currently hardcoded 0.2 in
  `generate_brief._call_openrouter` — make it a setting).
- **Persona / voice** config so the board has a consistent character:
  ```jsonc
  "voice": { "persona": "a wry Tokyo local who notices small things",
             "constraints": "never restate the forecast verbatim; vary sentence shape daily" }
  ```
- **Anti-repetition**: persist the last N days of `{date, headline, image_prompt,
  art_style}` in `runtime/history.json` and feed them in with an explicit
  "do not repeat these openings/structures/styles" instruction.
- **Angle-of-the-day rotation**: pass a rotating lens (commute / laundry /
  evening plans / what-to-wear / a seasonal note) so consecutive days differ
  even with similar weather.
- **Prompt template** (`weather_brief.txt`): rewrite around voice + events +
  anti-repetition + the strict output schema. Inject `today` date, weekday,
  season, holiday, events, and recent history.

---

## 3. Image generation (fixes #2 and #3)

### Subject variety (#2)
- `image_prompt` now comes from the creative director (event/season-aware), so
  the subject genuinely changes day-to-day.
- **Style selection**: either let the creative director pick `art_style` to suit
  the mood/event, or keep the rotation but make it content-seeded. Keep the
  9-style pool as the allowed vocabulary so e-ink legibility is preserved.
- **Seed**: pass a fresh random seed each run (flux/Gemini) so identical prompts
  still vary. Currently no seed → doubly samey.
- **Provider hygiene**: `fal-ai/flux/schnell` is guidance-distilled — drop
  `guidance_scale: 20` (it does nothing useful here) and keep low steps.
  Consider `flux/dev` if a little more spend is acceptable for composition.

### Aspect ratio that accounts for the panel (#3)
The panel covers the bottom `panel_h = round(0.24 * 640) = 154 px`. Visible art
region = **960 × 486**. Two implementation options (pick B):

- **A (minimal):** set fal `image_size` to `{ "width": 960, "height": 640 }`
  (true 3:2, eliminates the 4:3→3:2 crop) and keep the "leave bottom 24% quiet"
  instruction. Simple but still hides ~24% of generated pixels behind the panel.
- **B (recommended):** generate **for the visible region only** — request
  `960 × 480` (2:1, both multiples of 32 for flux) — and in `compose_board`
  paste the hero into the *top* region `(0,0)–(960,480)` with the text panel as a
  clean strip below. Nothing the model draws is cropped or buried; the
  "subject in upper 70%" guesswork disappears. Make panel height and image size
  derive from one shared constant so they can never drift.

Update `weather_image.txt` to state the real canvas, the 4-ink palette hard
constraint, "image fills the full frame; no reserved text zone" (option B), and
strong NO-TEXT / no-gradient guidance for e-ink.

---

## 4. Composition, layout & typography (`compose_board.py`)

- **Mood-driven layout**: use `brief.mood` / `brief.accent` to vary the board —
  e.g. `alert` → red header rule + red temp chip; `festive` → yellow accent;
  `cozy` → warmer arrangement. Today layout is identical every day.
- **Panel integration**: the flat opaque white box is the least dynamic element.
  Options within 4-ink limits: a thin red/black rule that matches `accent`, a
  dithered/halftone divider, or a corner cartouche instead of a full-width slab.
- **Richer glanceable data** in the panel/top strip (currently only date + high):
  - low/high pair, not just high
  - a tiny **hourly temp or rain sparkline** (4 inks render line art well)
  - sunrise/sunset or "rain 14:00–17:00" window chip
  - a one-line **event/holiday tag** when present (this is the "today feels
    different" payoff)
  - simple weather **pictogram** drawn in code (sun/cloud/rain/snow) for instant
    read, independent of the AI art.
- **Typography**: bundle a real display typeface in `assets/fonts/` instead of
  relying on system DejaVu/Arial paths that differ between Mac dev and Pi;
  tighten the headline/subtitle hierarchy and baseline math.

---

## 5. E-ink rendering quality (`palette_quantize.py`)

- Current quantizer is **nearest-colour per pixel** → flat banding on the art.
  Apply **Floyd–Steinberg dithering to the illustration region** (gives the
  illusion of more tones with 4 inks) while keeping **text/panel crisp**
  (threshold, no dither) so type stays sharp. Mask the two regions.
- Tune the red/yellow/black/gray thresholds against real panel output (the
  on-screen preview lies about how inks look); add a `--preview-palette` mode
  that shows the exact 4-ink result at dev time.
- Optionally render the AI art with awareness that only 4 inks exist (prompt +
  palette-aware pre-quantization) to reduce muddy mid-tones.

---

## 6. Temporal dynamism (cron cadence)

- Run **2–4× per day** with a time-of-day frame passed to the creative director:
  *morning briefing* (what to expect / what to wear), *midday*, *evening*
  (tomorrow preview / wind-down). Same weather, different message → the board
  changes through the day, not just day-to-day.
- Skip re-render if inputs are materially unchanged (cost control), but force a
  refresh on a new time-frame or a new event.

---

## 7. Reliability, cost & ops

- **Strict fallback ladder** per stage: LLM brief → 1 retry → deterministic
  fallback; image → retry → last good hero → code-drawn pictogram board (never
  blank). Surface `brief_source` / `image_source` on `last_success.json` and a
  small staleness badge on the board.
- **History log** `runtime/history.json` (also powers anti-repetition) for
  debugging "why was today like that".
- **Cost guardrails**: one text + one image per run; cache `:online` event
  lookups for the day; cap retries. Document expected daily spend.
- **Secrets**: keys already resolved from files/env — keep out of git; add a
  preflight check that warns when image is enabled but no key is present.
- **Config validation** in `preflight.py`: panel/image-size constant agreement,
  enabled-flags sanity, model names.

---

## 8. Testing

- Unit: `transform_weather` facts-only output; creative-director **validator**
  (schema, char limits, ASCII, NO-TEXT in image_prompt); fallback ladder.
- Contract: a recorded LLM response fixture → assert it maps to a valid board.
- Render: golden-dimension test (board is exactly 960×640; image region matches
  the shared panel constant); quantizer outputs only the 4 inks (+gray policy).
- Events: each context layer optional/degrades; offline mode produces a board.

---

## 9. Suggested sequencing

**Phase 1 — make it dynamic (highest impact, small surface)**
1. Enable LLM brief; move temperature to settings (~0.8).
2. Rewrite `weather_brief.txt` with voice + date/weekday/season + anti-repetition
   + the new output schema (incl. `image_prompt`, `mood`, `accent`).
3. `transform_weather` stops authoring prose; emits facts only. Deterministic
   text demoted to fallback.
4. Image prompt sourced from creative director; add seed; drop `guidance_scale`.
5. Fix aspect ratio (option B): shared panel/image constant, 960×480 art region.

**Phase 2 — "what's happening today"**
6. `fetch_context.py`: temporal + holidays + astronomy.
7. Events via `:online` model (or RSS); optional ICS calendar.
8. `history.json` + angle-of-day rotation.

**Phase 3 — make it beautiful**
9. Mood-driven layout, accent inks, code-drawn pictogram, sparkline, event tag.
10. Bundled font; Floyd–Steinberg dithering on art with crisp text.

**Phase 4 — operate it**
11. Multi-run/day time-frames; fallback ladder + staleness badge; preflight
    validation; cost guardrails; tests for all new modules.

---

## 10. Concrete first-PR checklist (Phase 1)
- [ ] `settings.json`: `enable_openrouter_brief: true`, add
      `voice`, `brief_temperature`, `panel_fraction` constant.
- [ ] `generate_brief.py`: read temperature from settings; feed history; new
      validator covering `image_prompt`/`mood`/`accent`.
- [ ] `transform_weather.py`: emit `facts` only; keep deterministic brief under
      a `fallback_brief` key.
- [ ] `weather_brief.txt`: full rewrite (voice, temporal anchors, schema).
- [ ] `generate_image.py`: prompt from `brief.image_prompt`; random seed; drop
      `guidance_scale`; image size from shared constant (960×480).
- [ ] `compose_board.py`: derive `panel_h` and paste region from the same
      constant; paste art into top region only.
- [ ] Tests updated for facts-only transform + new validator + board dimensions.
```
