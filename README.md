# Weather E-Ink Board

Image-first daily weather briefing for a Waveshare e-paper display (`960x640`), built for Raspberry Pi.

The board is designed as a **morning poster**: a generated weather illustration takes most of the screen, with minimal text overlay and a small high-temperature corner chip.

## Features

- Daily weather pipeline: fetch -> transform -> brief -> image -> compose -> display.
- Three refreshes a day that share one fixed daily theme (see *Update schedule*).
- Deterministic fallback path when APIs fail.
- OpenRouter text brief generation (optional).
- Image generation via fal or OpenRouter (optional).
- Art guardrail: after generation, rejects baked-in text, collage/photo-in-frame
  art, and art that leans on colours the 4-ink panel cannot show (off-palette),
  regenerating within a bounded retry. See `scripts/openrouter/art_guardrail.py`.
- Full-screen poster layout with minimal text.
- Local preview mode and Raspberry Pi hardware mode.
- Eval framework (`scripts/eval/`): A/B prompt variants and compare image models
  with a vision-LLM judge plus deterministic e-ink colour metrics.
- Local generation-history API and dashboard with an append-only event ledger,
  immutable image artifacts, model inputs/prompts, styles, seeds, guardrail
  results, stage logs, and failure history.

## Repository Layout

```text
config/
  settings.json
  sample_openmeteo.json
  prompt_templates/
scripts/
  weather/
  openrouter/      # brief + image generation, art guardrail
  render/          # compose_board, palette_quantize, palette_metrics
  display/
  eval/            # prompt A/B + image-model comparison harness
  history/         # append-only storage, recorder CLI, HTTP API + dashboard
tests/
tokyo_weather.sh
plan.md
```

## Update schedule

The systemd timer (`scripts/ops/systemd/weather-eink-board.timer`) fires three
times a day, each refresh playing a distinct role for the **forecast day**:

- **21:00** -- builds tomorrow's board from scratch, so the new day is already
  showing before you wake. Evening/night runs (>= 18:00) forecast *tomorrow*.
- **08:00** -- refreshes today's board only on a **major update** (a change of
  weather condition or rain level, or a high/low swing of ~3C+); an unchanged
  forecast reuses the 21:00 board untouched.
- **13:00** -- re-frames the brief for the **afternoon/evening** and re-renders
  the hero in the same locked style, unless the new illustration prompt is
  nearly identical to the morning's (then the existing art is kept).

All three runs for one forecast day share a **fixed theme**: the creative angle
(seeded on the target date, not the time of day) and the art style (locked per
target date) stay constant, so only the wording and any major forecast change
move between refreshes. The "major update" sensitivity and afternoon
re-render threshold are tunable in `config/settings.json`
(`regen_min_interval_seconds`, `afternoon_art_prompt_similarity_threshold`).

## Eval framework

Reusable harness under `scripts/eval/` for trying prompt/model changes against
real data before shipping:

- `run_eval.py` -- A/B prompt variants across cities; scores each board with a
  vision-LLM judge (title informativeness, art coherence, text/collage guardrail
  flags) plus deterministic e-ink colour metrics.
- `compare_models.py` -- feed one fixed prompt to several image models, N runs
  each; reports judge scores, colour balance, off-palette %, cost, latency, and
  links to each board. Supports `--append-to <run_dir>` to add a model later.
- `palette_metrics.py` lives in `scripts/render/` (it shares the device palette
  with `palette_quantize.py`) and is reused by the production art guardrail.

Outputs land under `runtime/eval*/` (gitignored).

## Requirements

- Python 3.10+
- `Pillow`
- (Optional) OpenRouter API key
- (Pi mode) Waveshare Python driver installed on the device

Install dependency:

```bash
python3 -m pip install --user pillow certifi
```

## Configuration

Main config: `config/settings.json`

Key sections:

- `location`: lat/lon/timezone and display label.
- `display.mode`:
  - `local_preview` (default) -> build image only.
  - `pi_display` -> push to Waveshare panel.
- `pipeline.enable_openrouter_brief`: enable LLM text brief.
- `pipeline.enable_openrouter_image`: enable generated hero image.
- `pipeline.image_provider`: `fal` (default) or `openrouter`.
- `pipeline.enable_image_guardrail`: post-generation QA on the hero art. When on,
  rejects baked-in text, collage/photo-in-frame, and off-palette art, then
  regenerates:
  - `image_guardrail_max_retries` (default `1`): extra attempts on a rejection.
  - `image_guardrail_timeout_seconds` (default `15`): vision-check timeout.
  - `image_guardrail_max_off_palette_pct` (default `0.15`): reject art with more
    than this fraction of pixels in colours the panel cannot show.
  Each check fails open, so the guardrail never blocks a board from rendering.
  Adds ~1 vision call (+ a possible regen) per refresh; set the flag to `false`
  to disable.
- `openrouter.text_model`: model for text brief.
- `openrouter.image_model`: image generation model (default: `google/gemini-3.1-flash-image-preview`).
- `openrouter.image_tool_model`: model used to invoke OpenRouter image server tool (default: `openai/gpt-5.2`).

## OpenRouter API Key

The project checks for the API key in this order:

1. `OPENROUTER_API_KEY` environment variable
2. `openrouter.api_key_file` from `config/settings.json`
3. `~/.openrouter.key`
4. `~/.config/openrouter/api_key`

Recommended:

```bash
printf '%s\n' 'sk-or-v1-...' > ~/.openrouter.key
chmod 600 ~/.openrouter.key
```

## Running

Run the full pipeline:

```bash
./scripts/display/update_display.sh
```

This orchestrates:

1. `scripts/weather/fetch_weather.py`
2. `scripts/weather/transform_weather.py`
3. `scripts/openrouter/generate_brief.py`
4. `scripts/openrouter/generate_image.py`
5. `scripts/render/compose_board.py`
6. `scripts/render/palette_quantize.py`
7. `scripts/display/push_to_epd.py`

### Force a full refresh

By default a re-run reuses caches: the brief is kept when inputs are unchanged
within `regen_min_interval_seconds`, the hero is kept when the brief was cached,
and holidays are read from an on-disk cache. To bypass all of these and
regenerate everything (weather is always fetched fresh regardless):

```bash
./scripts/display/update_display.sh --force   # or: make force
```

The flag fans out to `generate_brief.py --force`, `generate_image.py --force`,
and `fetch_context.py --force`.

## Runtime Artifacts

Outputs are written under `runtime/`:

- `last_payload.json`
- `last_brief.json`
- `hero.png` (when image generation succeeds)
- `final_display.png`
- `preview.png`
- `last_success.json`

## Generation history dashboard

Every full pipeline run now records its inputs, logs, decisions, and outputs.
Start the local dashboard with:

```bash
make history
```

Then open `http://127.0.0.1:8787`. To seed it with the existing
`runtime/history.json` entries and current surviving runtime artifacts:

```bash
make history-import
```

The importer is idempotent. On Raspberry Pi, install the dashboard as a service:

```bash
make history-install
ssh -L 8787:127.0.0.1:8787 <pi-user>@<raspberry-pi>
```

The service binds to localhost by default. Binding `--host 0.0.0.0` is possible
on a trusted network, but the server intentionally has no authentication, so an
SSH tunnel is the safer default.

### On-disk storage schema

`runtime/generations.jsonl` is the canonical append-only ledger. Each line is
one complete JSON event with this envelope:

```json
{
  "schema_version": 1,
  "event_id": "unique-id",
  "recorded_at": "ISO-8601 timestamp",
  "type": "run_started|stage_started|log_recorded|snapshot_added|artifact_added|...",
  "run_id": "generation-id",
  "data": {}
}
```

Writes take an exclusive file lock, append one line, flush, and `fsync`. Readers
take a shared lock and ignore an incomplete trailing line, making recovery after
power loss straightforward. Existing records are never updated or deleted.

The server materializes and indexes the ledger in memory by run ID, target date,
status, selected style, event type, and artifact/snapshot ID. At three scheduled
runs per day this remains small enough to rebuild cheaply for many years. If the
volume eventually grows, SQLite can be added as a disposable derived index
without changing the ledger format.

Generated images are immutable files under:

```text
runtime/generations/<run-id>/<artifact-kind>-<sha256-prefix>.png
```

The ledger stores each artifact's path, MIME type, SHA-256, byte size, and image
dimensions. It also stores JSON/text snapshots for raw weather inputs, Yahoo and
Open-Meteo aggregation, day context, transformed/generated briefs, complete text
and image prompts, model request parameters and random seed, style state,
guardrail verdicts, and each pipeline stage's combined stdout/stderr.

### HTTP API

- `GET /api/health`
- `GET /api/stats`
- `GET /api/runs?limit=50&offset=0&target_date=YYYY-MM-DD&status=degraded&style=Linocut`
- `GET /api/runs/<run-id>`
- `GET /api/snapshots/<snapshot-id>`
- `GET /api/artifacts/<artifact-id>`

## Local vs Pi Mode

### Local preview

Set in `config/settings.json`:

```json
"display": { "mode": "local_preview" }
```

Then run:

```bash
./scripts/display/update_display.sh
```

### Raspberry Pi display

Set:

```json
"display": { "mode": "pi_display" }
```

The display script probes both module candidates:

- `waveshare_epd.epd10in2g`
- `waveshare_epd.epd10in2_G`

## Raspberry Pi Bring-Up (SSH/tmux)

From the project root on the Pi:

```bash
chmod +x scripts/ops/setup_pi.sh scripts/ops/install_waveshare_driver.sh scripts/ops/preflight.py scripts/ops/install_systemd.sh scripts/display/update_display.sh
./scripts/ops/setup_pi.sh
```

After initial bring-up, use one command for day-to-day updates:

```bash
make update
```

`make update` runs `git pull --ff-only origin main`, installs/validates dependencies,
and executes the display pipeline.

`setup_pi.sh` installs Python GPIO deps (`spidev`, `RPi.GPIO`) and runs
`scripts/ops/install_waveshare_driver.sh`, which will:

- reuse `/home/trainboard/e-Paper` if present (copied with `sudo rsync`)
- otherwise clone `https://github.com/waveshare/e-Paper.git` into `~/e-Paper`
- install BCM2835 if missing

Update `config/settings.json` for device mode:

```json
"display": { "mode": "pi_display" }
```

Run preflight checks:

```bash
.venv/bin/python3 scripts/ops/preflight.py
```

Manual full run:

```bash
./scripts/display/update_display.sh
```

Install daily automation (08:00 local time):

```bash
./scripts/ops/install_systemd.sh
systemctl list-timers weather-eink-board.timer
```

See logs:

```bash
journalctl -u weather-eink-board.service -n 100 --no-pager
```

## Design Notes (Current Layout)

- Full-bleed generated image as background.
- Bottom minimal text panel:
  - headline (large)
  - subtitle (smaller)
- Small high-temperature chip in top-right (e.g. `30C`).
- Date in top-left.

## Troubleshooting

### 1) Image generation fallback shows proxy tunnel 403

Your environment proxy is blocking OpenRouter. Check allowlists/policy for `openrouter.ai`.

### 2) TLS cert verification failure

If your environment uses custom root CAs, set:

```json
"openrouter": {
  "ca_bundle_file": "/path/to/your/ca-bundle.pem"
}
```

The image script also attempts to use `certifi` automatically.

### 3) OpenRouter image runs but no hero appears

Run directly and inspect output:

```bash
python3 scripts/openrouter/generate_image.py --force-openrouter
```

If successful, it should write `runtime/hero.png`.

### 4) No live weather response available

When live fetch fails, the pipeline can use `config/sample_openmeteo.json` (`allow_sample_weather_on_failure`).

## Quick Sanity Checks

Deterministic-only:

```bash
python3 scripts/weather/fetch_weather.py
python3 scripts/weather/transform_weather.py
python3 scripts/render/compose_board.py
python3 scripts/render/palette_quantize.py
```

Force OpenRouter calls (for debugging):

```bash
python3 scripts/openrouter/generate_brief.py --force-openrouter
python3 scripts/openrouter/generate_image.py --force-openrouter
```
