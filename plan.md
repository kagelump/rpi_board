# Weather E-Ink Board Plan

## Goal

Build a Raspberry Pi weather display system that renders a once-daily morning weather briefing onto a Waveshare e-ink panel, using minimal text and a strong illustration-first composition so the board is quick to read from a distance.

## Known Display Constraints (from `../trainboard`)

- Hardware target: Waveshare 10.2inch e-Paper HAT (G) on Raspberry Pi (trainboard docs use Pi Zero 2 W).
- Native resolution: `960x640`.
- Color capability: 4 colors (`white`, `black`, `red`, `yellow`).
- Existing driver usage in trainboard:
  - `waveshare_epd.epd10in2g` in display update flow.
  - `waveshare_epd.epd10in2_G` appears in clear script.
  - Plan: verify exact installed module name on your Pi and standardize to one import path.
- Existing refresh pattern in trainboard is practical:
  - mostly partial refreshes,
  - periodic full refresh to reduce ghosting.

## Product Definition (v1)

- Product mode: `daily briefing`, generated once each morning around `08:00`.
- Show:
  - briefing date,
  - one short headline for today,
  - 3 decision-oriented bullets max,
  - today's rain window,
  - today's temperature range,
  - a small tomorrow preview,
  - key metadata (`updated at`, source health, stale marker when needed).
- Presentation principle:
  - optimize for a 5-10 second read,
  - prefer one strong visual plus a few high-signal facts,
  - do not try to show a complete forecast or dense hourly table,
  - text should support the illustration, not compete with it.
- Do not present the board as a live dashboard:
  - no "current time" as a primary element,
  - no "next refresh" countdown,
  - no dependence on intra-day reruns for the layout to make sense.
- Support two render styles:
  1. `text-first` (highest reliability; no generated image required),
  2. `illustrated` (OpenRouter image generation + local overlay text, with blank illustration area if generation fails).
- Survive network/API failures by displaying last successful board with a "stale" marker.

## System Architecture

1. **Data Fetch Layer**
   - Pull weather data from Open-Meteo (same source you already used).
   - Normalize to a single JSON payload (`weather_payload.json`).

2. **LLM Brief Layer (OpenRouter text model)**
   - Input: normalized weather JSON.
   - Output: strict JSON brief containing:
     - short headline,
     - up to 3 short decision bullets,
     - rain window,
     - temperature range,
     - tomorrow preview,
     - illustration prompt,
     - layout emphasis hints.
   - Enforce schema and fallback to deterministic local template if validation fails.

3. **Image Generation Layer (OpenRouter image model, optional)**
   - Input: constrained prompt generated from weather brief.
   - Output: base illustration image.
   - Post-process:
     - resize/crop to `960x640`,
     - quantize to e-ink 4-color palette,
     - dither tuning for readability.
   - If generation fails or times out, leave the reserved illustration area blank and continue rendering the board.

4. **Compositor Layer**
   - Combine minimal text, weather cues, and optional illustration into final `PNG`.
   - Render with high contrast and e-ink-safe typography.
   - Export:
     - `final_display.png` (full-res),
     - `preview.png` (optional debug artifact).

5. **Display Driver Layer**
   - Send `final_display.png` to Waveshare display using Python driver.
   - Refresh strategy:
     - full refresh on each scheduled daily update,
     - optional clear/full refresh on manual maintenance runs if ghosting appears,
     - avoid optimizing for partial-refresh-heavy patterns unless a later use case requires intra-day updates.

6. **Scheduler/Operations Layer**
   - systemd service + timer (same operational model as trainboard).
   - Default schedule: once daily around `08:00` local time.
   - Log to file + journal.
   - Keep last-success artifacts and run metadata.
   - Support a manual on-demand run for debugging or ad hoc refreshes.

## Proposed Repository Layout

```text
rpi_board/
  plan.md
  tokyo_weather.sh                       # existing CLI utility
  config/
    settings.json                        # display + API + schedule settings
    prompt_templates/
      weather_brief.txt
      weather_image.txt
  scripts/
    weather/
      fetch_weather.py
      transform_weather.py
    openrouter/
      generate_brief.py
      generate_image.py
    render/
      compose_board.py
      palette_quantize.py
    display/
      update_display.sh
      push_to_epd.py
      clear_display.sh
    ops/
      install_service.sh
  systemd/
    weather-display.service
    weather-display.timer
  runtime/
    last_payload.json
    last_brief.json
    final_display.png
    refresh_counter
    logs/
```

## Implementation Phases

### Phase 1 - Baseline weather board (no LLM/image)

- Build deterministic pipeline from weather API to display image.
- Reuse logic from `tokyo_weather.sh` for:
  - today's headline,
  - today's rain window,
  - today's temperature range,
  - tomorrow summary / next-day preview.
- Render a sparse, illustration-first board with selective red/yellow emphasis.
- Push to panel and validate display stability.

**Exit criteria**
- End-to-end update works on Pi in under 10 seconds.
- Text remains readable at a distance and does not dominate the layout.
- No hard crash on temporary API failure.

### Phase 2 - OpenRouter text intelligence

- Add OpenRouter call for headline + prioritization.
- Enforce JSON schema response:
  - reject and fallback if malformed.
- Keep LLM output to a headline and up to 3 short bullets.

**Exit criteria**
- Reliable structured responses over repeated runs.
- Fallback path works when LLM request fails.

### Phase 3 - OpenRouter image generation

- Add optional generated background/hero panel.
- Quantize and test for e-ink color fidelity.
- Keep text overlay independent so readability never depends on generated image quality.
- If the image request fails, render a blank illustration region and still complete the update.

**Exit criteria**
- Generated board remains legible on physical panel.
- Generation timeout does not block update cycle.

### Phase 4 - Automation and hardening

- Add systemd timer/service.
- Add log rotation and runtime retention policy.
- Add health checks and stale-data indicators.
- Validate the board as a morning briefing that remains useful all day without additional refreshes.

**Exit criteria**
- Runs unattended for at least 7 consecutive daily runs without intervention.
- No severe ghosting with full-refresh daily update strategy.

## OpenRouter Integration Plan

## Credentials

- Use environment variable: `OPENROUTER_API_KEY`.
- Never store API keys in committed config.

## Text Model Contract

- Prompt for strict JSON only.
- Include:
  - concise headline,
  - at most 3 short decision bullets,
  - rain timing,
  - temperature range,
  - tomorrow preview,
  - optional clothing / umbrella hint only if it is one of the top 3 bullets.
- Explicitly forbid verbose summaries, hourly recaps, and generic filler text.

## Image Model Contract

- Prompt constraints:
  - "minimal weather poster / briefing illustration",
  - "high contrast",
  - "limited palette suitable for white/black/red/yellow e-ink",
  - "avoid dense textures and tiny details",
  - "communicate the day's feel at a glance".
- Use deterministic seeds when available for reproducibility.

## Reliability

- Timeout budget:
  - brief generation: 6-8s,
  - image generation: 12-20s max.
- If text generation fails, use deterministic local copy.
- If image generation fails, continue with a blank illustration area and still refresh display.

## Display Rendering Rules

- Canvas size always `960x640`.
- Reserve zones:
  - top strip (`10-15%` height): briefing date, short headline, stale/status marker,
  - hero area (`50-60%` of board): primary illustration that communicates today's weather mood,
  - decision strip (`20-25%` of board): up to 3 bullets, rain window, temp range,
  - footer (`10-15%` height): small tomorrow preview + source/update metadata.
- Concrete v1 layout:
  - top: `TOKYO TODAY` style label + date + 1-line headline,
  - center: large illustration / hero graphic,
  - lower left: 3 short bullets max,
  - lower middle: rain window,
  - lower right: temp low/high,
  - bottom edge: tomorrow preview in one short line.
- Typography:
  - large numerals for temperature range,
  - avoid thin fonts,
  - headline and labels should be brief enough to fit without wrapping whenever possible,
  - maintain minimum 4.5:1 visual contrast equivalent.
- Color use:
  - black for primary text,
  - red for alerts / rain emphasis / strongest accent,
  - yellow for warmth / sun / secondary highlight only.
- Density rules:
  - never render a full hourly table in v1,
  - never exceed 3 bullets,
  - avoid paragraphs,
  - if space is tight, drop lower-priority text before shrinking fonts.

## Board Content Priority

1. Today's overall feel at a glance.
2. Whether you need rain gear.
3. Temperature range / clothing implication.
4. One-line tomorrow preview.
5. Operational metadata.

If the board becomes crowded, remove detail in this order:
- metadata detail,
- third bullet,
- tomorrow extra detail,
- illustration embellishment.

Keep the headline, rain window, and temp range at all costs.

## Operations Checklist

- Confirm SPI enabled on Pi.
- Confirm installed waveshare module naming (`epd10in2g` vs `epd10in2_G`).
- Add executable scripts and correct ownership.
- Install systemd units:
  - `weather-display.service`,
  - `weather-display.timer`.
- Set timer for morning local time (target `08:00`, with reasonable jitter tolerance if desired).
- Validate with:
  - manual run,
  - timer-triggered run,
  - cold boot run.

## Risks and Mitigations

- **Driver mismatch risk**: inconsistent module names across scripts.
  - Mitigation: runtime import probe and explicit config for driver module.
- **Board content feels stale later in the day** if layout depends on "current" conditions.
  - Mitigation: design strictly as a daily briefing with date-based summaries and time windows rather than live status.
- **Board becomes text-heavy and loses readability on e-ink.**
  - Mitigation: cap copy aggressively, reserve most area for the hero illustration, and drop low-priority text instead of shrinking typography.
- **E-ink ghosting** despite low refresh frequency.
  - Mitigation: use full refresh for each scheduled daily update and reserve extra clear cycles for maintenance only.
- **LLM/image latency** slows refresh.
  - Mitigation: strict timeouts; text falls back to deterministic copy, image area can remain blank.
- **Unreliable network** causes blank board.
  - Mitigation: cache last successful render and show stale banner.

## Suggested Next Build Step

Implement Phase 1 first: create a deterministic `fetch -> compose -> display` pipeline using the known `960x640` and Waveshare flow, then layer OpenRouter text and image generation on top.
